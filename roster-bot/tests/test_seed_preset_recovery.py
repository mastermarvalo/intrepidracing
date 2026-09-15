"""
G2: a season created without a preset was a permanent dead end.

`create_season` was the only place a preset was ever applied, so a
season created without one had no tiers and no `league_config` row, and
nothing in the bot could give it either. Every downstream command then
failed with advice ("create a season with the F1 preset") that could not
be followed for the season the admin had already named and activated.
"""

from decimal import Decimal

import pytest

from bot import queries, workflow
from bot.ui import setup_screen

pytestmark = pytest.mark.asyncio

GUILD = 778899


@pytest.fixture
def workflow_db(monkeypatch, pg_conn_migrated):
    """
    Point `workflow`'s `db.connect()` at the migrated test connection.

    Mirrors the fixture in `tests/test_workflow_setup.py`; the workflow
    layer opens its own connections, so it has to be redirected rather
    than passed one.
    """

    class _Ctx:
        async def __aenter__(self):
            return pg_conn_migrated

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(workflow.db, "connect", lambda: _Ctx())
    return pg_conn_migrated


async def _bare_season(conn, name: str = "S9") -> int:
    """A season created the way `/market-admin season create` does with
    no preset chosen: a row and nothing else."""
    return await queries.insert_season(conn, GUILD, name)


async def test_a_bare_season_has_no_tiers_and_no_config(workflow_db):
    """Establishes the dead end this fix exists to escape."""
    conn = workflow_db
    season_id = await _bare_season(conn)
    assert await queries.fetch_all_tiers(conn, season_id) == []
    assert await queries.fetch_league_config_row(conn, season_id, None) is None


async def test_seeding_gives_a_bare_season_tiers_and_config(workflow_db):
    conn = workflow_db
    await _bare_season(conn)

    result = await workflow.seed_preset_into_season(guild_id=GUILD, name="S9")
    assert result.tiers_created == 3

    tiers = await queries.fetch_all_tiers(conn, result.season_id)
    assert sorted(t.code for t in tiers) == ["t1", "t2", "t3"]
    assert await queries.fetch_league_config_row(conn, result.season_id, None)


async def test_seeding_installs_the_145m_cap(workflow_db):
    """
    The cap is the number the whole economy is built on. If seeding
    produced a config row with some other cap, every later signing
    would be validated against the wrong ceiling.
    """
    conn = workflow_db
    await _bare_season(conn)
    result = await workflow.seed_preset_into_season(guild_id=GUILD, name="S9")
    cfg = await queries.fetch_league_config_row(conn, result.season_id, None)
    # The league's $145M cap, which the whole economy is priced against.
    assert cfg.salary_cap == Decimal("145.00")
    assert cfg.min_salary == Decimal("1.00")


async def test_seeding_refuses_a_season_that_already_has_tiers(workflow_db):
    """
    The guard that keeps this a recovery path rather than a reset
    button. Re-seeding a live season would rewrite its valuation
    factors and cap underneath contracts already signed against them.
    """
    await _bare_season(workflow_db)
    await workflow.seed_preset_into_season(guild_id=GUILD, name="S9")

    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.seed_preset_into_season(guild_id=GUILD, name="S9")
    message = str(exc.value)
    assert "already has tiers" in message
    # The message must say why refusing is the safe answer, not just no.
    assert "contracts" in message


async def test_seeding_an_unknown_season_says_so(workflow_db):
    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.seed_preset_into_season(guild_id=GUILD, name="Nope")
    assert "No season named" in str(exc.value)


async def test_an_unknown_preset_is_rejected(workflow_db):
    await _bare_season(workflow_db)
    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.seed_preset_into_season(
            guild_id=GUILD, name="S9", preset="f2"
        )
    assert "Unknown preset" in str(exc.value)


async def test_config_error_text_no_longer_names_a_flag_that_never_existed(
    workflow_db,
):
    """
    The old message told admins to create a season with `--preset f1`.
    There has never been a `--preset` flag: the command takes a Discord
    choice option. Following the advice literally was impossible.
    """
    season_id = await _bare_season(workflow_db)
    await queries.activate_season(workflow_db, season_id)

    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.fetch_config_for_edit(guild_id=GUILD)
    message = str(exc.value)
    assert "--preset" not in message
    assert "seed-preset" in message


@pytest.mark.asyncio(loop_scope="function")
async def test_the_setup_screen_points_a_tierless_season_at_the_fix():
    """
    "Press Tier to add your first tier" was the old advice, and it did
    not create the config row, so the season stayed unusable.
    """

    class _Status:
        season_name = "S9"
        tiers: list = []
        driver_count = 0
        has_season = True
        has_tiers = False
        has_drivers = False
        setup_complete = False
        pending_offers = 0
        pending_trades = 0

    embed = setup_screen.build_setup_embed(_Status())
    next_step = [f for f in embed.fields if f.name == "Next step"]
    assert next_step, "a tierless season must still be told what to do"
    assert "seed-preset" in next_step[0].value
    assert "S9" in next_step[0].value, "name the season so it is copy-pasteable"
