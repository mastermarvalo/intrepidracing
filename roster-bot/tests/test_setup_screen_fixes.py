"""
Two Setup-screen defects from the UX audit.

G3 (High) — the checklist told the owner to run `/roster add`, which has
never existed: `bot/cogs/roster.py` defines no `add`. Discord simply shows
nothing when they type it, so the one instruction on the "Drivers" line was
a dead end. `test_setup_only_names_commands_that_exist` is the regression
guard: it extracts every slash command the embed mentions and checks it
against the command tree the cogs actually build, so a future copy-edit
cannot reintroduce a phantom route.

G25 (Low) — the tier modals suggested `len(tiers) + 1` as the rank order
but accepted anything, including a rank another tier already held.
`migrations/002_seasons_tiers.sql` has no unique constraint on
`(season_id, rank_order)`, and `workflow.move_driver_to_tier` decides
promotion versus relegation by comparing ranks, so a duplicate makes the
direction of every move between those two tiers meaningless. A migration
is the other possible fix; this is the modal-validation half. Gaps stay
legal (rank order is a sort key, not a sequence) and editing a tier
without touching its rank must keep working — both are asserted.
"""

import re

import pytest

from bot import queries, workflow
from bot.cogs.admin_market import AdminMarketCog
from bot.cogs.roster import RosterCog
from bot.presets import f1 as f1_preset
from bot.ui import setup_screen

GUILD = 8181


# ── fakes ────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.edits: list[dict] = []

    async def defer(self, **_kwargs) -> None:
        self.deferred = True

    async def send_message(self, content, **_kwargs) -> None:
        self.edits.append({"content": content})

    async def edit_message(self, **kwargs) -> None:
        self.edits.append(kwargs)

    def is_done(self) -> bool:
        return self.deferred or bool(self.edits)


class _FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content, **_kwargs) -> None:
        self.sent.append(content)


class _FakeInteraction:
    """Enough interaction surface for a modal submit."""

    def __init__(self, guild_id: int = GUILD) -> None:
        self.guild_id = guild_id
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.original_edits: list[dict] = []

    async def edit_original_response(self, **kwargs) -> None:
        self.original_edits.append(kwargs)


class _FakeOwner:
    """Stands in for SetupView / the tier menu: records the re-render."""

    def __init__(self) -> None:
        self.reloads: list[str | None] = []
        self.renders = 0

    async def reload(self, _interaction, *, note: str | None = None) -> None:
        self.reloads.append(note)

    async def render(self, _interaction) -> None:
        self.renders += 1


class _StubTier:
    def __init__(self, code: str, label: str, rank_order: int) -> None:
        self.code = code
        self.label = label
        self.rank_order = rank_order
        self.accent_color = None


def _errors(interaction: _FakeInteraction) -> str:
    return " ".join(interaction.followup.sent) + " ".join(
        str(e.get("content", "")) for e in interaction.response.edits
    )


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def screen_db(monkeypatch, pg_conn_migrated):
    """Route every `db.connect()` in workflow at the test's schema."""

    class _Ctx:
        async def __aenter__(self):
            return pg_conn_migrated

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(workflow.db, "connect", lambda: _Ctx())
    return pg_conn_migrated


async def _seeded_season(conn, *, name="Season 7"):
    """An active season with the F1 preset's three tiers, ranks 1-3."""
    season_id = await queries.insert_season(conn, GUILD, name, True)
    await f1_preset.seed_season(conn, season_id)
    return season_id


# ── G3: the checklist must name routes that exist ────────────────────

_COMMAND_PATTERN = re.compile(r"`(/[a-z0-9 -]+)`")


def _live_command_names() -> set[str]:
    """
    Every `/…` path the cogs actually register, as the user types it.

    Built by walking the `app_commands.Group` attributes rather than a
    hardcoded list, so the guard follows the command surface.
    """
    names: set[str] = set()
    for cog in (RosterCog, AdminMarketCog):
        for attr in vars(cog).values():
            walk = getattr(attr, "walk_commands", None)
            if walk is None:
                continue
            for command in walk():
                names.add(f"/{command.qualified_name}")
            root = getattr(attr, "qualified_name", None)
            if root:
                names.add(f"/{root}")
    return names


