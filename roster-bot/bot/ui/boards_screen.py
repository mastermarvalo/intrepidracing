"""
Board management as an interactive screen.

Boards are the auto-updating market embeds pinned in channels. Adding one
by command means remembering four arguments and which kinds need a tier;
this screen asks for kind, tier and channel in that order and refuses the
invalid combinations before they reach the database.

All mutations go through `bot.workflow`, the same functions
`/market-admin board add|remove|refresh` now call.
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


def build_boards_embed(boards: list[workflow.BoardInfo]) -> discord.Embed:
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

    unhealthy = [b for b in boards if not b.healthy]
    lines = []
    for b in boards:
        scope = f"tier `{b.tier_code}`" if b.tier_code else "cross-tier"
        mark = "" if b.healthy else " ⚠ not yet posted"
        lines.append(
            f"`{b.board_id}` · **{workflow.BOARD_KIND_LABELS.get(b.kind, b.kind)}** "
            f"· {scope} · <#{b.channel_id}>{mark}"
        )

    embed = discord.Embed(
        title="📊 Market boards",
        description=truncate_field("\n".join(lines)),
        color=COLOR_WARN if unhealthy else COLOR_OK,
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

        await self._flow.parent.reload(
            interaction,
            note=f"✅ Board `{board_id}` posted in <#{channel_id}>.",
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

    async def advance(self, interaction: discord.Interaction) -> None:
        assert self.kind is not None
        needs_tier = self.kind in workflow.TIER_SCOPED_BOARD_KINDS

        self.clear_items()
        if needs_tier and self.tier_code is None:
            tiers = await workflow.list_tier_choices(interaction.guild_id)
            if not tiers:
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


class _RemoveBoardSelect(discord.ui.Select):
    def __init__(self, boards: list[workflow.BoardInfo], parent: BoardsView) -> None:
        super().__init__(
            placeholder="Remove which board?",
            options=[
                discord.SelectOption(
                    label=(
                        f"#{b.board_id} "
                        f"{workflow.BOARD_KIND_LABELS.get(b.kind, b.kind)}"
                    )[:100],
                    value=str(b.board_id),
                    description=(
                        f"{'tier ' + b.tier_code if b.tier_code else 'cross-tier'}"
                    )[:100],
                )
                for b in boards[:SELECT_MAX_OPTIONS]
            ],
            disabled=not boards,
        )
        self._parent = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        board_id = int(self.values[0])
        try:
            await workflow.remove_board(interaction.client, board_id=board_id)
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        await self._parent.reload(
            interaction, note=f"✅ Board `{board_id}` removed."
        )


class BoardsView(AdminOwnedView):
    """List, add, remove and refresh boards."""

    def __init__(
        self,
        *,
        boards: list[workflow.BoardInfo],
        opener_id: int,
        on_back: BackCallback,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self._on_back = on_back
        self._boards = boards

        if boards:
            self.add_item(_RemoveBoardSelect(boards, self))

        self.add_item(_AddBoardButton(row=1))
        if boards:
            self.add_item(_RefreshBoardsButton(row=1))
        self.add_item(BackButton(on_back, row=1))

    async def reload(
        self, interaction: discord.Interaction, *, note: str | None = None
    ) -> None:
        boards = await workflow.list_boards(interaction.guild_id)
        view = BoardsView(
            boards=boards, opener_id=self.opener_id, on_back=self._on_back
        )
        embed = build_boards_embed(boards)

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
        if note:
            await interaction.followup.send(note, ephemeral=True)


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
            await workflow.refresh_boards(interaction.client, board_id=None)
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        await view.reload(interaction, note="✅ Refreshed every board.")


async def open_boards(
    interaction: discord.Interaction, *, opener_id: int, on_back: BackCallback
) -> None:
    """Entry point used by the Setup screen's Boards button."""
    boards = await workflow.list_boards(interaction.guild_id)
    view = BoardsView(boards=boards, opener_id=opener_id, on_back=on_back)
    await interaction.response.edit_message(
        embed=build_boards_embed(boards), view=view
    )
