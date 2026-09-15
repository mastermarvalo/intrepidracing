"""
Salary escrow against the real schema.

The engine is tested in `test_escrow_engine.py`; this file exercises the
parts only a database can prove: the partial unique index that makes
double-escrow impossible, the `(contract_id, round_id)` guard that makes
a re-imported round harmless, that a settlement's ledger rows sum to
exactly what the engine said to return, and that migration 015 does not
silently change the money rules for a season already in progress.
"""

from dataclasses import replace
from decimal import Decimal

import asyncpg
import pytest

from bot import queries
from bot.market import escrow_ops
from bot.presets import f1 as f1_preset

pytestmark = pytest.mark.asyncio


async def _bootstrap(conn, *, guild_id: int = 77, races_per_season: int = 24):
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) "
        "VALUES ($1, 'S9', TRUE) RETURNING id",
        guild_id,
    )
    await f1_preset.seed_season(conn, season_id)
    tier_id = await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", season_id,
    )
    await conn.execute(
        "UPDATE league_config SET races_per_season = $2 WHERE season_id = $1",
        season_id, races_per_season,
    )
    team = await conn.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES ($1, 'a', 'A', 200, 300) RETURNING id", guild_id,
    )
    other = await conn.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES ($1, 'b', 'B', 201, 301) RETURNING id", guild_id,
    )
    driver = await queries.insert_driver(
        conn, season_id, tier_id, member_id=111, display_name="Driver",
        status="active",
    )
    return dict(
        guild_id=guild_id, season_id=season_id, tier_id=tier_id,
        team=team, other=other, driver=driver,
    )


async def _sign(
    conn, ctx, *, value="24.00", term_races=36, team_key="team",
    races_served_before=0,
):
    contract_id = await queries.insert_contract(
        conn, season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        driver_id=ctx["driver"], team_id=ctx[team_key],
        contract_value=Decimal(value), signing_bonus=Decimal("0"),
        max_incentives=Decimal("0"), term_seasons=1, contract_type="standard",
        state="active", value_at_signing=None, approved_by=None,
        term_races=term_races, races_served_before=races_served_before,
    )
    return await queries.fetch_contract_by_id(conn, contract_id)


async def _round(conn, ctx, *, number: int):
    return await conn.fetchval(
        "INSERT INTO race_rounds (season_id, tier_id, round_label, round_order) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        ctx["season_id"], ctx["tier_id"], f"R{number}", number,
    )


async def _balance(conn, ctx, team_key="team") -> Decimal:
    return await queries.fetch_budget_balance(
        conn, ctx[team_key], ctx["season_id"]
    )


async def _publish_value(conn, ctx, value: str):
    run_id = await queries.insert_valuation_run(
        conn, season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        round_label="test", created_by=None,
    )
    await queries.insert_driver_valuations(
        conn, run_id,
        [{
            "driver_id": ctx["driver"],
            "market_value": Decimal(value),
            "previous_value": None,
            "delta": Decimal("0"),
            "rank_in_tier": 1,
            "capped": False,
            "breakdown": [],
        }],
    )
    await queries.publish_valuation_run(conn, run_id)


# ── in-flight season protection ──────────────────────────────────────


