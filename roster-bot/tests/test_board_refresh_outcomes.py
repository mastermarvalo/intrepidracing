"""
G20 — `refresh_board` reports what it did.

It used to return `None` and log permission failures at warning level.
The only signal a caller had was `message_id IS NOT NULL`, which cannot
tell "posted" from "posted earlier, and today's edit was rejected", so
`/market-admin board refresh` replied "✅ Refreshed all market boards"
even when Discord had refused every channel.

`refresh_board` is driven here with a fake channel that raises the
exceptions Discord would, because the whole point of the fix is the
behaviour on those paths.
"""

from decimal import Decimal
from types import SimpleNamespace

import discord
import pytest

from bot.cogs import admin_market
from bot.market import boards as market_boards
from bot.ui import boards_screen

_BOARD_ID = 7
_CHANNEL_ID = 4242
_MESSAGE_ID = 99


def _board(*, message_id: int | None = _MESSAGE_ID) -> SimpleNamespace:
    return SimpleNamespace(
        id=_BOARD_ID, season_id=1, tier_id=None, kind="market",
        channel_id=_CHANNEL_ID, message_id=message_id,
    )


def _forbidden() -> discord.Forbidden:
    return discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), "missing permissions"
    )


def _not_found() -> discord.NotFound:
    return discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"), "unknown message"
    )


class _FakeMessage:
    def __init__(self, *, edit_raises: Exception | None = None) -> None:
        self._edit_raises = edit_raises
        self.edits = 0

    async def edit(self, **_kwargs) -> None:
        if self._edit_raises is not None:
            raise self._edit_raises
        self.edits += 1


class _FakeChannel(discord.TextChannel):
    """
    A real `TextChannel` subclass so the `isinstance` check in
    `refresh_board` passes, with the Discord calls swapped out.
    """

    def __init__(
        self,
        *,
        fetch_raises: Exception | None = None,
        edit_raises: Exception | None = None,
        send_raises: Exception | None = None,
    ) -> None:
        self.message = _FakeMessage(edit_raises=edit_raises)
        self._fetch_raises = fetch_raises
        self._send_raises = send_raises
        self.sent = 0

    async def fetch_message(self, _message_id):
        if self._fetch_raises is not None:
            raise self._fetch_raises
        return self.message

    async def send(self, **_kwargs):
        if self._send_raises is not None:
            raise self._send_raises
        self.sent += 1
        return SimpleNamespace(id=_MESSAGE_ID)


class _FakeBot:
    def __init__(self, channel) -> None:
        self._channel = channel

    def get_channel(self, _channel_id):
        return self._channel


@pytest.fixture
def stub_embed(monkeypatch):
    """Board data is not what G20 is about; give it a fixed embed."""
    async def _build(_board):
        return discord.Embed(title="Market")

    monkeypatch.setattr(market_boards, "build_board_embed", _build)


@pytest.fixture
def stub_message_id_write(monkeypatch):
    """Record `set_market_board_message_id` instead of touching a DB."""
    calls: list[tuple[int, int | None]] = []

    class _Ctx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    async def _set(_conn, board_id, message_id):
        calls.append((board_id, message_id))

    monkeypatch.setattr(market_boards.db, "connect", lambda: _Ctx())
    monkeypatch.setattr(
        market_boards.queries, "set_market_board_message_id", _set
    )
    return calls


# ── refresh_board outcomes ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_successful_edit_reports_edited(stub_embed):
    channel = _FakeChannel()
    outcome = await market_boards.refresh_board(_FakeBot(channel), _board())
    assert outcome == market_boards.BoardRefresh(
        _BOARD_ID, True, market_boards.ACTION_EDITED
    )
    assert channel.message.edits == 1


@pytest.mark.asyncio
async def test_a_rejected_edit_reports_forbidden_while_still_posted(
    stub_embed,
):
    """
    The exact case board state could not see: the message is still in
    the channel, so `message_id IS NOT NULL` reads as healthy, but it is
    showing yesterday's numbers.
    """
    channel = _FakeChannel(edit_raises=_forbidden())
    outcome = await market_boards.refresh_board(_FakeBot(channel), _board())
    assert outcome.action == market_boards.ACTION_SKIPPED_FORBIDDEN
    assert outcome.posted is True
    assert outcome.action in market_boards.FAILED_ACTIONS


@pytest.mark.asyncio
async def test_a_deleted_message_reports_missing_and_clears_message_id(
    stub_embed, stub_message_id_write,
):
    channel = _FakeChannel(fetch_raises=_not_found())
    outcome = await market_boards.refresh_board(_FakeBot(channel), _board())
    assert outcome.action == market_boards.ACTION_MESSAGE_MISSING
    assert outcome.posted is False
    assert stub_message_id_write == [(_BOARD_ID, None)]


@pytest.mark.asyncio
async def test_a_first_post_reports_sent_and_stores_the_message_id(
    stub_embed, stub_message_id_write,
):
    channel = _FakeChannel()
    outcome = await market_boards.refresh_board(
        _FakeBot(channel), _board(message_id=None)
    )
    assert outcome.action == market_boards.ACTION_SENT
    assert outcome.posted is True
    assert channel.sent == 1
    assert stub_message_id_write == [(_BOARD_ID, _MESSAGE_ID)]


