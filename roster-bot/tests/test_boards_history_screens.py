"""
Boards (Screen 8) and History (Screen 9) behaviour, not just composition.

`tests/test_panel_screens.py` asserts these screens *render* inside
Discord's limits. The three defects covered here are about what the
screen *does*, so the views are driven through their callbacks with a
fake interaction and a real database:

  * **G24** — the Add-board wizard's no-tiers branch cleared the view's
    components without editing the message, so every later click on that
    message (Cancel included) hit a view with no matching child.
  * **G20** — "Board posted" / "Refreshed every board" were printed
    without checking. `bot/market/boards.py` logs and returns on a
    missing channel or a `discord.Forbidden`, so the only evidence the
    screen can have is the board state *after* the call.
  * **G19** — an unpublished run reached from History had no Publish
    button and never named `/market-admin valuation publish run_id: N`.

Known gap (needs a change in `bot/market/boards.py`, which this agent
does not own): re-reading `message_id` distinguishes "never posted" from
"posted", but *not* "posted earlier, and today's edit was rejected" —
`refresh_board` swallows that `discord.Forbidden` and returns `None`.
Closing it needs `refresh_board` to return its outcome, e.g.

    class BoardRefresh(NamedTuple):
        board_id: int
        posted: bool          # a message exists now
        action: str           # "sent" | "edited" | "skipped_no_channel"
                              # | "skipped_forbidden" | "message_missing"

    async def refresh_board(bot, board) -> BoardRefresh: ...
    async def refresh_all_boards(bot) -> list[BoardRefresh]: ...

with `workflow.refresh_boards` passing the list through. Until then the
screen says so out loud (`_EDIT_CAVEAT`) rather than claiming success.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

from bot import queries, workflow
from bot.presets import f1 as f1_preset
from bot.ui import boards_screen, history_screen

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


# ── fakes ────────────────────────────────────────────────────────────


class _FakeClient:
    """Stands in for the bot; only ever passed through to a stub."""

    def get_channel(self, _channel_id):
        return None


class _FakeResponse:
    def __init__(self, calls: list) -> None:
        self._calls = calls
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kwargs) -> None:
        self._done = True
        self._calls.append(("defer", kwargs))

    async def edit_message(self, **kwargs) -> None:
        self._done = True
        self._calls.append(("edit_message", kwargs))

    async def send_message(self, content=None, **kwargs) -> None:
        self._done = True
        self._calls.append(("send_message", {"content": content, **kwargs}))


class _FakeFollowup:
    def __init__(self, calls: list) -> None:
        self._calls = calls

    async def send(self, content=None, **kwargs) -> None:
        self._calls.append(("followup", {"content": content, **kwargs}))


class FakeInteraction:
    """
    Enough of `discord.Interaction` for a panel callback.

    Records every response so a test can assert *that the message was
    edited* — which is the whole point of G24 — as well as what was said.
    """

    def __init__(self, *, guild_id: int = 1, user_id: int = 7) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.response = _FakeResponse(self.calls)
        self.followup = _FakeFollowup(self.calls)
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id)
        self.client = _FakeClient()

    async def edit_original_response(self, **kwargs):
        self.calls.append(("edit_original_response", kwargs))
        return SimpleNamespace(id=1)

    async def original_response(self):
        return SimpleNamespace(id=1)

    # -- assertions helpers --

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]

    def edits(self) -> list[dict]:
        return [
            payload
            for kind, payload in self.calls
            if kind in ("edit_message", "edit_original_response")
        ]

    def notes(self) -> list[str]:
        return [
            payload["content"]
            for kind, payload in self.calls
            if kind in ("followup", "send_message") and payload.get("content")
        ]


async def noop_back(_interaction):  # pragma: no cover - navigation stub
    return None


def labels(view: discord.ui.View) -> set[str]:
    return {c.label for c in view.children if getattr(c, "label", None)}


def pick(select: discord.ui.Select, value: str) -> None:
    """Pretend the owner chose `value` — what Discord posts back."""
    select._values = [value]


def board(board_id=1, *, kind="market", tier="t1", channel=999, healthy=True):
    return workflow.BoardInfo(
        board_id=board_id,
        kind=kind,
        tier_code=tier,
        channel_id=channel,
        healthy=healthy,
    )


def run_summary(run_id=1, *, tier="t1", published=False):
    return workflow.ValuationRunSummary(
        run_id=run_id,
        tier_code=tier,
        round_label=f"R{run_id}",
        published=published,
        created_at=NOW,
    )


def round_summary(order=1, *, tier="t1"):
    return workflow.RaceRoundSummary(
        tier_code=tier,
        round_order=order,
        round_label=f"R{order}",
        held_on=None,
        result_count=20,
    )


def preview(run_id=7, *, published=False, tier="t1"):
    return workflow.ValuationRunPreview(
        run_id=run_id,
        tier_code=tier,
        round_label="R14 Abu Dhabi",
        published=published,
        rows=[],
    )


# ── db fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def workflow_db(monkeypatch, pg_conn_migrated):
    """Point `workflow.db.connect()` at the test schema."""

    class _Ctx:
        async def __aenter__(self):
            return pg_conn_migrated

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(workflow.db, "connect", lambda: _Ctx())
    return pg_conn_migrated


@pytest.fixture
def silent_boards(monkeypatch):
    """
    A board layer that posts nothing and raises nothing.

    This is exactly what `bot/market/boards.py` does on a non-cached
    channel or a `discord.Forbidden`: log, return, leave `message_id`
    NULL.
    """
    calls: list[str] = []

    async def _refresh_board(_client, board_row):
        calls.append(f"board:{board_row.id}")

    async def _refresh_all(_client):
        calls.append("all")

    monkeypatch.setattr(workflow.market_boards, "refresh_board", _refresh_board)
    monkeypatch.setattr(
        workflow.market_boards, "refresh_all_boards", _refresh_all
    )
    return calls


@pytest.fixture
def stub_tier_refresh(monkeypatch):
    calls: list[dict] = []

    async def _fake(_client, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        workflow.market_boards, "refresh_boards_for_tier", _fake
    )
    return calls


async def seed_season(conn, *, guild_id=1):
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) "
        "VALUES ($1, 'S7', TRUE) RETURNING id",
        guild_id,
    )
    await f1_preset.seed_season(conn, season_id)
    tier_id = await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", season_id
    )
    return season_id, tier_id


# ── G24: the no-tiers branch must not wedge the message ──────────────


@pytest.fixture
def no_tiers(monkeypatch):
    async def _none(_guild_id):
        return []

    monkeypatch.setattr(workflow, "list_tier_choices", _none)


def _add_flow():
    parent = boards_screen.BoardsView(
        boards=[], opener_id=1, on_back=noop_back
    )
    return boards_screen._AddBoardFlow(opener_id=1, parent=parent)


async def test_no_tiers_branch_leaves_the_view_with_components(no_tiers):
    flow = _add_flow()
    flow.kind = "market"
    interaction = FakeInteraction()

    await flow.advance(interaction)

    assert flow.children, "a view with no children fails every later click"
    assert "Cancel" in labels(flow)
    assert any(isinstance(c, boards_screen._KindSelect) for c in flow.children)


async def test_no_tiers_branch_edits_the_message_before_reporting(no_tiers):
    """The wedge was the missing edit, not the missing error."""
    flow = _add_flow()
    flow.kind = "market"
    interaction = FakeInteraction()

    await flow.advance(interaction)

    assert interaction.kinds()[0] == "edit_message"
    edited = interaction.edits()[0]
    assert edited["view"] is flow
    assert edited["view"].children
    assert "no tiers" in edited["embed"].description.lower()


async def test_no_tiers_branch_still_reports_the_error(no_tiers):
    flow = _add_flow()
    flow.kind = "market"
    interaction = FakeInteraction()

    await flow.advance(interaction)

    assert any("tier" in note.lower() for note in interaction.notes())
    assert any("Setup" in note for note in interaction.notes())


async def test_no_tiers_branch_says_nothing_was_created(no_tiers):
    flow = _add_flow()
    flow.kind = "market"
    interaction = FakeInteraction()

    await flow.advance(interaction)

    description = interaction.edits()[0]["embed"].description
    assert "nothing was created" in description.lower()


async def test_cancel_still_works_after_the_no_tiers_branch(
    no_tiers, monkeypatch
):
    """The symptom users reported: Cancel died as 'interaction failed'."""

    async def _no_boards(_guild_id):
        return []

    monkeypatch.setattr(workflow, "list_boards", _no_boards)

    flow = _add_flow()
    flow.kind = "market"
    await flow.advance(FakeInteraction())

    cancel = next(c for c in flow.children if getattr(c, "label", "") == "Cancel")
    second = FakeInteraction()
    await cancel.callback(second)

    assert second.edits(), "Cancel must re-render the boards screen"


async def test_no_tiers_branch_resets_to_step_one(no_tiers):
    """A cross-tier kind is still reachable without re-running /league."""
    flow = _add_flow()
    flow.kind = "market"

    await flow.advance(FakeInteraction())

    assert flow.kind is None
    assert flow.tier_code is None
    kind_select = next(
        c for c in flow.children if isinstance(c, boards_screen._KindSelect)
    )
    assert "dashboard" in {o.value for o in kind_select.options}


async def test_a_tier_scoped_kind_still_advances_when_tiers_exist(monkeypatch):
    async def _tiers(_guild_id):
        return [("t1", "t1 — Tier 1")]

    monkeypatch.setattr(workflow, "list_tier_choices", _tiers)
    flow = _add_flow()
    flow.kind = "market"

    await flow.advance(FakeInteraction())

    assert any(isinstance(c, boards_screen._TierSelect) for c in flow.children)


# ── G20: outcome notes come from re-read board state ─────────────────


def test_added_board_note_claims_a_post_only_when_one_exists():
    posted = boards_screen.build_added_board_note(
        board(7, healthy=True, channel=555), board_id=7, channel_id=555
    )

    assert "posted in <#555>" in posted
    assert posted.startswith("✅")


def test_added_board_note_names_the_missing_permissions():
    note = boards_screen.build_added_board_note(
        board(7, healthy=False, channel=555), board_id=7, channel_id=555
    )

    assert "nothing was posted" in note.lower()
    assert "Send Messages" in note and "Embed Links" in note
    assert "<#555>" in note
    assert "no other board changed" in note.lower()
    assert "✅" not in note


def test_added_board_note_handles_a_board_that_vanished():
    note = boards_screen.build_added_board_note(
        None, board_id=7, channel_id=555
    )

    assert "not in the board list" in note
    assert "✅" not in note


def test_refresh_all_note_never_claims_boards_that_are_not_posted():
    note = boards_screen.build_refresh_all_note(
        [board(1, healthy=True), board(2, healthy=False, channel=42)]
    )

    assert "1 of 2" in note
    assert "still not posted" in note
    assert "<#42>" in note
    assert "Send Messages" in note


def test_refresh_all_note_reports_a_fully_healthy_set():
    note = boards_screen.build_refresh_all_note(
        [board(1), board(2, tier="t2")]
    )

    assert "All 2" in note
    assert "not posted" not in note


def test_refresh_all_note_states_what_it_cannot_see():
    """Never claim a verification we did not make (the boards.py gap)."""
    note = boards_screen.build_refresh_all_note([board(1)])

    assert "bot log" in note
    assert "edit" in note.lower()


def test_refresh_all_note_pages_a_long_failure_list_rather_than_dropping_it():
    boards = [board(i, healthy=False, channel=100 + i) for i in range(12)]

    note = boards_screen.build_refresh_all_note(boards)

    assert "12 board(s) are still not posted" in note
    assert "and 7 more" in note
    assert len(note) <= 2000, "a note is a Discord message"


def test_refresh_all_note_with_no_boards_says_nothing_happened():
    assert "nothing was posted" in boards_screen.build_refresh_all_note([])


def test_refresh_one_note_distinguishes_posted_from_not():
    ok = boards_screen.build_refresh_one_note(board(3), board_id=3)
    bad = boards_screen.build_refresh_one_note(
        board(3, healthy=False, channel=8), board_id=3
    )

    assert ok.startswith("✅")
    assert "could not post" in bad
    assert "no other board changed" in bad.lower()


def test_refresh_one_note_handles_a_removed_board():
    note = boards_screen.build_refresh_one_note(None, board_id=3)

    assert "no longer in the board list" in note
    assert "Nothing was posted" in note


async def test_add_board_reports_the_failure_when_nothing_was_posted(
    workflow_db, silent_boards
):
    """End to end: `add_board` succeeds, the post does not."""
    season_id, tier_id = await seed_season(workflow_db)

    board_id = await workflow.add_board(
        _FakeClient(), guild_id=1, kind="market", channel_id=555, tier_code="t1"
    )
    boards = await workflow.list_boards(1)
    added = next(b for b in boards if b.board_id == board_id)
    note = boards_screen.build_added_board_note(
        added, board_id=board_id, channel_id=555
    )

    assert not added.healthy
    assert "nothing was posted" in note.lower()
    assert "Send Messages" in note
    row = await queries.fetch_market_board_by_id(workflow_db, board_id)
    assert (row.season_id, row.tier_id) == (season_id, tier_id)
    assert row.message_id is None, "nothing was posted, so nothing to edit"


async def test_refresh_all_button_reports_the_unposted_board(
    workflow_db, silent_boards
):
    await seed_season(workflow_db)
    await workflow.add_board(
        _FakeClient(), guild_id=1, kind="market", channel_id=555, tier_code="t1"
    )
    view = boards_screen.BoardsView(
        boards=await workflow.list_boards(1), opener_id=1, on_back=noop_back
    )
    button = next(
        c
        for c in view.children
        if isinstance(c, boards_screen._RefreshBoardsButton)
    )
    interaction = FakeInteraction()

    await button.callback(interaction)

    assert "all" in silent_boards, "the refresh pass must still run"
    note = "\n".join(interaction.notes())
    assert "still not posted" in note
    assert "<#555>" in note
    assert "Refreshed every board" not in note


async def test_refresh_all_button_reports_success_once_the_board_posted(
    workflow_db, silent_boards
):
    """Same click, different board state — the note has to follow state."""
    await seed_season(workflow_db)
    board_id = await workflow.add_board(
        _FakeClient(), guild_id=1, kind="market", channel_id=555, tier_code="t1"
    )
    await queries.set_market_board_message_id(workflow_db, board_id, 4242)

    view = boards_screen.BoardsView(
        boards=await workflow.list_boards(1), opener_id=1, on_back=noop_back
    )
    button = next(
        c
        for c in view.children
        if isinstance(c, boards_screen._RefreshBoardsButton)
    )
    interaction = FakeInteraction()

    await button.callback(interaction)

    note = "\n".join(interaction.notes())
    assert "All 1" in note
    assert "still not posted" not in note


async def test_refresh_one_select_reports_per_board_state(
    workflow_db, silent_boards
):
    await seed_season(workflow_db)
    board_id = await workflow.add_board(
        _FakeClient(), guild_id=1, kind="market", channel_id=555, tier_code="t1"
    )
    view = boards_screen.BoardsView(
        boards=await workflow.list_boards(1), opener_id=1, on_back=noop_back
    )
    select = next(
        c for c in view.children if isinstance(c, boards_screen._RefreshOneSelect)
    )
    pick(select, str(board_id))
    interaction = FakeInteraction()

    await select.callback(interaction)

    note = "\n".join(interaction.notes())
    assert f"`{board_id}`" in note
    assert "could not post" in note


# ── G20/Screen 8: removal is irreversible, so it confirms ────────────


async def test_removing_a_board_asks_for_confirmation_first(
    workflow_db, silent_boards
):
    await seed_season(workflow_db)
    board_id = await workflow.add_board(
        _FakeClient(), guild_id=1, kind="market", channel_id=555, tier_code="t1"
    )
    view = boards_screen.BoardsView(
        boards=await workflow.list_boards(1), opener_id=1, on_back=noop_back
    )
    select = next(
        c for c in view.children if isinstance(c, boards_screen._RemoveBoardSelect)
    )
    pick(select, str(board_id))
    interaction = FakeInteraction()

    await select.callback(interaction)

    payload = interaction.edits()[0]
    assert isinstance(payload["view"], boards_screen._ConfirmRemoveView)
    assert "cannot be undone" in payload["embed"].description.lower()
    assert await queries.fetch_market_board_by_id(workflow_db, board_id)


async def test_confirming_removal_deletes_the_board_and_says_what_survived(
    workflow_db, silent_boards
):
    await seed_season(workflow_db)
    board_id = await workflow.add_board(
        _FakeClient(), guild_id=1, kind="market", channel_id=555, tier_code="t1"
    )
    parent = boards_screen.BoardsView(
        boards=await workflow.list_boards(1), opener_id=1, on_back=noop_back
    )
    confirm = boards_screen._ConfirmRemoveView(
        opener_id=1, parent=parent, board_id=board_id
    )
    button = next(
        c
        for c in confirm.children
        if isinstance(c, boards_screen._ConfirmRemoveButton)
    )
    interaction = FakeInteraction()

    await button.callback(interaction)

    assert await queries.fetch_market_board_by_id(workflow_db, board_id) is None
    note = "\n".join(interaction.notes())
    assert "removed" in note
    assert "nothing else changed" in note.lower()


def test_remove_confirm_embed_states_the_message_is_deleted_too():
    embed = boards_screen.build_remove_confirm_embed(
        board(4, healthy=True, channel=77), board_id=4
    )

    assert "posted message will be deleted" in embed.description
    assert "<#77>" in embed.description


def test_remove_confirm_embed_handles_a_board_that_never_posted():
    embed = boards_screen.build_remove_confirm_embed(
        board(4, healthy=False), board_id=4
    )

    assert "no posted message" in embed.description


# ── Screen 8: more than 25 boards pages instead of vanishing ─────────


def test_boards_selects_page_instead_of_truncating():
    boards = [board(i) for i in range(60)]

    first = boards_screen.BoardsView(
        boards=boards, opener_id=1, on_back=noop_back
    )
    last = boards_screen.BoardsView(
        boards=boards, opener_id=1, on_back=noop_back, page=2
    )

    first_values = {
        o.value
        for c in first.children
        if isinstance(c, boards_screen._RemoveBoardSelect)
        for o in c.options
    }
    last_values = {
        o.value
        for c in last.children
        if isinstance(c, boards_screen._RemoveBoardSelect)
        for o in c.options
    }
    assert len(first_values) == boards_screen.BOARDS_PER_PAGE
    assert "0" in first_values and "0" not in last_values
    assert "59" in last_values
    assert "Next page" in labels(first)
    assert "Previous page" in labels(last)


def test_board_pages_are_clamped_and_labelled():
    boards = [board(i) for i in range(30)]

    view = boards_screen.BoardsView(
        boards=boards, opener_id=1, on_back=noop_back, page=99
    )
    embed = boards_screen.build_boards_embed(boards, page=view.page)

    assert view.page == boards_screen.page_count(len(boards)) - 1
    assert any("page 2 of 2" in f.value for f in embed.fields)


def test_a_single_page_of_boards_has_no_page_buttons():
    view = boards_screen.BoardsView(
        boards=[board(1)], opener_id=1, on_back=noop_back
    )

    assert "Next page" not in labels(view)


# ── G19: publishing a dry-run from History ───────────────────────────


def _preview_view(*, published=False, run_id=7):
    parent = history_screen._ValuationBrowserView(
        opener_id=1,
        parent=history_screen.HistoryView(opener_id=1, on_back=noop_back),
        tier_choices=[("t1", "t1 — Tier 1")],
        runs=[run_summary(run_id, published=published)],
        tier_filter=None,
    )
    return history_screen._ValuationPreviewView(
        opener_id=1,
        parent=parent,
        preview=preview(run_id, published=published),
    )


def test_an_unpublished_run_offers_a_publish_button():
    view = _preview_view(published=False)

    assert "Publish this run" in labels(view)
    assert "Back to runs" in labels(view)


def test_a_published_run_has_no_publish_button():
    view = _preview_view(published=True)

    assert "Publish this run" not in labels(view)


def test_the_preview_embed_names_the_run_and_the_typed_command():
    embed = history_screen.build_valuation_preview_embed(preview(9))

    assert "#9" in embed.description
    assert "/market-admin valuation publish run_id: 9" in embed.description


def test_a_published_preview_does_not_advertise_publishing():
    embed = history_screen.build_valuation_preview_embed(
        preview(9, published=True)
    )

    assert "valuation publish" not in embed.description
    assert "PUBLISHED" in embed.description


def test_an_unpublished_run_names_its_fallback_on_expiry():
    view = _preview_view(published=False, run_id=12)

    assert view.expiry_hint is not None
    assert "valuation publish run_id: 12" in view.expiry_hint


def test_the_confirm_note_restates_the_consequence():
    note = history_screen.build_publish_confirm_note(preview(7))

    assert "cannot be undone" in note.lower()
    assert "t1" in note
    assert "run_id: 7" in note


def test_the_success_note_states_what_did_not_happen():
    outcome = workflow.PublishOutcome(
        run_id=7,
        season_id=1,
        tier_id=1,
        already_published=False,
        boards_refreshed=True,
    )

    note = history_screen.build_published_note(outcome, tier_code="t1")

    assert "Published run #7" in note
    assert "No contract, budget or Discord role changed" in note


def test_the_success_note_admits_a_failed_board_refresh():
    outcome = workflow.PublishOutcome(
        run_id=7,
        season_id=1,
        tier_id=1,
        already_published=False,
        boards_refreshed=False,
    )

    note = history_screen.build_published_note(outcome, tier_code="t1")

    assert "board refresh failed" in note.lower()


def test_an_already_published_run_is_reported_as_unchanged():
    outcome = workflow.PublishOutcome(
        run_id=7,
        season_id=1,
        tier_id=1,
        already_published=True,
        boards_refreshed=False,
    )

    note = history_screen.build_published_note(outcome, tier_code="t1")

    assert "already published" in note
    assert "nothing changed" in note


async def _run_row(conn, *, published=False):
    season_id, tier_id = await seed_season(conn)
    run_id = await queries.insert_valuation_run(
        conn,
        season_id=season_id,
        tier_id=tier_id,
        round_label="R14 Abu Dhabi",
        created_by=1,
        published=published,
    )
    return season_id, tier_id, run_id


async def test_the_first_click_arms_and_publishes_nothing(
    workflow_db, stub_tier_refresh
):
    _, _, run_id = await _run_row(workflow_db)
    view = _preview_view(run_id=run_id)
    button = next(
        c
        for c in view.children
        if isinstance(c, history_screen._PublishRunButton)
    )
    interaction = FakeInteraction()

    await button.callback(interaction)

    row = await queries.fetch_valuation_run(workflow_db, run_id)
    assert not row["published"], "the first click must not publish"
    assert button.label == "Confirm publish"
    assert button.style is discord.ButtonStyle.danger
    assert stub_tier_refresh == []
    assert any("cannot be undone" in n.lower() for n in interaction.notes())


async def test_the_second_click_publishes_and_reports_honestly(
    workflow_db, stub_tier_refresh
):
    season_id, tier_id, run_id = await _run_row(workflow_db)
    view = _preview_view(run_id=run_id)
    button = next(
        c
        for c in view.children
        if isinstance(c, history_screen._PublishRunButton)
    )

    await button.callback(FakeInteraction())
    second = FakeInteraction()
    await button.callback(second)

    row = await queries.fetch_valuation_run(workflow_db, run_id)
    assert row["published"]
    assert stub_tier_refresh == [
        {"guild_id": 1, "season_id": season_id, "tier_id": tier_id}
    ]
    note = "\n".join(second.notes())
    assert f"Published run #{run_id}" in note
    assert "No contract, budget or Discord role changed" in note


async def test_publishing_re_renders_the_run_without_the_button(
    workflow_db, stub_tier_refresh
):
    _, _, run_id = await _run_row(workflow_db)
    view = _preview_view(run_id=run_id)
    button = next(
        c
        for c in view.children
        if isinstance(c, history_screen._PublishRunButton)
    )

    await button.callback(FakeInteraction())
    second = FakeInteraction()
    await button.callback(second)

    rendered = second.edits()[-1]
    assert "Publish this run" not in labels(rendered["view"])
    assert "PUBLISHED" in rendered["embed"].description


async def test_publishing_an_already_published_run_changes_nothing(
    workflow_db, stub_tier_refresh
):
    _, _, run_id = await _run_row(workflow_db, published=True)
    # Reached by two admins racing: this panel still thinks it is a dry-run.
    view = _preview_view(run_id=run_id)
    button = next(
        c
        for c in view.children
        if isinstance(c, history_screen._PublishRunButton)
    )

    await button.callback(FakeInteraction())
    second = FakeInteraction()
    await button.callback(second)

    assert stub_tier_refresh == [], "a repeat publish must not re-announce"
    assert any("already published" in n for n in second.notes())


async def test_a_missing_run_is_a_user_facing_error(
    workflow_db, stub_tier_refresh
):
    view = _preview_view(run_id=999_999)
    button = next(
        c
        for c in view.children
        if isinstance(c, history_screen._PublishRunButton)
    )

    await button.callback(FakeInteraction())
    second = FakeInteraction()
    await button.callback(second)

    assert any("999999" in n.replace("`", "") for n in second.notes())


# ── Screen 9: browsers page rather than truncate ─────────────────────


def test_round_browser_pages_instead_of_truncating():
    rounds = [round_summary(i) for i in range(40)]

    first = history_screen._RoundsBrowserView(
        opener_id=1,
        parent=history_screen.HistoryView(opener_id=1, on_back=noop_back),
        tier_choices=[("t1", "t1 — Tier 1")],
        rounds=rounds,
        tier_filter=None,
    )
    second = history_screen._RoundsBrowserView(
        opener_id=1,
        parent=history_screen.HistoryView(opener_id=1, on_back=noop_back),
        tier_choices=[("t1", "t1 — Tier 1")],
        rounds=rounds,
        tier_filter=None,
        page=1,
    )

    def values(view):
        return {
            o.value
            for c in view.children
            if isinstance(c, history_screen._RoundSelect)
            for o in c.options
        }

    assert len(values(first)) == history_screen.ITEMS_PER_PAGE
    assert values(first).isdisjoint(values(second))
    assert len(values(second)) == len(rounds) - history_screen.ITEMS_PER_PAGE
    assert "Next page" in labels(first)


def test_round_list_embed_says_which_page_it_is_showing():
    rounds = [round_summary(i) for i in range(40)]

    embed = history_screen.build_rounds_list_embed(
        rounds, tier_filter=None, page=1
    )

    assert any("of 40" in f.value for f in embed.fields)
    assert "R39" in embed.description
    assert "R0" not in embed.description


def test_valuation_browser_pages_too():
    runs = [run_summary(i) for i in range(30)]

    view = history_screen._ValuationBrowserView(
        opener_id=1,
        parent=history_screen.HistoryView(opener_id=1, on_back=noop_back),
        tier_choices=[],
        runs=runs,
        tier_filter=None,
        page=1,
    )
    select = next(
        c for c in view.children if isinstance(c, history_screen._ValuationRunSelect)
    )

    assert len(select.options) == len(runs) - history_screen.ITEMS_PER_PAGE
    assert "Previous page" in labels(view)


def test_a_single_page_browser_has_no_page_buttons():
    view = history_screen._RoundsBrowserView(
        opener_id=1,
        parent=history_screen.HistoryView(opener_id=1, on_back=noop_back),
        tier_choices=[],
        rounds=[round_summary(1)],
        tier_filter=None,
    )

    assert "Next page" not in labels(view)
