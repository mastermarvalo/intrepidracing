"""
DB read/write helpers for Team and TeamSlot objects.

All functions accept an open asyncpg.Connection so callers control transaction
boundaries. Use inside `async with db.connect() as conn:` (which already wraps
the work in a transaction).
"""

from typing import Sequence

import asyncpg

from bot.models import GuildConfig, StatBoard, Team, TeamSlot


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
