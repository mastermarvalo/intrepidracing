"""
The four status reads behind the control panel.

These are cheap queries but the panel's "next step" line is only correct
if they are: a leaked cross-tier or cross-season row would tell an admin
to publish a run that belongs to a different tier. Tier and season
isolation is therefore the main thing asserted here.
"""

import itertools
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bot import queries
from bot.presets import f1 as f1_preset

PENDING = "pending_approval"


async def _season(conn, *, guild_id=1, name="S1", active=True):
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES ($1, $2, $3) RETURNING id",
        guild_id,
        name,
        active,
    )
    await f1_preset.seed_season(conn, season_id)
    tiers = {
        r["code"]: r["id"]
        for r in await conn.fetch("SELECT code, id FROM tiers WHERE season_id = $1", season_id)
    }
    return season_id, tiers


async def _team(conn, key="mcl", *, guild_id=1):
    return await queries.insert_team(
        conn,
        guild_id,
        key,
        key.upper(),
        team_role_id=1,
        channel_id=1,
        tagline=None,
        logo_url=None,
        banner_url=None,
        principal_role_id=None,
    )


_member_seq = itertools.count(200)


async def _offer(conn, season_id, tier_id, *, state=PENDING):
    member_id = next(_member_seq)
    driver_id = await queries.insert_driver(
        conn, season_id, tier_id, member_id=member_id,
        display_name=f"D{member_id}", status="active",
    )
    team_id = await _team(conn, key=f"t{driver_id}")
    return await queries.insert_offer(
        conn,
        season_id=season_id,
        tier_id=tier_id,
        driver_id=driver_id,
        team_id=team_id,
        offered_by=1,
        offer_kind="new",
        salary=Decimal("5.00"),
        term_seasons=1,
        contract_type="standard",
        state=state,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=48),
    )


async def _trade(conn, season_id, *, state=PENDING, suffix="a"):
    a = await _team(conn, key=f"x{suffix}")
    b = await _team(conn, key=f"y{suffix}")
    return await queries.insert_trade(
        conn,
        season_id=season_id,
        proposing_team_id=a,
        other_team_id=b,
        proposed_by=1,
        state=state,
        message=None,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=48),
    )


# ── fetch_latest_unpublished_run_id ──────────────────────────────────


async def test_no_runs_returns_none(pg_conn_migrated):
    season_id, tiers = await _season(pg_conn_migrated)
    got = await queries.fetch_latest_unpublished_run_id(
        pg_conn_migrated, season_id, tiers["t1"]
    )
    assert got is None


async def test_published_runs_are_not_reported_as_pending(pg_conn_migrated):
    season_id, tiers = await _season(pg_conn_migrated)
    await queries.insert_valuation_run(
        pg_conn_migrated,
        season_id=season_id,
        tier_id=tiers["t1"],
        round_label="R1",
        created_by=1,
        published=True,
    )
    assert (
        await queries.fetch_latest_unpublished_run_id(
            pg_conn_migrated, season_id, tiers["t1"]
        )
        is None
    )


async def test_most_recent_unpublished_run_wins(pg_conn_migrated):
    season_id, tiers = await _season(pg_conn_migrated)
    for label in ("R1", "R2", "R3"):
        newest = await queries.insert_valuation_run(
            pg_conn_migrated,
            season_id=season_id,
            tier_id=tiers["t1"],
            round_label=label,
            created_by=1,
        )
    got = await queries.fetch_latest_unpublished_run_id(
        pg_conn_migrated, season_id, tiers["t1"]
    )
    assert got == newest


async def test_unpublished_run_does_not_leak_across_tiers(pg_conn_migrated):
    """Tier isolation — the panel must never tell t2 to publish t1's run."""
    season_id, tiers = await _season(pg_conn_migrated)
    await queries.insert_valuation_run(
        pg_conn_migrated,
        season_id=season_id,
        tier_id=tiers["t1"],
        round_label="R1",
        created_by=1,
    )
    assert (
        await queries.fetch_latest_unpublished_run_id(
            pg_conn_migrated, season_id, tiers["t2"]
        )
        is None
    )


