"""
Migrations apply cleanly onto both a truly fresh database and an
already-live 001_init.sql database with existing teams rows — the two
real upgrade paths (fresh install and in-place upgrade from the current
production schema).

These tests use the pg_conn fixture (temp schema, auto-dropped), so
running the suite never touches the developer's real dev DB.
"""

from decimal import Decimal

from tests.conftest import apply_migrations


async def test_fresh_db_all_migrations_apply(pg_conn):
    applied = await apply_migrations(pg_conn)
    assert "001_init.sql" in applied
    assert "002_seasons_tiers.sql" in applied
    assert "003_drivers_and_tier_membership.sql" in applied
    assert "004_valuations.sql" in applied
    assert "006_league_config_and_presets.sql" in applied

    tables = {
        r["table_name"]
        for r in await pg_conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema()"
        )
    }
    # Existing baseline
    assert {"teams", "team_slots", "guild_config", "stat_boards", "transactions"} <= tables
    # Phase 1 additions
    assert {
        "seasons", "tiers", "drivers", "driver_statuses",
        "contract_types", "contract_states", "offer_states",
        "transaction_kinds", "board_kinds", "valuation_factors",
        "league_config",
    } <= tables
    # Phase 2 additions
    assert {"valuation_runs", "driver_valuations"} <= tables
    # Phase 3 additions
    assert {"market_boards"} <= tables
    assert "007_market_boards.sql" in applied
    # Phase 4 additions
    assert {"contracts", "contract_offers", "contract_ledger"} <= tables
    assert "008_contracts_and_offers.sql" in applied
    # Phase 5 additions
    assert {"trades", "trade_items", "dead_money", "trade_states"} <= tables
    assert "009_trades_and_dead_money.sql" in applied


async def test_phase2_migration_upgrades_a_phase1_db(pg_conn):
    """
    Simulate a live database that already ran Phase 1 (through 006)
    and now receives migration 004. This is the real upgrade path for
    an already-deployed league.
    """
    for name in (
        "001_init.sql",
        "002_seasons_tiers.sql",
        "003_drivers_and_tier_membership.sql",
        "006_league_config_and_presets.sql",
    ):
        path = next(p for p in _list_migrations() if p.name == name)
        await pg_conn.execute(path.read_text())

    # Seed a season, tier, driver so 004's FK targets exist.
    season_id = await pg_conn.fetchval(
        "INSERT INTO seasons (guild_id, name) VALUES (1, 'S1') RETURNING id"
    )
    tier_id = await pg_conn.fetchval(
        "INSERT INTO tiers (season_id, code, label, rank_order) "
        "VALUES ($1, 't1', 'Tier 1', 1) RETURNING id",
        season_id,
    )
    await pg_conn.execute(
        "INSERT INTO driver_statuses (code, label) VALUES ('active', 'Active') "
        "ON CONFLICT DO NOTHING"
    )
    driver_id = await pg_conn.fetchval(
        "INSERT INTO drivers (season_id, tier_id, member_id, display_name, status) "
        "VALUES ($1, $2, 111, 'Test', 'active') RETURNING id",
        season_id, tier_id,
    )

    # Now apply migration 004.
    path = next(p for p in _list_migrations() if p.name == "004_valuations.sql")
    await pg_conn.execute(path.read_text())

    # Prove the new tables function: create a run + a driver_valuation.
    run_id = await pg_conn.fetchval(
        "INSERT INTO valuation_runs (season_id, tier_id, round_label) "
        "VALUES ($1, $2, 'Test') RETURNING id",
        season_id, tier_id,
    )
    await pg_conn.execute(
        """
        INSERT INTO driver_valuations
            (run_id, driver_id, market_value, previous_value, delta,
             rank_in_tier, capped, breakdown)
        VALUES ($1, $2, 20.75, 20.00, 0.75, 1, FALSE, '[]'::jsonb)
        """,
        run_id, driver_id,
    )
    row = await pg_conn.fetchrow(
        "SELECT market_value, breakdown FROM driver_valuations WHERE run_id = $1",
        run_id,
    )
    assert row["market_value"] == Decimal("20.75")
    # asyncpg returns JSONB as a text string by default (no codec
    # registered). The application layer parses when it needs a list.
    assert row["breakdown"] == "[]"


