"""
Shared fixtures.

Render fixtures (FakeMember / FakeRole / make_team / make_slot) satisfy the
MemberLike protocol without touching discord.py internals — plain
dataclasses, no mocking framework needed.

DB fixtures (`pg_conn`, `pg_conn_migrated`) let migration and preset tests
run against a real Postgres. They isolate per-test in a temporary schema so
tests never leak into each other or into the developer's dev database, and
they skip cleanly when TEST_DATABASE_URL is not set — matching the
existing convention that plain `uv run pytest` should work without any
external services.

To run the DB-backed tests locally:
    TEST_DATABASE_URL=postgresql://roster:roster@127.0.0.1:5432/roster \
        uv run pytest
"""

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

try:
    import asyncpg
except ImportError:  # pragma: no cover - asyncpg is a hard dep of the bot itself
    asyncpg = None  # type: ignore[assignment]

from bot.models import Team, TeamSlot


@dataclass
class FakeRole:
    id: int


@dataclass
class FakeAvatar:
    url: str = "https://cdn.discordapp.com/embed/avatars/0.png"


@dataclass
class FakeMember:
    """
    A stand-in for discord.Member.

    `display_name` defaults to a per-id value (`Member10`, `Member11`, ...)
    so render assertions can tell two members apart. The renderer emits
    display names rather than `<@id>` mentions (see `render._slot_block`),
    so a shared constant default would make those assertions vacuous.
    """

    id: int
    roles: list[FakeRole] = field(default_factory=list)
    display_name: str = ""
    display_avatar: FakeAvatar = field(default_factory=FakeAvatar)

    def __post_init__(self) -> None:
        if not self.display_name:
            self.display_name = f"Member{self.id}"


def make_team(
    *,
    team_role_id: int = 100,
    slots: list[TeamSlot] | None = None,
    tagline: str | None = None,
    logo_url: str | None = None,
) -> Team:
    return Team(
        id=1,
        guild_id=999,
        key="testteam",
        name="Test Team",
        team_role_id=team_role_id,
        channel_id=1,
        tagline=tagline,
        logo_url=logo_url,
        slots=slots or [],
    )


def make_slot(
    *,
    slot_role_id: int,
    label: str = "Slot",
    quantity: int = 2,
    slot_type: str = "driver",
    sort_order: int = 0,
) -> TeamSlot:
    return TeamSlot(
        id=1,
        team_id=1,
        slot_role_id=slot_role_id,
        label=label,
        quantity=quantity,
        slot_type=slot_type,  # type: ignore[arg-type]
        sort_order=sort_order,
    )


# ── DB fixtures ─────────────────────────────────────────────────────────

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def _test_dsn() -> str | None:
    return os.getenv("TEST_DATABASE_URL")


@pytest.fixture
async def pg_conn():
    """
    Isolated per-test Postgres connection scoped to a fresh temporary schema.

    Skips if TEST_DATABASE_URL isn't set so `uv run pytest` still passes on
    a machine without Postgres. Every table the migrations create lands in
    the throwaway schema and vanishes at teardown.
    """
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL not set")
    if asyncpg is None:
        pytest.skip("asyncpg not installed")

    conn = await asyncpg.connect(dsn)
    schema = f"test_{uuid.uuid4().hex[:16]}"
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}"')
    try:
        yield conn
    finally:
        try:
            await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await conn.close()


async def apply_migrations(conn, *, up_to: str | None = None) -> list[str]:
    """
    Apply migrations/*.sql in sorted filename order.

    If `up_to` is provided, stops after applying that filename (inclusive).
    Returns the list of filenames actually applied so tests can assert on
    the sequence. Mirrors bot.db._run_migrations but does not require the
    pool — the fixture provides the connection directly.
    """
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        await conn.execute(path.read_text())
        applied.append(path.name)
        if up_to is not None and path.name == up_to:
            break
    return applied


@pytest.fixture
async def pg_conn_migrated(pg_conn):
    """Convenience: pg_conn with every migration applied."""
    await apply_migrations(pg_conn)
    return pg_conn