async def test_a_new_season_gets_escrow_on(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    assert await escrow_ops.is_enabled(
        pg_conn_migrated, season_id=ctx["season_id"], tier_id=ctx["tier_id"]
    )


async def test_a_season_that_predates_the_migration_keeps_escrow_off(pg_conn_migrated):
    """
    The migration backfills existing `budget_config` rows to FALSE. Were
    they switched on instead, every team in a part-finished season would
    gain its whole payroll in spendable money overnight, because escrow
    mode stops netting payroll off the balance while no cash has
    actually been taken.
    """
    ctx = await _bootstrap(pg_conn_migrated)
    await pg_conn_migrated.execute(
        "UPDATE budget_config SET escrow_enabled = FALSE WHERE season_id = $1",
        ctx["season_id"],
    )
    assert not await escrow_ops.is_enabled(
        pg_conn_migrated, season_id=ctx["season_id"], tier_id=ctx["tier_id"]
    )


async def test_a_season_with_no_budget_config_is_escrow_off(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    await pg_conn_migrated.execute(
        "DELETE FROM budget_config WHERE season_id = $1", ctx["season_id"]
    )
    assert not await escrow_ops.is_enabled(
        pg_conn_migrated, season_id=ctx["season_id"], tier_id=ctx["tier_id"]
    )


# ── opening ──────────────────────────────────────────────────────────


async def test_opening_a_holding_costs_the_team_nothing_yet(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx)
    escrow_id = await escrow_ops.open_for_contract(
        pg_conn_migrated, contract=contract
    )
    assert escrow_id is not None
    holding = await queries.fetch_held_escrow(pg_conn_migrated, contract.id)
    assert holding["amount_held"] == Decimal("0.00")
    assert await _balance(pg_conn_migrated, ctx) == Decimal("0.00")


async def test_opening_twice_reuses_the_holding_rather_than_charging_twice(
    pg_conn_migrated,
):
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx)
    first = await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    second = await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    assert first == second


async def test_the_database_refuses_two_live_holdings(pg_conn_migrated):
    """
    `uq_contract_escrow_one_held` is the last line of defence: even if
    application code were wrong, a contract cannot be escrowed twice.
    """
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx)
    await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    with pytest.raises(asyncpg.UniqueViolationError):
        await queries.insert_contract_escrow(
            pg_conn_migrated, contract_id=contract.id,
            origin_contract_id=contract.id, season_id=ctx["season_id"],
            team_id=ctx["team"],
        )


async def test_an_omitted_term_is_derived_from_seasons_not_left_null(
    pg_conn_migrated,
):
    """
    `contracts.term_races` is NOT NULL. A caller that still thinks in
    seasons must get the same conversion migration 015 used to backfill
    existing rows, or a signing through an older path would fail outright.
    """
    ctx = await _bootstrap(pg_conn_migrated, races_per_season=24)
    contract = await _sign(pg_conn_migrated, ctx, term_races=None)
    assert contract.term_races == 24


async def test_an_in_memory_contract_with_no_term_escrows_nothing(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx)
    assert await escrow_ops.open_for_contract(
        pg_conn_migrated, contract=replace(contract, term_races=None)
    ) is None


async def test_escrow_off_opens_nothing(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    await pg_conn_migrated.execute(
        "UPDATE budget_config SET escrow_enabled = FALSE WHERE season_id = $1",
        ctx["season_id"],
    )
    contract = await _sign(pg_conn_migrated, ctx)
    assert await escrow_ops.open_for_contract(
        pg_conn_migrated, contract=contract
    ) is None


# ── charging races ───────────────────────────────────────────────────


async def test_one_race_takes_one_races_share_from_cash(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx)
    await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    round_id = await _round(pg_conn_migrated, ctx, number=1)

    charge = await escrow_ops.charge_race(
        pg_conn_migrated, contract=contract, round_id=round_id,
        races_per_season=24,
    )
    assert charge.amount == Decimal("1.00")
    assert charge.total_held == Decimal("1.00")
    assert charge.races_served == 1
    assert not charge.term_complete
    assert await _balance(pg_conn_migrated, ctx) == Decimal("-1.00")


async def test_re_importing_a_round_charges_nothing_and_does_not_advance_the_term(
    pg_conn_migrated,
):
    """
    A stewards' decision means results get re-imported. If that charged
    again, a team would pay twice for one race and the term would run
    short.
    """
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx)
    await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    round_id = await _round(pg_conn_migrated, ctx, number=1)

    await escrow_ops.charge_race(
        pg_conn_migrated, contract=contract, round_id=round_id,
        races_per_season=24,
    )
    repeat = await escrow_ops.charge_race(
        pg_conn_migrated, contract=contract, round_id=round_id,
        races_per_season=24,
    )
    assert repeat is None
    assert await _balance(pg_conn_migrated, ctx) == Decimal("-1.00")
    assert await queries.count_race_service(pg_conn_migrated, contract.id) == 1


