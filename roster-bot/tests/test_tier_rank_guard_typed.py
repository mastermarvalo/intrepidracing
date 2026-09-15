"""
G25, typed path: `/market-admin tier add|edit` accepted duplicate ranks.

The panel modal was guarded, but `workflow.add_tier` / `edit_tier` — the
functions the slash commands call — were not, so the same invalid state
was still reachable by typing the command. `rank_order` has no unique
constraint in the schema, and `move_driver_to_tier` decides promotion
versus relegation by comparing ranks, so two tiers sharing a rank makes
promote/relegate ambiguous.
"""

import pytest

from bot import queries, workflow

pytestmark = pytest.mark.asyncio

GUILD = 991122


@pytest.fixture
def workflow_db(monkeypatch, pg_conn_migrated):
    class _Ctx:
        async def __aenter__(self):
            return pg_conn_migrated

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(workflow.db, "connect", lambda: _Ctx())
    return pg_conn_migrated


async def _season(conn) -> int:
    season_id = await queries.insert_season(conn, GUILD, "S9")
    await queries.activate_season(conn, season_id)
    return season_id


async def test_adding_a_tier_with_a_taken_rank_is_refused(workflow_db):
    await _season(workflow_db)
    await workflow.add_tier(guild_id=GUILD, code="t1", label="Tier 1", rank_order=1)

    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.add_tier(
            guild_id=GUILD, code="t2", label="Tier 2", rank_order=1
        )
    message = str(exc.value)
    assert "Rank order 1" in message
    # Name the tier in the way so the admin does not have to go look.
    assert "t1" in message


async def test_a_refused_add_writes_nothing(workflow_db):
    """A rejected add must not leave a half-created tier behind."""
    season_id = await _season(workflow_db)
    await workflow.add_tier(guild_id=GUILD, code="t1", label="Tier 1", rank_order=1)
    with pytest.raises(workflow.WorkflowError):
        await workflow.add_tier(
            guild_id=GUILD, code="t2", label="Tier 2", rank_order=1
        )
    tiers = await queries.fetch_all_tiers(workflow_db, season_id)
    assert [t.code for t in tiers] == ["t1"]


async def test_a_free_rank_is_still_accepted(workflow_db):
    """The guard must not block ordinary tier creation."""
    season_id = await _season(workflow_db)
    await workflow.add_tier(guild_id=GUILD, code="t1", label="Tier 1", rank_order=1)
    await workflow.add_tier(guild_id=GUILD, code="t2", label="Tier 2", rank_order=2)
    tiers = await queries.fetch_all_tiers(workflow_db, season_id)
    assert sorted(t.rank_order for t in tiers) == [1, 2]


async def test_gaps_in_rank_order_are_allowed(workflow_db):
    """Ranks are a sort key; only exact collisions are a problem."""
    await _season(workflow_db)
    await workflow.add_tier(guild_id=GUILD, code="t1", label="Tier 1", rank_order=1)
    await workflow.add_tier(guild_id=GUILD, code="t3", label="Tier 3", rank_order=9)


async def test_editing_a_tier_onto_a_taken_rank_is_refused(workflow_db):
    await _season(workflow_db)
    await workflow.add_tier(guild_id=GUILD, code="t1", label="Tier 1", rank_order=1)
    await workflow.add_tier(guild_id=GUILD, code="t2", label="Tier 2", rank_order=2)

    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.edit_tier(
            guild_id=GUILD,
            tier_code="t2",
            label="Tier 2",
            rank_order=1,
            accent_color=None,
        )
    assert "Rank order 1" in str(exc.value)


async def test_a_tier_may_keep_its_own_rank_while_editing(workflow_db):
    """
    Renaming a tier without touching its rank must not be blocked by
    the tier's own existing rank — the commonest edit there is.
    """
    season_id = await _season(workflow_db)
    await workflow.add_tier(guild_id=GUILD, code="t1", label="Tier 1", rank_order=1)
    await workflow.edit_tier(
        guild_id=GUILD,
        tier_code="t1",
        label="Premier Tier",
        rank_order=1,
        accent_color=None,
    )
    tier = await queries.fetch_tier(workflow_db, season_id, "t1")
    assert tier.label == "Premier Tier"
    assert tier.rank_order == 1


async def test_editing_onto_a_free_rank_works(workflow_db):
    season_id = await _season(workflow_db)
    await workflow.add_tier(guild_id=GUILD, code="t1", label="Tier 1", rank_order=1)
    await workflow.edit_tier(
        guild_id=GUILD,
        tier_code="t1",
        label="Tier 1",
        rank_order=5,
        accent_color=None,
    )
    tier = await queries.fetch_tier(workflow_db, season_id, "t1")
    assert tier.rank_order == 5


async def test_a_refused_edit_leaves_the_tier_untouched(workflow_db):
    season_id = await _season(workflow_db)
    await workflow.add_tier(guild_id=GUILD, code="t1", label="Tier 1", rank_order=1)
    await workflow.add_tier(guild_id=GUILD, code="t2", label="Tier 2", rank_order=2)
    with pytest.raises(workflow.WorkflowError):
        await workflow.edit_tier(
            guild_id=GUILD,
            tier_code="t2",
            label="Renamed",
            rank_order=1,
            accent_color=None,
        )
    tier = await queries.fetch_tier(workflow_db, season_id, "t2")
    assert tier.label == "Tier 2", "the label must not change on a refused edit"
    assert tier.rank_order == 2


async def test_the_panel_and_the_command_share_one_rule():
    """
    The setup screen must delegate rather than keep a second copy that
    can drift out of sync with the command path.
    """
    from bot.ui import setup_screen

    assert setup_screen.find_rank_conflict is workflow.find_rank_conflict
    assert setup_screen.rank_conflict_message is workflow.rank_conflict_message
