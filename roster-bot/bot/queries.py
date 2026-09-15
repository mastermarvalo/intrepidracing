"""
DB read/write helpers for Team and TeamSlot objects.

All functions accept an open asyncpg.Connection so callers control transaction
boundaries. Use inside `async with db.connect() as conn:` (which already wraps
the work in a transaction).
"""

import json
from decimal import Decimal
from typing import Any, Sequence

import asyncpg

from bot.market import budget as budget_engine
from bot.market import results as results_engine
from bot.models import (
    BudgetEntry,
    Contract,
    ContractOffer,
    DeadMoneyEntry,
    Driver,
    GuildConfig,
    LeagueConfig,
    LedgerEntry,
    MarketBoard,
    Season,
    StatBoard,
    Team,
    TeamSlot,
    Tier,
    Trade,
    TradeItem,
)


def _row_to_slot(row: asyncpg.Record) -> TeamSlot:
    return TeamSlot(
        id=row["id"],
        team_id=row["team_id"],
        slot_role_id=row["slot_role_id"],
        label=row["label"],
        quantity=row["quantity"],
        slot_type=row["slot_type"],
        sort_order=row["sort_order"],
    )


def _row_to_team(row: asyncpg.Record, slots: list[TeamSlot]) -> Team:
    return Team(
        id=row["id"],
        guild_id=row["guild_id"],
        key=row["key"],
        name=row["name"],
        team_role_id=row["team_role_id"],
        channel_id=row["channel_id"],
        tagline=row["tagline"],
        logo_url=row["logo_url"],
        banner_url=row["banner_url"],
        principal_role_id=row["principal_role_id"],
        color=row["color"],
        message_id=row["message_id"],
        info_label=row["info_label"],
        info_body=row["info_body"],
        dark_mode=bool(row["dark_mode"]),
        slots=slots,
    )


async def fetch_team(
    conn: asyncpg.Connection, guild_id: int, key: str
) -> Team | None:
    row = await conn.fetchrow(
        "SELECT * FROM teams WHERE guild_id = $1 AND key = $2", guild_id, key
    )
    if row is None:
        return None
    slots = await fetch_slots(conn, row["id"])
    return _row_to_team(row, slots)


async def fetch_team_by_id(conn: asyncpg.Connection, team_id: int) -> Team | None:
    row = await conn.fetchrow("SELECT * FROM teams WHERE id = $1", team_id)
    if row is None:
        return None
    slots = await fetch_slots(conn, team_id)
    return _row_to_team(row, slots)


async def fetch_all_teams(conn: asyncpg.Connection, guild_id: int) -> list[Team]:
    rows = await conn.fetch(
        "SELECT * FROM teams WHERE guild_id = $1 ORDER BY key", guild_id
    )
    teams: list[Team] = []
    for row in rows:
        slots = await fetch_slots(conn, row["id"])
        teams.append(_row_to_team(row, slots))
    return teams


async def fetch_slots(conn: asyncpg.Connection, team_id: int) -> list[TeamSlot]:
    rows = await conn.fetch(
        "SELECT * FROM team_slots WHERE team_id = $1 ORDER BY sort_order", team_id
    )
    return [_row_to_slot(r) for r in rows]


async def insert_team(
    conn: asyncpg.Connection,
    guild_id: int,
    key: str,
    name: str,
    team_role_id: int,
    channel_id: int,
    tagline: str | None,
    logo_url: str | None,
    banner_url: str | None,
    principal_role_id: int | None,
    color: int | None = None,
    info_label: str | None = None,
    info_body: str | None = None,
    dark_mode: bool = False,
) -> int:
    """Insert a new team row and return its id."""
    return await conn.fetchval(
        """
        INSERT INTO teams
            (guild_id, key, name, team_role_id, channel_id, tagline, logo_url, banner_url,
             principal_role_id, color, info_label, info_body, dark_mode)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        RETURNING id
        """,
        guild_id, key, name, team_role_id, channel_id, tagline, logo_url, banner_url,
        principal_role_id, color, info_label, info_body, dark_mode,
    )


async def update_team(
    conn: asyncpg.Connection,
    team_id: int,
    name: str,
    team_role_id: int,
    channel_id: int,
    tagline: str | None,
    logo_url: str | None,
    banner_url: str | None,
    principal_role_id: int | None,
    color: int | None = None,
    info_label: str | None = None,
    info_body: str | None = None,
    dark_mode: bool = False,
) -> None:
    await conn.execute(
        """
        UPDATE teams
        SET name = $1, team_role_id = $2, channel_id = $3, tagline = $4, logo_url = $5,
            banner_url = $6, principal_role_id = $7, color = $8, info_label = $9,
            info_body = $10, dark_mode = $11
        WHERE id = $12
        """,
        name, team_role_id, channel_id, tagline, logo_url, banner_url,
        principal_role_id, color, info_label, info_body, dark_mode, team_id,
    )


async def set_message_id(
    conn: asyncpg.Connection, team_id: int, message_id: int | None
) -> None:
    await conn.execute(
        "UPDATE teams SET message_id = $1 WHERE id = $2", message_id, team_id
    )


async def replace_slots(
    conn: asyncpg.Connection,
    team_id: int,
    slots: Sequence[tuple[int, str, int, str, int]],
) -> None:
    """
    Replace all slots for a team in one shot.
    Each tuple: (slot_role_id, label, quantity, slot_type, sort_order).
    """
    await conn.execute("DELETE FROM team_slots WHERE team_id = $1", team_id)
    if slots:
        await conn.executemany(
            """
            INSERT INTO team_slots (team_id, slot_role_id, label, quantity, slot_type, sort_order)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            [(team_id, *s) for s in slots],
        )


async def delete_team(conn: asyncpg.Connection, team_id: int) -> None:
    await conn.execute("DELETE FROM teams WHERE id = $1", team_id)


# ── transaction log ───────────────────────────────────────────────────────────


async def log_transaction(
    conn: asyncpg.Connection,
    guild_id: int,
    team_id: int,
    member_id: int,
    member_name: str,
    action: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO transactions (guild_id, team_id, member_id, member_name, action)
        VALUES ($1, $2, $3, $4, $5)
        """,
        guild_id, team_id, member_id, member_name, action,
    )


async def fetch_transactions(
    conn: asyncpg.Connection,
    guild_id: int,
    team_id: int,
    limit: int = 20,
) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT member_name, action, created_at
        FROM transactions
        WHERE guild_id = $1 AND team_id = $2
        ORDER BY created_at DESC
        LIMIT $3
        """,
        guild_id, team_id, limit,
    )


# ── guild config ──────────────────────────────────────────────────────────────


async def fetch_guild_config(conn: asyncpg.Connection, guild_id: int) -> GuildConfig:
    row = await conn.fetchrow(
        "SELECT * FROM guild_config WHERE guild_id = $1", guild_id
    )
    if row is None:
        return GuildConfig(guild_id=guild_id)
    return GuildConfig(
        guild_id=row["guild_id"],
        free_agent_role_id=row["free_agent_role_id"],
        fa_channel_id=row["fa_channel_id"],
        fa_message_id=row["fa_message_id"],
        transactions_channel_id=row["transactions_channel_id"],
    )


async def upsert_guild_config(
    conn: asyncpg.Connection, guild_id: int, free_agent_role_id: int
) -> None:
    await conn.execute(
        """
        INSERT INTO guild_config (guild_id, free_agent_role_id) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE SET free_agent_role_id = excluded.free_agent_role_id
        """,
        guild_id, free_agent_role_id,
    )


async def upsert_fa_channel(
    conn: asyncpg.Connection, guild_id: int, fa_channel_id: int
) -> None:
    await conn.execute(
        """
        INSERT INTO guild_config (guild_id, fa_channel_id) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE SET fa_channel_id = excluded.fa_channel_id
        """,
        guild_id, fa_channel_id,
    )


async def set_fa_message_id(
    conn: asyncpg.Connection, guild_id: int, message_id: int | None
) -> None:
    await conn.execute(
        "UPDATE guild_config SET fa_message_id = $1 WHERE guild_id = $2",
        message_id, guild_id,
    )


async def upsert_transactions_channel(
    conn: asyncpg.Connection, guild_id: int, channel_id: int
) -> None:
    await conn.execute(
        """
        INSERT INTO guild_config (guild_id, transactions_channel_id) VALUES ($1, $2)
        ON CONFLICT (guild_id) DO UPDATE
            SET transactions_channel_id = excluded.transactions_channel_id
        """,
        guild_id, channel_id,
    )


# ── stat boards ───────────────────────────────────────────────────────────────


def _row_to_board(row: asyncpg.Record) -> StatBoard:
    return StatBoard(
        id=row["id"],
        guild_id=row["guild_id"],
        title=row["title"],
        sheet_id=row["sheet_id"],
        sheet_range=row["sheet_range"],
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        forum_thread_id=row["forum_thread_id"],
    )


async def fetch_all_stat_boards(conn: asyncpg.Connection, guild_id: int) -> list[StatBoard]:
    rows = await conn.fetch(
        "SELECT * FROM stat_boards WHERE guild_id = $1 ORDER BY title", guild_id
    )
    return [_row_to_board(r) for r in rows]


async def fetch_stat_board(
    conn: asyncpg.Connection, guild_id: int, title: str
) -> StatBoard | None:
    row = await conn.fetchrow(
        "SELECT * FROM stat_boards WHERE guild_id = $1 AND lower(title) = lower($2)",
        guild_id, title,
    )
    return _row_to_board(row) if row else None


async def fetch_stat_board_by_id(conn: asyncpg.Connection, board_id: int) -> StatBoard | None:
    row = await conn.fetchrow("SELECT * FROM stat_boards WHERE id = $1", board_id)
    return _row_to_board(row) if row else None


async def insert_stat_board(
    conn: asyncpg.Connection,
    guild_id: int,
    title: str,
    sheet_id: str,
    sheet_range: str,
    channel_id: int,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO stat_boards (guild_id, title, sheet_id, sheet_range, channel_id)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        guild_id, title, sheet_id, sheet_range, channel_id,
    )


async def set_stat_board_message_id(
    conn: asyncpg.Connection, board_id: int, message_id: int | None
) -> None:
    await conn.execute(
        "UPDATE stat_boards SET message_id = $1 WHERE id = $2", message_id, board_id
    )


async def set_stat_board_forum_thread_id(
    conn: asyncpg.Connection, board_id: int, forum_thread_id: int | None
) -> None:
    await conn.execute(
        "UPDATE stat_boards SET forum_thread_id = $1 WHERE id = $2", forum_thread_id, board_id
    )


async def fetch_forum_thread_for_channel(
    conn: asyncpg.Connection, guild_id: int, channel_id: int
) -> int | None:
    """Return the forum_thread_id used by any board in this channel, or None."""
    return await conn.fetchval(
        """
        SELECT forum_thread_id FROM stat_boards
        WHERE guild_id = $1 AND channel_id = $2 AND forum_thread_id IS NOT NULL
        LIMIT 1
        """,
        guild_id, channel_id,
    )


async def delete_stat_board(conn: asyncpg.Connection, board_id: int) -> None:
    await conn.execute("DELETE FROM stat_boards WHERE id = $1", board_id)


# ── seasons ───────────────────────────────────────────────────────────────────


def _row_to_season(row: asyncpg.Record) -> Season:
    return Season(
        id=row["id"],
        guild_id=row["guild_id"],
        name=row["name"],
        is_active=row["is_active"],
        created_at=row["created_at"],
    )


async def insert_season(
    conn: asyncpg.Connection, guild_id: int, name: str, is_active: bool = False
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO seasons (guild_id, name, is_active) VALUES ($1, $2, $3)
        RETURNING id
        """,
        guild_id, name, is_active,
    )


