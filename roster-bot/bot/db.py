"""
aiosqlite connection helper and migration runner.

Call `await db.init()` once at startup (from setup_hook), then use
`async with db.connect() as conn:` throughout the bot.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import aiosqlite

log = logging.getLogger(__name__)

_DB_PATH = Path(os.getenv("DB_PATH", "./roster.db"))
_MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


async def _run_migrations(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (filename TEXT PRIMARY KEY)"
    )
    await conn.commit()

    applied: set[str] = set()
    async with conn.execute("SELECT filename FROM schema_migrations") as cursor:
        async for row in cursor:
            applied.add(row[0])

    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        log.info("Applying migration %s", path.name)
        # executescript commits implicitly and resets the connection isolation level,
        # so we insert the tracking row in a follow-up execute.
        await conn.executescript(path.read_text())
        await conn.execute(
            "INSERT INTO schema_migrations (filename) VALUES (?)", (path.name,)
        )
        await conn.commit()
        log.info("Migration %s applied", path.name)


async def init() -> None:
    """Run pending migrations. Call once from setup_hook before the bot goes online."""
    async with aiosqlite.connect(_DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await _run_migrations(conn)
    log.info("Database ready at %s", _DB_PATH)


@asynccontextmanager
async def connect() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Short-lived connection for a single operation or transaction."""
    async with aiosqlite.connect(_DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        yield conn