async def test_distinct_rounds_accumulate(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx)
    await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    for n in (1, 2, 3):
        charge = await escrow_ops.charge_race(
            pg_conn_migrated, contract=contract,
            round_id=await _round(pg_conn_migrated, ctx, number=n),
            races_per_season=24,
        )
    assert charge.races_served == 3
    assert charge.total_held == Decimal("3.00")
    assert await _balance(pg_conn_migrated, ctx) == Decimal("-3.00")


async def test_a_carried_row_counts_the_races_earlier_rows_served(pg_conn_migrated):
    """
    `races_served_before` is what keeps a term from restarting when a
    contract is carried into a new season.
    """
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx, races_served_before=24)
    await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    charge = await escrow_ops.charge_race(
        pg_conn_migrated, contract=contract,
        round_id=await _round(pg_conn_migrated, ctx, number=1),
        races_per_season=24,
    )
    assert charge.races_served == 25


async def test_the_final_race_of_a_term_reports_completion(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(
        pg_conn_migrated, ctx, term_races=2, races_served_before=1
    )
    await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    charge = await escrow_ops.charge_race(
        pg_conn_migrated, contract=contract,
        round_id=await _round(pg_conn_migrated, ctx, number=1),
        races_per_season=24,
    )
    assert charge.term_complete


async def test_charging_without_a_holding_does_nothing(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx)
    assert await escrow_ops.charge_race(
        pg_conn_migrated, contract=contract,
        round_id=await _round(pg_conn_migrated, ctx, number=1),
        races_per_season=24,
    ) is None


async def test_the_ledger_row_names_the_contract_it_belongs_to(pg_conn_migrated):
    """Provenance is what lets a budget history explain an escrow debit."""
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx)
    await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    await escrow_ops.charge_race(
        pg_conn_migrated, contract=contract,
        round_id=await _round(pg_conn_migrated, ctx, number=1),
        races_per_season=24,
    )
    row = await pg_conn_migrated.fetchrow(
        "SELECT kind, amount, contract_id FROM team_budget_ledger "
        "WHERE kind = 'salary_escrow' AND team_id = $1", ctx["team"],
    )
    assert row["amount"] == Decimal("-1.00")
    assert row["contract_id"] == contract.id


# ── settlement ───────────────────────────────────────────────────────


async def _serve(conn, ctx, contract, races: int, start: int = 1):
    for n in range(start, start + races):
        await escrow_ops.charge_race(
            conn, contract=contract,
            round_id=await _round(conn, ctx, number=n),
            races_per_season=24,
        )


async def test_a_completed_term_returns_escrow_plus_pl(pg_conn_migrated):
    """
    The runbook's headline example, end to end: $24M/season over 36
    races, driver ends worth $27.50M, so $36M escrowed returns $39.50M.
    """
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(
        pg_conn_migrated, ctx, term_races=36, races_served_before=0
    )
    await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    await _serve(pg_conn_migrated, ctx, contract, 36)
    await _publish_value(pg_conn_migrated, ctx, "27.50")

    settled = await escrow_ops.settle_holding(
        pg_conn_migrated, contract=contract,
        reason=escrow_ops.REASON_TERM_COMPLETE,
    )
    assert settled.result.amount_held == Decimal("36.00")
    assert settled.result.pl == Decimal("3.50")
    assert settled.result.amount_returned == Decimal("39.50")
    # Cash: 36 out over the term, 39.50 back.
    assert await _balance(pg_conn_migrated, ctx) == Decimal("3.50")


async def test_settlement_ledger_rows_sum_to_the_amount_returned(pg_conn_migrated):
    """
    The escrow and the P/L are written as two rows because they are two
    different facts, but together they must equal the engine's figure.
    """
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx, term_races=4)
    await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    await _serve(pg_conn_migrated, ctx, contract, 4)
    await _publish_value(pg_conn_migrated, ctx, "26.00")

    settled = await escrow_ops.settle_holding(
        pg_conn_migrated, contract=contract,
        reason=escrow_ops.REASON_TERM_COMPLETE,
    )
    total = await pg_conn_migrated.fetchval(
        "SELECT COALESCE(SUM(amount), 0) FROM team_budget_ledger "
        "WHERE team_id = $1 AND kind IN ('escrow_return', 'escrow_pl')",
        ctx["team"],
    )
    assert Decimal(total) == settled.result.amount_returned


