"""
Market-layer DB queries: fetch_market_table_for_tier,
fetch_movers_for_tier, fetch_cross_tier_top, and the market_boards
CRUD. All exercised against a temp-schema Postgres to catch SQL bugs
that unit-only tests can't.
"""

from decimal import Decimal

from bot import queries
from bot.market import valuation as engine
from bot.presets import f1 as f1_preset


async def _seed_tier(conn, guild_id: int = 1, tier_code: str = "t1"):
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES ($1, 'S1', TRUE) "
        "RETURNING id",
        guild_id,
    )
    await f1_preset.seed_season(conn, season_id)
    tier_id = await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = $2",
        season_id, tier_code,
    )
    return season_id, tier_id


async def _seed_driver(conn, season_id, tier_id, *, member_id, name):
    return await queries.insert_driver(
        conn, season_id, tier_id,
        member_id=member_id, display_name=name, status="active",
    )


async def _publish_run(conn, season_id, tier_id, drivers_with_deltas):
    """
    drivers_with_deltas: list of (driver_id, name, prev_value, delta).
    Uses the real engine so the persistence path matches what the
    admin cog would write.
    """
    driver_inputs = [
        engine.DriverInput(
            driver_id=did, display_name=name,
            previous_value=prev, factor_values={"x": delta},
        )
        for did, name, prev, delta in drivers_with_deltas
    ]
    factors = [engine.FactorWeight(code="x", weight=Decimal("1"), max_contribution=None)]
    caps = engine.MovementCaps(weekly=Decimal("10"), exceptional=Decimal("20"))
    outcomes = engine.compute_run(factors, driver_inputs, caps)
    run_id = await queries.insert_valuation_run(
        conn,
        season_id=season_id, tier_id=tier_id,
        round_label="R1", created_by=None, published=True,
    )
    await queries.insert_driver_valuations(
        conn, run_id,
        [{
            "driver_id": v.driver_id,
            "market_value": v.market_value,
            "previous_value": v.previous_value,
            "delta": v.delta,
            "rank_in_tier": v.rank_in_tier,
            "capped": v.capped,
            "breakdown": engine.breakdown_to_json(v.breakdown),
        } for v in outcomes],
    )
    return run_id


# ── market table ─────────────────────────────────────────────────────


async def test_fetch_market_table_returns_empty_when_no_run(pg_conn_migrated):
    season_id, tier_id = await _seed_tier(pg_conn_migrated)
    await _seed_driver(pg_conn_migrated, season_id, tier_id, member_id=1, name="A")
    rows = await queries.fetch_market_table_for_tier(pg_conn_migrated, tier_id)
    assert rows == []


async def test_fetch_market_table_uses_latest_published_run(pg_conn_migrated):
    season_id, tier_id = await _seed_tier(pg_conn_migrated)
    a = await _seed_driver(pg_conn_migrated, season_id, tier_id, member_id=1, name="A")
    b = await _seed_driver(pg_conn_migrated, season_id, tier_id, member_id=2, name="B")
    # Older published run — should be ignored.
    await _publish_run(pg_conn_migrated, season_id, tier_id, [
        (a, "A", Decimal("10.00"), Decimal("1")),
        (b, "B", Decimal("10.00"), Decimal("2")),
    ])
    # Newer published run.
    await _publish_run(pg_conn_migrated, season_id, tier_id, [
        (a, "A", Decimal("11.00"), Decimal("5")),
        (b, "B", Decimal("12.00"), Decimal("0")),
    ])
    rows = await queries.fetch_market_table_for_tier(pg_conn_migrated, tier_id)
    by_name = {r["display_name"]: r for r in rows}
    assert by_name["A"]["market_value"] == Decimal("16.00")
    assert by_name["B"]["market_value"] == Decimal("12.00")
    # Rank ordering: A (16.00) beats B (12.00) → A rank 1, B rank 2.
    assert by_name["A"]["rank_in_tier"] == 1
    assert by_name["B"]["rank_in_tier"] == 2


