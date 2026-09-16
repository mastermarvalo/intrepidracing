"""
Team lifecycle in the panel.

Setup → Teams used to list teams and adjust caps, and told an admin with
no teams to go and run `/roster create` — the one command a brand-new
league cannot avoid. Creating, editing, recovering and deleting a team
are now all reachable from the screen.

Also covers the foreign-key trap behind deletion: `contracts`, offers,
trades, dead money and the budget ledger all reference `teams(id)`
WITHOUT `ON DELETE CASCADE`, so deleting a team that ever signed anyone
raised a raw `ForeignKeyViolationError` at the admin.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot import flow, queries, workflow
from bot.presets import f1 as f1_preset
from bot.ui import base, setup_screen

_GUILD = 42


@pytest.fixture
def workflow_db(monkeypatch, pg_conn_migrated):
    """`workflow.*` calls run against the migrated test database."""
    class _Ctx:
        async def __aenter__(self):
            return pg_conn_migrated

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(workflow.db, "connect", lambda: _Ctx())
    return pg_conn_migrated


class _Parent:
    opener_id = 1

    async def reload(self, _interaction, note=None):
        return None


def _team(i: int, *, payroll: str = "40.00") -> SimpleNamespace:
    return SimpleNamespace(
        team_id=i, key=f"t{i}", name=f"Team {i}",
        payroll=Decimal(payroll),
    )


def _view(teams) -> setup_screen._TeamsView:
    """The view as `render` assembles it, without touching Discord."""
    async def _back(_interaction):
        return None

    view = setup_screen._TeamsView(parent=_Parent())
    view.clear_items()
    view.add_item(setup_screen._CreateTeamButton())
    if teams:
        view.add_item(setup_screen._EditTeamSelect(view, teams))
        view.add_item(setup_screen._AdjustCapSelect(view, teams))
        view.add_item(setup_screen._DeleteTeamSelect(view, teams))
    view.add_item(setup_screen._RelinkTeamButton())
    view.add_item(base.BackButton(_back, row=4))
    return view


# ── the screen ───────────────────────────────────────────────────────


def test_an_empty_league_is_offered_a_create_button_not_a_command():
    embed = setup_screen._build_teams_embed([])
    assert "/roster create" not in embed.description
    assert "Create team" in embed.description

    labels = {c.label for c in _view([]).children if getattr(c, "label", None)}
    assert "Create team" in labels


def test_every_lifecycle_action_is_on_the_screen():
    view = _view([_team(1)])
    labels = {c.label for c in view.children if getattr(c, "label", None)}
    placeholders = {
        c.placeholder for c in view.children
        if getattr(c, "placeholder", None)
    }
    assert "Create team" in labels
    assert "Recover a lost team" in labels
    assert any("Edit a team" in p for p in placeholders)
    assert any("Delete a team" in p for p in placeholders)
    assert any("Adjust cap" in p for p in placeholders)


def test_the_view_fits_discord_component_limits():
    view = _view([_team(i) for i in range(30)])
    assert len(view.children) <= 25
    rows = {c.row for c in view.children}
    assert rows == {0, 1, 2, 3, 4}
    # Each select owns a whole row, so two must never share one.
    select_rows = [
        c.row for c in view.children if getattr(c, "options", None)
    ]
    assert len(select_rows) == len(set(select_rows))


def test_selects_never_exceed_the_option_ceiling():
    view = _view([_team(i) for i in range(40)])
    for child in view.children:
        options = getattr(child, "options", None) or []
        assert len(options) <= base.SELECT_MAX_OPTIONS


def test_a_thirty_team_league_is_listed_in_full():
    """
    `truncate_field`'s default is the 1024-char *field* limit, which
    silently dropped the tail of a three-tier league's team list.
    """
    teams = [_team(i) for i in range(30)]
    embed = setup_screen._build_teams_embed(teams)
    assert "Team 29" in embed.description
    assert len(embed.description) <= base.EMBED_DESCRIPTION_LIMIT


def test_the_footer_stops_calling_cap_adjustments_audit_only():
    """G18 made them real; the footer used to say otherwise."""
    embed = setup_screen._build_teams_embed([_team(1)])
    assert "audit-ledger row" not in (embed.footer.text or "")
    assert "effective ceiling" in embed.footer.text


# ── the create modal ─────────────────────────────────────────────────


def test_the_create_modal_carries_the_key_as_a_field():
    """
    Discord will not let one modal open another, so the key cannot be
    collected separately before the wizard's first form.
    """
    modal = flow.PanelCreateModal()
    assert len(modal.children) <= base.MODAL_MAX_INPUTS
    labels = [c.label for c in modal.children]
    assert "Team key (short id)" in labels
    assert "Display name" in labels


def test_the_panel_derives_the_key_exactly_as_the_command_does():
    """
    `/roster create name:"Red Bull"` lowercases its argument verbatim.
    A second slug rule here would produce a different key for the same
    typed name.
    """
    assert flow.normalise_team_key("  Red Bull ") == "red bull"
    assert flow.normalise_team_key("MCLAREN") == "mclaren"


# ── guarded deletion ─────────────────────────────────────────────────


async def _bootstrap(conn):
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) "
        "VALUES ($1, 'S7', TRUE) RETURNING id",
        _GUILD,
    )
    await f1_preset.seed_season(conn, season_id)
    tier_id = await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", season_id
    )
    team_id = await conn.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES ($1, 'mclaren', 'McLaren', 200, 300) RETURNING id",
        _GUILD,
    )
    return {"season_id": season_id, "tier_id": tier_id, "team_id": team_id}


@pytest.mark.asyncio
async def test_a_clean_team_has_no_delete_blockers(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    assert await queries.fetch_team_delete_blockers(
        pg_conn_migrated, ctx["team_id"]
    ) == {}


@pytest.mark.asyncio
async def test_a_team_with_a_contract_reports_a_blocker(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    driver_id = await queries.insert_driver(
        pg_conn_migrated, ctx["season_id"], ctx["tier_id"],
        member_id=111, display_name="Driver", status="active",
    )
    await queries.insert_contract(
        pg_conn_migrated, season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        driver_id=driver_id, team_id=ctx["team_id"],
        contract_value=Decimal("10.00"), signing_bonus=Decimal("0"),
        max_incentives=Decimal("0"), term_seasons=1,
        contract_type="standard", state="active",
        value_at_signing=None, approved_by=None,
    )
    blockers = await queries.fetch_team_delete_blockers(
        pg_conn_migrated, ctx["team_id"]
    )
    assert blockers == {"contracts": 1}


@pytest.mark.asyncio
async def test_deleting_a_team_with_history_is_refused_in_plain_english(
    workflow_db,
):
    """
    The old path let Postgres raise. An admin saw an unexpected-error
    message and had no idea a contract was the reason.
    """
    ctx = await _bootstrap(workflow_db)
    driver_id = await queries.insert_driver(
        workflow_db, ctx["season_id"], ctx["tier_id"],
        member_id=111, display_name="Driver", status="active",
    )
    await queries.insert_contract(
        workflow_db, season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        driver_id=driver_id, team_id=ctx["team_id"],
        contract_value=Decimal("10.00"), signing_bonus=Decimal("0"),
        max_incentives=Decimal("0"), term_seasons=1,
        contract_type="standard", state="active",
        value_at_signing=None, approved_by=None,
    )
    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.remove_team(
            _FakeClient(), guild_id=_GUILD, team_key="mclaren"
        )
    message = str(exc.value)
    assert "McLaren" in message
    assert "1 contracts" in message
    assert "money history" in message
    # It must say what to do instead of only refusing.
    assert "Release or trade" in message


@pytest.mark.asyncio
async def test_a_refused_delete_leaves_the_team_in_place(workflow_db):
    ctx = await _bootstrap(workflow_db)
    driver_id = await queries.insert_driver(
        workflow_db, ctx["season_id"], ctx["tier_id"],
        member_id=111, display_name="Driver", status="active",
    )
    await queries.insert_contract(
        workflow_db, season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        driver_id=driver_id, team_id=ctx["team_id"],
        contract_value=Decimal("10.00"), signing_bonus=Decimal("0"),
        max_incentives=Decimal("0"), term_seasons=1,
        contract_type="standard", state="active",
        value_at_signing=None, approved_by=None,
    )
    with pytest.raises(workflow.WorkflowError):
        await workflow.remove_team(
            _FakeClient(), guild_id=_GUILD, team_key="mclaren"
        )
    assert await queries.fetch_team(workflow_db, _GUILD, "mclaren") is not None


@pytest.mark.asyncio
async def test_a_clean_team_is_deleted_and_its_name_returned(workflow_db):
    await _bootstrap(workflow_db)
    name = await workflow.remove_team(
        _FakeClient(), guild_id=_GUILD, team_key="mclaren"
    )
    assert name == "McLaren"
    assert await queries.fetch_team(workflow_db, _GUILD, "mclaren") is None


@pytest.mark.asyncio
async def test_deleting_an_unknown_team_says_so(workflow_db):
    await _bootstrap(workflow_db)
    with pytest.raises(workflow.WorkflowError, match="No team"):
        await workflow.remove_team(
            _FakeClient(), guild_id=_GUILD, team_key="ferrari"
        )


@pytest.mark.asyncio
async def test_the_team_key_is_matched_case_insensitively(workflow_db):
    await _bootstrap(workflow_db)
    name = await workflow.remove_team(
        _FakeClient(), guild_id=_GUILD, team_key="McLaren"
    )
    assert name == "McLaren"


@pytest.mark.asyncio
async def test_a_message_that_cannot_be_deleted_does_not_fail_the_delete(
    workflow_db,
):
    """
    The row is what the bot is authoritative over. A roster message the
    bot can no longer touch must not strand the team in the database.
    """
    ctx = await _bootstrap(workflow_db)
    await workflow_db.execute(
        "UPDATE teams SET message_id = 555 WHERE id = $1", ctx["team_id"]
    )

    class _Boom:
        async def fetch_message(self, _mid):
            raise RuntimeError("no access")

    class _Client:
        def get_channel(self, _cid):
            return _Boom()

    name = await workflow.remove_team(
        _Client(), guild_id=_GUILD, team_key="mclaren"
    )
    assert name == "McLaren"
    assert await queries.fetch_team(workflow_db, _GUILD, "mclaren") is None


class _FakeClient:
    def get_channel(self, _channel_id):
        return None
