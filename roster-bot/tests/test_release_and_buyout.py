"""
Release freezes P/L in the ledger; buyout additionally records
dead money that counts against the team's effective payroll. Both
paths refuse to touch a contract that's in an open trade.
"""

from decimal import Decimal

import pytest

from bot import queries
from bot.contracts import service
from bot.presets import f1 as f1_preset


async def _bootstrap(pg_conn_migrated):
    season_id = await pg_conn_migrated.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES (1, 'S', TRUE) "
        "RETURNING id",
    )
    await f1_preset.seed_season(pg_conn_migrated, season_id)
    tier_id = await pg_conn_migrated.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", season_id,
    )
    team_id = await pg_conn_migrated.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES (1, 'a', 'A', 200, 300) RETURNING id",
    )
    other_team_id = await pg_conn_migrated.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES (1, 'b', 'B', 201, 301) RETURNING id",
    )
    driver_id = await queries.insert_driver(
        pg_conn_migrated, season_id, tier_id,
        member_id=111, display_name="Driver", status="active",
    )
    contract_id = await queries.insert_contract(
        pg_conn_migrated,
        season_id=season_id, tier_id=tier_id,
        driver_id=driver_id, team_id=team_id,
        contract_value=Decimal("10.00"),
        signing_bonus=Decimal("0"), max_incentives=Decimal("0"),
        term_seasons=2, contract_type="standard", state="active",
        value_at_signing=None, approved_by=None,
    )
    return {
        "season_id": season_id, "tier_id": tier_id,
        "team_id": team_id, "other_team_id": other_team_id,
        "driver_id": driver_id, "contract_id": contract_id,
    }


# ── release ──────────────────────────────────────────────────────────


async def test_release_flips_state_and_records_pl_snapshot(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    await service.release_contract(
        pg_conn_migrated, ctx["contract_id"],
        actor_id=999,
        market_value_at_release=Decimal("15.00"),
        note="salary dump",
    )
    contract = await queries.fetch_contract_by_id(
        pg_conn_migrated, ctx["contract_id"],
    )
    assert contract.state == "terminated"
    # P/L is frozen in the ledger detail.
    ledger = await queries.fetch_ledger_for_contract(
        pg_conn_migrated, ctx["contract_id"],
    )
    release = next(e for e in ledger if e.kind == "release")
    assert release.detail["contract_value"] == "10.00"
    assert release.detail["market_value_at_release"] == "15.00"
    assert release.detail["pl_at_release"] == "5.00"


async def test_release_without_market_value_leaves_pl_null(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    await service.release_contract(
        pg_conn_migrated, ctx["contract_id"],
        actor_id=999,
        market_value_at_release=None,
        note=None,
    )
    ledger = await queries.fetch_ledger_for_contract(
        pg_conn_migrated, ctx["contract_id"],
    )
    release = next(e for e in ledger if e.kind == "release")
    assert release.detail["pl_at_release"] is None


async def test_release_twice_raises(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    await service.release_contract(
        pg_conn_migrated, ctx["contract_id"],
        actor_id=999, market_value_at_release=None,
    )
    with pytest.raises(service.TransitionError):
        await service.release_contract(
            pg_conn_migrated, ctx["contract_id"],
            actor_id=999, market_value_at_release=None,
        )


async def test_release_blocked_when_contract_in_open_trade(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    other_contract_id = await queries.insert_contract(
        pg_conn_migrated,
        season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        driver_id=await queries.insert_driver(
            pg_conn_migrated, ctx["season_id"], ctx["tier_id"],
            member_id=222, display_name="Other", status="active",
        ),
        team_id=ctx["other_team_id"],
        contract_value=Decimal("12.00"),
        signing_bonus=Decimal("0"), max_incentives=Decimal("0"),
        term_seasons=1, contract_type="standard", state="active",
        value_at_signing=None, approved_by=None,
    )
    await service.propose_trade(
        pg_conn_migrated,
        season_id=ctx["season_id"],
        proposing_team_id=ctx["team_id"],
        other_team_id=ctx["other_team_id"],
        proposed_by=100,
        items=[
            (ctx["team_id"], ctx["contract_id"]),
            (ctx["other_team_id"], other_contract_id),
        ],
        message=None, ttl_hours=24,
    )
    with pytest.raises(service.TransitionError) as excinfo:
        await service.release_contract(
            pg_conn_migrated, ctx["contract_id"],
            actor_id=999, market_value_at_release=None,
        )
    assert "open trade" in str(excinfo.value)


# ── buyout ───────────────────────────────────────────────────────────


async def test_buyout_records_dead_money_row(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    await service.buyout_contract(
        pg_conn_migrated, ctx["contract_id"],
        actor_id=999,
        buyout_amount=Decimal("3.00"),
        market_value_at_release=Decimal("7.00"),
        note="rebuild",
    )
    contract = await queries.fetch_contract_by_id(
        pg_conn_migrated, ctx["contract_id"],
    )
    assert contract.state == "terminated"
    dm = await queries.fetch_dead_money_for_team(
        pg_conn_migrated, ctx["team_id"], ctx["season_id"],
    )
    assert len(dm) == 1
    assert dm[0].amount == Decimal("3.00")
    assert dm[0].source_contract_id == ctx["contract_id"]


async def test_buyout_increases_effective_payroll(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    payroll_before = await queries.fetch_team_effective_payroll(
        pg_conn_migrated, ctx["team_id"], ctx["season_id"],
    )
    await service.buyout_contract(
        pg_conn_migrated, ctx["contract_id"],
        actor_id=999,
        buyout_amount=Decimal("3.00"),
        market_value_at_release=None,
    )
    payroll_after = await queries.fetch_team_effective_payroll(
        pg_conn_migrated, ctx["team_id"], ctx["season_id"],
    )
    # Active-contract-value goes to 0 (-10), dead money adds 3 → net -7.
    assert payroll_after - payroll_before == Decimal("-7.00")


async def test_buyout_negative_amount_raises(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    with pytest.raises(service.TransitionError):
        await service.buyout_contract(
            pg_conn_migrated, ctx["contract_id"],
            actor_id=999,
            buyout_amount=Decimal("-1.00"),
            market_value_at_release=None,
        )


async def test_buyout_blocked_when_contract_in_open_trade(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    other_contract_id = await queries.insert_contract(
        pg_conn_migrated,
        season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        driver_id=await queries.insert_driver(
            pg_conn_migrated, ctx["season_id"], ctx["tier_id"],
            member_id=333, display_name="Other2", status="active",
        ),
        team_id=ctx["other_team_id"],
        contract_value=Decimal("8.00"),
        signing_bonus=Decimal("0"), max_incentives=Decimal("0"),
        term_seasons=1, contract_type="standard", state="active",
        value_at_signing=None, approved_by=None,
    )
    await service.propose_trade(
        pg_conn_migrated,
        season_id=ctx["season_id"],
        proposing_team_id=ctx["team_id"],
        other_team_id=ctx["other_team_id"],
        proposed_by=100,
        items=[
            (ctx["team_id"], ctx["contract_id"]),
            (ctx["other_team_id"], other_contract_id),
        ],
        message=None, ttl_hours=24,
    )
    with pytest.raises(service.TransitionError):
        await service.buyout_contract(
            pg_conn_migrated, ctx["contract_id"],
            actor_id=999,
            buyout_amount=Decimal("3.00"),
            market_value_at_release=None,
        )
