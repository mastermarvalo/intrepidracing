"""
G23: the results spreadsheet is remembered across panels and restarts.

Before migration 016 the URL lived on the Race Night view object, so it
survived 600 seconds and no restarts. The commissioner re-pasted the
same long Google Sheets URL before every race of every season.
"""

import pytest

from bot import queries
from bot.presets import f1 as f1_preset

pytestmark = pytest.mark.asyncio

SHEET = "https://docs.google.com/spreadsheets/d/abc123/edit"
RANGE = "R14 Abu Dhabi!A1:I30"


async def _season(
    conn, guild_id: int = 4242, name: str = "S9", active: bool = True
) -> int:
    # Only one season per guild may be active (uq_seasons_one_active).
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) "
        "VALUES ($1, $2, $3) RETURNING id",
        guild_id, name, active,
    )
    await f1_preset.seed_season(conn, season_id)
    return season_id


async def test_nothing_is_remembered_before_the_first_import(
    pg_conn_migrated,
):
    """
    A fresh league must report no memory rather than an empty string,
    so the modal shows its placeholder instead of a blank prefilled box.
    """
    season_id = await _season(pg_conn_migrated)
    assert await queries.fetch_remembered_results_sheet(
        pg_conn_migrated, season_id=season_id, tier_code="t1"
    ) == (None, None)


async def test_a_remembered_sheet_comes_back(pg_conn_migrated):
    conn = pg_conn_migrated
    season_id = await _season(conn)
    await queries.remember_results_sheet(
        conn, season_id=season_id, tier_code="t1",
        sheet_url=SHEET, sheet_range=RANGE,
    )
    assert await queries.fetch_remembered_results_sheet(
        conn, season_id=season_id, tier_code="t1"
    ) == (SHEET, RANGE)


async def test_each_tier_remembers_its_own_sheet(pg_conn_migrated):
    """
    Tiers race off different spreadsheets. Leaking Tier 1's sheet into
    Tier 2's import would silently import the wrong races.
    """
    conn = pg_conn_migrated
    season_id = await _season(conn)
    await queries.remember_results_sheet(
        conn, season_id=season_id, tier_code="t1",
        sheet_url=SHEET, sheet_range="T1!A1:I30",
    )
    await queries.remember_results_sheet(
        conn, season_id=season_id, tier_code="t2",
        sheet_url="https://docs.google.com/spreadsheets/d/zzz/edit",
        sheet_range="T2!A1:I30",
    )
    t1 = await queries.fetch_remembered_results_sheet(
        conn, season_id=season_id, tier_code="t1"
    )
    t2 = await queries.fetch_remembered_results_sheet(
        conn, season_id=season_id, tier_code="t2"
    )
    assert t1 != t2
    assert t1[1] == "T1!A1:I30"
    assert t2[1] == "T2!A1:I30"


async def test_a_later_import_replaces_the_remembered_sheet(
    pg_conn_migrated,
):
    """A league that moves to a new spreadsheet must not keep the old."""
    conn = pg_conn_migrated
    season_id = await _season(conn)
    await queries.remember_results_sheet(
        conn, season_id=season_id, tier_code="t1",
        sheet_url=SHEET, sheet_range=RANGE,
    )
    await queries.remember_results_sheet(
        conn, season_id=season_id, tier_code="t1",
        sheet_url="https://docs.google.com/spreadsheets/d/new/edit",
        sheet_range="R15!A1:I30",
    )
    url, rng = await queries.fetch_remembered_results_sheet(
        conn, season_id=season_id, tier_code="t1"
    )
    assert url.endswith("new/edit")
    assert rng == "R15!A1:I30"


async def test_a_blank_sheet_is_never_remembered(pg_conn_migrated):
    """
    Writing a blank would prefill an empty box while claiming a sheet
    was remembered, and the schema CHECK rejects it outright.
    """
    conn = pg_conn_migrated
    season_id = await _season(conn)
    await queries.remember_results_sheet(
        conn, season_id=season_id, tier_code="t1",
        sheet_url="   ", sheet_range=RANGE,
    )
    assert await queries.fetch_remembered_results_sheet(
        conn, season_id=season_id, tier_code="t1"
    ) == (None, None)


async def test_values_are_stored_trimmed(pg_conn_migrated):
    """A pasted URL usually carries whitespace; it must not round-trip."""
    conn = pg_conn_migrated
    season_id = await _season(conn)
    await queries.remember_results_sheet(
        conn, season_id=season_id, tier_code="t1",
        sheet_url=f"  {SHEET}  ", sheet_range=f"\n{RANGE}\t",
    )
    assert await queries.fetch_remembered_results_sheet(
        conn, season_id=season_id, tier_code="t1"
    ) == (SHEET, RANGE)


async def test_a_season_default_sheet_covers_every_tier(pg_conn_migrated):
    """
    Most leagues keep all three tiers on one workbook. Setting the
    season-level row should mean pasting the URL once, not three times.
    """
    conn = pg_conn_migrated
    season_id = await _season(conn)
    await conn.execute(
        "UPDATE results_config SET sheet_url = $2, sheet_range = $3 "
        "WHERE season_id = $1 AND tier_id IS NULL",
        season_id, SHEET, RANGE,
    )
    for code in ("t1", "t2", "t3"):
        assert await queries.fetch_remembered_results_sheet(
            conn, season_id=season_id, tier_code=code
        ) == (SHEET, RANGE)


async def test_a_tier_sheet_beats_the_season_default(pg_conn_migrated):
    conn = pg_conn_migrated
    season_id = await _season(conn)
    await conn.execute(
        "UPDATE results_config SET sheet_url = $2, sheet_range = $3 "
        "WHERE season_id = $1 AND tier_id IS NULL",
        season_id, SHEET, RANGE,
    )
    await queries.remember_results_sheet(
        conn, season_id=season_id, tier_code="t2",
        sheet_url="https://docs.google.com/spreadsheets/d/own/edit",
        sheet_range="T2!A1:I30",
    )
    url, _ = await queries.fetch_remembered_results_sheet(
        conn, season_id=season_id, tier_code="t2"
    )
    assert url.endswith("own/edit")


async def test_the_schema_rejects_a_blank_written_directly(
    pg_conn_migrated,
):
    """
    The CHECK is the real guard; the helper's trim is a convenience. If
    the constraint were missing, any other writer could store a blank.
    """
    conn = pg_conn_migrated
    season_id = await _season(conn)
    with pytest.raises(Exception) as exc:
        await conn.execute(
            "UPDATE results_config SET sheet_url = '' "
            "WHERE season_id = $1 AND tier_id IS NULL",
            season_id,
        )
    assert "sheet_url_not_blank" in str(exc.value)


async def test_remembering_is_scoped_to_the_season(pg_conn_migrated):
    """
    Season 9 starting on a new workbook must not inherit season 8's.
    """
    conn = pg_conn_migrated
    old = await _season(conn, name="S8", active=False)
    new = await _season(conn, name="S9")
    await queries.remember_results_sheet(
        conn, season_id=old, tier_code="t1",
        sheet_url=SHEET, sheet_range=RANGE,
    )
    assert await queries.fetch_remembered_results_sheet(
        conn, season_id=new, tier_code="t1"
    ) == (None, None)
