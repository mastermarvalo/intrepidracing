"""
Board management as an interactive screen.

Boards are the auto-updating market embeds pinned in channels. Adding one
by command means remembering four arguments and which kinds need a tier;
this screen asks for kind, tier and channel in that order and refuses the
invalid combinations before they reach the database.

All mutations go through `bot.workflow`, the same functions
`/market-admin board add|remove|refresh` now call.

Reporting rule for this screen: `bot/market/boards.py` logs and returns
on a missing channel or a `discord.Forbidden`, so "the call returned"
never means "the embed is live". Every note below is therefore derived
from a *re-read* of board state after the call (`workflow.list_boards`,
whose `healthy` flag is `message_id IS NOT NULL`) rather than from the
absence of an exception. What that still cannot see is named in
`_EDIT_CAVEAT` — see the module gap note in
`tests/test_boards_history_screens.py`.
"""

from __future__ import annotations

import discord

from bot import workflow
from bot.ui.base import (
    COLOR_INFO,
    COLOR_OK,
    COLOR_WARN,
    SELECT_MAX_OPTIONS,
    AdminOwnedView,
    BackButton,
    BackCallback,
    report_error,
    truncate_field,
)

# One select page holds a full select's worth of boards.
BOARDS_PER_PAGE = SELECT_MAX_OPTIONS

# A follow-up note is a Discord message, so the per-board failure list is
# bounded; the remainder is still visible (and counted) in the list embed.
_MAX_LISTED_FAILURES = 5

_PERMISSION_HINT = "**Send Messages** + **Embed Links**"

_EDIT_CAVEAT = (
    "_Checked by re-reading each board's saved message. A board that is "
    "already posted but whose edit Discord rejected cannot be seen from "
    "here — that failure only appears in the bot log._"
)



def _kind_label(board: workflow.BoardInfo) -> str:
    return workflow.BOARD_KIND_LABELS.get(board.kind, board.kind)


def _scope_label(board: workflow.BoardInfo) -> str:
    return f"tier `{board.tier_code}`" if board.tier_code else "cross-tier"


def _failure_line(board: workflow.BoardInfo) -> str:
    return (
        f"`{board.board_id}` **{_kind_label(board)}** in "
        f"<#{board.channel_id}> — could not post; the bot needs "
        f"{_PERMISSION_HINT} there."
    )