def _no_driver_status():
    return workflow.LeagueStatus(
        season_name="Season 7",
        season_id=1,
        tiers=[
            workflow.TierStatus(
                code="t1",
                label="Tier 1",
                driver_count=0,
                has_role=True,
                latest_round_label=None,
                latest_round_order=None,
                unpublished_run_id=None,
                has_published_valuation=False,
            )
        ],
        has_config=True,
        commissioner_role_id=None,
        board_count=0,
        pending_offers=0,
        pending_trades=0,
    )


def _embed_text(embed) -> str:
    return (embed.description or "") + " ".join(
        f"{f.name} {f.value}" for f in embed.fields
    )


def test_setup_no_longer_tells_the_owner_to_run_roster_add():
    text = _embed_text(setup_screen.build_setup_embed(_no_driver_status()))

    assert "/roster add" not in text


def test_setup_names_the_bulk_enrol_route_and_the_panel():
    text = _embed_text(setup_screen.build_setup_embed(_no_driver_status()))

    assert "/market-admin driver sync-all" in text, (
        "the bulk path is the one an owner setting up wants first"
    )
    assert "Drivers" in text, "the panel route must be offered too"


def test_setup_only_names_commands_that_exist():
    """The actual G3 guard: no phantom command may appear in the embed."""
    live = _live_command_names()
    statuses = [
        _no_driver_status(),
        workflow.LeagueStatus(
            season_name=None,
            season_id=None,
            tiers=[],
            has_config=False,
            commissioner_role_id=None,
            board_count=0,
            pending_offers=0,
            pending_trades=0,
        ),
    ]
    mentioned: set[str] = set()
    for status in statuses:
        text = _embed_text(setup_screen.build_setup_embed(status))
        mentioned.update(m.strip() for m in _COMMAND_PATTERN.findall(text))

    unknown = {
        name
        for name in mentioned
        if name.startswith(("/roster", "/market-admin")) and name not in live
    }
    assert not unknown, f"setup embed names commands that do not exist: {unknown}"


def test_readme_setup_sample_matches_the_screen():
    from pathlib import Path

    readme = (Path(__file__).parent.parent / "README.md").read_text()

    assert "/roster add" not in readme
    assert "/market-admin driver sync-all" in readme


# ── G25: duplicate rank orders ───────────────────────────────────────


def test_find_rank_conflict_names_the_holder():
    tiers = [_StubTier("t1", "Tier 1", 1), _StubTier("t2", "Tier 2", 2)]

    holder = setup_screen.find_rank_conflict(tiers, 2)

    assert holder is not None and holder.code == "t2"
    assert "t2" in setup_screen.rank_conflict_message(2, holder)
    assert "Tier 2" in setup_screen.rank_conflict_message(2, holder)


def test_find_rank_conflict_allows_a_tier_to_keep_its_own_rank():
    tiers = [_StubTier("t1", "Tier 1", 1), _StubTier("t2", "Tier 2", 2)]

    assert setup_screen.find_rank_conflict(tiers, 2, exclude_code="t2") is None


def test_find_rank_conflict_ignores_gaps():
    """Rank order is a sort key; 1, 2, 9 is untidy but unambiguous."""
    tiers = [_StubTier("t1", "Tier 1", 1), _StubTier("t2", "Tier 2", 2)]

    assert setup_screen.find_rank_conflict(tiers, 9) is None


async def test_adding_a_tier_on_a_taken_rank_is_blocked(screen_db):
    await _seeded_season(screen_db)
    owner = _FakeOwner()
    modal = setup_screen._TierModal(owner, suggested_rank=4)
    modal._code._value = "t4"
    modal._label._value = "Tier 4"
    modal._rank._value = "2"
    modal._color._value = ""
    interaction = _FakeInteraction()

    await modal.on_submit(interaction)

    tiers = await workflow.list_tiers(GUILD)
    assert [t.code for t in tiers] == ["t1", "t2", "t3"], (
        "the duplicate-rank tier must not be created"
    )
    assert "t2" in _errors(interaction), "say which tier holds rank 2"
    assert not owner.reloads, "a blocked submit must not report success"