async def fetch_season_by_name(
    conn: asyncpg.Connection, guild_id: int, name: str
) -> Season | None:
    row = await conn.fetchrow(
        "SELECT * FROM seasons WHERE guild_id = $1 AND name = $2", guild_id, name
    )
    return _row_to_season(row) if row else None


async def fetch_season_by_id(conn: asyncpg.Connection, season_id: int) -> Season | None:
    row = await conn.fetchrow("SELECT * FROM seasons WHERE id = $1", season_id)
    return _row_to_season(row) if row else None


async def fetch_active_season(conn: asyncpg.Connection, guild_id: int) -> Season | None:
    row = await conn.fetchrow(
        "SELECT * FROM seasons WHERE guild_id = $1 AND is_active LIMIT 1", guild_id
    )
    return _row_to_season(row) if row else None


async def fetch_all_seasons(conn: asyncpg.Connection, guild_id: int) -> list[Season]:
    rows = await conn.fetch(
        "SELECT * FROM seasons WHERE guild_id = $1 ORDER BY created_at DESC", guild_id
    )
    return [_row_to_season(r) for r in rows]


async def activate_season(conn: asyncpg.Connection, season_id: int) -> None:
    """Make season_id the sole active season for its guild."""
    guild_id = await conn.fetchval("SELECT guild_id FROM seasons WHERE id = $1", season_id)
    if guild_id is None:
        raise ValueError(f"Season {season_id} does not exist")
    await conn.execute(
        "UPDATE seasons SET is_active = FALSE WHERE guild_id = $1 AND is_active", guild_id
    )
    await conn.execute("UPDATE seasons SET is_active = TRUE WHERE id = $1", season_id)


# ── tiers ─────────────────────────────────────────────────────────────────────


def _row_to_tier(row: asyncpg.Record) -> Tier:
    return Tier(
        id=row["id"],
        season_id=row["season_id"],
        code=row["code"],
        label=row["label"],
        rank_order=row["rank_order"],
        tier_role_id=row["tier_role_id"],
        accent_color=row["accent_color"],
    )


async def insert_tier(
    conn: asyncpg.Connection,
    season_id: int,
    code: str,
    label: str,
    rank_order: int,
    tier_role_id: int | None = None,
    accent_color: int | None = None,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO tiers (season_id, code, label, rank_order, tier_role_id, accent_color)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        season_id, code, label, rank_order, tier_role_id, accent_color,
    )


async def fetch_tier(
    conn: asyncpg.Connection, season_id: int, code: str
) -> Tier | None:
    row = await conn.fetchrow(
        "SELECT * FROM tiers WHERE season_id = $1 AND code = $2", season_id, code
    )
    return _row_to_tier(row) if row else None


async def fetch_tier_by_id(conn: asyncpg.Connection, tier_id: int) -> Tier | None:
    row = await conn.fetchrow("SELECT * FROM tiers WHERE id = $1", tier_id)
    return _row_to_tier(row) if row else None


async def fetch_all_tiers(conn: asyncpg.Connection, season_id: int) -> list[Tier]:
    rows = await conn.fetch(
        "SELECT * FROM tiers WHERE season_id = $1 ORDER BY rank_order", season_id
    )
    return [_row_to_tier(r) for r in rows]


async def update_tier(
    conn: asyncpg.Connection,
    tier_id: int,
    label: str,
    rank_order: int,
    tier_role_id: int | None,
    accent_color: int | None,
) -> None:
    await conn.execute(
        """
        UPDATE tiers SET label = $1, rank_order = $2, tier_role_id = $3, accent_color = $4
        WHERE id = $5
        """,
        label, rank_order, tier_role_id, accent_color, tier_id,
    )


# ── drivers ───────────────────────────────────────────────────────────────────


def _row_to_driver(row: asyncpg.Record) -> Driver:
    return Driver(
        id=row["id"],
        season_id=row["season_id"],
        tier_id=row["tier_id"],
        member_id=row["member_id"],
        display_name=row["display_name"],
        status=row["status"],
        created_at=row["created_at"],
    )


async def insert_driver(
    conn: asyncpg.Connection,
    season_id: int,
    tier_id: int,
    member_id: int,
    display_name: str,
    status: str,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO drivers (season_id, tier_id, member_id, display_name, status)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        season_id, tier_id, member_id, display_name, status,
    )


async def fetch_driver(
    conn: asyncpg.Connection, season_id: int, tier_id: int, member_id: int
) -> Driver | None:
    row = await conn.fetchrow(
        """
        SELECT * FROM drivers
        WHERE season_id = $1 AND tier_id = $2 AND member_id = $3
        """,
        season_id, tier_id, member_id,
    )
    return _row_to_driver(row) if row else None


async def fetch_drivers_in_tier(
    conn: asyncpg.Connection, tier_id: int
) -> list[Driver]:
    rows = await conn.fetch(
        "SELECT * FROM drivers WHERE tier_id = $1 ORDER BY display_name", tier_id
    )
    return [_row_to_driver(r) for r in rows]


async def fetch_driver_by_id(
    conn: asyncpg.Connection, driver_id: int
) -> Driver | None:
    row = await conn.fetchrow("SELECT * FROM drivers WHERE id = $1", driver_id)
    return _row_to_driver(row) if row else None


async def set_driver_status(
    conn: asyncpg.Connection, driver_id: int, status: str
) -> None:
    await conn.execute("UPDATE drivers SET status = $1 WHERE id = $2", status, driver_id)


# ── league config ─────────────────────────────────────────────────────────────


def _row_to_league_config(row: asyncpg.Record) -> LeagueConfig:
    return LeagueConfig(
        id=row["id"],
        season_id=row["season_id"],
        tier_id=row["tier_id"],
        salary_cap=row["salary_cap"],
        min_salary=row["min_salary"],
        max_salary=row["max_salary"],
        active_driver_slots=row["active_driver_slots"],
        weekly_move_cap=row["weekly_move_cap"],
        exceptional_move_cap=row["exceptional_move_cap"],
        min_term_seasons=row["min_term_seasons"],
        max_term_seasons=row["max_term_seasons"],
        max_incentive_pct=row["max_incentive_pct"],
        offer_ttl_hours=row["offer_ttl_hours"],
        free_agency_open=row["free_agency_open"],
        market_channel_id=row["market_channel_id"],
        transactions_channel_id=row["transactions_channel_id"],
        approvals_channel_id=row["approvals_channel_id"],
        commissioner_role_id=row["commissioner_role_id"],
    )


async def upsert_league_config(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int | None,
    salary_cap: Decimal,
    min_salary: Decimal,
    max_salary: Decimal | None,
    active_driver_slots: int,
    weekly_move_cap: Decimal,
    exceptional_move_cap: Decimal,
    min_term_seasons: int,
    max_term_seasons: int,
    max_incentive_pct: Decimal,
    offer_ttl_hours: int,
) -> int:
    """
    Insert or replace the league_config row for (season_id, tier_id).
    A tier_id of None writes the season-default row.
    """
    existing = await conn.fetchval(
        """
        SELECT id FROM league_config
        WHERE season_id = $1 AND tier_id IS NOT DISTINCT FROM $2
        """,
        season_id, tier_id,
    )
    if existing is None:
        return await conn.fetchval(
            """
            INSERT INTO league_config (
                season_id, tier_id, salary_cap, min_salary, max_salary,
                active_driver_slots, weekly_move_cap, exceptional_move_cap,
                min_term_seasons, max_term_seasons, max_incentive_pct,
                offer_ttl_hours
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            RETURNING id
            """,
            season_id, tier_id, salary_cap, min_salary, max_salary,
            active_driver_slots, weekly_move_cap, exceptional_move_cap,
            min_term_seasons, max_term_seasons, max_incentive_pct,
            offer_ttl_hours,
        )
    await conn.execute(
        """
        UPDATE league_config SET
            salary_cap = $1, min_salary = $2, max_salary = $3,
            active_driver_slots = $4, weekly_move_cap = $5,
            exceptional_move_cap = $6, min_term_seasons = $7,
            max_term_seasons = $8, max_incentive_pct = $9,
            offer_ttl_hours = $10
        WHERE id = $11
        """,
        salary_cap, min_salary, max_salary, active_driver_slots,
        weekly_move_cap, exceptional_move_cap, min_term_seasons,
        max_term_seasons, max_incentive_pct, offer_ttl_hours, existing,
    )
    return existing