async def test_fetch_movers_splits_by_sign(pg_conn_migrated):
    season_id, tier_id = await _seed_tier(pg_conn_migrated)
    a = await _seed_driver(pg_conn_migrated, season_id, tier_id, member_id=1, name="A")
    b = await _seed_driver(pg_conn_migrated, season_id, tier_id, member_id=2, name="B")
    c = await _seed_driver(pg_conn_migrated, season_id, tier_id, member_id=3, name="C")
    await _publish_run(pg_conn_migrated, season_id, tier_id, [
        (a, "A", Decimal("10.00"), Decimal("3")),   # up
        (b, "B", Decimal("10.00"), Decimal("-2")),  # down
        (c, "C", Decimal("10.00"), Decimal("0")),   # flat — excluded from both
    ])
    risers, fallers = await queries.fetch_movers_for_tier(
        pg_conn_migrated, tier_id, limit=5
    )
    assert [r["display_name"] for r in risers] == ["A"]
    assert [r["display_name"] for r in fallers] == ["B"]


async def test_cross_tier_top_returns_one_bucket_per_tier(pg_conn_migrated):
    # Two tiers, one driver each, publish both.
    season_id, t1_id = await _seed_tier(pg_conn_migrated, tier_code="t1")
    t2_id = await pg_conn_migrated.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't2'", season_id
    )
    a = await _seed_driver(pg_conn_migrated, season_id, t1_id, member_id=1, name="A")
    b = await _seed_driver(pg_conn_migrated, season_id, t2_id, member_id=2, name="B")
    await _publish_run(pg_conn_migrated, season_id, t1_id, [
        (a, "A", Decimal("10.00"), Decimal("0")),
    ])
    await _publish_run(pg_conn_migrated, season_id, t2_id, [
        (b, "B", Decimal("10.00"), Decimal("0")),
    ])
    rows = await queries.fetch_cross_tier_top(pg_conn_migrated, season_id, per_tier=3)
    codes = [r["tier_code"] for r in rows]
    assert "t1" in codes and "t2" in codes


# ── market boards CRUD ───────────────────────────────────────────────


async def test_market_board_insert_fetch_update_delete(pg_conn_migrated):
    season_id, tier_id = await _seed_tier(pg_conn_migrated)

    board_id = await queries.insert_market_board(
        pg_conn_migrated,
        season_id=season_id, tier_id=tier_id,
        kind="market", channel_id=1234,
    )
    board = await queries.fetch_market_board_by_id(pg_conn_migrated, board_id)
    assert board is not None
    assert board.kind == "market"
    assert board.channel_id == 1234
    assert board.message_id is None

    await queries.set_market_board_message_id(pg_conn_migrated, board_id, 5678)
    board2 = await queries.fetch_market_board_by_id(pg_conn_migrated, board_id)
    assert board2 is not None
    assert board2.message_id == 5678

    # Cross-tier (dashboard) board coexists with tier board.
    await queries.insert_market_board(
        pg_conn_migrated,
        season_id=season_id, tier_id=None,
        kind="dashboard", channel_id=1235,
    )
    tier_scoped = await queries.fetch_market_boards_for_tier(
        pg_conn_migrated, season_id, tier_id
    )
    cross_scoped = await queries.fetch_market_boards_for_tier(
        pg_conn_migrated, season_id, None
    )
    assert len(tier_scoped) == 1
    assert len(cross_scoped) == 1
    assert cross_scoped[0].tier_id is None

    all_boards = await queries.fetch_market_boards_in_season(pg_conn_migrated, season_id)
    assert len(all_boards) == 2

    await queries.delete_market_board(pg_conn_migrated, board_id)
    assert await queries.fetch_market_board_by_id(pg_conn_migrated, board_id) is None


async def test_market_board_kind_fk_enforced(pg_conn_migrated):
    """Unknown board kinds should be rejected by the FK to board_kinds."""
    season_id, _ = await _seed_tier(pg_conn_migrated)
    import asyncpg
    try:
        await queries.insert_market_board(
            pg_conn_migrated,
            season_id=season_id, tier_id=None,
            kind="nonsense", channel_id=1,
        )
    except asyncpg.ForeignKeyViolationError:
        pass
    else:
        raise AssertionError("Expected ForeignKeyViolationError for unknown kind")
