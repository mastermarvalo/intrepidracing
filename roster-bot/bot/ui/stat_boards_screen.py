"""
Panel screen for Google Sheets live stat boards.

These are the boards that mirror an *external* Google Sheet and refresh
every ten minutes. They are not `bot/market/boards.py` market boards,
which the bot renders from its own data — the two are easy to confuse
and live on separate screens for that reason.

`/sheets add|remove|list|refresh` were the last admin commands with no
panel route at all. Rendering lives in `bot/sheet_boards.py` so this
screen and the command group drive identical code.
"""

from __future__ import annotations

import discord

from bot import sheet_boards, sheets
from bot.ui import base
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

#: How many boards fit one screen before the list is trimmed.
BOARDS_PER_SCREEN = SELECT_MAX_OPTIONS

_DEFAULT_RANGE = "Sheet1"


def build_stat_boards_embed(boards, note: str | None = None) -> discord.Embed:
    if not boards:
        embed = discord.Embed(
            title="📈 Stat boards",
            description=(
                "No stat boards yet.\n\n"
                "A stat board mirrors a Google Sheet into a Discord "
                "message and refreshes it every 10 minutes — useful for "
                "a championship table you already maintain in a "
                "spreadsheet.\n\n"
                "Press **Add board** to connect one."
            ),
            color=COLOR_INFO,
        )
        if note:
            embed.add_field(name="\u200b", value=note, inline=False)
        return embed

    lines = []
    for board in boards[:BOARDS_PER_SCREEN]:
        status = "✅ posted" if board.message_id else "⚠️ not posted yet"
        lines.append(
            f"**{board.title}** — <#{board.channel_id}> · "
            f"range `{board.sheet_range}` · {status}"
        )
    if len(boards) > BOARDS_PER_SCREEN:
        lines.append(f"…and {len(boards) - BOARDS_PER_SCREEN} more.")

    embed = discord.Embed(
        title="📈 Stat boards",
        description=truncate_field(
            "\n".join(lines), limit=base.EMBED_DESCRIPTION_LIMIT
        ),
        color=COLOR_INFO,
    ).set_footer(
        text=f"{len(boards)} board(s) · all refresh automatically every "
             "10 minutes"
    )
    if note:
        embed.add_field(name="\u200b", value=note, inline=False)
    return embed


def build_add_note(title: str, channel_id: int, *, fetch_failed: bool) -> str:
    """
    Honest reporting of what the add actually achieved (the G20 rule).

    A board whose first fetch failed is still created — the message is
    posted showing the error — so saying only "added" would be a lie.
    """
    if fetch_failed:
        return (
            f"⚠️ **{title}** was created in <#{channel_id}>, but the first "
            "read of the sheet failed. The posted message shows why. "
            "Check that the sheet is shared publicly (or with the bot's "
            "service account) and press **Refresh all**."
        )
    return (
        f"✅ **{title}** posted to <#{channel_id}> and will refresh every "
        "10 minutes."
    )


class StatBoardsView(AdminOwnedView):
    """List, add, remove and force-refresh Google Sheets stat boards."""

    def __init__(
        self, *, boards, opener_id: int, on_back: BackCallback
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.boards = boards
        self._on_back = on_back

        if boards:
            self.add_item(_RemoveStatBoardSelect(boards[:BOARDS_PER_SCREEN], self))
        self.add_item(_AddStatBoardButton(row=1))
        if boards:
            self.add_item(_RefreshStatBoardsButton(row=1))
        self.add_item(BackButton(on_back, row=1))

    async def reload(
        self, interaction: discord.Interaction, *, note: str | None = None
    ) -> None:
        boards = await sheet_boards.list_stat_boards(interaction.guild_id)
        view = StatBoardsView(
            boards=boards, opener_id=self.opener_id, on_back=self._on_back
        )
        embed = build_stat_boards_embed(boards)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
        await view.bind_message(interaction)
        if note:
            await interaction.followup.send(note, ephemeral=True)


class _AddStatBoardButton(discord.ui.Button):
    def __init__(self, *, row: int) -> None:
        super().__init__(
            label="Add board",
            style=discord.ButtonStyle.success,
            emoji="\u2795",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            _AddStatBoardModal(self.view)  # type: ignore[arg-type]
        )


class _AddStatBoardModal(base.PanelModal, title="Add a stat board (1/2)"):
    """
    Title, sheet and range first; the channel is picked next.

    Discord has no channel field inside a modal, so the channel needs a
    `ChannelSelect` on a following view. Validating the URL here means a
    typo is caught before the admin is asked where to put the board.
    """

    board_title = discord.ui.TextInput(
        label="Board title",
        placeholder="Drivers' Championship",
        max_length=100,
    )
    url = discord.ui.TextInput(
        label="Google Sheets URL",
        placeholder="https://docs.google.com/spreadsheets/d/…/edit",
        max_length=400,
    )
    sheet_range = discord.ui.TextInput(
        label="Tab or range",
        placeholder=_DEFAULT_RANGE,
        required=False,
        max_length=100,
    )

    def __init__(self, parent: StatBoardsView) -> None:
        super().__init__()
        self._owner = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        sheet_id = sheets.parse_sheet_id(str(self.url.value))
        if sheet_id is None:
            await interaction.response.send_message(
                "That does not look like a Google Sheets link. It should "
                "look like:\n"
                "`https://docs.google.com/spreadsheets/d/<ID>/edit…`",
                ephemeral=True,
            )
            return

        view = _PickStatBoardChannel(
            parent=self._owner,
            title=str(self.board_title.value).strip(),
            sheet_id=sheet_id,
            sheet_range=str(self.sheet_range.value).strip() or _DEFAULT_RANGE,
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="📈 Add a stat board (2/2)",
                description=(
                    f"**{self.board_title.value}**\n"
                    f"Range `{view.sheet_range}`\n\n"
                    "Where should it be posted?"
                ),
                color=COLOR_INFO,
            ),
            view=view,
            ephemeral=True,
        )