async def fetch_league_config_row(
    conn: asyncpg.Connection, season_id: int, tier_id: int | None
) -> LeagueConfig | None:
    """
    Fetch the exact (season, tier) row. Does not fall back — use
    bot/market/config.py (Phase 2) for resolution with tier→season default.
    """
    row = await conn.fetchrow(
        """
        SELECT * FROM league_config
        WHERE season_id = $1 AND tier_id IS NOT DISTINCT FROM $2
        """,
        season_id, tier_id,
    )
    return _row_to_league_config(row) if row else None


async def set_league_config_channels(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int | None,
    market_channel_id: int | None = None,
    transactions_channel_id: int | None = None,
    approvals_channel_id: int | None = None,
    commissioner_role_id: int | None = None,
    free_agency_open: bool | None = None,
) -> None:
    """
    Update channel/role/flag fields on an existing league_config row.
    Only sets fields the caller passed in.
    """
    sets: list[str] = []
    args: list[object] = []
    if market_channel_id is not None:
        sets.append(f"market_channel_id = ${len(args) + 1}")
        args.append(market_channel_id)
    if transactions_channel_id is not None:
        sets.append(f"transactions_channel_id = ${len(args) + 1}")
        args.append(transactions_channel_id)
    if approvals_channel_id is not None:
        sets.append(f"approvals_channel_id = ${len(args) + 1}")
        args.append(approvals_channel_id)
    if commissioner_role_id is not None:
        sets.append(f"commissioner_role_id = ${len(args) + 1}")
        args.append(commissioner_role_id)
    if free_agency_open is not None:
        sets.append(f"free_agency_open = ${len(args) + 1}")
        args.append(free_agency_open)
    if not sets:
        return
    args.extend([season_id, tier_id])
    sql = (
        f"UPDATE league_config SET {', '.join(sets)} "
        f"WHERE season_id = ${len(args) - 1} "
        f"AND tier_id IS NOT DISTINCT FROM ${len(args)}"
    )
    await conn.execute(sql, *args)


# ── valuations (Phase 2) ──────────────────────────────────────────────────────
#
# Returns raw asyncpg.Records rather than typed dataclasses so that
# bot/queries.py stays free of bot/market/ imports. Callers in the cog
# layer adapt these rows into the engine's FactorWeight / DriverInput
# types.


async def fetch_valuation_factors(
    conn: asyncpg.Connection, season_id: int
) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT code, label, weight, max_contribution, sort_order
        FROM valuation_factors WHERE season_id = $1
        ORDER BY sort_order, code
        """,
        season_id,
    )


async def insert_valuation_run(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int,
    round_label: str,
    created_by: int | None,
    published: bool = False,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO valuation_runs
            (season_id, tier_id, round_label, created_by, published, published_at)
        VALUES ($1, $2, $3, $4, $5, CASE WHEN $5 THEN NOW() ELSE NULL END)
        RETURNING id
        """,
        season_id, tier_id, round_label, created_by, published,
    )


async def insert_driver_valuations(
    conn: asyncpg.Connection,
    run_id: int,
    rows: Sequence[dict[str, Any]],
) -> None:
    """
    Bulk-insert per-driver valuations for a run. Each row is a dict with:
      driver_id, market_value (Decimal), previous_value (Decimal|None),
      delta (Decimal), rank_in_tier (int), capped (bool),
      breakdown (list[dict] — JSON-serialisable).

    The `breakdown` list is serialised to a JSON string and cast to
    jsonb in the INSERT so we do not have to register a custom asyncpg
    codec.
    """
    if not rows:
        return
    await conn.executemany(
        """
        INSERT INTO driver_valuations
            (run_id, driver_id, market_value, previous_value, delta,
             rank_in_tier, capped, breakdown)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
        """,
        [
            (
                run_id,
                r["driver_id"],
                r["market_value"],
                r["previous_value"],
                r["delta"],
                r["rank_in_tier"],
                r["capped"],
                json.dumps(r["breakdown"]),
            )
            for r in rows
        ],
    )


async def fetch_valuation_run(
    conn: asyncpg.Connection, run_id: int
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT * FROM valuation_runs WHERE id = $1", run_id
    )


