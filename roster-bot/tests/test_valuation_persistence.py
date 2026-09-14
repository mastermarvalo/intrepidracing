"""
Round-trip: engine outputs → queries.insert_driver_valuations → DB row →
queries.fetch_latest_published_valuation.

This is the seam where the pure engine meets the persistence layer, and
where two things could go wrong that unit tests alone don't catch:
JSONB serialisation of the breakdown, and the "latest published"
lookup ignoring dry-run rows.
"""

from decimal import Decimal

from bot import queries
from bot.market import valuation as engine
from bot.presets import f1 as f1_preset


async def _bootstrap(conn):
    """Season + tier + one driver, plus F1 preset for lookups."""
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES (1, 'S1', TRUE) RETURNING id"
    )
    await f1_preset.seed_season(conn, season_id)
    tier_id = await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", season_id
    )
    driver_id = await queries.insert_driver(
        conn, season_id, tier_id, member_id=111, display_name="Test", status="active"
    )
    return season_id, tier_id, driver_id


async def test_engine_outputs_persist_and_reload_intact(pg_conn_migrated):
    season_id, tier_id, driver_id = await _bootstrap(pg_conn_migrated)

    outcomes = engine.compute_run(
        factors=[
            engine.FactorWeight(code="wins", weight=Decimal("0.3"), max_contribution=None),
        ],
        drivers=[
            engine.DriverInput(
                driver_id=driver_id,
                display_name="Test",
                previous_value=Decimal("20.00"),
                factor_values={"wins": Decimal("1")},
            ),
        ],
        caps=engine.MovementCaps(weekly=Decimal("0.75"), exceptional=Decimal("1.25")),
    )
    assert outcomes[0].delta == Decimal("0.30")

    run_id = await queries.insert_valuation_run(
        pg_conn_migrated,
        season_id=season_id,
        tier_id=tier_id,
        round_label="R1",
        created_by=None,
        published=False,
    )
    await queries.insert_driver_valuations(
        pg_conn_migrated,
        run_id,
        [{
            "driver_id": outcomes[0].driver_id,
            "market_value": outcomes[0].market_value,
            "previous_value": outcomes[0].previous_value,
            "delta": outcomes[0].delta,
            "rank_in_tier": outcomes[0].rank_in_tier,
            "capped": outcomes[0].capped,
            "breakdown": engine.breakdown_to_json(outcomes[0].breakdown),
        }],
    )

    stored = await queries.fetch_driver_valuations_for_run(pg_conn_migrated, run_id)
    assert len(stored) == 1
    assert stored[0]["market_value"] == Decimal("20.30")
    assert stored[0]["delta"] == Decimal("0.30")
    # JSONB round-trips as a Python list.
    breakdown = stored[0]["breakdown"]
    if isinstance(breakdown, str):
        # asyncpg without the JSONB codec returns str; parse to compare.
        import json
        breakdown = json.loads(breakdown)
    assert isinstance(breakdown, list)
    assert breakdown[0]["code"] == "wins"
    assert breakdown[0]["contribution"] == "0.3"


async def test_fetch_latest_published_ignores_dry_runs(pg_conn_migrated):
    season_id, tier_id, driver_id = await _bootstrap(pg_conn_migrated)

    # A dry-run at $19.
    dry_run_id = await queries.insert_valuation_run(
        pg_conn_migrated,
        season_id=season_id, tier_id=tier_id,
        round_label="Dry", created_by=None, published=False,
    )
    await queries.insert_driver_valuations(
        pg_conn_migrated,
        dry_run_id,
        [{
            "driver_id": driver_id,
            "market_value": Decimal("19.00"),
            "previous_value": None,
            "delta": Decimal("0.00"),
            "rank_in_tier": 1,
            "capped": False,
            "breakdown": [],
        }],
    )
    # A published run at $20 (older by insertion order — but the
    # published_at column is what sorts them).
    published_run_id = await queries.insert_valuation_run(
        pg_conn_migrated,
        season_id=season_id, tier_id=tier_id,
        round_label="Pub", created_by=None, published=True,
    )
    await queries.insert_driver_valuations(
        pg_conn_migrated,
        published_run_id,
        [{
            "driver_id": driver_id,
            "market_value": Decimal("20.00"),
            "previous_value": None,
            "delta": Decimal("0.00"),
            "rank_in_tier": 1,
            "capped": False,
            "breakdown": [],
        }],
    )
    latest = await queries.fetch_latest_published_valuation(pg_conn_migrated, driver_id)
    assert latest == Decimal("20.00")


async def test_publish_run_flips_flag(pg_conn_migrated):
    season_id, tier_id, driver_id = await _bootstrap(pg_conn_migrated)

    run_id = await queries.insert_valuation_run(
        pg_conn_migrated,
        season_id=season_id, tier_id=tier_id,
        round_label="R1", created_by=None, published=False,
    )
    assert (await queries.fetch_valuation_run(pg_conn_migrated, run_id))["published"] is False

    await queries.publish_valuation_run(pg_conn_migrated, run_id)
    row = await queries.fetch_valuation_run(pg_conn_migrated, run_id)
    assert row["published"] is True
    assert row["published_at"] is not None

    # Re-publishing is a no-op (WHERE NOT published clause keeps the timestamp stable).
    first_publish_ts = row["published_at"]
    await queries.publish_valuation_run(pg_conn_migrated, run_id)
    row2 = await queries.fetch_valuation_run(pg_conn_migrated, run_id)
    assert row2["published_at"] == first_publish_ts