def page_count(total: int, *, per_page: int = BOARDS_PER_PAGE) -> int:
    """Number of pages needed for `total` boards (at least one)."""
    if total <= 0:
        return 1
    return -(-total // per_page)


def build_boards_embed(
    boards: list[workflow.BoardInfo], *, page: int = 0
) -> discord.Embed:
    if not boards:
        return discord.Embed(
            title="📊 Market boards",
            description=(
                "No boards yet.\n\n"
                "A board is a message the bot keeps updated automatically "
                "after every valuation publish, plus a safety refresh every "
                "15 minutes. Add one with the button below."
            ),
            color=COLOR_INFO,
        )

    pages = page_count(len(boards))
    page = min(max(page, 0), pages - 1)
    start = page * BOARDS_PER_PAGE
    visible = boards[start : start + BOARDS_PER_PAGE]

    unhealthy = [b for b in boards if not b.healthy]
    lines = []
    for b in visible:
        mark = "" if b.healthy else " ⚠ not yet posted"
        lines.append(
            f"`{b.board_id}` · **{_kind_label(b)}** "
            f"· {_scope_label(b)} · <#{b.channel_id}>{mark}"
        )

    embed = discord.Embed(
        title="📊 Market boards",
        description=truncate_field("\n".join(lines)),
        color=COLOR_WARN if unhealthy else COLOR_OK,
    )
    if pages > 1:
        embed.add_field(
            name="Page",
            value=(
                f"Showing boards {start + 1}–{start + len(visible)} of "
                f"{len(boards)} (page {page + 1} of {pages}). The selects "
                "below act on this page only."
            ),
            inline=False,
        )
    if unhealthy:
        embed.add_field(
            name="⚠ Needs attention",
            value=(
                f"{len(unhealthy)} board(s) have no message yet. Usually this "
                "means the bot lacks **Send Messages** or **Embed Links** in "
                "the channel. Fix permissions, then press Refresh all."
            ),
            inline=False,
        )
    embed.set_footer(
        text="Equivalent commands: /market-admin board add · remove · refresh · list"
    )
    return embed


# ── honest outcome notes (G20) ───────────────────────────────────────


def build_added_board_note(
    board: workflow.BoardInfo | None, *, board_id: int, channel_id: int
) -> str:
    """
    What actually happened after `workflow.add_board`.

    `add_board` returns an id whether or not the embed reached the
    channel, so the caller re-reads the board and passes it here.
    """
    if board is None:
        return (
            f"⚠ Board `{board_id}` was created but is not in the board list "
            "any more — re-open **Boards** to check it."
        )
    if board.healthy:
        return (
            f"✅ Board `{board_id}` posted in <#{channel_id}>.\n"
            f"{_EDIT_CAVEAT}"
        )
    return (
        f"⚠ Board `{board_id}` was saved but **nothing was posted** in "
        f"<#{channel_id}> — the bot needs {_PERMISSION_HINT} there.\n"
        "Grant them, then press **Refresh all**. No message was sent and "
        "no other board changed."
    )


def build_refresh_all_note(
    boards: list[workflow.BoardInfo],
    outcomes: list[workflow.BoardRefresh] | None = None,
) -> str:
    """
    Per-board outcome for Refresh all.

    `boards` is the state afterwards. `outcomes` is what the refresh
    pass itself reported (G20) — supplying it lets this say "posted, but
    Discord rejected the edit", which board state alone cannot show. It
    stays optional so a caller without outcomes degrades to the old
    state-only report plus its caveat.
    """
    if not boards:
        return "No boards to refresh — nothing was posted or changed."

    posted = [b for b in boards if b.healthy]
    failed = [b for b in boards if not b.healthy]

    lines: list[str] = []
    if posted and not failed:
        lines.append(f"✅ All {len(posted)} board(s) are posted.")
    elif posted:
        lines.append(
            f"✅ {len(posted)} of {len(boards)} board(s) are posted."
        )
    if failed:
        lines.append(f"❌ {len(failed)} board(s) are still not posted:")
        for b in failed[:_MAX_LISTED_FAILURES]:
            lines.append(f"• {_failure_line(b)}")
        extra = len(failed) - _MAX_LISTED_FAILURES
        if extra > 0:
            lines.append(
                f"• …and {extra} more, marked ⚠ not yet posted in the list."
            )
    if outcomes is None:
        lines.append(_EDIT_CAVEAT)
        return "\n".join(lines)

    rejected = [
        o for o in outcomes
        if o.action in workflow.BOARD_FAILED_ACTIONS and o.posted
    ]
    if rejected:
        lines.append(
            f"❌ {len(rejected)} board(s) are posted but Discord refused "
            "the update, so they are showing stale numbers:"
        )
        for o in rejected[:_MAX_LISTED_FAILURES]:
            lines.append(f"• {workflow.describe_refresh(o)}")
        lines.append(f"Grant the bot {_PERMISSION_HINT} and refresh again.")
    elif not failed:
        lines.append("_Every board was rebuilt and Discord accepted each edit._")
    return "\n".join(lines)


def build_refresh_one_note(
    board: workflow.BoardInfo | None,
    *,
    board_id: int,
    outcome: workflow.BoardRefresh | None = None,
) -> str:
    if board is None:
        return (
            f"⚠ Board `{board_id}` is no longer in the board list — it may "
            "have been removed. Nothing was posted."
        )
    if board.healthy:
        if outcome is not None and outcome.action in workflow.BOARD_FAILED_ACTIONS:
            return (
                f"❌ Board `{board_id}` is posted in <#{board.channel_id}> "
                f"but the update was refused — "
                f"{workflow.boards_action_label(outcome.action)}. It is "
                f"showing stale numbers. Grant the bot {_PERMISSION_HINT} "
                "there and refresh again."
            )
        if outcome is not None:
            return (
                f"✅ Board `{board_id}` was "
                f"{workflow.boards_action_label(outcome.action)} in "
                f"<#{board.channel_id}>."
            )
        return (
            f"✅ Board `{board_id}` is posted in <#{board.channel_id}>.\n"
            f"{_EDIT_CAVEAT}"
        )
    return (
        f"❌ {_failure_line(board)}\n"
        "Nothing was posted and no other board changed.\n"
        f"{_EDIT_CAVEAT}"
    )


async def _reread(guild_id: int, board_id: int) -> workflow.BoardInfo | None:
    boards = await workflow.list_boards(guild_id)
    return next((b for b in boards if b.board_id == board_id), None)


class _KindSelect(discord.ui.Select):
    """Step 1 of adding a board: which kind."""

    def __init__(self, flow: _AddBoardFlow) -> None:
        super().__init__(
            placeholder="What kind of board?",
            options=[
                discord.SelectOption(
                    label=workflow.BOARD_KIND_LABELS[kind],
                    value=kind,
                    description=(
                        "Needs a tier"
                        if kind in workflow.TIER_SCOPED_BOARD_KINDS
                        else "Covers every tier at once"
                    ),
                )
                for kind in workflow.BOARD_KINDS
            ],
        )
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        self._flow.kind = self.values[0]
        await self._flow.advance(interaction)


class _TierSelect(discord.ui.Select):
    """Step 2, only shown for tier-scoped kinds."""

    def __init__(self, flow: _AddBoardFlow, tiers: list[tuple[str, str]]) -> None:
        super().__init__(
            placeholder="Which tier?",
            options=[
                discord.SelectOption(label=label, value=code)
                for code, label in tiers[:SELECT_MAX_OPTIONS]
            ],
            disabled=not tiers,
        )
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        self._flow.tier_code = self.values[0]
        await self._flow.advance(interaction)


class _BoardChannelSelect(discord.ui.ChannelSelect):
    """Final step: where it lives."""

    def __init__(self, flow: _AddBoardFlow) -> None:
        super().__init__(
            placeholder="Which channel?",
            channel_types=[discord.ChannelType.text],
            max_values=1,
        )
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel_id = self.values[0].id
        try:
            board_id = await workflow.add_board(
                interaction.client,
                guild_id=interaction.guild_id,
                kind=self._flow.kind,
                channel_id=channel_id,
                tier_code=self._flow.tier_code,
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return

        # `add_board` returns an id even when the send was refused, so ask
        # the database what the board looks like now instead of claiming a
        # post we have not seen (G20).
        added = await _reread(interaction.guild_id, board_id)
        await self._flow.parent.reload(
            interaction,
            note=build_added_board_note(
                added, board_id=board_id, channel_id=channel_id
            ),
        )


class _AddBoardFlow(AdminOwnedView):
    """
    A tiny three-step wizard held in one view.

    State lives on the view rather than in the component values because
    each step replaces the components; carrying the choice forward on the
    view is what lets the channel step still know the kind and tier.
    """

    def __init__(self, *, opener_id: int, parent: BoardsView) -> None:
        super().__init__(opener_id=opener_id)
        self.parent = parent
        self.kind: str | None = None
        self.tier_code: str | None = None
        self.add_item(_KindSelect(self))
        self.add_item(BackButton(self._back, label="Cancel", row=1))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)

    def _reset_to_kind_step(self) -> None:
        """Put the wizard back on step 1 after a dead-end branch."""
        self.clear_items()
        self.kind = None
        self.tier_code = None
        self.add_item(_KindSelect(self))
        self.add_item(BackButton(self._back, label="Cancel", row=1))


    async def advance(self, interaction: discord.Interaction) -> None:
        assert self.kind is not None
        needs_tier = self.kind in workflow.TIER_SCOPED_BOARD_KINDS

        self.clear_items()
        if needs_tier and self.tier_code is None:
            tiers = await workflow.list_tier_choices(interaction.guild_id)
            if not tiers:
                # The message still shows the components we just cleared,
                # so re-render step 1 before reporting; otherwise every
                # later click — Cancel included — hits a view with no
                # matching child and dies as "This interaction failed"
                # (G24).
                kind_label = workflow.BOARD_KIND_LABELS.get(self.kind, self.kind)
                self._reset_to_kind_step()
                embed = discord.Embed(
                    title="📊 Add a board",
                    description=(
                        "**No tiers in the active season yet.**\n\n"
                        f"A **{kind_label}** "
                        "board is tier-scoped, so it needs a tier to point "
                        "at. Add one in **Setup → Tiers** (or "
                        "`/market-admin tier add`), then come back.\n\n"
                        "Nothing was created. Pick a cross-tier kind below, "
                        "or press Cancel."
                    ),
                    color=COLOR_WARN,
                )
                await interaction.response.edit_message(embed=embed, view=self)
                await self.bind_message(interaction)
                await report_error(
                    interaction,
                    "No tiers in the active season yet. Add a tier in Setup first.",
                )
                return
            self.add_item(_TierSelect(self, tiers))
            step = "Step 2 of 3 — pick the tier."
        else:
            self.add_item(_BoardChannelSelect(self))
            step = "Final step — pick the channel."

        self.add_item(BackButton(self._back, label="Cancel", row=1))

        scope = f" · tier `{self.tier_code}`" if self.tier_code else ""
        embed = discord.Embed(
            title="📊 Add a board",
            description=(
                f"**{workflow.BOARD_KIND_LABELS.get(self.kind, self.kind)}**"
                f"{scope}\n\n{step}"
            ),
            color=COLOR_INFO,
        )
        await interaction.response.edit_message(embed=embed, view=self)
        await self.bind_message(interaction)


class _RemoveBoardSelect(discord.ui.Select):
    def __init__(self, boards: list[workflow.BoardInfo], parent: BoardsView) -> None:
        super().__init__(
            placeholder="Remove which board?",
            options=[
                discord.SelectOption(
                    label=(f"#{b.board_id} {_kind_label(b)}")[:100],
                    value=str(b.board_id),
                    description=_scope_label(b)[:100],
                )
                for b in boards
            ],
            disabled=not boards,
        )
        self._owner = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        board_id = int(self.values[0])
        board = next(
            (b for b in self._owner.boards if b.board_id == board_id), None
        )
        # Removal deletes the posted message and cannot be undone, so it
        # gets a confirm step that restates the consequence.
        view = _ConfirmRemoveView(
            opener_id=self._owner.opener_id, parent=self._owner, board_id=board_id
        )
        await interaction.response.edit_message(
            embed=build_remove_confirm_embed(board, board_id=board_id), view=view
        )
        await view.bind_message(interaction)


def build_remove_confirm_embed(
    board: workflow.BoardInfo | None, *, board_id: int
) -> discord.Embed:
    where = (
        f"<#{board.channel_id}>" if board is not None else "its channel"
    )
    what = (
        f"**{_kind_label(board)}** · {_scope_label(board)}"
        if board is not None
        else "this board"
    )
    posted = (
        "Its posted message will be deleted too."
        if board is not None and board.healthy
        else "It has no posted message, so only the row goes."
    )
    return discord.Embed(
        title="🗑 Remove this board?",
        description=(
            f"Board `{board_id}` — {what} in {where}.\n\n"
            f"**This cannot be undone.** {posted} Re-adding it later posts a "
            "brand-new message.\n\n"
            "No valuation, contract or driver data is touched, and no other "
            "board changes."
        ),
        color=COLOR_WARN,
    )


class _ConfirmRemoveView(AdminOwnedView):
    """The second click for an irreversible board removal."""

    def __init__(
        self, *, opener_id: int, parent: BoardsView, board_id: int
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.parent = parent
        self.board_id = board_id
        self.add_item(_ConfirmRemoveButton())
        self.add_item(BackButton(self._back, label="Cancel"))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)


class _ConfirmRemoveButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Remove board",
            style=discord.ButtonStyle.danger,
            emoji="🗑",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        view = self.view
        assert isinstance(view, _ConfirmRemoveView)
        board_id = view.board_id
        try:
            await workflow.remove_board(interaction.client, board_id=board_id)
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        await view.parent.reload(
            interaction,
            note=(
                f"✅ Board `{board_id}` removed, and its message deleted if "
                "it still existed. Nothing else changed — no other board, "
                "and no valuation or contract data."
            ),
        )


class BoardsView(AdminOwnedView):
    """List, add, remove and refresh boards."""

    def __init__(
        self,
        *,
        boards: list[workflow.BoardInfo],
        opener_id: int,
        on_back: BackCallback,
        page: int = 0,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self._on_back = on_back
        self.boards = boards

        pages = page_count(len(boards))
        self.page = min(max(page, 0), pages - 1)
        start = self.page * BOARDS_PER_PAGE
        visible = boards[start : start + BOARDS_PER_PAGE]
        self.visible = visible

        if visible:
            self.add_item(_RemoveBoardSelect(visible, self))
            self.add_item(_RefreshOneSelect(visible, self))

        self.add_item(_AddBoardButton(row=2))
        if boards:
            self.add_item(_RefreshBoardsButton(row=2))
        self.add_item(_StatBoardsButton(row=2))
        self.add_item(BackButton(on_back, row=2))

        if pages > 1:
            self.add_item(_PageButton(delta=-1, enabled=self.page > 0, row=3))
            self.add_item(
                _PageButton(delta=1, enabled=self.page + 1 < pages, row=3)
            )

    async def reload(
        self,
        interaction: discord.Interaction,
        *,
        note: str | None = None,
        page: int | None = None,
    ) -> None:
        boards = await workflow.list_boards(interaction.guild_id)
        view = BoardsView(
            boards=boards,
            opener_id=self.opener_id,
            on_back=self._on_back,
            page=self.page if page is None else page,
        )
        embed = build_boards_embed(boards, page=view.page)

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
        await view.bind_message(interaction)
        if note:
            await interaction.followup.send(note, ephemeral=True)


class _PageButton(discord.ui.Button):
    """Page the board selects rather than silently dropping boards 26+."""

    def __init__(self, *, delta: int, enabled: bool, row: int) -> None:
        super().__init__(
            label="Previous page" if delta < 0 else "Next page",
            style=discord.ButtonStyle.secondary,
            emoji="◀" if delta < 0 else "▶",
            disabled=not enabled,
            row=row,
        )
        self._delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, BoardsView)
        await view.reload(interaction, page=view.page + self._delta)


class _AddBoardButton(discord.ui.Button):
    def __init__(self, *, row: int) -> None:
        super().__init__(
            label="Add board", style=discord.ButtonStyle.success, emoji="➕", row=row
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, BoardsView)
        flow = _AddBoardFlow(opener_id=view.opener_id, parent=view)
        embed = discord.Embed(
            title="📊 Add a board",
            description="Step 1 of 3 — pick the kind of board.",
            color=COLOR_INFO,
        )
        await interaction.response.edit_message(embed=embed, view=flow)
        await flow.bind_message(interaction)


class _RefreshBoardsButton(discord.ui.Button):
    def __init__(self, *, row: int) -> None:
        super().__init__(
            label="Refresh all",
            style=discord.ButtonStyle.secondary,
            emoji="🔄",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        view = self.view
        assert isinstance(view, BoardsView)
        try:
            outcomes = await workflow.refresh_boards(
                interaction.client, board_id=None
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        # Board state afterwards plus what the pass itself reported: the
        # second half is what makes a rejected edit visible (G20).
        boards = await workflow.list_boards(interaction.guild_id)
        await view.reload(
            interaction, note=build_refresh_all_note(boards, outcomes)
        )


class _RefreshOneSelect(discord.ui.Select):
    """
    Re-render one board in place.

    Useful when a single board is showing stale content (e.g. bot lost
    the message, admin deleted it) and re-posting just the one is
    quicker than refreshing every board.
    """

    def __init__(self, boards: list[workflow.BoardInfo], parent: BoardsView) -> None:
        super().__init__(
            placeholder="Refresh one board…",
            options=[
                discord.SelectOption(
                    label=(f"#{b.board_id} {_kind_label(b)}")[:100],
                    value=str(b.board_id),
                    description=_scope_label(b)[:100],
                )
                for b in boards
            ],
            disabled=not boards,
        )
        self._owner = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        board_id = int(self.values[0])
        try:
            outcomes = await workflow.refresh_boards(
                interaction.client, board_id=board_id
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        board = await _reread(interaction.guild_id, board_id)
        await self._owner.reload(
            interaction,
            note=build_refresh_one_note(
                board,
                board_id=board_id,
                outcome=outcomes[0] if outcomes else None,
            ),
        )


async def open_boards(
    interaction: discord.Interaction, *, opener_id: int, on_back: BackCallback
) -> None:
    """Entry point used by the Setup screen's Boards button."""
    boards = await workflow.list_boards(interaction.guild_id)
    view = BoardsView(boards=boards, opener_id=opener_id, on_back=on_back)
    await interaction.response.edit_message(
        embed=build_boards_embed(boards), view=view
    )
    await view.bind_message(interaction)


class _StatBoardsButton(discord.ui.Button):
    """
    Route to the *other* kind of board.

    Market boards are rendered by the bot from its own data. Stat boards
    mirror an external Google Sheet. Both used to be called "boards",
    and only one of them had a panel at all.
    """

    def __init__(self, *, row: int) -> None:
        super().__init__(
            label="Google Sheets boards",
            style=discord.ButtonStyle.secondary,
            emoji="\U0001f4c8",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        from bot.ui import stat_boards_screen

        parent = self.view
        assert parent is not None

        async def _back(inner: discord.Interaction) -> None:
            await parent.reload(inner)

        await stat_boards_screen.open_stat_boards(
            interaction, opener_id=parent.opener_id, on_back=_back
        )