async def fetch_driver_valuations_for_run(
    conn: asyncpg.Connection, run_id: int
) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT dv.*, d.display_name
        FROM driver_valuations dv
        JOIN drivers d ON d.id = dv.driver_id
        WHERE dv.run_id = $1
        ORDER BY dv.rank_in_tier
        """,
        run_id,
    )


async def list_valuation_runs(
    conn: asyncpg.Connection,
    season_id: int,
    tier_id: int | None = None,
    limit: int = 20,
) -> list[asyncpg.Record]:
    """Recent runs for the season, or scoped to a tier. Newest first."""
    if tier_id is None:
        return list(
            await conn.fetch(
                """
                SELECT vr.id, vr.round_label, vr.published, vr.created_at,
                       t.code AS tier_code
                  FROM valuation_runs vr
                  JOIN tiers t ON t.id = vr.tier_id
                 WHERE vr.season_id = $1
                 ORDER BY vr.created_at DESC
                 LIMIT $2
                """,
                season_id, limit,
            )
        )
    return list(
        await conn.fetch(
            """
            SELECT vr.id, vr.round_label, vr.published, vr.created_at,
                   t.code AS tier_code
              FROM valuation_runs vr
              JOIN tiers t ON t.id = vr.tier_id
             WHERE vr.season_id = $1 AND vr.tier_id = $2
             ORDER BY vr.created_at DESC
             LIMIT $3
            """,
            season_id, tier_id, limit,
        )
    )


async def publish_valuation_run(conn: asyncpg.Connection, run_id: int) -> None:
    await conn.execute(
        """
        UPDATE valuation_runs
        SET published = TRUE, published_at = NOW()
        WHERE id = $1 AND NOT published
        """,
        run_id,
    )


async def fetch_latest_published_valuation(
    conn: asyncpg.Connection, driver_id: int
) -> Decimal | None:
    """Most recent published market_value for a driver, or None."""
    return await conn.fetchval(
        """
        SELECT dv.market_value
        FROM driver_valuations dv
        JOIN valuation_runs vr ON vr.id = dv.run_id
        WHERE dv.driver_id = $1 AND vr.published
        ORDER BY vr.published_at DESC NULLS LAST, vr.created_at DESC
        LIMIT 1
        """,
        driver_id,
    )


# ── market surfaces (Phase 3) ────────────────────────────────────────────────
#
# "Latest published run" per tier is the anchor for every market
# surface. Rather than repeat the CTE at every call site, this helper
# returns the id (or None if the tier has never had a published run).


async def fetch_latest_published_run_id(
    conn: asyncpg.Connection, tier_id: int
) -> int | None:
    return await conn.fetchval(
        """
        SELECT id FROM valuation_runs
        WHERE tier_id = $1 AND published
        ORDER BY published_at DESC NULLS LAST, created_at DESC
        LIMIT 1
        """,
        tier_id,
    )


async def fetch_market_table_for_tier(
    conn: asyncpg.Connection, tier_id: int
) -> list[asyncpg.Record]:
    """
    All drivers in the tier from the most recent published run,
    including their previous_value and delta so the render layer can
    show week-over-week movement without a second query.

    Returns rows sorted by rank_in_tier (ascending — 1 first).
    """
    run_id = await fetch_latest_published_run_id(conn, tier_id)
    if run_id is None:
        return []
    return await conn.fetch(
        """
        SELECT dv.driver_id,
               d.display_name,
               dv.market_value,
               dv.previous_value,
               dv.delta,
               dv.rank_in_tier,
               dv.capped
        FROM driver_valuations dv
        JOIN drivers d ON d.id = dv.driver_id
        WHERE dv.run_id = $1
        ORDER BY dv.rank_in_tier
        """,
        run_id,
    )


async def fetch_movers_for_tier(
    conn: asyncpg.Connection, tier_id: int, limit: int
) -> tuple[list[asyncpg.Record], list[asyncpg.Record]]:
    """
    Top `limit` risers and top `limit` fallers from the most recent
    published run. Returns (risers, fallers) — risers sorted by delta
    DESC, fallers by delta ASC. Empty lists if the tier has no
    published run yet or no non-zero deltas.
    """
    run_id = await fetch_latest_published_run_id(conn, tier_id)
    if run_id is None:
        return ([], [])
    risers = await conn.fetch(
        """
        SELECT dv.driver_id, d.display_name, dv.market_value,
               dv.previous_value, dv.delta, dv.capped
        FROM driver_valuations dv
        JOIN drivers d ON d.id = dv.driver_id
        WHERE dv.run_id = $1 AND dv.delta > 0
        ORDER BY dv.delta DESC, d.display_name
        LIMIT $2
        """,
        run_id, limit,
    )
    fallers = await conn.fetch(
        """
        SELECT dv.driver_id, d.display_name, dv.market_value,
               dv.previous_value, dv.delta, dv.capped
        FROM driver_valuations dv
        JOIN drivers d ON d.id = dv.driver_id
        WHERE dv.run_id = $1 AND dv.delta < 0
        ORDER BY dv.delta ASC, d.display_name
        LIMIT $2
        """,
        run_id, limit,
    )
    return (risers, fallers)


async def fetch_cross_tier_top(
    conn: asyncpg.Connection, season_id: int, per_tier: int
) -> list[asyncpg.Record]:
    """
    Top `per_tier` drivers from each tier in the season, from each
    tier's most recent published run. One row per driver, joined with
    tier metadata so the dashboard can group by tier without a second
    trip.
    """
    return await conn.fetch(
        """
        WITH latest AS (
            SELECT DISTINCT ON (tier_id) id AS run_id, tier_id
            FROM valuation_runs
            WHERE season_id = $1 AND published
            ORDER BY tier_id,
                     published_at DESC NULLS LAST,
                     created_at DESC
        ),
        ranked AS (
            SELECT dv.driver_id, d.display_name, dv.market_value,
                   dv.previous_value, dv.delta, dv.rank_in_tier,
                   dv.capped, l.tier_id
            FROM latest l
            JOIN driver_valuations dv ON dv.run_id = l.run_id
            JOIN drivers d ON d.id = dv.driver_id
            WHERE dv.rank_in_tier <= $2
        )
        SELECT r.*, t.code AS tier_code, t.label AS tier_label,
               t.rank_order AS tier_rank_order, t.accent_color
        FROM ranked r
        JOIN tiers t ON t.id = r.tier_id
        ORDER BY t.rank_order, r.rank_in_tier
        """,
        season_id, per_tier,
    )


async def fetch_driver_valuation_history(
    conn: asyncpg.Connection, driver_id: int, limit: int
) -> list[asyncpg.Record]:
    """
    Last `limit` PUBLISHED valuations for a driver, newest first.
    Useful for the driver-card trend line and the movers rationale.
    """
    return await conn.fetch(
        """
        SELECT vr.round_label, vr.published_at, vr.created_at,
               dv.market_value, dv.previous_value, dv.delta, dv.capped
        FROM driver_valuations dv
        JOIN valuation_runs vr ON vr.id = dv.run_id
        WHERE dv.driver_id = $1 AND vr.published
        ORDER BY vr.published_at DESC NULLS LAST, vr.created_at DESC
        LIMIT $2
        """,
        driver_id, limit,
    )


async def fetch_driver_by_member(
    conn: asyncpg.Connection, season_id: int, member_id: int
) -> Driver | None:
    """
    A member may have one drivers row per tier (see migration 003 note).
    This returns the first one found — used by `/market driver` when the
    caller passes a Discord mention rather than a tier-scoped identifier.
    """
    row = await conn.fetchrow(
        """
        SELECT * FROM drivers
        WHERE season_id = $1 AND member_id = $2
        ORDER BY id
        LIMIT 1
        """,
        season_id, member_id,
    )
    return _row_to_driver(row) if row else None


async def fetch_driver_by_display_name(
    conn: asyncpg.Connection, season_id: int, display_name: str
) -> Driver | None:
    row = await conn.fetchrow(
        """
        SELECT * FROM drivers
        WHERE season_id = $1 AND lower(display_name) = lower($2)
        ORDER BY id
        LIMIT 1
        """,
        season_id, display_name,
    )
    return _row_to_driver(row) if row else None


# ── market boards ────────────────────────────────────────────────────────────


def _row_to_market_board(row: asyncpg.Record) -> MarketBoard:
    return MarketBoard(
        id=row["id"],
        season_id=row["season_id"],
        tier_id=row["tier_id"],
        kind=row["kind"],
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        page=row["page"],
        forum_thread_id=row["forum_thread_id"],
        created_at=row["created_at"],
    )


async def insert_market_board(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int | None,
    kind: str,
    channel_id: int,
    page: int = 0,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO market_boards (season_id, tier_id, kind, channel_id, page)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        season_id, tier_id, kind, channel_id, page,
    )


async def set_market_board_message_id(
    conn: asyncpg.Connection, board_id: int, message_id: int | None
) -> None:
    await conn.execute(
        "UPDATE market_boards SET message_id = $1 WHERE id = $2",
        message_id, board_id,
    )


async def fetch_market_board_by_id(
    conn: asyncpg.Connection, board_id: int
) -> MarketBoard | None:
    row = await conn.fetchrow("SELECT * FROM market_boards WHERE id = $1", board_id)
    return _row_to_market_board(row) if row else None


async def fetch_market_boards_in_season(
    conn: asyncpg.Connection, season_id: int
) -> list[MarketBoard]:
    rows = await conn.fetch(
        "SELECT * FROM market_boards WHERE season_id = $1 ORDER BY created_at",
        season_id,
    )
    return [_row_to_market_board(r) for r in rows]


async def fetch_market_boards_for_tier(
    conn: asyncpg.Connection, season_id: int, tier_id: int | None
) -> list[MarketBoard]:
    """
    Boards scoped to a specific tier (or the cross-tier `NULL` bucket
    for dashboards). Used by the auto-refresh path after a publish.
    """
    rows = await conn.fetch(
        """
        SELECT * FROM market_boards
        WHERE season_id = $1 AND tier_id IS NOT DISTINCT FROM $2
        ORDER BY created_at
        """,
        season_id, tier_id,
    )
    return [_row_to_market_board(r) for r in rows]


async def delete_market_board(conn: asyncpg.Connection, board_id: int) -> None:
    await conn.execute("DELETE FROM market_boards WHERE id = $1", board_id)


# ── contracts (Phase 4) ──────────────────────────────────────────────────────


def _row_to_contract(row: asyncpg.Record) -> Contract:
    return Contract(
        id=row["id"],
        season_id=row["season_id"],
        tier_id=row["tier_id"],
        driver_id=row["driver_id"],
        team_id=row["team_id"],
        contract_value=row["contract_value"],
        signing_bonus=row["signing_bonus"],
        max_incentives=row["max_incentives"],
        term_seasons=row["term_seasons"],
        contract_type=row["contract_type"],
        state=row["state"],
        value_at_signing=row["value_at_signing"],
        signed_at=row["signed_at"],
        expires_after=row["expires_after"],
        voided_at=row["voided_at"],
        approved_by=row["approved_by"],
        external_ref=row["external_ref"],
        created_at=row["created_at"],
    )


async def insert_contract(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int,
    driver_id: int,
    team_id: int,
    contract_value: Decimal,
    signing_bonus: Decimal,
    max_incentives: Decimal,
    term_seasons: int,
    contract_type: str,
    state: str,
    value_at_signing: Decimal | None,
    approved_by: int | None,
    external_ref: str | None = None,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO contracts
            (season_id, tier_id, driver_id, team_id, contract_value,
             signing_bonus, max_incentives, term_seasons, contract_type,
             state, value_at_signing, signed_at, approved_by, external_ref)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                CASE WHEN $10 = 'active' THEN NOW() ELSE NULL END,
                $12, $13)
        RETURNING id
        """,
        season_id, tier_id, driver_id, team_id, contract_value,
        signing_bonus, max_incentives, term_seasons, contract_type,
        state, value_at_signing, approved_by, external_ref,
    )


async def fetch_contract_by_id(
    conn: asyncpg.Connection, contract_id: int
) -> Contract | None:
    row = await conn.fetchrow("SELECT * FROM contracts WHERE id = $1", contract_id)
    return _row_to_contract(row) if row else None


async def fetch_active_contract_for_driver(
    conn: asyncpg.Connection, driver_id: int
) -> Contract | None:
    row = await conn.fetchrow(
        "SELECT * FROM contracts WHERE driver_id = $1 AND state = 'active'",
        driver_id,
    )
    return _row_to_contract(row) if row else None


async def fetch_active_contracts_for_team(
    conn: asyncpg.Connection, team_id: int
) -> list[Contract]:
    rows = await conn.fetch(
        "SELECT * FROM contracts WHERE team_id = $1 AND state = 'active' "
        "ORDER BY contract_value DESC",
        team_id,
    )
    return [_row_to_contract(r) for r in rows]


async def fetch_contract_history_for_driver(
    conn: asyncpg.Connection, driver_id: int
) -> list[Contract]:
    rows = await conn.fetch(
        "SELECT * FROM contracts WHERE driver_id = $1 ORDER BY created_at DESC",
        driver_id,
    )
    return [_row_to_contract(r) for r in rows]


async def void_contract(
    conn: asyncpg.Connection, contract_id: int, actor_id: int | None
) -> None:
    await conn.execute(
        """
        UPDATE contracts
        SET state = 'voided', voided_at = NOW()
        WHERE id = $1 AND state = 'active'
        """,
        contract_id,
    )
    # actor_id is captured on the paired ledger entry by the caller.
    _ = actor_id


async def fetch_team_payroll(conn: asyncpg.Connection, team_id: int) -> Decimal:
    """
    Sum of contract_value across active contracts. Cap compliance is
    measured against this per CLAUDE.md §2 — the market value never
    enters this number.
    """
    value = await conn.fetchval(
        """
        SELECT COALESCE(SUM(contract_value), 0)
        FROM contracts WHERE team_id = $1 AND state = 'active'
        """,
        team_id,
    )
    return value if isinstance(value, Decimal) else Decimal(value)


async def fetch_team_active_slot_count(
    conn: asyncpg.Connection, team_id: int
) -> int:
    return await conn.fetchval(
        "SELECT COUNT(*) FROM contracts WHERE team_id = $1 AND state = 'active'",
        team_id,
    )


async def fetch_team_cap_sheet_rows(
    conn: asyncpg.Connection, team_id: int
) -> list[asyncpg.Record]:
    """
    Every active contract for the team joined with the driver's latest
    published market value. `market_value` is NULL if the driver has
    never been valued — the render layer surfaces that as "—".
    """
    return await conn.fetch(
        """
        SELECT c.id AS contract_id, c.driver_id, c.contract_value,
               c.signing_bonus, c.max_incentives, c.term_seasons,
               c.contract_type, c.signed_at, c.value_at_signing,
               d.display_name,
               (
                   SELECT dv.market_value
                   FROM driver_valuations dv
                   JOIN valuation_runs vr ON vr.id = dv.run_id
                   WHERE dv.driver_id = c.driver_id AND vr.published
                   ORDER BY vr.published_at DESC NULLS LAST, vr.created_at DESC
                   LIMIT 1
               ) AS market_value
        FROM contracts c
        JOIN drivers d ON d.id = c.driver_id
        WHERE c.team_id = $1 AND c.state = 'active'
        ORDER BY c.contract_value DESC
        """,
        team_id,
    )


