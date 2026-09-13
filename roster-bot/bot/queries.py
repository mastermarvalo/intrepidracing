"""
DB read/write helpers for Team and TeamSlot objects.

All functions accept an open asyncpg.Connection so callers control transaction
boundaries. Use inside `async with db.connect() as conn:` (which already wraps
the work in a transaction).
"""

from decimal import Decimal
from typing import Sequence

import asyncpg

from bot.models import (
    Driver,
    GuildConfig,
    LeagueConfig,
    Season,
    StatBoard,
    Team,
    TeamSlot,
    Tier,
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
        ON CONFLICT (guild_id) DO UPDATE SET transactions_channel_id = excluded.transactions_channel_id
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
                max_term_seasons, max_incentive_pct, offer_ttl_hours
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id
            """,
            season_id, tier_id, salary_cap, min_salary, max_salary,
            active_driver_slots, weekly_move_cap, exceptional_move_cap,
            max_term_seasons, max_incentive_pct, offer_ttl_hours,
        )
    await conn.execute(
        """
        UPDATE league_config SET
            salary_cap = $1, min_salary = $2, max_salary = $3,
            active_driver_slots = $4, weekly_move_cap = $5,
            exceptional_move_cap = $6, max_term_seasons = $7,
            max_incentive_pct = $8, offer_ttl_hours = $9
        WHERE id = $10
        """,
        salary_cap, min_salary, max_salary, active_driver_slots,
        weekly_move_cap, exceptional_move_cap, max_term_seasons,
        max_incentive_pct, offer_ttl_hours, existing,
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
