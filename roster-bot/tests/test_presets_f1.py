"""
F1 preset seeds a season's lookup rows, tiers, valuation factors, and
default league config exactly once — running it a second time is a no-op
so re-triggering setup during onboarding never causes duplicates or
overwrites.
"""

from decimal import Decimal

from bot import queries
from bot.presets import f1 as f1_preset


async def _create_season(conn, guild_id: int = 42, name: str = "F1 2026") -> int:
    return await queries.insert_season(conn, guild_id, name, is_active=True)


async def test_seeds_expected_generic_rows(pg_conn_migrated):
    season_id = await _create_season(pg_conn_migrated)
    await f1_preset.seed_season(pg_conn_migrated, season_id)

    # Three tiers, ranked in order.
    tiers = await queries.fetch_all_tiers(pg_conn_migrated, season_id)
    assert [t.code for t in tiers] == ["t1", "t2", "t3"]
    assert [t.rank_order for t in tiers] == [1, 2, 3]
    # Tier roles are unset — commissioner assigns via /market-admin tier edit.
    assert all(t.tier_role_id is None for t in tiers)

    # Global lookups populated (checking a representative row from each).
    for table, code in [
        ("driver_statuses", "active"),
        ("contract_types", "standard"),
        ("contract_states", "active"),
        ("offer_states", "pending_driver"),
        ("transaction_kinds", "contract_signed"),
        ("board_kinds", "market"),
    ]:
        assert await pg_conn_migrated.fetchval(
            f"SELECT count(*) FROM {table} WHERE code = $1", code
        ) == 1, f"{table} missing '{code}' row"

    # Valuation factors seeded scoped to this season.
    factors = await pg_conn_migrated.fetch(
        "SELECT code, weight FROM valuation_factors WHERE season_id = $1 ORDER BY sort_order",
        season_id,
    )
    codes = [r["code"] for r in factors]
    assert "race_finish" in codes
    assert "wins" in codes
    assert any(r["weight"] < 0 for r in factors), "expected at least one penalty factor"

    # Default league config row (tier_id NULL) exists with Decimal money.
    cfg = await queries.fetch_league_config_row(pg_conn_migrated, season_id, None)
    assert cfg is not None
    assert isinstance(cfg.salary_cap, Decimal)
    assert cfg.salary_cap > Decimal("0")
    assert cfg.active_driver_slots >= 1
    assert cfg.weekly_move_cap <= cfg.exceptional_move_cap


async def test_seed_is_idempotent(pg_conn_migrated):
    season_id = await _create_season(pg_conn_migrated)
    await f1_preset.seed_season(pg_conn_migrated, season_id)

    counts_first = await _snapshot(pg_conn_migrated, season_id)
    await f1_preset.seed_season(pg_conn_migrated, season_id)
    counts_second = await _snapshot(pg_conn_migrated, season_id)

    assert counts_first == counts_second


async def test_two_seasons_share_global_lookups_but_have_own_factors(pg_conn_migrated):
    season_a = await _create_season(pg_conn_migrated, guild_id=1, name="A")
    season_b = await _create_season(pg_conn_migrated, guild_id=2, name="B")
    await f1_preset.seed_season(pg_conn_migrated, season_a)
    await f1_preset.seed_season(pg_conn_migrated, season_b)

    # Global lookups deduplicate: only one 'active' driver_status row exists.
    assert await pg_conn_migrated.fetchval(
        "SELECT count(*) FROM driver_statuses WHERE code = 'active'"
    ) == 1

    # But each season has its own valuation_factors rows.
    per_season_factor_counts = await pg_conn_migrated.fetch(
        "SELECT season_id, count(*) AS c FROM valuation_factors "
        "WHERE season_id = ANY($1::bigint[]) GROUP BY season_id",
        [season_a, season_b],
    )
    counts = {r["season_id"]: r["c"] for r in per_season_factor_counts}
    assert counts[season_a] == counts[season_b] > 0

    # Each season has its own default league_config row (season-scoped uniqueness).
    cfg_a = await queries.fetch_league_config_row(pg_conn_migrated, season_a, None)
    cfg_b = await queries.fetch_league_config_row(pg_conn_migrated, season_b, None)
    assert cfg_a is not None and cfg_b is not None
    assert cfg_a.id != cfg_b.id


async def _snapshot(conn, season_id: int) -> dict[str, int]:
    tables = [
        "driver_statuses", "contract_types", "contract_states",
        "offer_states", "transaction_kinds", "board_kinds",
    ]
    counts = {t: await conn.fetchval(f"SELECT count(*) FROM {t}") for t in tables}
    counts["tiers"] = await conn.fetchval(
        "SELECT count(*) FROM tiers WHERE season_id = $1", season_id
    )
    counts["valuation_factors"] = await conn.fetchval(
        "SELECT count(*) FROM valuation_factors WHERE season_id = $1", season_id
    )
    counts["league_config"] = await conn.fetchval(
        "SELECT count(*) FROM league_config WHERE season_id = $1", season_id
    )
    return counts