async def fetch_tier_contracts_with_market(
    conn: asyncpg.Connection, tier_id: int
) -> list[asyncpg.Record]:
    """
    Every active contract for drivers in a tier, joined with team +
    market value. Used by /market surplus and /market underwater to
    compute P/L (market_value − contract_value) and rank drivers by it.
    """
    return await conn.fetch(
        """
        SELECT c.id AS contract_id, c.driver_id, c.team_id,
               c.contract_value, d.display_name, t.name AS team_name,
               t.color AS team_color,
               (
                   SELECT dv.market_value
                   FROM driver_valuations dv
                   JOIN valuation_runs vr ON vr.id = dv.run_id
                   WHERE dv.driver_id = c.driver_id AND vr.published
                   ORDER BY vr.published_at DESC NULLS LAST, vr.created_at DESC
                   LIMIT 1
               ) AS market_value
        FROM contracts c
        JOIN drivers d ON d.id = c.driver_id
        JOIN teams t ON t.id = c.team_id
        WHERE c.tier_id = $1 AND c.state = 'active'
        """,
        tier_id,
    )


# ── contract offers ──────────────────────────────────────────────────────────


def _row_to_offer(row: asyncpg.Record) -> ContractOffer:
    raw_validation = row["validation"]
    validation = (
        json.loads(raw_validation) if isinstance(raw_validation, str) else raw_validation
    )
    return ContractOffer(
        id=row["id"],
        season_id=row["season_id"],
        tier_id=row["tier_id"],
        driver_id=row["driver_id"],
        team_id=row["team_id"],
        offered_by=row["offered_by"],
        offer_kind=row["offer_kind"],
        salary=row["salary"],
        term_seasons=row["term_seasons"],
        contract_type=row["contract_type"],
        signing_bonus=row["signing_bonus"],
        incentives=row["incentives"],
        message=row["message"],
        state=row["state"],
        parent_offer_id=row["parent_offer_id"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
        resolved_by=row["resolved_by"],
        thread_id=row["thread_id"],
        validation=validation,
    )


# Aligned with the partial unique index in migration 008: `countered`
# is terminal for the parent (the child carries the negotiation
# forward), so it does not count as "open" for duplicate-check or
# "my open offers" purposes.
OPEN_OFFER_STATES = (
    "draft", "pending_driver", "pending_team",
    "accepted", "pending_approval",
)


async def insert_offer(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int,
    driver_id: int,
    team_id: int,
    offered_by: int,
    offer_kind: str,
    salary: Decimal,
    term_seasons: int,
    contract_type: str,
    state: str,
    expires_at,
    signing_bonus: Decimal = Decimal("0"),
    incentives: str | None = None,
    message: str | None = None,
    parent_offer_id: int | None = None,
    validation: dict | None = None,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO contract_offers
            (season_id, tier_id, driver_id, team_id, offered_by,
             offer_kind, salary, term_seasons, contract_type,
             signing_bonus, incentives, message, state,
             parent_offer_id, expires_at, validation)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                $13, $14, $15, $16::jsonb)
        RETURNING id
        """,
        season_id, tier_id, driver_id, team_id, offered_by,
        offer_kind, salary, term_seasons, contract_type,
        signing_bonus, incentives, message, state,
        parent_offer_id, expires_at, json.dumps(validation or {}),
    )


async def fetch_offer_by_id(
    conn: asyncpg.Connection, offer_id: int
) -> ContractOffer | None:
    row = await conn.fetchrow(
        "SELECT * FROM contract_offers WHERE id = $1", offer_id
    )
    return _row_to_offer(row) if row else None


async def update_offer_state(
    conn: asyncpg.Connection,
    offer_id: int,
    *,
    new_state: str,
    resolved_by: int | None,
    resolved_at,
) -> None:
    await conn.execute(
        """
        UPDATE contract_offers
        SET state = $1, resolved_by = $2, resolved_at = $3
        WHERE id = $4
        """,
        new_state, resolved_by, resolved_at, offer_id,
    )


async def set_offer_thread_id(
    conn: asyncpg.Connection, offer_id: int, thread_id: int | None
) -> None:
    await conn.execute(
        "UPDATE contract_offers SET thread_id = $1 WHERE id = $2",
        thread_id, offer_id,
    )


async def fetch_open_offer_for_team_driver(
    conn: asyncpg.Connection, team_id: int, driver_id: int
) -> ContractOffer | None:
    row = await conn.fetchrow(
        f"""
        SELECT * FROM contract_offers
        WHERE team_id = $1 AND driver_id = $2
          AND state IN ({','.join('$' + str(i + 3) for i in range(len(OPEN_OFFER_STATES)))})
        LIMIT 1
        """,
        team_id, driver_id, *OPEN_OFFER_STATES,
    )
    return _row_to_offer(row) if row else None


async def fetch_open_offers_for_team(
    conn: asyncpg.Connection, team_id: int
) -> list[ContractOffer]:
    rows = await conn.fetch(
        f"""
        SELECT * FROM contract_offers
        WHERE team_id = $1
          AND state IN ({','.join('$' + str(i + 2) for i in range(len(OPEN_OFFER_STATES)))})
        ORDER BY created_at DESC
        """,
        team_id, *OPEN_OFFER_STATES,
    )
    return [_row_to_offer(r) for r in rows]


async def fetch_open_offers_for_driver(
    conn: asyncpg.Connection, driver_id: int
) -> list[ContractOffer]:
    rows = await conn.fetch(
        f"""
        SELECT * FROM contract_offers
        WHERE driver_id = $1
          AND state IN ({','.join('$' + str(i + 2) for i in range(len(OPEN_OFFER_STATES)))})
        ORDER BY created_at DESC
        """,
        driver_id, *OPEN_OFFER_STATES,
    )
    return [_row_to_offer(r) for r in rows]


async def fetch_expired_open_offers(
    conn: asyncpg.Connection, now
) -> list[ContractOffer]:
    rows = await conn.fetch(
        f"""
        SELECT * FROM contract_offers
        WHERE expires_at <= $1
          AND state IN ({','.join('$' + str(i + 2) for i in range(len(OPEN_OFFER_STATES)))})
        """,
        now, *OPEN_OFFER_STATES,
    )
    return [_row_to_offer(r) for r in rows]


# ── contract ledger (append-only) ────────────────────────────────────────────


def _row_to_ledger(row: asyncpg.Record) -> LedgerEntry:
    raw_detail = row["detail"]
    detail = (
        json.loads(raw_detail) if isinstance(raw_detail, str) else raw_detail
    )
    return LedgerEntry(
        id=row["id"],
        season_id=row["season_id"],
        tier_id=row["tier_id"],
        driver_id=row["driver_id"],
        team_id=row["team_id"],
        contract_id=row["contract_id"],
        offer_id=row["offer_id"],
        kind=row["kind"],
        amount=row["amount"],
        detail=detail,
        actor_id=row["actor_id"],
        created_at=row["created_at"],
    )


async def append_ledger(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int,
    kind: str,
    detail: dict,
    driver_id: int | None = None,
    team_id: int | None = None,
    contract_id: int | None = None,
    offer_id: int | None = None,
    amount: Decimal | None = None,
    actor_id: int | None = None,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO contract_ledger
            (season_id, tier_id, driver_id, team_id, contract_id,
             offer_id, kind, amount, detail, actor_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
        RETURNING id
        """,
        season_id, tier_id, driver_id, team_id, contract_id, offer_id,
        kind, amount, json.dumps(detail), actor_id,
    )


async def fetch_ledger_for_driver(
    conn: asyncpg.Connection, driver_id: int, limit: int
) -> list[LedgerEntry]:
    rows = await conn.fetch(
        """
        SELECT * FROM contract_ledger WHERE driver_id = $1
        ORDER BY created_at DESC LIMIT $2
        """,
        driver_id, limit,
    )
    return [_row_to_ledger(r) for r in rows]


async def fetch_ledger_for_team(
    conn: asyncpg.Connection, team_id: int, limit: int
) -> list[LedgerEntry]:
    rows = await conn.fetch(
        """
        SELECT * FROM contract_ledger WHERE team_id = $1
        ORDER BY created_at DESC LIMIT $2
        """,
        team_id, limit,
    )
    return [_row_to_ledger(r) for r in rows]


async def fetch_ledger_for_contract(
    conn: asyncpg.Connection, contract_id: int
) -> list[LedgerEntry]:
    rows = await conn.fetch(
        """
        SELECT * FROM contract_ledger WHERE contract_id = $1
        ORDER BY created_at ASC
        """,
        contract_id,
    )
    return [_row_to_ledger(r) for r in rows]


# ── contract lifecycle helpers (Phase 5) ─────────────────────────────────────


async def update_contract_terms(
    conn: asyncpg.Connection,
    contract_id: int,
    *,
    contract_value: Decimal,
    term_seasons: int,
    signing_bonus: Decimal,
) -> None:
    """
    Extension path: overwrite the money terms on an ACTIVE contract in
    place. The contracts table has no dedicated "history of terms"
    column, but every change is captured in the ledger (kind =
    'contract_extended'), so the audit trail is intact even without
    versioning the row.
    """
    await conn.execute(
        """
        UPDATE contracts
        SET contract_value = $1, term_seasons = $2, signing_bonus = $3
        WHERE id = $4 AND state = 'active'
        """,
        contract_value, term_seasons, signing_bonus, contract_id,
    )


