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


async def test_seeding_refuses_a_season_that_is_already_set_up(workflow_db):
    """
    The guard that keeps this a recovery path rather than a reset
    button. Re-seeding a live season would rewrite its valuation
    factors and cap underneath contracts already signed against them.

    The guard is the config row, not the tiers — see the settings-only
    tests below for why tiers alone must not block it.
    """
    await _bare_season(workflow_db)
    await workflow.seed_preset_into_season(guild_id=GUILD, name="S9")

    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.seed_preset_into_season(guild_id=GUILD, name="S9")
    message = str(exc.value)
    assert "already set up" in message
    # The message must say why refusing is the safe answer, not just no.
    assert "contracts" in message


# ── G2 residual: tiers by hand, settings never seeded ────────────────
#
# `add_tier` writes a tier without creating a league_config row, and
# seeding used to refuse the moment any tier existed. So: create a
# season with no preset, add one tier through Setup → Tiers, and the
# season was stuck — seeding refused because tiers existed, and the
# config panel it redirected to refused because there was no row to
# edit. Nothing in the bot could rescue it.


async def _season_with_a_handmade_tier(conn, name: str = "S9") -> int:
    """The exact state G2 describes: a tier, no settings."""
    season_id = await _bare_season(conn, name)
    await queries.activate_season(conn, season_id)
    await workflow.add_tier(
        guild_id=GUILD, code="pro", label="Pro Division", rank_order=1
    )
    assert await queries.fetch_league_config_row(conn, season_id, None) is None
    return season_id


async def test_seeding_fills_in_the_settings_a_handmade_tier_never_created(
    workflow_db,
):
    conn = workflow_db
    season_id = await _season_with_a_handmade_tier(conn)

    result = await workflow.seed_preset_into_season(guild_id=GUILD, name="S9")

    assert result.settings_only is True
    assert result.tiers_kept == 1
    assert result.tiers_created == 0
    cfg = await queries.fetch_league_config_row(conn, season_id, None)
    assert cfg is not None, "the whole point: the season now has settings"
    assert cfg.salary_cap == Decimal("145.00")


async def test_settings_only_seeding_leaves_the_handmade_tiers_alone(
    workflow_db,
):
    """
    Seeding t1/t2/t3 alongside a hand-built `pro` tier would leave two
    tiers sharing rank_order 1 — a state `add_tier`'s rank guard exists
    to prevent, and which `queries.insert_tier` does not check.
    """
    conn = workflow_db
    season_id = await _season_with_a_handmade_tier(conn)

    await workflow.seed_preset_into_season(guild_id=GUILD, name="S9")

    tiers = await queries.fetch_all_tiers(conn, season_id)
    assert [t.code for t in tiers] == ["pro"]
    assert tiers[0].label == "Pro Division"
    assert len({t.rank_order for t in tiers}) == len(tiers)


async def test_settings_only_seeding_still_installs_the_scoring_table(
    workflow_db,
):
    """
    Valuations read the factor weights and the position curve, so a
    config row on its own would not make the season usable.
    """
    conn = workflow_db
    season_id = await _season_with_a_handmade_tier(conn)

    await workflow.seed_preset_into_season(guild_id=GUILD, name="S9")

    assert await queries.fetch_valuation_factors(conn, season_id)
    assert await queries.fetch_position_scores(conn, season_id)


async def test_the_config_panel_works_after_settings_only_seeding(
    workflow_db,
):
    """The redirect target has to actually work once you follow it."""
    await _season_with_a_handmade_tier(workflow_db)

    with pytest.raises(workflow.WorkflowError):
        await workflow.fetch_config_for_edit(guild_id=GUILD)

    await workflow.seed_preset_into_season(guild_id=GUILD, name="S9")

    _season_id, tier_id, cfg = await workflow.fetch_config_for_edit(
        guild_id=GUILD
    )
    assert tier_id is None
    assert cfg.salary_cap == Decimal("145.00")


async def test_valuation_advice_names_a_command_that_now_works(workflow_db):
    """
    The old text was "Seed one with the F1 preset first." — which the
    admin could not do, because seeding refused once a tier existed.
    """
    await _season_with_a_handmade_tier(workflow_db)

    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.run_valuation(
            guild_id=GUILD, user_id=1, tier="pro", round_label="R1"
        )
    message = str(exc.value)
    assert "seed-preset" in message
    assert "S9" in message, "name the season so it is copy-pasteable"
    assert "Seed one with the F1 preset first" not in message


async def test_show_config_advice_no_longer_says_create_a_new_season(
    workflow_db,
):
    """
    `show_config` told admins to "create a season with the F1 preset" —
    advice that cannot be followed for the season already named,
    activated and holding their tiers.
    """
    await _season_with_a_handmade_tier(workflow_db)

    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.show_config(GUILD)
    message = str(exc.value)
    assert "seed-preset" in message
    assert "Create a season" not in message

    await workflow.seed_preset_into_season(guild_id=GUILD, name="S9")
    assert await workflow.show_config(GUILD) is not None


async def test_the_whole_dead_end_is_escapable_end_to_end(workflow_db):
    """
    Walks the reported reproduction: season with no preset, tier added
    through Setup, then out again — with a valuation actually running.
    """
    conn = workflow_db
    await _season_with_a_handmade_tier(conn)

    await workflow.seed_preset_into_season(guild_id=GUILD, name="S9")

    status = await workflow.fetch_league_status(GUILD)
    assert status.has_config is True
    assert status.has_tiers is True
    # No drivers yet, so the valuation stops there — but it is now the
    # drivers that are missing, not the settings.
    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.run_valuation(
            guild_id=GUILD, user_id=1, tier="pro", round_label="R1"
        )
    assert "No drivers in tier" in str(exc.value)


async def test_the_setup_screen_tells_a_settings_less_season_what_to_press(
    workflow_db,
):
    await _season_with_a_handmade_tier(workflow_db)

    status = await workflow.fetch_league_status(GUILD)
    embed = setup_screen.build_setup_embed(status)

    next_step = [f for f in embed.fields if f.name == "Next step"]
    assert next_step, "a settings-less season must still be told what to do"
    assert "Seed settings" in next_step[0].value
    assert "seed-preset" in next_step[0].value
    assert "S9" in next_step[0].value


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
        has_config = False
        has_drivers = False
        setup_complete = False
        pending_offers = 0
        pending_trades = 0

    embed = setup_screen.build_setup_embed(_Status())
    next_step = [f for f in embed.fields if f.name == "Next step"]
    assert next_step, "a tierless season must still be told what to do"
    assert "seed-preset" in next_step[0].value
    assert "S9" in next_step[0].value, "name the season so it is copy-pasteable"