async def test_upgrade_from_001_only_preserves_existing_teams(pg_conn):
    # Simulate the live production schema (001 only) with real data.
    await apply_migrations(pg_conn, up_to="001_init.sql")
    await pg_conn.execute(
        """
        INSERT INTO teams
            (guild_id, key, name, team_role_id, channel_id)
        VALUES (100, 'redbull', 'Red Bull', 200, 300),
               (100, 'ferrari', 'Ferrari', 201, 301)
        """
    )

    # Now run the Phase 1 migrations on top of that.
    for name in (
        "002_seasons_tiers.sql",
        "003_drivers_and_tier_membership.sql",
        "006_league_config_and_presets.sql",
    ):
        path = next(p for p in _list_migrations() if p.name == name)
        await pg_conn.execute(path.read_text())

    # Existing team rows survive with season_id / tier_id both NULL — the
    # documented "belongs to the active season" default.
    rows = await pg_conn.fetch("SELECT key, season_id, tier_id FROM teams ORDER BY key")
    assert [r["key"] for r in rows] == ["ferrari", "redbull"]
    assert all(r["season_id"] is None for r in rows)
    assert all(r["tier_id"] is None for r in rows)


async def test_only_one_active_season_per_guild_enforced(pg_conn_migrated):
    await pg_conn_migrated.execute(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES (1, 'S1', TRUE)"
    )
    # A second active row in the same guild must be rejected by the
    # partial unique index. Postgres raises UniqueViolationError.
    import asyncpg
    try:
        await pg_conn_migrated.execute(
            "INSERT INTO seasons (guild_id, name, is_active) VALUES (1, 'S2', TRUE)"
        )
    except asyncpg.UniqueViolationError:
        pass
    else:
        raise AssertionError("Expected UniqueViolationError")

    # But an inactive second season in the same guild is fine.
    await pg_conn_migrated.execute(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES (1, 'S2', FALSE)"
    )


async def test_league_config_partial_uniqueness(pg_conn_migrated):
    # Set up prerequisites.
    season_id = await pg_conn_migrated.fetchval(
        "INSERT INTO seasons (guild_id, name) VALUES (1, 'S1') RETURNING id"
    )
    tier_id = await pg_conn_migrated.fetchval(
        "INSERT INTO tiers (season_id, code, label, rank_order) "
        "VALUES ($1, 't1', 'Tier 1', 1) RETURNING id",
        season_id,
    )
    base_args = (
        season_id, None, 100.00, 1.00, None, 2, 0.75, 1.25, 3, 0.15, 48,
    )
    sql = """
        INSERT INTO league_config
            (season_id, tier_id, salary_cap, min_salary, max_salary,
             active_driver_slots, weekly_move_cap, exceptional_move_cap,
             max_term_seasons, max_incentive_pct, offer_ttl_hours)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    """

    # Two season-default rows (tier_id NULL) for the same season → rejected.
    await pg_conn_migrated.execute(sql, *base_args)
    import asyncpg
    try:
        await pg_conn_migrated.execute(sql, *base_args)
    except asyncpg.UniqueViolationError:
        pass
    else:
        raise AssertionError("Expected UniqueViolationError for duplicate season-default row")

    # Tier-override row alongside the default → allowed.
    tier_args = (
        season_id, tier_id, 100.00, 1.00, None, 2, 0.75, 1.25, 3, 0.15, 48,
    )
    await pg_conn_migrated.execute(sql, *tier_args)
    # Second row for the same (season, tier) → rejected.
    try:
        await pg_conn_migrated.execute(sql, *tier_args)
    except asyncpg.UniqueViolationError:
        pass
    else:
        raise AssertionError("Expected UniqueViolationError for duplicate tier override row")


def _list_migrations():
    from tests.conftest import MIGRATIONS_DIR
    return sorted(MIGRATIONS_DIR.glob("*.sql"))