async def terminate_contract(
    conn: asyncpg.Connection, contract_id: int, *, state: str = "terminated"
) -> None:
    """
    Release path: flip an ACTIVE contract's state to `terminated`
    (or `expired`) and stamp voided_at. The caller writes the ledger
    entry that captures why.
    """
    await conn.execute(
        """
        UPDATE contracts
        SET state = $1, voided_at = NOW()
        WHERE id = $2 AND state = 'active'
        """,
        state, contract_id,
    )


async def transfer_contract(
    conn: asyncpg.Connection,
    contract_id: int,
    *,
    new_team_id: int,
    new_tier_id: int | None = None,
) -> None:
    """
    Trade path: move the contract to a different team without
    touching contract_value (CLAUDE.md §2 rule 6). Optionally moves
    tier as well (promote/relegate uses this same helper).
    """
    if new_tier_id is None:
        await conn.execute(
            "UPDATE contracts SET team_id = $1 WHERE id = $2 AND state = 'active'",
            new_team_id, contract_id,
        )
    else:
        await conn.execute(
            """
            UPDATE contracts SET team_id = $1, tier_id = $2
            WHERE id = $3 AND state = 'active'
            """,
            new_team_id, new_tier_id, contract_id,
        )


async def set_driver_tier(
    conn: asyncpg.Connection, driver_id: int, tier_id: int
) -> None:
    await conn.execute(
        "UPDATE drivers SET tier_id = $1 WHERE id = $2", tier_id, driver_id
    )


# ── trades (Phase 5) ─────────────────────────────────────────────────────────


OPEN_TRADE_STATES = ("draft", "pending_other", "accepted", "pending_approval")


def _row_to_trade(row: asyncpg.Record) -> Trade:
    return Trade(
        id=row["id"],
        season_id=row["season_id"],
        proposing_team_id=row["proposing_team_id"],
        other_team_id=row["other_team_id"],
        proposed_by=row["proposed_by"],
        state=row["state"],
        message=row["message"],
        expires_at=row["expires_at"],
        resolved_at=row["resolved_at"],
        resolved_by=row["resolved_by"],
        thread_id=row["thread_id"],
        approved_ref=row["approved_ref"],
        created_at=row["created_at"],
    )


async def insert_trade(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    proposing_team_id: int,
    other_team_id: int,
    proposed_by: int,
    state: str,
    message: str | None,
    expires_at,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO trades
            (season_id, proposing_team_id, other_team_id, proposed_by,
             state, message, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        season_id, proposing_team_id, other_team_id, proposed_by,
        state, message, expires_at,
    )


async def insert_trade_item(
    conn: asyncpg.Connection,
    trade_id: int,
    *,
    from_team_id: int,
    contract_id: int,
) -> None:
    await conn.execute(
        """
        INSERT INTO trade_items (trade_id, from_team_id, contract_id)
        VALUES ($1, $2, $3)
        """,
        trade_id, from_team_id, contract_id,
    )


async def fetch_trade_by_id(
    conn: asyncpg.Connection, trade_id: int
) -> Trade | None:
    row = await conn.fetchrow("SELECT * FROM trades WHERE id = $1", trade_id)
    return _row_to_trade(row) if row else None


async def fetch_trade_items(
    conn: asyncpg.Connection, trade_id: int
) -> list[TradeItem]:
    rows = await conn.fetch(
        "SELECT * FROM trade_items WHERE trade_id = $1 ORDER BY from_team_id",
        trade_id,
    )
    return [
        TradeItem(
            trade_id=r["trade_id"],
            from_team_id=r["from_team_id"],
            contract_id=r["contract_id"],
        )
        for r in rows
    ]


async def update_trade_state(
    conn: asyncpg.Connection,
    trade_id: int,
    *,
    new_state: str,
    resolved_by: int | None,
    resolved_at,
    approved_ref: str | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE trades
        SET state = $1, resolved_by = $2, resolved_at = $3,
            approved_ref = COALESCE($4, approved_ref)
        WHERE id = $5
        """,
        new_state, resolved_by, resolved_at, approved_ref, trade_id,
    )


async def set_trade_thread_id(
    conn: asyncpg.Connection, trade_id: int, thread_id: int | None
) -> None:
    await conn.execute(
        "UPDATE trades SET thread_id = $1 WHERE id = $2",
        thread_id, trade_id,
    )


async def fetch_open_trades_for_team(
    conn: asyncpg.Connection, team_id: int
) -> list[Trade]:
    rows = await conn.fetch(
        f"""
        SELECT * FROM trades
        WHERE (proposing_team_id = $1 OR other_team_id = $1)
          AND state IN ({','.join('$' + str(i + 2) for i in range(len(OPEN_TRADE_STATES)))})
        ORDER BY created_at DESC
        """,
        team_id, *OPEN_TRADE_STATES,
    )
    return [_row_to_trade(r) for r in rows]


async def fetch_expired_open_trades(
    conn: asyncpg.Connection, now
) -> list[Trade]:
    rows = await conn.fetch(
        f"""
        SELECT * FROM trades
        WHERE expires_at <= $1
          AND state IN ({','.join('$' + str(i + 2) for i in range(len(OPEN_TRADE_STATES)))})
        """,
        now, *OPEN_TRADE_STATES,
    )
    return [_row_to_trade(r) for r in rows]


async def fetch_trade_involves_contract(
    conn: asyncpg.Connection, contract_id: int
) -> Trade | None:
    """
    Any open trade that references this contract. Used before allowing
    a release/buyout/extend to avoid clobbering an in-flight trade.
    """
    row = await conn.fetchrow(
        f"""
        SELECT t.* FROM trades t
        JOIN trade_items ti ON ti.trade_id = t.id
        WHERE ti.contract_id = $1
          AND t.state IN ({','.join('$' + str(i + 2) for i in range(len(OPEN_TRADE_STATES)))})
        LIMIT 1
        """,
        contract_id, *OPEN_TRADE_STATES,
    )
    return _row_to_trade(row) if row else None


# ── dead money (Phase 5) ─────────────────────────────────────────────────────


def _row_to_dead_money(row: asyncpg.Record) -> DeadMoneyEntry:
    return DeadMoneyEntry(
        id=row["id"],
        season_id=row["season_id"],
        tier_id=row["tier_id"],
        team_id=row["team_id"],
        amount=row["amount"],
        source_contract_id=row["source_contract_id"],
        note=row["note"],
        actor_id=row["actor_id"],
        created_at=row["created_at"],
    )


async def insert_dead_money(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int,
    team_id: int,
    amount: Decimal,
    source_contract_id: int | None,
    note: str | None,
    actor_id: int | None,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO dead_money
            (season_id, tier_id, team_id, amount, source_contract_id, note, actor_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        season_id, tier_id, team_id, amount, source_contract_id, note, actor_id,
    )


async def fetch_dead_money_for_team(
    conn: asyncpg.Connection, team_id: int, season_id: int
) -> list[DeadMoneyEntry]:
    rows = await conn.fetch(
        """
        SELECT * FROM dead_money
        WHERE team_id = $1 AND season_id = $2
        ORDER BY created_at DESC
        """,
        team_id, season_id,
    )
    return [_row_to_dead_money(r) for r in rows]


async def fetch_dead_money_total(
    conn: asyncpg.Connection, team_id: int, season_id: int
) -> Decimal:
    value = await conn.fetchval(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM dead_money WHERE team_id = $1 AND season_id = $2
        """,
        team_id, season_id,
    )
    return value if isinstance(value, Decimal) else Decimal(value)


async def fetch_team_effective_payroll(
    conn: asyncpg.Connection, team_id: int, season_id: int
) -> Decimal:
    """
    Payroll (sum of active contract_value) + dead_money for the given
    season. THIS is the number cap enforcement measures against — the
    cap-headroom rule in bot/contracts/rules.py takes this as
    `team_payroll_before`.
    """
    payroll = await fetch_team_payroll(conn, team_id)
    dead = await fetch_dead_money_total(conn, team_id, season_id)
    return payroll + dead


# ── Race results & normalization (Phase 6) ──────────────────────────────


def _row_to_position_score(row: asyncpg.Record) -> results_engine.PositionScore:
    return results_engine.PositionScore(
        position=row["position"],
        race_score=row["race_score"],
        quali_score=row["quali_score"],
        points=row["points"],
        is_win=row["is_win"],
        is_podium=row["is_podium"],
        is_pole=row["is_pole"],
    )


def _row_to_round_result(row: asyncpg.Record) -> results_engine.RoundResult:
    return results_engine.RoundResult(
        round_order=row["round_order"],
        driver_id=row["driver_id"],
        finish_position=row["finish_position"],
        grid_position=row["grid_position"],
        dnf=row["dnf"],
        dns=row["dns"],
        fastest_lap=row["fastest_lap"],
        driver_of_day=row["driver_of_day"],
        incident_points=row["incident_points"],
    )


async def fetch_position_scores(
    conn: asyncpg.Connection, season_id: int
) -> list[results_engine.PositionScore]:
    """The season's normalization curve, ordered by position."""
    rows = await conn.fetch(
        "SELECT * FROM position_scores WHERE season_id = $1 ORDER BY position",
        season_id,
    )
    return [_row_to_position_score(r) for r in rows]


async def fetch_results_tuning(
    conn: asyncpg.Connection, season_id: int, tier_id: int | None
) -> results_engine.ResultsTuning | None:
    """
    Resolved results_config for (season, tier): the tier override if one
    exists, otherwise the season default. Mirrors the league_config
    resolution pattern.
    """
    row = None
    if tier_id is not None:
        row = await conn.fetchrow(
            "SELECT * FROM results_config WHERE season_id = $1 AND tier_id = $2",
            season_id,
            tier_id,
        )
    if row is None:
        row = await conn.fetchrow(
            "SELECT * FROM results_config WHERE season_id = $1 AND tier_id IS NULL",
            season_id,
        )
    if row is None:
        return None
    return results_engine.ResultsTuning(
        form_window_rounds=row["form_window_rounds"],
        consistency_window_rounds=row["consistency_window_rounds"],
        max_incident_points=row["max_incident_points"],
    )


async def fetch_race_round(
    conn: asyncpg.Connection, season_id: int, tier_id: int, round_label: str
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT * FROM race_rounds
         WHERE season_id = $1 AND tier_id = $2 AND round_label = $3
        """,
        season_id,
        tier_id,
        round_label,
    )


async def fetch_race_round_by_id(
    conn: asyncpg.Connection, round_id: int
) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT * FROM race_rounds WHERE id = $1", round_id)


async def next_round_order(
    conn: asyncpg.Connection, season_id: int, tier_id: int
) -> int:
    """
    The next round_order for a tier. Rounds are ordered per tier because
    tiers race their own calendars.
    """
    row = await conn.fetchrow(
        """
        SELECT COALESCE(MAX(round_order), 0) + 1 AS next
          FROM race_rounds WHERE season_id = $1 AND tier_id = $2
        """,
        season_id,
        tier_id,
    )
    return row["next"]


async def upsert_race_round(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int,
    round_label: str,
    round_order: int | None = None,
    held_on: Any = None,
    imported_by: int | None = None,
    source: str | None = None,
) -> asyncpg.Record:
    """
    Create the round, or return the existing one for this label so a
    re-import after a stewards' decision updates facts in place rather
    than creating a duplicate round.
    """
    existing = await fetch_race_round(conn, season_id, tier_id, round_label)
    if existing is not None:
        await conn.execute(
            """
            UPDATE race_rounds
               SET imported_at = NOW(), imported_by = $2, source = $3
             WHERE id = $1
            """,
            existing["id"],
            imported_by,
            source,
        )
        return await fetch_race_round_by_id(conn, existing["id"])  # type: ignore[return-value]

    order = (
        round_order
        if round_order is not None
        else await next_round_order(conn, season_id, tier_id)
    )
    return await conn.fetchrow(
        """
        INSERT INTO race_rounds
            (season_id, tier_id, round_label, round_order, held_on,
             imported_by, source)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING *
        """,
        season_id,
        tier_id,
        round_label,
        order,
        held_on,
        imported_by,
        source,
    )


async def upsert_race_results(
    conn: asyncpg.Connection,
    *,
    round_id: int,
    rows: Sequence[dict[str, Any]],
) -> int:
    """
    Write one round's results. Re-importing the same round overwrites
    each driver's facts — results are facts, not money, so correcting
    them is not a ledger event.

    Returns the number of rows written.
    """
    if not rows:
        return 0
    await conn.executemany(
        """
        INSERT INTO race_results
            (round_id, driver_id, finish_position, grid_position, dnf, dns,
             fastest_lap, driver_of_day, incident_points, note)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (round_id, driver_id) DO UPDATE SET
            finish_position = EXCLUDED.finish_position,
            grid_position   = EXCLUDED.grid_position,
            dnf             = EXCLUDED.dnf,
            dns             = EXCLUDED.dns,
            fastest_lap     = EXCLUDED.fastest_lap,
            driver_of_day   = EXCLUDED.driver_of_day,
            incident_points = EXCLUDED.incident_points,
            note            = EXCLUDED.note
        """,
        [
            (
                round_id,
                r["driver_id"],
                r.get("finish_position"),
                r.get("grid_position"),
                r.get("dnf", False),
                r.get("dns", False),
                r.get("fastest_lap", False),
                r.get("driver_of_day", False),
                r.get("incident_points", Decimal(0)),
                r.get("note"),
            )
            for r in rows
        ],
    )
    return len(rows)


async def fetch_results_for_round(
    conn: asyncpg.Connection, round_id: int
) -> list[results_engine.RoundResult]:
    rows = await conn.fetch(
        """
        SELECT rr.*, r.round_order
          FROM race_results rr
          JOIN race_rounds r ON r.id = rr.round_id
         WHERE rr.round_id = $1
         ORDER BY rr.driver_id
        """,
        round_id,
    )
    return [_row_to_round_result(r) for r in rows]


async def fetch_results_history(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int,
    through_round_order: int,
) -> dict[int, list[results_engine.RoundResult]]:
    """
    Every result in this tier up to and including `through_round_order`,
    grouped by driver and ordered oldest-first.

    Bounded by round_order rather than "everything" so that re-running a
    mid-season round reproduces exactly the form and consistency values
    it originally saw — a later round must never leak backwards into an
    earlier valuation.
    """
    rows = await conn.fetch(
        """
        SELECT rr.*, r.round_order
          FROM race_results rr
          JOIN race_rounds r ON r.id = rr.round_id
         WHERE r.season_id = $1 AND r.tier_id = $2 AND r.round_order <= $3
         ORDER BY rr.driver_id, r.round_order
        """,
        season_id,
        tier_id,
        through_round_order,
    )
    grouped: dict[int, list[results_engine.RoundResult]] = {}
    for row in rows:
        grouped.setdefault(row["driver_id"], []).append(_row_to_round_result(row))
    return grouped


async def list_race_rounds(
    conn: asyncpg.Connection, season_id: int, tier_id: int | None = None
) -> list[asyncpg.Record]:
    if tier_id is None:
        return list(
            await conn.fetch(
                """
                SELECT r.*, t.code AS tier_code,
                       (SELECT COUNT(*) FROM race_results x WHERE x.round_id = r.id)
                           AS result_count
                  FROM race_rounds r
                  JOIN tiers t ON t.id = r.tier_id
                 WHERE r.season_id = $1
                 ORDER BY t.rank_order, r.round_order
                """,
                season_id,
            )
        )
    return list(
        await conn.fetch(
            """
            SELECT r.*, t.code AS tier_code,
                   (SELECT COUNT(*) FROM race_results x WHERE x.round_id = r.id)
                       AS result_count
              FROM race_rounds r
              JOIN tiers t ON t.id = r.tier_id
             WHERE r.season_id = $1 AND r.tier_id = $2
             ORDER BY r.round_order
            """,
            season_id,
            tier_id,
        )
    )


async def set_valuation_run_round(
    conn: asyncpg.Connection, run_id: int, round_id: int | None
) -> None:
    await conn.execute(
        "UPDATE valuation_runs SET round_id = $2 WHERE id = $1", run_id, round_id
    )


# ── Guided-panel status reads ────────────────────────────────────────
# Small aggregate reads backing the /league home panel. They exist here
# rather than in the panel cog because the queries layer is the only
# place SQL lives.


async def fetch_latest_unpublished_run_id(
    conn: asyncpg.Connection, season_id: int, tier_id: int
) -> int | None:
    """Most recent dry-run awaiting publication for a tier, if any."""
    return await conn.fetchval(
        """
        SELECT id
          FROM valuation_runs
         WHERE season_id = $1
           AND tier_id   = $2
           AND published = FALSE
         ORDER BY created_at DESC
         LIMIT 1
        """,
        season_id,
        tier_id,
    )


async def tier_has_published_valuation(conn: asyncpg.Connection, tier_id: int) -> bool:
    """Whether a tier has ever published a run (i.e. has a live market)."""
    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM valuation_runs
                 WHERE tier_id = $1 AND published = TRUE
            )
            """,
            tier_id,
        )
    )


