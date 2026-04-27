"""
DB read/write helpers for Team and TeamSlot objects.

All functions accept an open aiosqlite.Connection so callers control
transaction boundaries. Use inside `async with db.connect() as conn:`.
"""

from typing import Sequence

import aiosqlite

from bot.models import GuildConfig, StatBoard, Team, TeamSlot


def _row_to_slot(row: aiosqlite.Row) -> TeamSlot:
    return TeamSlot(
        id=row["id"],
        team_id=row["team_id"],
        slot_role_id=row["slot_role_id"],
        label=row["label"],
        quantity=row["quantity"],
        slot_type=row["slot_type"],
        sort_order=row["sort_order"],
    )


def _row_to_team(row: aiosqlite.Row, slots: list[TeamSlot]) -> Team:
    return Team(
        id=row["id"],
        guild_id=row["guild_id"],
        key=row["key"],
        name=row["name"],
        team_role_id=row["team_role_id"],
        channel_id=row["channel_id"],
        tagline=row["tagline"],
        logo_url=row["logo_url"],
        principal_role_id=row["principal_role_id"],
        color=row["color"],
        message_id=row["message_id"],
        slots=slots,
    )


async def fetch_team(
    conn: aiosqlite.Connection, guild_id: int, key: str
) -> Team | None:
    async with conn.execute(
        "SELECT * FROM teams WHERE guild_id = ? AND key = ?", (guild_id, key)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    slots = await fetch_slots(conn, row["id"])
    return _row_to_team(row, slots)


async def fetch_team_by_id(conn: aiosqlite.Connection, team_id: int) -> Team | None:
    async with conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    slots = await fetch_slots(conn, team_id)
    return _row_to_team(row, slots)


async def fetch_all_teams(conn: aiosqlite.Connection, guild_id: int) -> list[Team]:
    async with conn.execute(
        "SELECT * FROM teams WHERE guild_id = ? ORDER BY key", (guild_id,)
    ) as cur:
        rows = await cur.fetchall()
    teams: list[Team] = []
    for row in rows:
        slots = await fetch_slots(conn, row["id"])
        teams.append(_row_to_team(row, slots))
    return teams


async def fetch_slots(conn: aiosqlite.Connection, team_id: int) -> list[TeamSlot]:
    async with conn.execute(
        "SELECT * FROM team_slots WHERE team_id = ? ORDER BY sort_order", (team_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_slot(r) for r in rows]


async def insert_team(
    conn: aiosqlite.Connection,
    guild_id: int,
    key: str,
    name: str,
    team_role_id: int,
    channel_id: int,
    tagline: str | None,
    logo_url: str | None,
    principal_role_id: int | None,
    color: int | None = None,
) -> int:
    """Insert a new team row and return its id."""
    async with conn.execute(
        """
        INSERT INTO teams
            (guild_id, key, name, team_role_id, channel_id, tagline, logo_url, principal_role_id, color)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (guild_id, key, name, team_role_id, channel_id, tagline, logo_url, principal_role_id, color),
    ) as cur:
        assert cur.lastrowid is not None
        return cur.lastrowid


async def update_team(
    conn: aiosqlite.Connection,
    team_id: int,
    name: str,
    team_role_id: int,
    channel_id: int,
    tagline: str | None,
    logo_url: str | None,
    principal_role_id: int | None,
    color: int | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE teams
        SET name = ?, team_role_id = ?, channel_id = ?, tagline = ?, logo_url = ?,
            principal_role_id = ?, color = ?
        WHERE id = ?
        """,
        (name, team_role_id, channel_id, tagline, logo_url, principal_role_id, color, team_id),
    )


async def set_message_id(
    conn: aiosqlite.Connection, team_id: int, message_id: int | None
) -> None:
    await conn.execute(
        "UPDATE teams SET message_id = ? WHERE id = ?", (message_id, team_id)
    )


async def replace_slots(
    conn: aiosqlite.Connection,
    team_id: int,
    slots: Sequence[tuple[int, str, int, str, int]],
) -> None:
    """
    Replace all slots for a team in one shot.
    Each tuple: (slot_role_id, label, quantity, slot_type, sort_order).
    """
    await conn.execute("DELETE FROM team_slots WHERE team_id = ?", (team_id,))
    await conn.executemany(
        """
        INSERT INTO team_slots (team_id, slot_role_id, label, quantity, slot_type, sort_order)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(team_id, *s) for s in slots],
    )


async def delete_team(conn: aiosqlite.Connection, team_id: int) -> None:
    await conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))


# ── guild config ──────────────────────────────────────────────────────────────


async def fetch_guild_config(conn: aiosqlite.Connection, guild_id: int) -> GuildConfig:
    async with conn.execute(
        "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
    ) as cur:
        row = await cur.fetchone()
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
    conn: aiosqlite.Connection, guild_id: int, free_agent_role_id: int
) -> None:
    await conn.execute(
        """
        INSERT INTO guild_config (guild_id, free_agent_role_id) VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET free_agent_role_id = excluded.free_agent_role_id
        """,
        (guild_id, free_agent_role_id),
    )


async def upsert_fa_channel(
    conn: aiosqlite.Connection, guild_id: int, fa_channel_id: int
) -> None:
    await conn.execute(
        """
        INSERT INTO guild_config (guild_id, fa_channel_id) VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET fa_channel_id = excluded.fa_channel_id
        """,
        (guild_id, fa_channel_id),
    )


async def set_fa_message_id(
    conn: aiosqlite.Connection, guild_id: int, message_id: int | None
) -> None:
    await conn.execute(
        "UPDATE guild_config SET fa_message_id = ? WHERE guild_id = ?",
        (message_id, guild_id),
    )


async def upsert_transactions_channel(
    conn: aiosqlite.Connection, guild_id: int, channel_id: int
) -> None:
    await conn.execute(
        """
        INSERT INTO guild_config (guild_id, transactions_channel_id) VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET transactions_channel_id = excluded.transactions_channel_id
        """,
        (guild_id, channel_id),
    )


# ── stat boards ───────────────────────────────────────────────────────────────


def _row_to_board(row: aiosqlite.Row) -> StatBoard:
    return StatBoard(
        id=row["id"],
        guild_id=row["guild_id"],
        title=row["title"],
        sheet_id=row["sheet_id"],
        sheet_range=row["sheet_range"],
        channel_id=row["channel_id"],
        message_id=row["message_id"],
    )


async def fetch_all_stat_boards(conn: aiosqlite.Connection, guild_id: int) -> list[StatBoard]:
    async with conn.execute(
        "SELECT * FROM stat_boards WHERE guild_id = ? ORDER BY title", (guild_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_board(r) for r in rows]


async def fetch_stat_board(
    conn: aiosqlite.Connection, guild_id: int, title: str
) -> StatBoard | None:
    async with conn.execute(
        "SELECT * FROM stat_boards WHERE guild_id = ? AND lower(title) = lower(?)",
        (guild_id, title),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_board(row) if row else None


async def fetch_stat_board_by_id(conn: aiosqlite.Connection, board_id: int) -> StatBoard | None:
    async with conn.execute("SELECT * FROM stat_boards WHERE id = ?", (board_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_board(row) if row else None


async def insert_stat_board(
    conn: aiosqlite.Connection,
    guild_id: int,
    title: str,
    sheet_id: str,
    sheet_range: str,
    channel_id: int,
) -> int:
    async with conn.execute(
        """
        INSERT INTO stat_boards (guild_id, title, sheet_id, sheet_range, channel_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (guild_id, title, sheet_id, sheet_range, channel_id),
    ) as cur:
        assert cur.lastrowid is not None
        return cur.lastrowid


async def set_stat_board_message_id(
    conn: aiosqlite.Connection, board_id: int, message_id: int | None
) -> None:
    await conn.execute(
        "UPDATE stat_boards SET message_id = ? WHERE id = ?", (message_id, board_id)
    )


async def delete_stat_board(conn: aiosqlite.Connection, board_id: int) -> None:
    await conn.execute("DELETE FROM stat_boards WHERE id = ?", (board_id,))
