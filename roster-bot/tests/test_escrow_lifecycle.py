"""
Phase 9 end to end: does a contract's money actually move?

The unit tests in test_escrow_engine.py prove the arithmetic and
test_escrow_ops.py proves each write. This file proves the thing the
league owner actually cares about: sign a driver, run races, and watch
salary leave the team's cash one race at a time, then come back with the
driver's profit or loss when the term ends.

Every number here is derived in a comment rather than copied from a
previous run, so a wrong answer shows up as a disagreement with the
reasoning instead of as a mysteriously changed constant.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bot import queries
from bot.contracts import service
from bot.market import escrow_ops
from bot.presets import f1 as f1_preset

pytestmark = pytest.mark.asyncio


# ── scaffolding ──────────────────────────────────────────────────────
#
# Mirrors tests/test_escrow_ops.py: teams are guild-scoped with a key,
# drivers go through queries.insert_driver, and there is no guilds table.


async def _bootstrap(conn, *, guild_id: int = 9001):
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) "
        "VALUES ($1, 'S9', TRUE) RETURNING id",
        guild_id,
    )
    await f1_preset.seed_season(conn, season_id)
    tier_id = await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", season_id,
    )
    team = await conn.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES ($1, 'a', 'Williams', 200, 300) RETURNING id", guild_id,
    )
    other = await conn.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES ($1, 'b', 'Sauber', 201, 301) RETURNING id", guild_id,
    )
    driver = await queries.insert_driver(
        conn, season_id, tier_id, member_id=111, display_name="ZeezinDomar",
        status="active",
    )
    return dict(
        guild_id=guild_id, season_id=season_id, tier_id=tier_id,
        team=team, other=other, driver=driver,
    )


async def _round(conn, ctx, *, number: int) -> int:
    return await conn.fetchval(
        "INSERT INTO race_rounds (season_id, tier_id, round_label, round_order) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        ctx["season_id"], ctx["tier_id"], f"R{number}", number,
    )


async def _balance(conn, ctx, team_key="team") -> Decimal:
    return await queries.fetch_budget_balance(
        conn, ctx[team_key], ctx["season_id"]
    )


async def _sign(
    conn, ctx, *, value: str, term_races: int, team_key: str = "team",
) -> int:
    """Insert an active contract and open its holding, as approval does."""
    contract_id = await queries.insert_contract(
        conn,
        season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        driver_id=ctx["driver"], team_id=ctx[team_key],
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


async def _publish_value(conn, ctx, *, value: str) -> None:
    run_id = await queries.insert_valuation_run(
        conn, season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        round_label="V", created_by=1, published=True,
    )
    await queries.insert_driver_valuations(
        conn, run_id, [dict(
            driver_id=ctx["driver"], market_value=Decimal(value),
            previous_value=Decimal("0"), delta=Decimal(value), rank_in_tier=1,
            capped=False, breakdown={},
        )],
    )


async def _charge(conn, ctx, *, number: int):
    return await escrow_ops.charge_round_for_tier(
        conn,
        season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        round_id=await _round(conn, ctx, number=number),
        races_per_season=24, actor_id=1,
    )


# ── the headline case ────────────────────────────────────────────────


async def test_salary_leaves_cash_race_by_race_and_returns_with_profit(
    pg_conn_migrated,
):
    """
    The whole feature in one test.

    A 4-race deal worth 24.00 a season on a 24-race calendar costs
    24.00 / 24 = 1.00 a race. After 4 races the team has paid 4.00 into
    escrow. The driver ends the term valued at 27.50 against a 24.00
    deal, so the P/L is +3.50 and the team is paid 4.00 + 3.50 = 7.50.

    Net effect on cash across the whole term: −4.00 + 7.50 = +3.50,
    exactly the driver's appreciation. That identity is the point of the
    design — a team's cash outcome is the driver's P/L, no more.
    """
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn)

    opening = await _balance(conn, ctx)

    contract_id = await _sign(conn, ctx, value="24.00", term_races=4)

    # Signing itself costs nothing — salary is only taken per race.
    assert await _balance(conn, ctx) == opening

    await _publish_value(conn, ctx, value="27.50")

    # Races 1-3: one share each, no settlement yet.
    for order in (1, 2, 3):
        outcome = await _charge(conn, ctx, number=order)
        assert len(outcome.charges) == 1
        assert outcome.charges[0].amount == Decimal("1.00")
        assert outcome.settlements == []

    balance_after_three = await _balance(conn, ctx)
    assert balance_after_three == opening - Decimal("3.00")

    # Race 4 completes the term: settle and pay back.
    final = await _charge(conn, ctx, number=4)
    assert len(final.charges) == 1
    assert final.completed_contract_ids == [contract_id]
    assert len(final.settlements) == 1

    settlement = final.settlements[0].result
    assert settlement.amount_held == Decimal("4.00")
    assert settlement.pl == Decimal("3.50")
    assert settlement.pl_applied == Decimal("3.50")
    assert settlement.amount_returned == Decimal("7.50")

    closing = await _balance(conn, ctx)
    assert closing == opening + Decimal("3.50")

    contract = await queries.fetch_contract_by_id(conn, contract_id)
    assert contract.state == "completed"


async def test_reimporting_the_same_round_charges_nothing_twice(
    pg_conn_migrated,
):
    """
    Owners re-import sheets to fix a typo. If that advanced the term and
    took another payment, a corrected result would quietly cost a team
    real money and shorten its contracts.
    """
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn)
    await _sign(conn, ctx, value="24.00", term_races=10)
    round_id = await _round(conn, ctx, number=1)

    def again():
        return escrow_ops.charge_round_for_tier(
            conn, season_id=ctx["season_id"], tier_id=ctx["tier_id"],
            round_id=round_id, races_per_season=24, actor_id=1,
        )

    first = await again()
    after_first = await _balance(conn, ctx)
    second = await again()

    assert len(first.charges) == 1
    assert second.charges == []
    assert second.skipped == 1
    assert await _balance(conn, ctx) == after_first


async def test_releasing_early_pro_rates_the_profit(pg_conn_migrated):
    """
    A team that held a driver for 1 of 10 races should not collect the
    same profit as one that saw the deal through. Escrow back in full,
    P/L scaled to the races actually served.

    1 race of a 10-race term at 24.00/24 = 1.00 escrowed. Driver ends at
    34.00 on a 24.00 deal, so full-term P/L is +10.00 and the served
    share is 1/10 → +1.00. Paid back: 1.00 + 1.00 = 2.00.
    """
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn)
    contract_id = await _sign(conn, ctx, value="24.00", term_races=10)
    await _charge(conn, ctx, number=1)

    settlement = await service.release_contract(
        conn, contract_id, actor_id=1,
        market_value_at_release=Decimal("34.00"),
        note="early exit",
    )

    assert settlement is not None
    assert settlement.result.amount_held == Decimal("1.00")
    assert settlement.result.pl == Decimal("10.00")
    assert settlement.result.pl_applied == Decimal("1.00")
    assert settlement.result.amount_returned == Decimal("2.00")


async def test_a_team_can_lose_its_escrow_and_never_more(pg_conn_migrated):
    """
    A driver can fall further than the team has escrowed. The team must
    not be charged beyond what it put in — a budget cannot be driven
    negative by a valuation.

    1 race escrowed = 1.00. Driver collapses to 4.00 on a 24.00 deal, so
    the full-term P/L is −20.00. Pro-rated over 1 of 10 races that is
    −2.00, which still exceeds the 1.00 held, so it is capped: nothing
    comes back and the loss stops there.
    """
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn)
    contract_id = await _sign(conn, ctx, value="24.00", term_races=10)
    await _charge(conn, ctx, number=1)
    before = await _balance(conn, ctx)

    settlement = await service.release_contract(
        conn, contract_id, actor_id=1,
        market_value_at_release=Decimal("4.00"),
        note="collapse",
    )

    assert settlement is not None
    assert settlement.result.clamped
    assert settlement.result.amount_returned == Decimal("0.00")
    # Cash does not move: the escrow is simply kept, not topped up.
    assert await _balance(conn, ctx) == before


async def test_a_trade_settles_the_seller_and_starts_the_buyer_fresh(
    pg_conn_migrated,
):
    """
    The escrow follows the contract. The selling team is settled for the
    races it served; the buying team opens a new holding whose service
    window starts at the trade, so it is never pro-rated over races it
    did not pay for.
    """
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn)
    contract_id = await _sign(conn, ctx, value="24.00", term_races=10)
    for order in (1, 2, 3):
        await _charge(conn, ctx, number=order)

    # Seller escrowed 3.00 over 3 races.
    held = await queries.fetch_held_escrow(conn, contract_id)
    assert Decimal(held["amount_held"]) == Decimal("3.00")
    assert held["team_id"] == ctx["team"]

    await queries.transfer_contract(conn, contract_id, new_team_id=ctx["other"])
    moved = await queries.fetch_contract_by_id(conn, contract_id)
    await escrow_ops.settle_holding(
        conn, contract=moved, reason=escrow_ops.REASON_TRADE, actor_id=1,
    )
    await escrow_ops.open_for_contract(
        conn, contract=moved, tier_id=ctx["tier_id"], actor_id=1,
        races_served_at_open=3,
    )

    new_holding = await queries.fetch_held_escrow(conn, contract_id)
    assert new_holding["team_id"] == ctx["other"]
    # Fresh window: nothing held, and the buyer's exposure starts at race 3.
    assert Decimal(new_holding["amount_held"]) == Decimal("0.00")
    assert new_holding["races_served_at_open"] == 3


async def test_escrow_stays_out_of_the_way_when_disabled(pg_conn_migrated):
    """
    Seasons 1-8 of this league run without escrow and must keep doing so
    after the migration: no holdings, no charges, no cash movement.
    """
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn)
    await conn.execute(
        "UPDATE budget_config SET escrow_enabled = FALSE WHERE season_id = $1",
        ctx["season_id"],
    )
    contract_id = await _sign(conn, ctx, value="24.00", term_races=4)
    before = await _balance(conn, ctx)

    assert await queries.fetch_held_escrow(conn, contract_id) is None
    outcome = await _charge(conn, ctx, number=1)
    assert outcome.charges == []
    assert outcome.settlements == []
    assert await _balance(conn, ctx) == before