@pytest.mark.asyncio
async def test_a_refused_first_post_reports_forbidden_and_not_posted(
    stub_embed,
):
    channel = _FakeChannel(send_raises=_forbidden())
    outcome = await market_boards.refresh_board(
        _FakeBot(channel), _board(message_id=None)
    )
    assert outcome.action == market_boards.ACTION_SKIPPED_FORBIDDEN
    assert outcome.posted is False


@pytest.mark.asyncio
async def test_a_missing_channel_reports_no_channel(stub_embed):
    outcome = await market_boards.refresh_board(_FakeBot(None), _board())
    assert outcome.action == market_boards.ACTION_SKIPPED_NO_CHANNEL
    assert outcome.posted is False


@pytest.mark.asyncio
async def test_a_board_with_nothing_to_show_reports_no_data(monkeypatch):
    async def _none(_board):
        return None

    monkeypatch.setattr(market_boards, "build_board_embed", _none)
    outcome = await market_boards.refresh_board(_FakeBot(None), _board())
    assert outcome.action == market_boards.ACTION_SKIPPED_NO_DATA
    # Nothing to show is not a failure the operator must act on.
    assert outcome.action not in market_boards.FAILED_ACTIONS


@pytest.mark.asyncio
async def test_a_refresh_pass_never_stops_early_on_one_bad_channel(
    stub_embed,
):
    """
    The original rationale for swallowing errors stays true: one broken
    channel must not take out the rest of the pass.
    """
    channel = _FakeChannel(edit_raises=_forbidden())
    outcomes = [
        await market_boards.refresh_board(_FakeBot(channel), _board())
        for _ in range(3)
    ]
    assert len(outcomes) == 3


def test_every_action_has_a_human_label():
    """A new action must not render as a bare identifier to an operator."""
    actions = {
        market_boards.ACTION_SENT,
        market_boards.ACTION_EDITED,
        market_boards.ACTION_MESSAGE_MISSING,
        market_boards.ACTION_SKIPPED_NO_CHANNEL,
        market_boards.ACTION_SKIPPED_FORBIDDEN,
        market_boards.ACTION_SKIPPED_NO_DATA,
    }
    assert actions == set(market_boards.ACTION_LABELS)
    for action in actions:
        assert market_boards.ACTION_LABELS[action][:1].islower()


# ── what operators are told ──────────────────────────────────────────


def test_the_typed_command_does_not_claim_success_with_no_boards():
    note = admin_market._render_refresh_outcomes([])
    assert "✅" not in note
    assert "board add" in note


def test_the_typed_command_names_the_boards_that_did_not_update():
    outcomes = [
        market_boards.BoardRefresh(1, True, market_boards.ACTION_EDITED),
        market_boards.BoardRefresh(
            2, True, market_boards.ACTION_SKIPPED_FORBIDDEN
        ),
    ]
    note = admin_market._render_refresh_outcomes(outcomes)
    assert "1 of 2" in note
    assert "`2`" in note
    assert "permission" in note
    assert note.startswith("⚠")


def test_the_typed_command_confirms_a_clean_pass():
    outcomes = [
        market_boards.BoardRefresh(1, True, market_boards.ACTION_EDITED),
        market_boards.BoardRefresh(2, True, market_boards.ACTION_SENT),
    ]
    note = admin_market._render_refresh_outcomes(outcomes)
    assert note.startswith("✅")
    assert "2 market board(s)" in note


def _info(board_id: int, *, healthy: bool):
    return SimpleNamespace(
        board_id=board_id, kind="market", channel_id=_CHANNEL_ID,
        tier_code=None, tier_label=None, healthy=healthy,
        message_id=_MESSAGE_ID if healthy else None,
        season_name="S7", driver_count=Decimal("0"),
    )


def test_the_panel_reports_a_rejected_edit_when_given_outcomes():
    note = boards_screen.build_refresh_all_note(
        [_info(1, healthy=True)],
        [market_boards.BoardRefresh(
            1, True, market_boards.ACTION_SKIPPED_FORBIDDEN
        )],
    )
    assert "stale" in note
    assert "❌" in note


def test_the_panel_keeps_its_caveat_when_it_has_no_outcomes():
    """Backward compatibility: callers without outcomes still degrade."""
    note = boards_screen.build_refresh_all_note([_info(1, healthy=True)])
    assert "only appears in the bot log" in note


def test_the_panel_stops_hedging_when_every_edit_was_accepted():
    note = boards_screen.build_refresh_all_note(
        [_info(1, healthy=True)],
        [market_boards.BoardRefresh(1, True, market_boards.ACTION_EDITED)],
    )
    assert "only appears in the bot log" not in note
    assert "accepted" in note


def test_refresh_one_flags_a_posted_board_whose_update_was_refused():
    note = boards_screen.build_refresh_one_note(
        _info(3, healthy=True),
        board_id=3,
        outcome=market_boards.BoardRefresh(
            3, True, market_boards.ACTION_SKIPPED_FORBIDDEN
        ),
    )
    assert note.startswith("❌")
    assert "stale" in note