async def test_unpublished_run_does_not_leak_across_seasons(pg_conn_migrated):
    old_id, old_tiers = await _season(pg_conn_migrated, name="S0", active=False)
    new_id, _ = await _season(pg_conn_migrated, name="S1")
    await queries.insert_valuation_run(
        pg_conn_migrated,
        season_id=old_id,
        tier_id=old_tiers["t1"],
        round_label="R1",
        created_by=1,
    )
    assert (
        await queries.fetch_latest_unpublished_run_id(
            pg_conn_migrated, new_id, old_tiers["t1"]
        )
        is None
    )


# ── tier_has_published_valuation ─────────────────────────────────────


async def test_tier_without_runs_has_no_market(pg_conn_migrated):
    _, tiers = await _season(pg_conn_migrated)
    assert not await queries.tier_has_published_valuation(pg_conn_migrated, tiers["t1"])


async def test_dry_run_alone_is_not_a_market(pg_conn_migrated):
    season_id, tiers = await _season(pg_conn_migrated)
    await queries.insert_valuation_run(
        pg_conn_migrated,
        season_id=season_id,
        tier_id=tiers["t1"],
        round_label="R1",
        created_by=1,
    )
    assert not await queries.tier_has_published_valuation(pg_conn_migrated, tiers["t1"])


async def test_one_published_run_makes_a_market(pg_conn_migrated):
    season_id, tiers = await _season(pg_conn_migrated)
    await queries.insert_valuation_run(
        pg_conn_migrated,
        season_id=season_id,
        tier_id=tiers["t1"],
        round_label="R1",
        created_by=1,
        published=True,
    )
    assert await queries.tier_has_published_valuation(pg_conn_migrated, tiers["t1"])
    assert not await queries.tier_has_published_valuation(pg_conn_migrated, tiers["t2"])


# ── Approval queues ──────────────────────────────────────────────────


async def test_offer_queue_counts_only_pending_approval(pg_conn_migrated):
    season_id, tiers = await _season(pg_conn_migrated)
    await _offer(pg_conn_migrated, season_id, tiers["t1"], state=PENDING)
    await _offer(pg_conn_migrated, season_id, tiers["t1"], state=PENDING)
    for other in ("draft", "pending_driver", "accepted", "declined"):
        await _offer(pg_conn_migrated, season_id, tiers["t1"], state=other)

    assert await queries.count_offers_awaiting_approval(pg_conn_migrated, season_id) == 2


async def test_offer_queue_is_season_scoped(pg_conn_migrated):
    old_id, old_tiers = await _season(pg_conn_migrated, name="S0", active=False)
    new_id, _ = await _season(pg_conn_migrated, name="S1")
    await _offer(pg_conn_migrated, old_id, old_tiers["t1"])

    assert await queries.count_offers_awaiting_approval(pg_conn_migrated, new_id) == 0
    assert await queries.count_offers_awaiting_approval(pg_conn_migrated, old_id) == 1


async def test_trade_queue_counts_only_pending_approval(pg_conn_migrated):
    season_id, _ = await _season(pg_conn_migrated)
    await _trade(pg_conn_migrated, season_id, suffix="1")
    for i, other in enumerate(("draft", "pending_other", "accepted", "declined")):
        await _trade(pg_conn_migrated, season_id, state=other, suffix=f"o{i}")

    assert await queries.count_trades_awaiting_approval(pg_conn_migrated, season_id) == 1


async def test_trade_queue_is_season_scoped(pg_conn_migrated):
    old_id, _ = await _season(pg_conn_migrated, name="S0", active=False)
    new_id, _ = await _season(pg_conn_migrated, name="S1")
    await _trade(pg_conn_migrated, old_id, suffix="1")

    assert await queries.count_trades_awaiting_approval(pg_conn_migrated, new_id) == 0


async def test_empty_queues_return_zero_not_none(pg_conn_migrated):
    season_id, _ = await _season(pg_conn_migrated)
    assert await queries.count_offers_awaiting_approval(pg_conn_migrated, season_id) == 0
    assert await queries.count_trades_awaiting_approval(pg_conn_migrated, season_id) == 0