class _PickStatBoardChannel(AdminOwnedView):
    def __init__(
        self,
        *,
        parent: StatBoardsView,
        title: str,
        sheet_id: str,
        sheet_range: str,
    ) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.board_title = title
        self.sheet_id = sheet_id
        self.sheet_range = sheet_range
        self.add_item(_StatBoardChannelSelect(self))


class _StatBoardChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, flow: _PickStatBoardChannel) -> None:
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
            fetch_failed = await sheet_boards.create_stat_board(
                interaction.client,
                guild_id=interaction.guild_id,
                title=self._flow.board_title,
                sheet_id=self._flow.sheet_id,
                sheet_range=self._flow.sheet_range,
                channel_id=channel_id,
            )
        except Exception as exc:  # surfaced, never swallowed
            await report_error(
                interaction,
                f"Could not create the board: {exc}",
            )
            return
        await self._flow.parent.reload(
            interaction,
            note=build_add_note(
                self._flow.board_title, channel_id, fetch_failed=fetch_failed
            ),
        )


class _RemoveStatBoardSelect(discord.ui.Select):
    def __init__(self, boards, parent: StatBoardsView) -> None:
        super().__init__(
            placeholder="Remove a stat board…",
            options=[
                discord.SelectOption(
                    label=b.title[:100],
                    value=str(b.id),
                    description=f"range {b.sheet_range}"[:100],
                )
                for b in boards
            ],
            row=0,
        )
        self._owner = parent
        self._by_id = {str(b.id): b for b in boards}

    async def callback(self, interaction: discord.Interaction) -> None:
        board = self._by_id[self.values[0]]
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Remove this stat board?",
                description=(
                    f"**{board.title}** in <#{board.channel_id}>\n\n"
                    "Its Discord message is deleted too. The Google Sheet "
                    "itself is never touched."
                ),
                color=COLOR_WARN,
            ),
            view=_ConfirmRemoveStatBoard(self._owner, board),
            ephemeral=True,
        )


class _ConfirmRemoveStatBoard(AdminOwnedView):
    def __init__(self, parent: StatBoardsView, board) -> None:
        super().__init__(opener_id=parent.opener_id)
        self._owner = parent
        self._board = board

    @discord.ui.button(label="Remove board", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await sheet_boards.remove_stat_board(interaction.client, self._board.id)
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="Removed",
                description=f"**{self._board.title}** is gone.",
                color=COLOR_OK,
            ),
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Cancelled",
                description="Nothing was removed.",
                color=COLOR_INFO,
            ),
            view=None,
        )
        self.stop()


class _RefreshStatBoardsButton(discord.ui.Button):
    def __init__(self, *, row: int) -> None:
        super().__init__(
            label="Refresh all",
            style=discord.ButtonStyle.primary,
            emoji="\U0001f504",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        count = await sheet_boards.refresh_stat_boards(
            interaction.client, interaction.guild_id
        )
        # Deliberately says "re-read", not "updated": a board whose
        # channel is gone or whose sheet is unreadable is logged and
        # skipped inside the renderer, and claiming success for it would
        # repeat the bug G20 fixed on market boards.
        await self.view.reload(  # type: ignore[union-attr]
            interaction,
            note=(
                f"Re-read **{count}** stat board(s) from Google Sheets. "
                "Any board that could not be posted is listed above as "
                "not posted."
            ),
        )


async def open_stat_boards(
    interaction: discord.Interaction, *, opener_id: int, on_back: BackCallback
) -> None:
    """Entry point used by the Boards screen."""
    boards = await sheet_boards.list_stat_boards(interaction.guild_id)
    view = StatBoardsView(
        boards=boards, opener_id=opener_id, on_back=on_back
    )
    await interaction.response.edit_message(
        embed=build_stat_boards_embed(boards), view=view
    )
    await view.bind_message(interaction)
