"""
`/market-admin driver` enrolment is idempotent, writes a ledger row on
insert only, and skips the ledger on repeated syncs. The pure helper
lives in bot.market.driver_ops so it can be tested against
`pg_conn_migrated` directly without any discord.py fakes.
"""

from bot import queries
from bot.market import driver_ops
from bot.presets import f1 as f1_preset


async def _prime(conn):
    """A season with F1 preset (three tiers + generic lookups)."""
    season_id = await queries.insert_season(conn, guild_id=42, name="F1 2026", is_active=True)
    await f1_preset.seed_season(conn, season_id)
    tiers = await queries.fetch_all_tiers(conn, season_id)
    return season_id, tiers[0]  # tier t1


async def test_migration_seeds_driver_registered_kind(pg_conn_migrated):
    """Migration 012 inserts the ledger kind even on a fresh DB."""
    assert await pg_conn_migrated.fetchval(
        "SELECT count(*) FROM transaction_kinds WHERE code = 'driver_registered'"
    ) == 1


async def test_enrol_driver_creates_row_and_ledger(pg_conn_migrated):
    season_id, tier = await _prime(pg_conn_migrated)

    result = await driver_ops.enrol_driver(
        pg_conn_migrated,
        season_id=season_id,
        tier_id=tier.id,
        seed=driver_ops.DriverSeed(member_id=555, display_name="Alonso"),
        status="active",
        actor_id=999,
    )

    assert result.created is True
    driver = await queries.fetch_driver(pg_conn_migrated, season_id, tier.id, 555)
    assert driver is not None
    assert driver.display_name == "Alonso"
    assert driver.status == "active"

    ledger_count = await pg_conn_migrated.fetchval(
        "SELECT count(*) FROM contract_ledger "
        "WHERE driver_id = $1 AND kind = 'driver_registered'",
        driver.id,
    )
    assert ledger_count == 1


async def test_enrol_driver_is_idempotent(pg_conn_migrated):
    """Re-enrolling the same member returns created=False and writes no ledger row."""
    season_id, tier = await _prime(pg_conn_migrated)
    seed = driver_ops.DriverSeed(member_id=777, display_name="Sainz")

    first = await driver_ops.enrol_driver(
        pg_conn_migrated, season_id=season_id, tier_id=tier.id,
        seed=seed, status="active", actor_id=1,
    )
    second = await driver_ops.enrol_driver(
        pg_conn_migrated, season_id=season_id, tier_id=tier.id,
        seed=seed, status="active", actor_id=1,
    )

    assert first.created is True
    assert second.created is False
    assert first.driver_id == second.driver_id

    ledger_count = await pg_conn_migrated.fetchval(
        "SELECT count(*) FROM contract_ledger "
        "WHERE driver_id = $1 AND kind = 'driver_registered'",
        first.driver_id,
    )
    assert ledger_count == 1


async def test_sync_tier_enrols_only_missing(pg_conn_migrated):
    season_id, tier = await _prime(pg_conn_migrated)
    await driver_ops.enrol_driver(
        pg_conn_migrated, season_id=season_id, tier_id=tier.id,
        seed=driver_ops.DriverSeed(member_id=1, display_name="A"),
        status="active", actor_id=None,
    )

    seeds = [
        driver_ops.DriverSeed(member_id=1, display_name="A"),  # already registered
        driver_ops.DriverSeed(member_id=2, display_name="B"),
        driver_ops.DriverSeed(member_id=3, display_name="C"),
    ]
    results = await driver_ops.sync_tier(
        pg_conn_migrated, season_id=season_id, tier_id=tier.id,
        seeds=seeds, status="active", actor_id=None,
    )

    created = [r for r in results if r.created]
    skipped = [r for r in results if not r.created]
    assert {r.seed.member_id for r in created} == {2, 3}
    assert {r.seed.member_id for r in skipped} == {1}


async def test_sync_tier_isolates_across_tiers(pg_conn_migrated):
    """
    Registering a member in tier 1 must not silence them in tier 2 — the
    same member can hold a drivers row in each tier (CLAUDE.md §4/003).
    """
    season_id, t1 = await _prime(pg_conn_migrated)
    t2 = (await queries.fetch_all_tiers(pg_conn_migrated, season_id))[1]

    seed = driver_ops.DriverSeed(member_id=42, display_name="Verstappen")
    r1 = await driver_ops.enrol_driver(
        pg_conn_migrated, season_id=season_id, tier_id=t1.id,
        seed=seed, status="active", actor_id=None,
    )
    r2 = await driver_ops.enrol_driver(
        pg_conn_migrated, season_id=season_id, tier_id=t2.id,
        seed=seed, status="reserve", actor_id=None,
    )

    assert r1.created is True
    assert r2.created is True
    assert r1.driver_id != r2.driver_id


async def test_unregistered_in_tier_returns_only_missing(pg_conn_migrated):
    season_id, tier = await _prime(pg_conn_migrated)
    await driver_ops.enrol_driver(
        pg_conn_migrated, season_id=season_id, tier_id=tier.id,
        seed=driver_ops.DriverSeed(member_id=11, display_name="X"),
        status="active", actor_id=None,
    )

    missing = await driver_ops.unregistered_in_tier(
        pg_conn_migrated, season_id=season_id, tier_id=tier.id,
        seeds=[
            driver_ops.DriverSeed(member_id=11, display_name="X"),
            driver_ops.DriverSeed(member_id=12, display_name="Y"),
        ],
    )
    assert [s.member_id for s in missing] == [12]
