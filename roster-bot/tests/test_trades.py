"""
Trade lifecycle: propose / accept / decline / withdraw / approve /
reject. Every legal transition succeeds; every illegal one raises
TransitionError; approve transfers contracts without touching
contract_value; teams must differ; contracts must be active.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bot import queries
from bot.contracts import service
from bot.presets import f1 as f1_preset


async def _bootstrap(pg_conn_migrated):
    guild_id = 42
    season_id = await pg_conn_migrated.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES ($1, 'S', TRUE) "
        "RETURNING id", guild_id,
    )
    await f1_preset.seed_season(pg_conn_migrated, season_id)
    tier_id = await pg_conn_migrated.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", season_id,
    )
    team_a = await pg_conn_migrated.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES ($1, 'a', 'A', 200, 300) RETURNING id", guild_id,
    )
    team_b = await pg_conn_migrated.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES ($1, 'b', 'B', 201, 301) RETURNING id", guild_id,
    )
    driver_a = await queries.insert_driver(
        pg_conn_migrated, season_id, tier_id,
        member_id=111, display_name="A-Driver", status="active",
    )
    driver_b = await queries.insert_driver(
        pg_conn_migrated, season_id, tier_id,
        member_id=222, display_name="B-Driver", status="active",
    )
    contract_a = await queries.insert_contract(
        pg_conn_migrated,
        season_id=season_id, tier_id=tier_id,
        driver_id=driver_a, team_id=team_a,
        contract_value=Decimal("10.00"),
        signing_bonus=Decimal("0"), max_incentives=Decimal("0"),
        term_seasons=2, contract_type="standard", state="active",
        value_at_signing=None, approved_by=None,
    )
    contract_b = await queries.insert_contract(
        pg_conn_migrated,
        season_id=season_id, tier_id=tier_id,
        driver_id=driver_b, team_id=team_b,
        contract_value=Decimal("12.00"),
        signing_bonus=Decimal("0"), max_incentives=Decimal("0"),
        term_seasons=1, contract_type="standard", state="active",
        value_at_signing=None, approved_by=None,
    )
    return {
        "season_id": season_id, "tier_id": tier_id,
        "team_a": team_a, "team_b": team_b,
        "driver_a": driver_a, "driver_b": driver_b,
        "contract_a": contract_a, "contract_b": contract_b,
    }


async def _propose(pg_conn_migrated, ctx, ttl_hours: int = 24) -> int:
    return await service.propose_trade(
        pg_conn_migrated,
        season_id=ctx["season_id"],
        proposing_team_id=ctx["team_a"],
        other_team_id=ctx["team_b"],
        proposed_by=100,
        items=[
            (ctx["team_a"], ctx["contract_a"]),
            (ctx["team_b"], ctx["contract_b"]),
        ],
        message=None,
        ttl_hours=ttl_hours,
    )


# ── legal flow ───────────────────────────────────────────────────────


async def test_full_trade_transfers_contracts_without_touching_value(
    pg_conn_migrated,
):
    ctx = await _bootstrap(pg_conn_migrated)
    trade_id = await _propose(pg_conn_migrated, ctx)
    await service.accept_trade(pg_conn_migrated, trade_id, actor_id=200)
    await service.commissioner_approve_trade(
        pg_conn_migrated, trade_id, actor_id=999,
    )
    trade = await queries.fetch_trade_by_id(pg_conn_migrated, trade_id)
    assert trade.state == "approved"
    assert trade.approved_ref is not None

    # Contract A now belongs to team B (moved via trade).
    ca = await queries.fetch_contract_by_id(pg_conn_migrated, ctx["contract_a"])
    cb = await queries.fetch_contract_by_id(pg_conn_migrated, ctx["contract_b"])
    assert ca.team_id == ctx["team_b"]
    assert cb.team_id == ctx["team_a"]
    # Contract value untouched (CLAUDE.md §2 rule 6).
    assert ca.contract_value == Decimal("10.00")
    assert cb.contract_value == Decimal("12.00")
    # State stays active.
    assert ca.state == "active" and cb.state == "active"


async def test_decline_ends_trade(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    trade_id = await _propose(pg_conn_migrated, ctx)
    await service.decline_trade(pg_conn_migrated, trade_id, actor_id=200)
    trade = await queries.fetch_trade_by_id(pg_conn_migrated, trade_id)
    assert trade.state == "declined"

    ca = await queries.fetch_contract_by_id(pg_conn_migrated, ctx["contract_a"])
    # Contract untouched.
    assert ca.team_id == ctx["team_a"]


async def test_withdraw_ends_trade(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    trade_id = await _propose(pg_conn_migrated, ctx)
    await service.withdraw_trade(pg_conn_migrated, trade_id, actor_id=100)
    trade = await queries.fetch_trade_by_id(pg_conn_migrated, trade_id)
    assert trade.state == "withdrawn"


async def test_commissioner_reject_ends_after_accept(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    trade_id = await _propose(pg_conn_migrated, ctx)
    await service.accept_trade(pg_conn_migrated, trade_id, actor_id=200)
    await service.commissioner_reject_trade(
        pg_conn_migrated, trade_id, actor_id=999, note="cap concern",
    )
    trade = await queries.fetch_trade_by_id(pg_conn_migrated, trade_id)
    assert trade.state == "rejected"


# ── illegal transitions raise ────────────────────────────────────────


async def test_cannot_propose_self_trade(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    with pytest.raises(service.TransitionError):
        await service.propose_trade(
            pg_conn_migrated,
            season_id=ctx["season_id"],
            proposing_team_id=ctx["team_a"],
            other_team_id=ctx["team_a"],
            proposed_by=100,
            items=[(ctx["team_a"], ctx["contract_a"])],
            message=None, ttl_hours=24,
        )


async def test_cannot_propose_trade_with_non_active_contract(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    # Void contract A.
    await pg_conn_migrated.execute(
        "UPDATE contracts SET state = 'voided' WHERE id = $1",
        ctx["contract_a"],
    )
    with pytest.raises(service.TransitionError):
        await _propose(pg_conn_migrated, ctx)


async def test_cannot_propose_trade_with_wrong_from_team(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    with pytest.raises(service.TransitionError):
        await service.propose_trade(
            pg_conn_migrated,
            season_id=ctx["season_id"],
            proposing_team_id=ctx["team_a"],
            other_team_id=ctx["team_b"],
            proposed_by=100,
            items=[
                # Claim team A is sending B's contract — invalid.
                (ctx["team_a"], ctx["contract_b"]),
            ],
            message=None, ttl_hours=24,
        )


async def test_accept_after_terminal_state_raises(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    trade_id = await _propose(pg_conn_migrated, ctx)
    await service.decline_trade(pg_conn_migrated, trade_id, actor_id=200)
    with pytest.raises(service.TransitionError):
        await service.accept_trade(pg_conn_migrated, trade_id, actor_id=200)


async def test_approve_before_accept_raises(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    trade_id = await _propose(pg_conn_migrated, ctx)
    with pytest.raises(service.TransitionError):
        await service.commissioner_approve_trade(
            pg_conn_migrated, trade_id, actor_id=999,
        )


# ── expiry ───────────────────────────────────────────────────────────


async def test_expire_all_past_ttl_trades_flips_expired(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    trade_id = await _propose(pg_conn_migrated, ctx)
    past = datetime.now(UTC) - timedelta(hours=1)
    await pg_conn_migrated.execute(
        "UPDATE trades SET expires_at = $1 WHERE id = $2", past, trade_id,
    )
    count = await service.expire_all_past_ttl_trades(pg_conn_migrated)
    assert count == 1
    trade = await queries.fetch_trade_by_id(pg_conn_migrated, trade_id)
    assert trade.state == "expired"


async def test_lazy_expiry_blocks_action_on_stale_trade(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    trade_id = await _propose(pg_conn_migrated, ctx)
    past = datetime.now(UTC) - timedelta(hours=1)
    await pg_conn_migrated.execute(
        "UPDATE trades SET expires_at = $1 WHERE id = $2", past, trade_id,
    )
    with pytest.raises(service.TransitionError):
        await service.accept_trade(pg_conn_migrated, trade_id, actor_id=200)
    trade = await queries.fetch_trade_by_id(pg_conn_migrated, trade_id)
    assert trade.state == "expired"


# ── ledger / audit ──────────────────────────────────────────────────


async def test_trade_writes_ledger_rows_for_both_teams(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    await _propose(pg_conn_migrated, ctx)
    ledger_a = await queries.fetch_ledger_for_team(
        pg_conn_migrated, ctx["team_a"], limit=10,
    )
    ledger_b = await queries.fetch_ledger_for_team(
        pg_conn_migrated, ctx["team_b"], limit=10,
    )
    assert any(e.kind == "trade" for e in ledger_a)
    assert any(e.kind == "trade" for e in ledger_b)


async def test_approve_ledger_captures_contract_transfer(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    trade_id = await _propose(pg_conn_migrated, ctx)
    await service.accept_trade(pg_conn_migrated, trade_id, actor_id=200)
    await service.commissioner_approve_trade(
        pg_conn_migrated, trade_id, actor_id=999,
    )
    # Ledger for contract A should have a 'trade' entry describing
    # the transfer.
    entries = await queries.fetch_ledger_for_contract(
        pg_conn_migrated, ctx["contract_a"],
    )
    transfer_events = [
        e for e in entries
        if e.kind == "trade" and e.detail.get("event") == "contract_transferred"
    ]
    assert len(transfer_events) == 1
    assert transfer_events[0].detail["from_team_id"] == ctx["team_a"]
    assert transfer_events[0].detail["to_team_id"] == ctx["team_b"]