async def count_offers_awaiting_approval(conn: asyncpg.Connection, season_id: int) -> int:
    """Contract offers sitting in the commissioner's queue."""
    return (
        await conn.fetchval(
            """
            SELECT COUNT(*) FROM contract_offers
             WHERE season_id = $1 AND state = 'pending_approval'
            """,
            season_id,
        )
        or 0
    )


async def count_trades_awaiting_approval(conn: asyncpg.Connection, season_id: int) -> int:
    """Trades sitting in the commissioner's queue."""
    return (
        await conn.fetchval(
            """
            SELECT COUNT(*) FROM trades
             WHERE season_id = $1 AND state = 'pending_approval'
            """,
            season_id,
        )
        or 0
    )


# ── Approval queue listings (control panel) ──────────────────────────────
#
# The counts in `count_*_awaiting_approval` drive the panel's badge; these
# two return enough detail to render an actionable queue, so a
# commissioner never has to go hunt for ids.


async def fetch_offers_awaiting_approval(
    conn: asyncpg.Connection, season_id: int, limit: int
) -> list[asyncpg.Record]:
    """
    Offers in `pending_approval`, oldest first, with display names joined.

    Oldest first because the queue is worked front to back — the offer
    that has been waiting longest is the one holding up a signing.
    """
    return await conn.fetch(
        """
        SELECT o.id,
               o.salary,
               o.term_seasons,
               o.contract_type,
               o.signing_bonus,
               o.offer_kind,
               o.created_at,
               d.display_name AS driver_name,
               t.name         AS team_name,
               ti.code        AS tier_code
          FROM contract_offers o
          JOIN drivers d ON d.id = o.driver_id
          JOIN teams   t ON t.id = o.team_id
          JOIN tiers  ti ON ti.id = o.tier_id
         WHERE o.season_id = $1
           AND o.state = 'pending_approval'
         ORDER BY o.created_at ASC
         LIMIT $2
        """,
        season_id,
        limit,
    )


async def fetch_trades_awaiting_approval(
    conn: asyncpg.Connection, season_id: int, limit: int
) -> list[asyncpg.Record]:
    """Trades in `pending_approval`, oldest first, with team names and size."""
    return await conn.fetch(
        """
        SELECT tr.id,
               tr.created_at,
               pt.name AS proposing_team_name,
               ot.name AS other_team_name,
               (SELECT COUNT(*) FROM trade_items i WHERE i.trade_id = tr.id)
                   AS item_count
          FROM trades tr
          JOIN teams pt ON pt.id = tr.proposing_team_id
          JOIN teams ot ON ot.id = tr.other_team_id
         WHERE tr.season_id = $1
           AND tr.state = 'pending_approval'
         ORDER BY tr.created_at ASC
         LIMIT $2
        """,
        season_id,
        limit,
    )


