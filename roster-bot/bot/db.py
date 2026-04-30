"""
asyncpg pool + migration runner.

Call `await db.init()` once at startup, then use `async with db.connect() as conn:`
throughout the bot. Each `connect()` wraps work in a transaction that commits on
clean exit and rolls back on exception, matching the implicit-transaction
behaviour SQLite/aiosqlite gave us — call sites don't need explicit commits.
"""

import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import asyncpg

log = logging.getLogger(__name__)

_DSN = os.getenv("DATABASE_URL")
_MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

_pool: asyncpg.Pool | None = None


def _redact(dsn: str) -> str:
    return re.sub(r"://([^:/@]+):[^@]+@", r"://\1:***@", dsn)


async def init() -> None:
    """Create the pool and run pending migrations. Call once from setup_hook."""
    global _pool
    if not _DSN:
        raise RuntimeError("DATABASE_URL not set")
    _pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=10)
    async with _pool.acquire() as conn:
        await _run_migrations(conn)
    log.info("Database ready at %s", _redact(_DSN))


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def connect() -> AsyncIterator[asyncpg.Connection]:
    """Acquire a pooled connection wrapped in a transaction."""
    if _pool is None:
        raise RuntimeError("db.init() must be called before db.connect()")
    async with _pool.acquire() as conn:
        async with conn.transaction():
            yield conn


async def _run_migrations(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (filename TEXT PRIMARY KEY)"
    )
    rows = await conn.fetch("SELECT filename FROM schema_migrations")
    applied = {r["filename"] for r in rows}

    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        log.info("Applying migration %s", path.name)
        async with conn.transaction():
            await conn.execute(path.read_text())
            await conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES ($1)", path.name
            )
        log.info("Migration %s applied", path.name)
