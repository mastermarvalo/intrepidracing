"""
Race-based terms must run down whether or not escrow is on.

The bug this file pins: the row that records "this contract served this
race" was written only inside `escrow_ops.charge_race`, which returns
early when `budget_config.escrow_enabled` is false. So in an escrow-off
league nothing was ever recorded, `races_served` stayed where it
started, and a race-based term never completed. A 30-race contract
stayed `active` forever and had to be ended by hand with a release.

It was latent rather than live because a new season defaults to escrow
on, so the default install never hit it. It bit only a league that
deliberately chose the commitment-only model — which is a supported,
documented choice, and was this league's model for seven seasons.

The fix separates the two facts. Serving a race is a league fact and is
recorded unconditionally; escrowing its salary is a money fact and still
depends on the setting. These tests assert both halves: that terms
advance and complete with escrow off, and that no money moves while they
do.

Numbers are derived in comments rather than copied from a run.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from bot import queries
from bot.market import escrow_ops
from bot.presets import f1 as f1_preset

pytestmark = pytest.mark.asyncio


# ── scaffolding ──────────────────────────────────────────────────────


async def _bootstrap(conn, *, guild_id: int = 9101, escrow: bool = False):
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) "
        "VALUES ($1, 'S8', TRUE) RETURNING id",
        guild_id,
    )
    await f1_preset.seed_season(conn, season_id)
    tier_id = await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", season_id,
    )
    # seed_season creates the budget config with escrow on by default, so
    # an escrow-off league is made by switching it off, exactly as the
    # commissioner's own command does.
    await conn.execute(
        "UPDATE budget_config SET escrow_enabled = $2 WHERE season_id = $1",
        season_id, escrow,
    )
    team = await conn.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES ($1, 'a', 'Williams', 200, 300) RETURNING id", guild_id,
    )
    driver = await queries.insert_driver(
        conn, season_id, tier_id, member_id=111, display_name="ZeezinDomar",
        status="active",
    )
    return dict(
        guild_id=guild_id, season_id=season_id, tier_id=tier_id,
        team=team, driver=driver,
    )


async def _round(conn, ctx, *, number: int) -> int:
    return await conn.fetchval(
        "INSERT INTO race_rounds (season_id, tier_id, round_label, round_order) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        ctx["season_id"], ctx["tier_id"], f"R{number}", number,
    )


async def _sign(conn, ctx, *, value: str = "24.00", term_races: int = 3) -> int:
    """Insert an active contract and try to open its holding.

    `open_for_contract` is a no-op when escrow is off, which is the point:
    these contracts have no holding, and their terms must still run.
    """
    contract_id = await queries.insert_contract(
        conn,
        season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        driver_id=ctx["driver"], team_id=ctx["team"],
        contract_value=Decimal(value),
        signing_bonus=Decimal("0"),
        max_incentives=Decimal("0"),
        term_seasons=1,
        term_races=term_races,
        contract_type="standard",
        state="active",
        value_at_signing=Decimal(value),
        approved_by=None,
    )
    contract = await queries.fetch_contract_by_id(conn, contract_id)
    await escrow_ops.open_for_contract(
        conn, contract=contract, tier_id=ctx["tier_id"], actor_id=1,
    )
    return contract_id


async def _charge(conn, ctx, *, number: int):
    return await escrow_ops.charge_round_for_tier(
        conn,
        season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        round_id=await _round(conn, ctx, number=number),
        races_per_season=24, actor_id=1,
    )


async def _state(conn, contract_id: int) -> str:
    return await conn.fetchval(
        "SELECT state FROM contracts WHERE id = $1", contract_id,
    )


async def _balance(conn, ctx) -> Decimal:
    return await queries.fetch_budget_balance(
        conn, ctx["team"], ctx["season_id"]
    )


# ── the bug ──────────────────────────────────────────────────────────


async def test_a_term_advances_with_escrow_off(pg_conn_migrated):
    """
    The heart of it. Before the fix this stayed at zero forever.
    """
    ctx = await _bootstrap(pg_conn_migrated, escrow=False)
    contract_id = await _sign(pg_conn_migrated, ctx, term_races=3)

    await _charge(pg_conn_migrated, ctx, number=1)

    contract = await queries.fetch_contract_by_id(pg_conn_migrated, contract_id)
    assert await escrow_ops.races_served(pg_conn_migrated, contract) == 1


async def test_a_term_completes_with_escrow_off(pg_conn_migrated):
    """
    A 3-race contract must be closed by its third race, not left active
    for the rest of the league's existence.
    """
    ctx = await _bootstrap(pg_conn_migrated, escrow=False)
    contract_id = await _sign(pg_conn_migrated, ctx, term_races=3)

    for n in (1, 2):
        outcome = await _charge(pg_conn_migrated, ctx, number=n)
        assert outcome.completed_contract_ids == [], f"closed early at race {n}"
        assert await _state(pg_conn_migrated, contract_id) == "active"

    outcome = await _charge(pg_conn_migrated, ctx, number=3)
    assert outcome.completed_contract_ids == [contract_id]
    assert await _state(pg_conn_migrated, contract_id) == "completed"


async def test_no_money_moves_while_the_term_runs_down(pg_conn_migrated):
    """
    The fix must not have quietly turned escrow on. Commitment-only means
    the contract counts against the cap and the cash never moves.
    """
    ctx = await _bootstrap(pg_conn_migrated, escrow=False)
    await _sign(pg_conn_migrated, ctx, term_races=3)

    for n in (1, 2, 3):
        outcome = await _charge(pg_conn_migrated, ctx, number=n)
        assert outcome.charges == []
        assert outcome.settlements == []

    assert await _balance(pg_conn_migrated, ctx) == Decimal("0.00")
    assert await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM team_budget_ledger WHERE kind = 'salary_escrow'"
    ) == 0


async def test_the_service_row_records_zero_when_nothing_was_escrowed(
    pg_conn_migrated,
):
    """
    `amount` on the service row is what was escrowed, and with escrow off
    that is zero. It has to be zero rather than the share, or a reader
    summing the column would conclude money moved when none did.

    The term still advances, because races_served counts rows.
    """
    ctx = await _bootstrap(pg_conn_migrated, escrow=False)
    contract_id = await _sign(pg_conn_migrated, ctx, value="24.00", term_races=3)

    await _charge(pg_conn_migrated, ctx, number=1)

    assert await pg_conn_migrated.fetchval(
        "SELECT amount FROM contract_race_service WHERE contract_id = $1",
        contract_id,
    ) == Decimal("0.00")


async def test_a_reimported_round_does_not_advance_the_term_twice(
    pg_conn_migrated,
):
    """
    The re-import guard used to live in the escrow path. Moving service
    recording out of it must not have left the guard behind.
    """
    ctx = await _bootstrap(pg_conn_migrated, escrow=False)
    contract_id = await _sign(pg_conn_migrated, ctx, term_races=3)
    round_id = await _round(pg_conn_migrated, ctx, number=1)

    first = await escrow_ops.charge_round_for_tier(
        pg_conn_migrated,
        season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        round_id=round_id, races_per_season=24, actor_id=1,
    )
    again = await escrow_ops.charge_round_for_tier(
        pg_conn_migrated,
        season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        round_id=round_id, races_per_season=24, actor_id=1,
    )

    assert first.advanced == 1
    assert again.advanced == 0, "the same round advanced the term twice"
    assert again.skipped == 1

    contract = await queries.fetch_contract_by_id(pg_conn_migrated, contract_id)
    assert await escrow_ops.races_served(pg_conn_migrated, contract) == 1


async def test_a_completion_is_recorded_in_the_ledger_even_with_no_money(
    pg_conn_migrated,
):
    """
    With escrow off `settle_holding` returns None, so nothing used to be
    written anywhere: a contract went from active to complete with no
    trace. `term_completed` was declared by migration 015 and never
    written by anything until now.
    """
    ctx = await _bootstrap(pg_conn_migrated, escrow=False)
    contract_id = await _sign(pg_conn_migrated, ctx, term_races=2)

    await _charge(pg_conn_migrated, ctx, number=1)
    await _charge(pg_conn_migrated, ctx, number=2)

    row = await pg_conn_migrated.fetchrow(
        "SELECT kind, detail, contract_id FROM contract_ledger "
        "WHERE kind = 'term_completed' AND contract_id = $1",
        contract_id,
    )
    assert row is not None, "a contract ended with no ledger row"
    assert row["contract_id"] == contract_id


async def test_carried_races_still_count_with_escrow_off(pg_conn_migrated):
    """
    A contract carried into a new season starts partway through its term.
    That must hold with escrow off too, or carry-over would silently
    reset every term.
    """
    ctx = await _bootstrap(pg_conn_migrated, escrow=False)
    contract_id = await queries.insert_contract(
        pg_conn_migrated,
        season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        driver_id=ctx["driver"], team_id=ctx["team"],
        contract_value=Decimal("24.00"),
        signing_bonus=Decimal("0"), max_incentives=Decimal("0"),
        term_seasons=2, term_races=10,
        contract_type="standard", state="active",
        value_at_signing=Decimal("24.00"), approved_by=None,
    )
    # 9 of the 10 races were served by an earlier contract row, so the
    # very next race is the one that ends the term.
    await pg_conn_migrated.execute(
        "UPDATE contracts SET races_served_before = 9 WHERE id = $1",
        contract_id,
    )

    outcome = await _charge(pg_conn_migrated, ctx, number=1)

    assert outcome.completed_contract_ids == [contract_id]
    assert await _state(pg_conn_migrated, contract_id) == "completed"


# ── escrow on must be unchanged ──────────────────────────────────────


async def test_escrow_on_still_charges_and_settles(pg_conn_migrated):
    """
    The regression guard. Restructuring the loop must not have changed
    the escrow-on path at all.

    $24.00M over 24 races is $1.00M per race, so a 2-race term holds
    $2.00M and the balance sits at −$2.00M before settlement returns it.
    """
    ctx = await _bootstrap(pg_conn_migrated, escrow=True)
    contract_id = await _sign(pg_conn_migrated, ctx, value="24.00", term_races=2)

    first = await _charge(pg_conn_migrated, ctx, number=1)
    assert len(first.charges) == 1
    assert first.charges[0].amount == Decimal("1.00")
    assert first.advanced == 1
    assert await _balance(pg_conn_migrated, ctx) == Decimal("-1.00")

    second = await _charge(pg_conn_migrated, ctx, number=2)
    assert second.completed_contract_ids == [contract_id]
    assert len(second.settlements) == 1
    # Escrow held is returned; the driver was never priced, so no P/L is
    # applied and the team gets back exactly what it put in.
    assert second.settlements[0].result.amount_held == Decimal("2.00")
    assert await _state(pg_conn_migrated, contract_id) == "completed"


async def test_a_contract_with_no_holding_still_completes_with_escrow_on(
    pg_conn_migrated,
):
    """
    The narrower half of the same bug. A contract signed while escrow was
    off has no holding. Once escrow is switched on, the season-level
    check passes but `fetch_held_escrow` returns nothing — and the old
    loop skipped the contract entirely, so its term never ended.

    No money can move for it, because nothing was ever held. Its term
    must still complete.
    """
    ctx = await _bootstrap(pg_conn_migrated, escrow=False)
    contract_id = await _sign(pg_conn_migrated, ctx, term_races=2)
    assert await queries.fetch_held_escrow(pg_conn_migrated, contract_id) is None

    # The commissioner switches escrow on mid-contract.
    await pg_conn_migrated.execute(
        "UPDATE budget_config SET escrow_enabled = TRUE WHERE season_id = $1",
        ctx["season_id"],
    )

    await _charge(pg_conn_migrated, ctx, number=1)
    outcome = await _charge(pg_conn_migrated, ctx, number=2)

    assert outcome.completed_contract_ids == [contract_id]
    assert outcome.charges == [], "money moved against a contract with no holding"
    assert await _state(pg_conn_migrated, contract_id) == "completed"
    assert await _balance(pg_conn_migrated, ctx) == Decimal("0.00")


# ── the direct entry point keeps its own guard ───────────────────────


async def test_charge_race_alone_still_guards_its_own_reimport(
    pg_conn_migrated,
):
    """
    `charge_race` is called directly by tests and could be called by a
    future path. With `service_counted` left alone it must stay
    self-contained: record the service itself, and return None for a
    round already counted.
    """
    ctx = await _bootstrap(pg_conn_migrated, escrow=True)
    contract_id = await _sign(pg_conn_migrated, ctx, term_races=3)
    contract = await queries.fetch_contract_by_id(pg_conn_migrated, contract_id)
    round_id = await _round(pg_conn_migrated, ctx, number=1)

    first = await escrow_ops.charge_race(
        pg_conn_migrated, contract=contract, round_id=round_id,
        races_per_season=24,
    )
    repeat = await escrow_ops.charge_race(
        pg_conn_migrated, contract=contract, round_id=round_id,
        races_per_season=24,
    )

    assert first is not None
    assert repeat is None
    assert await escrow_ops.races_served(pg_conn_migrated, contract) == 1


async def test_service_counted_lets_the_caller_own_the_guard(pg_conn_migrated):
    """
    When the round loop has already written the service row, `charge_race`
    must not read the resulting conflict as "already served" and refuse
    to move the money. That mistake would be silent: terms would advance
    correctly and salary would never be charged.
    """
    ctx = await _bootstrap(pg_conn_migrated, escrow=True)
    contract_id = await _sign(pg_conn_migrated, ctx, value="24.00", term_races=3)
    contract = await queries.fetch_contract_by_id(pg_conn_migrated, contract_id)
    round_id = await _round(pg_conn_migrated, ctx, number=1)

    counted = await escrow_ops.record_race_served(
        pg_conn_migrated, contract=contract, round_id=round_id,
        amount=Decimal("1.00"),
    )
    assert counted is not None

    charge = await escrow_ops.charge_race(
        pg_conn_migrated, contract=contract, round_id=round_id,
        races_per_season=24, service_counted=True,
    )

    assert charge is not None, "the caller's own service row blocked the charge"
    assert charge.amount == Decimal("1.00")
    assert await _balance(pg_conn_migrated, ctx) == Decimal("-1.00")
    # And still only one service row for the round.
    assert await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM contract_race_service WHERE contract_id = $1",
        contract_id,
    ) == 1


async def test_record_race_served_ignores_a_contract_with_no_term(
    pg_conn_migrated,
):
    """
    `term_races` is NOT NULL in the schema and `insert_contract` derives
    it when a caller omits it, so this state cannot be loaded from the
    database. It is reachable only in memory — a caller that built a
    Contract without a term and never persisted it, which is the case
    `open_for_contract` documents. There is no term to advance, so
    nothing is recorded.
    """
    ctx = await _bootstrap(pg_conn_migrated, escrow=False)
    contract_id = await _sign(pg_conn_migrated, ctx, term_races=3)
    stored = await queries.fetch_contract_by_id(pg_conn_migrated, contract_id)
    termless = replace(stored, term_races=None)

    assert await escrow_ops.record_race_served(
        pg_conn_migrated, contract=termless,
        round_id=await _round(pg_conn_migrated, ctx, number=1),
        amount=Decimal("0"),
    ) is None
    assert await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM contract_race_service WHERE contract_id = $1",
        contract_id,
    ) == 0
