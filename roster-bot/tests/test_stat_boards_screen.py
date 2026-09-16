"""
Panel screen for Google Sheets stat boards.

`/sheets add|remove|list|refresh` were the last admin commands with no
panel route of any kind. Rendering was also trapped inside the cog, so
a second caller would have had to duplicate forum-thread reuse and the
edit-then-repost fallback.
"""

from types import SimpleNamespace

import discord
import pytest

from bot import sheet_boards
from bot.ui import base, boards_screen
from bot.ui import stat_boards_screen as screen


def _board(i: int, *, message_id: int | None = 900, title: str | None = None):
    return SimpleNamespace(
        id=i,
        title=title or f"Board {i}",
        sheet_id="abc123",
        sheet_range="Sheet1",
        channel_id=500,
        message_id=message_id,
        forum_thread_id=None,
    )


async def _noop_back(_interaction):
    return None


# ── the renderer is shared, not duplicated ───────────────────────────


def test_the_command_group_uses_the_extracted_renderer():
    import bot.cogs.sheets as cog

    assert cog._rerender_board is sheet_boards._rerender_board
    assert cog._get_forum_thread is sheet_boards._get_forum_thread


# ── the screen ───────────────────────────────────────────────────────


def test_an_empty_server_gets_an_add_button_not_a_command():
    embed = screen.build_stat_boards_embed([])
    assert "/sheets add" not in embed.description
    assert "Add board" in embed.description

    view = screen.StatBoardsView(boards=[], opener_id=1, on_back=_noop_back)
    labels = {c.label for c in view.children if getattr(c, "label", None)}
    assert "Add board" in labels
    # Nothing to refresh or remove yet, so neither is offered.
    assert "Refresh all" not in labels
    assert not [c for c in view.children if getattr(c, "options", None)]


def test_every_action_appears_once_boards_exist():
    view = screen.StatBoardsView(
        boards=[_board(1)], opener_id=1, on_back=_noop_back
    )
    labels = {c.label for c in view.children if getattr(c, "label", None)}
    assert {"Add board", "Refresh all", "Back"} <= labels
    placeholders = {
        c.placeholder for c in view.children if getattr(c, "placeholder", None)
    }
    assert any("Remove a stat board" in p for p in placeholders)


def test_the_screen_fits_discord_limits_with_many_boards():
    view = screen.StatBoardsView(
        boards=[_board(i) for i in range(40)], opener_id=1, on_back=_noop_back
    )
    assert len(view.children) <= 25
    for child in view.children:
        options = getattr(child, "options", None) or []
        assert len(options) <= base.SELECT_MAX_OPTIONS
        if child.row is not None:
            assert child.row < 5


def test_a_board_with_no_message_is_flagged_not_hidden():
    embed = screen.build_stat_boards_embed([_board(1, message_id=None)])
    assert "not posted yet" in embed.description


def test_a_posted_board_reads_as_posted():
    embed = screen.build_stat_boards_embed([_board(1)])
    assert "posted" in embed.description
    assert "not posted yet" not in embed.description


def test_a_long_list_is_trimmed_with_an_honest_count():
    embed = screen.build_stat_boards_embed([_board(i) for i in range(30)])
    assert "and 5 more" in embed.description


def test_the_boards_screen_links_to_the_sheets_screen():
    view = boards_screen.BoardsView(
        boards=[], opener_id=1, on_back=_noop_back
    )
    labels = {c.label for c in view.children if getattr(c, "label", None)}
    assert "Google Sheets boards" in labels


def test_the_two_board_kinds_are_distinguished_in_words():
    """
    Market boards and Google Sheets boards were both just "boards".
    An owner needs to be able to tell which screen they are on.
    """
    empty = screen.build_stat_boards_embed([])
    assert "Google Sheet" in empty.description
    assert screen.__doc__ is not None
    assert "market" in screen.__doc__.lower()


# ── add-flow reporting ───────────────────────────────────────────────


def test_a_failed_first_fetch_is_reported_not_papered_over():
    """The board is still created; claiming success would be a lie."""
    note = screen.build_add_note("Standings", 500, fetch_failed=True)
    assert "⚠️" in note
    assert "first read of the sheet failed" in note
    assert "shared" in note


def test_a_clean_add_says_so_plainly():
    note = screen.build_add_note("Standings", 500, fetch_failed=False)
    assert "✅" in note
    assert "10 minutes" in note
    assert "failed" not in note


def test_a_bad_url_is_rejected_before_the_channel_is_asked_for():
    """
    Validating in the modal means a typo costs one step, not two, and
    no half-made board is left behind.
    """
    from bot import sheets

    assert sheets.parse_sheet_id("not a link") is None
    assert (
        sheets.parse_sheet_id(
            "https://docs.google.com/spreadsheets/d/ABC123/edit#gid=0"
        )
        == "ABC123"
    )


def test_the_add_modal_asks_only_what_a_modal_can_hold():
    view = screen.StatBoardsView(boards=[], opener_id=1, on_back=_noop_back)
    modal = screen._AddStatBoardModal(view)
    assert len(modal.children) <= base.MODAL_MAX_INPUTS
    labels = [c.label for c in modal.children]
    assert "Board title" in labels
    assert "Google Sheets URL" in labels
    # The channel is deliberately absent: Discord modals have no
    # channel field, so it is a ChannelSelect on the next step.
    assert not any("hannel" in label for label in labels)


def test_the_range_defaults_rather_than_being_required():
    view = screen.StatBoardsView(boards=[], opener_id=1, on_back=_noop_back)
    modal = screen._AddStatBoardModal(view)
    range_field = next(
        c for c in modal.children if "range" in (c.label or "").lower()
    )
    assert range_field.required is False


# ── permissions and safety ───────────────────────────────────────────


def test_the_screen_is_admin_owned():
    view = screen.StatBoardsView(boards=[], opener_id=7, on_back=_noop_back)
    assert isinstance(view, base.AdminOwnedView)
    assert view.opener_id == 7


def test_removal_is_behind_a_confirmation():
    view = screen.StatBoardsView(
        boards=[_board(1)], opener_id=1, on_back=_noop_back
    )
    confirm = screen._ConfirmRemoveStatBoard(view, _board(1))
    labels = {c.label for c in confirm.children}
    assert labels == {"Remove board", "Cancel"}
    danger = next(c for c in confirm.children if c.label == "Remove board")
    assert danger.style is discord.ButtonStyle.danger


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", [None, object()])
async def test_creating_a_board_in_an_unusable_channel_is_refused(
    channel, monkeypatch
):
    """A non-text channel must fail loudly, not post nothing silently."""

    async def _fake_embed(_title, _sid, _rng):
        return discord.Embed(title="x", color=discord.Color.green())

    monkeypatch.setattr(
        sheet_boards.sheets, "fetch_and_build_embed", _fake_embed
    )

    class _Bot:
        def get_channel(self, _cid):
            return channel

    with pytest.raises(ValueError, match="not a text channel"):
        await sheet_boards.create_stat_board(
            _Bot(), guild_id=1, title="T", sheet_id="s",
            sheet_range="Sheet1", channel_id=5,
        )