async def test_adding_a_tier_on_a_free_rank_still_works(screen_db):
    await _seeded_season(screen_db)
    owner = _FakeOwner()
    modal = setup_screen._TierModal(owner, suggested_rank=4)
    modal._code._value = "t4"
    modal._label._value = "Tier 4"
    modal._rank._value = "4"
    modal._color._value = ""
    interaction = _FakeInteraction()

    await modal.on_submit(interaction)

    tiers = await workflow.list_tiers(GUILD)
    assert [t.code for t in tiers] == ["t1", "t2", "t3", "t4"]
    assert owner.reloads, "a successful add re-renders the checklist"
    assert not interaction.followup.sent


async def test_adding_a_tier_on_an_out_of_sequence_rank_is_allowed(screen_db):
    """Gaps are legal — only an exact duplicate is refused."""
    await _seeded_season(screen_db)
    owner = _FakeOwner()
    modal = setup_screen._TierModal(owner, suggested_rank=4)
    modal._code._value = "t9"
    modal._label._value = "Tier 9"
    modal._rank._value = "9"
    modal._color._value = ""
    interaction = _FakeInteraction()

    await modal.on_submit(interaction)

    ranks = {t.code: t.rank_order for t in await workflow.list_tiers(GUILD)}
    assert ranks["t9"] == 9


async def test_editing_a_tier_onto_a_taken_rank_is_blocked(screen_db):
    season_id = await _seeded_season(screen_db)
    tiers = await queries.fetch_all_tiers(screen_db, season_id)
    t3 = next(t for t in tiers if t.code == "t3")
    menu = _FakeOwner()
    modal = setup_screen._TierEditModal(menu, t3)
    modal._label._value = "Tier 3"
    modal._rank._value = "1"
    modal._color._value = ""
    interaction = _FakeInteraction()

    await modal.on_submit(interaction)

    ranks = {t.code: t.rank_order for t in await workflow.list_tiers(GUILD)}
    assert ranks["t3"] == t3.rank_order, "the write must not land"
    assert "t1" in _errors(interaction)
    assert menu.renders == 0


async def test_editing_a_tier_may_keep_its_own_rank(screen_db):
    """The common case: rename a tier, leave the rank alone."""
    season_id = await _seeded_season(screen_db)
    tiers = await queries.fetch_all_tiers(screen_db, season_id)
    t2 = next(t for t in tiers if t.code == "t2")
    menu = _FakeOwner()
    modal = setup_screen._TierEditModal(menu, t2)
    modal._label._value = "Formula 2"
    modal._rank._value = str(t2.rank_order)
    modal._color._value = ""
    interaction = _FakeInteraction()

    await modal.on_submit(interaction)

    by_code = {t.code: t for t in await workflow.list_tiers(GUILD)}
    assert by_code["t2"].label == "Formula 2"
    assert by_code["t2"].rank_order == t2.rank_order
    assert menu.renders == 1
    assert any("updated" in msg for msg in interaction.followup.sent)


async def test_editing_a_tier_onto_a_free_rank_is_allowed(screen_db):
    season_id = await _seeded_season(screen_db)
    tiers = await queries.fetch_all_tiers(screen_db, season_id)
    t3 = next(t for t in tiers if t.code == "t3")
    menu = _FakeOwner()
    modal = setup_screen._TierEditModal(menu, t3)
    modal._label._value = "Tier 3"
    modal._rank._value = "7"
    modal._color._value = ""
    interaction = _FakeInteraction()

    await modal.on_submit(interaction)

    ranks = {t.code: t.rank_order for t in await workflow.list_tiers(GUILD)}
    assert ranks["t3"] == 7
    assert menu.renders == 1


async def test_rank_is_re_read_at_submit_not_taken_from_the_screen(screen_db):
    """
    A modal can sit open for minutes.

    The check reads the season's tiers at submit time, so a tier another
    admin added while the modal was open still blocks the collision.
    """
    season_id = await _seeded_season(screen_db)
    owner = _FakeOwner()
    modal = setup_screen._TierModal(owner, suggested_rank=4)
    await queries.insert_tier(
        screen_db,
        season_id,
        code="t4",
        label="Tier 4",
        rank_order=4,
        tier_role_id=None,
        accent_color=None,
    )
    modal._code._value = "t5"
    modal._label._value = "Tier 5"
    modal._rank._value = "4"
    modal._color._value = ""
    interaction = _FakeInteraction()

    await modal.on_submit(interaction)

    assert "t4" in _errors(interaction)
    assert [t.code for t in await workflow.list_tiers(GUILD)] == [
        "t1",
        "t2",
        "t3",
        "t4",
    ]