# ── Team budgets (Phase 7) ───────────────────────────────────────────────
# The budget is a per-team, per-season BALANCE distinct from the
# league-wide spending cap. There is no stored balance column: balance
# is SUM(amount) over `team_budget_ledger`, so every dollar is explained
# by an append-only row. See migrations/013_team_budgets.sql.


def _row_to_budget_config(row: asyncpg.Record) -> budget_engine.BudgetConfig:
    return budget_engine.BudgetConfig(
        id=row["id"],
        season_id=row["season_id"],
        tier_id=row["tier_id"],
        enforce_budget=row["enforce_budget"],
        rollover_enabled=row["rollover_enabled"],
        opening_budget=row["opening_budget"],
        earnings_per_point=row["earnings_per_point"],
        dnf_penalty=row["dnf_penalty"],
        dns_penalty=row["dns_penalty"],
        penalty_per_incident_pt=row["penalty_per_incident_pt"],
    )


async def fetch_budget_config(
    conn: asyncpg.Connection, season_id: int, tier_id: int | None
) -> budget_engine.BudgetConfig | None:
    """
    Resolved budget_config for (season, tier): the tier override if one
    exists, otherwise the season default. None means budgets are not
    configured for the season — callers treat that as "not enforced".
    """
    row = None
    if tier_id is not None:
        row = await conn.fetchrow(
            "SELECT * FROM budget_config WHERE season_id = $1 AND tier_id = $2",
            season_id, tier_id,
        )
    if row is None:
        row = await conn.fetchrow(
            "SELECT * FROM budget_config WHERE season_id = $1 AND tier_id IS NULL",
            season_id,
        )
    return _row_to_budget_config(row) if row else None


async def fetch_budget_config_exact(
    conn: asyncpg.Connection, season_id: int, tier_id: int | None
) -> budget_engine.BudgetConfig | None:
    """The row for exactly this scope, with no fallback — for editing."""
    if tier_id is None:
        row = await conn.fetchrow(
            "SELECT * FROM budget_config WHERE season_id = $1 AND tier_id IS NULL",
            season_id,
        )
    else:
        row = await conn.fetchrow(
            "SELECT * FROM budget_config WHERE season_id = $1 AND tier_id = $2",
            season_id, tier_id,
        )
    return _row_to_budget_config(row) if row else None


async def upsert_budget_config(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int | None,
    enforce_budget: bool,
    rollover_enabled: bool,
    opening_budget: Decimal,
    earnings_per_point: Decimal,
    dnf_penalty: Decimal,
    dns_penalty: Decimal,
    penalty_per_incident_pt: Decimal,
) -> budget_engine.BudgetConfig:
    existing = await fetch_budget_config_exact(conn, season_id, tier_id)
    if existing is None:
        row = await conn.fetchrow(
            """
            INSERT INTO budget_config
                (season_id, tier_id, enforce_budget, rollover_enabled,
                 opening_budget, earnings_per_point, dnf_penalty, dns_penalty,
                 penalty_per_incident_pt)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *
            """,
            season_id, tier_id, enforce_budget, rollover_enabled,
            opening_budget, earnings_per_point, dnf_penalty, dns_penalty,
            penalty_per_incident_pt,
        )
    else:
        row = await conn.fetchrow(
            """
            UPDATE budget_config
               SET enforce_budget = $2, rollover_enabled = $3,
                   opening_budget = $4, earnings_per_point = $5,
                   dnf_penalty = $6, dns_penalty = $7,
                   penalty_per_incident_pt = $8
             WHERE id = $1
            RETURNING *
            """,
            existing.id, enforce_budget, rollover_enabled,
            opening_budget, earnings_per_point, dnf_penalty, dns_penalty,
            penalty_per_incident_pt,
        )
    return _row_to_budget_config(row)


def _row_to_budget_entry(row: asyncpg.Record) -> BudgetEntry:
    detail = row["detail"]
    if isinstance(detail, str):
        detail = json.loads(detail)
    return BudgetEntry(
        id=row["id"],
        season_id=row["season_id"],
        team_id=row["team_id"],
        kind=row["kind"],
        amount=row["amount"],
        race_result_id=row["race_result_id"],
        round_id=row["round_id"],
        from_season_id=row["from_season_id"],
        note=row["note"],
        detail=detail or {},
        is_correction=row["is_correction"],
        actor_id=row["actor_id"],
        created_at=row["created_at"],
    )


async def insert_budget_entry(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    team_id: int,
    kind: str,
    amount: Decimal,
    detail: dict | None = None,
    race_result_id: int | None = None,
    round_id: int | None = None,
    from_season_id: int | None = None,
    note: str | None = None,
    actor_id: int | None = None,
    is_correction: bool = False,
) -> int:
    """
    Append one signed budget row. The DB trigger enforces sign-by-kind
    unless `is_correction` — a correction reverses an earlier automatic
    charge and so legitimately points the other way.
    """
    return await conn.fetchval(
        """
        INSERT INTO team_budget_ledger
            (season_id, team_id, kind, amount, race_result_id, round_id,
             from_season_id, note, detail, actor_id, is_correction)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11)
        RETURNING id
        """,
        season_id, team_id, kind, amount, race_result_id, round_id,
        from_season_id, note, json.dumps(detail or {}), actor_id, is_correction,
    )


async def fetch_budget_balance(
    conn: asyncpg.Connection, team_id: int, season_id: int
) -> Decimal:
    """Sum of the team's budget ledger for the season. Zero if no rows."""
    total = await conn.fetchval(
        """
        SELECT COALESCE(SUM(amount), 0)
          FROM team_budget_ledger
         WHERE team_id = $1 AND season_id = $2
        """,
        team_id, season_id,
    )
    return Decimal(total)


async def fetch_budget_balances_for_season(
    conn: asyncpg.Connection, season_id: int
) -> dict[int, Decimal]:
    """team_id → balance for every team with at least one row this season."""
    rows = await conn.fetch(
        """
        SELECT team_id, SUM(amount) AS balance
          FROM team_budget_ledger
         WHERE season_id = $1
         GROUP BY team_id
        """,
        season_id,
    )
    return {r["team_id"]: Decimal(r["balance"]) for r in rows}


async def fetch_budget_entries(
    conn: asyncpg.Connection, team_id: int, season_id: int, limit: int
) -> list[BudgetEntry]:
    rows = await conn.fetch(
        """
        SELECT * FROM team_budget_ledger
         WHERE team_id = $1 AND season_id = $2
         ORDER BY created_at DESC, id DESC
         LIMIT $3
        """,
        team_id, season_id, limit,
    )
    return [_row_to_budget_entry(r) for r in rows]


async def fetch_budget_totals_by_kind(
    conn: asyncpg.Connection, team_id: int, season_id: int
) -> dict[str, Decimal]:
    """kind → signed total, so a cap sheet can show where the money went."""
    rows = await conn.fetch(
        """
        SELECT kind, SUM(amount) AS total
          FROM team_budget_ledger
         WHERE team_id = $1 AND season_id = $2
         GROUP BY kind
        """,
        team_id, season_id,
    )
    return {r["kind"]: Decimal(r["total"]) for r in rows}


async def budget_entry_exists(
    conn: asyncpg.Connection, team_id: int, season_id: int, kind: str
) -> bool:
    return await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM team_budget_ledger
             WHERE team_id = $1 AND season_id = $2 AND kind = $3
        )
        """,
        team_id, season_id, kind,
    )


async def fetch_result_charge_net(
    conn: asyncpg.Connection, round_id: int
) -> dict[tuple[int, int, str], Decimal]:
    """
    (race_result_id, team_id, kind) → net amount already in the ledger
    for a round, corrections included. The ingest path diffs the
    engine's desired charges against this and writes only the delta, so
    re-importing a round never double-bills and a revised result
    produces exactly the change.
    """
    rows = await conn.fetch(
        """
        SELECT race_result_id, team_id, kind, SUM(amount) AS net
          FROM team_budget_ledger
         WHERE round_id = $1 AND race_result_id IS NOT NULL
         GROUP BY race_result_id, team_id, kind
        """,
        round_id,
    )
    return {
        (r["race_result_id"], r["team_id"], r["kind"]): Decimal(r["net"])
        for r in rows
    }


async def fetch_result_facts_for_round(
    conn: asyncpg.Connection, round_id: int
) -> list[budget_engine.ResultFacts]:
    """
    Each result in the round joined to the driver's ACTIVE contract, so
    the budget engine knows which team to charge. team_id is NULL for a
    driver with no active contract — the engine reports those rather
    than charging nobody silently.
    """
    rows = await conn.fetch(
        """
        SELECT rr.id            AS race_result_id,
               rr.driver_id,
               c.team_id,
               rr.finish_position,
               rr.dnf,
               rr.dns,
               rr.incident_points
          FROM race_results rr
          LEFT JOIN contracts c
                 ON c.driver_id = rr.driver_id
                AND c.state = 'active'
         WHERE rr.round_id = $1
         ORDER BY rr.driver_id
        """,
        round_id,
    )
    return [
        budget_engine.ResultFacts(
            race_result_id=r["race_result_id"],
            driver_id=r["driver_id"],
            team_id=r["team_id"],
            finish_position=r["finish_position"],
            dnf=r["dnf"],
            dns=r["dns"],
            incident_points=Decimal(r["incident_points"]),
        )
        for r in rows
    ]


async def fetch_team_ids_with_budget_rows(
    conn: asyncpg.Connection, season_id: int
) -> list[int]:
    rows = await conn.fetch(
        "SELECT DISTINCT team_id FROM team_budget_ledger WHERE season_id = $1 ORDER BY team_id",
        season_id,
    )
    return [r["team_id"] for r in rows]