async def test_an_unpriced_driver_returns_the_escrow_with_pl_recorded_unknown(
    pg_conn_migrated,
):
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx, term_races=4)
    await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    await _serve(pg_conn_migrated, ctx, contract, 4)

    settled = await escrow_ops.settle_holding(
        pg_conn_migrated, contract=contract,
        reason=escrow_ops.REASON_TERM_COMPLETE,
    )
    assert settled.result.pl is None
    assert settled.result.amount_returned == Decimal("4.00")
    row = await pg_conn_migrated.fetchrow(
        "SELECT market_value, pl FROM escrow_settlements WHERE id = $1",
        settled.settlement_id,
    )
    assert row["market_value"] is None and row["pl"] is None


async def test_an_early_release_pro_rates_the_pl(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx, term_races=36)
    await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    await _serve(pg_conn_migrated, ctx, contract, 18)
    await _publish_value(pg_conn_migrated, ctx, "27.50")

    settled = await escrow_ops.settle_holding(
        pg_conn_migrated, contract=contract, reason=escrow_ops.REASON_RELEASE,
    )
    assert settled.result.pl == Decimal("3.50")
    assert settled.result.pl_applied == Decimal("1.75")
    assert settled.result.amount_returned == Decimal("19.75")


async def test_settling_closes_the_holding_so_it_cannot_settle_twice(
    pg_conn_migrated,
):
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx, term_races=2)
    await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    await _serve(pg_conn_migrated, ctx, contract, 2)

    first = await escrow_ops.settle_holding(
        pg_conn_migrated, contract=contract,
        reason=escrow_ops.REASON_TERM_COMPLETE,
    )
    assert first is not None
    again = await escrow_ops.settle_holding(
        pg_conn_migrated, contract=contract,
        reason=escrow_ops.REASON_TERM_COMPLETE,
    )
    assert again is None, "a settled holding must not pay out a second time"


async def test_settling_a_contract_that_was_never_escrowed_reports_nothing(
    pg_conn_migrated,
):
    """
    None rather than a $0.00 settlement, so a receipt can stay silent
    about money instead of claiming a payout that never happened.
    """
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx)
    assert await escrow_ops.settle_holding(
        pg_conn_migrated, contract=contract, reason=escrow_ops.REASON_VOID,
    ) is None


async def test_a_settlement_after_a_trade_pays_the_team_that_held_the_escrow(
    pg_conn_migrated,
):
    """
    The holding carries its own `team_id`. A contract row that changed
    hands must repay the team whose cash was actually taken, not
    whichever team happens to be on the contract now.
    """
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx, term_races=2)
    await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    await _serve(pg_conn_migrated, ctx, contract, 2)
    await pg_conn_migrated.execute(
        "UPDATE contracts SET team_id = $2 WHERE id = $1",
        contract.id, ctx["other"],
    )

    settled = await escrow_ops.settle_holding(
        pg_conn_migrated, contract=contract, reason=escrow_ops.REASON_TRADE,
    )
    assert settled.team_id == ctx["team"]
    assert await _balance(pg_conn_migrated, ctx, "other") == Decimal("0.00")


async def test_team_escrow_held_explains_the_gap_in_a_teams_money(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx, term_races=36)
    await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    await _serve(pg_conn_migrated, ctx, contract, 5)

    held = await queries.fetch_team_escrow_held(
        pg_conn_migrated, ctx["team"], ctx["season_id"]
    )
    assert held == Decimal("5.00")


async def test_settled_holdings_stop_counting_as_held(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    contract = await _sign(pg_conn_migrated, ctx, term_races=2)
    await escrow_ops.open_for_contract(pg_conn_migrated, contract=contract)
    await _serve(pg_conn_migrated, ctx, contract, 2)
    await escrow_ops.settle_holding(
        pg_conn_migrated, contract=contract,
        reason=escrow_ops.REASON_TERM_COMPLETE,
    )
    held = await queries.fetch_team_escrow_held(
        pg_conn_migrated, ctx["team"], ctx["season_id"]
    )
    assert held == Decimal("0.00")
