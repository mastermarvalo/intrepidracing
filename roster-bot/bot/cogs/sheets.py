"""
/sheets command group and background poll loop for Google Sheets stat boards.

Admin commands (add, remove, list, refresh) require Manage Server.
The poll loop re-renders all boards every 10 minutes.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot import db, queries, sheets

log = logging.getLogger(__name__)


def _is_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.manage_guild


async def _rerender_board(bot: commands.Bot, board) -> None:  # type: ignore[type-arg]
    """Fetch fresh sheet data, rebuild the embed, and edit the pinned message.

    If the message was deleted (or never posted), re-posts it so the board is self-healing.
    """
    embed = await sheets.fetch_and_build_embed(board.title, board.sheet_id, board.sheet_range)

    channel = bot.get_channel(board.channel_id)
    if not isinstance(channel, discord.TextChannel):
        log.warning("Stat board %r: channel %s unavailable", board.title, board.channel_id)
        return

    if board.message_id is not None:
        try:
            msg = await channel.fetch_message(board.message_id)
            await msg.edit(embed=embed)
            return
        except discord.NotFound:
            log.info("Stat board %r: message was deleted — re-posting", board.title)
        except discord.Forbidden:
            log.warning(
                "Stat board %r: no permission to edit message in channel %s",
                board.title,
                board.channel_id,
            )
            return

    # message_id is None or the message was deleted — re-post
    try:
        msg = await channel.send(embed=embed)
    except discord.Forbidden:
        log.warning(
            "Stat board %r: no permission to post in channel %s",
            board.title,
            board.channel_id,
        )
        return
    async with db.connect() as conn:
        await queries.set_stat_board_message_id(conn, board.id, msg.id)
        await conn.commit()
    log.info("Stat board %r: re-posted as message %s", board.title, msg.id)


class SheetsCog(commands.Cog):
    sheets_group = app_commands.Group(
        name="sheets",
        description="Manage Google Sheets live stat boards",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.poll_loop.start()

    def cog_unload(self) -> None:
        self.poll_loop.cancel()

    # ── /sheets add ───────────────────────────────────────────────────────────

    @sheets_group.command(name="add", description="Add a Google Sheets live stat board")
    @app_commands.describe(
        title="Display title for the board",
        url="Google Sheets URL",
        channel="Channel to post the board in",
        range="Sheet tab / range (default: Sheet1)",
    )
    async def sheets_add(
        self,
        interaction: discord.Interaction,
        title: str,
        url: str,
        channel: discord.TextChannel,
        range: str = "Sheet1",
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "You need **Manage Server** to use this command.", ephemeral=True
            )
            return
        assert interaction.guild is not None and interaction.guild_id is not None

        sheet_id = sheets.parse_sheet_id(url)
        if sheet_id is None:
            await interaction.response.send_message(
                "Couldn't extract a spreadsheet ID from that URL.\n"
                "Make sure it looks like:\n"
                "`https://docs.google.com/spreadsheets/d/<ID>/edit…`",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        embed = await sheets.fetch_and_build_embed(title, sheet_id, range)
        fetch_failed = embed.color == discord.Color.red()

        msg = await channel.send(embed=embed)

        async with db.connect() as conn:
            board_id = await queries.insert_stat_board(
                conn, interaction.guild_id, title, sheet_id, range, channel.id
            )
            await queries.set_stat_board_message_id(conn, board_id, msg.id)
            await conn.commit()

        if fetch_failed:
            await interaction.followup.send(
                f"Board **{title}** created in {channel.mention}, but the initial fetch failed "
                "(see the posted embed for details). Fix the URL or API key and use "
                "`/sheets refresh` when ready.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Stat board **{title}** posted to {channel.mention} and will update every 10 minutes.",
                ephemeral=True,
            )

    # ── /sheets remove ────────────────────────────────────────────────────────

    @sheets_group.command(name="remove", description="Remove a stat board")
    @app_commands.describe(name="Title of the board to remove")
    async def sheets_remove(self, interaction: discord.Interaction, name: str) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "You need **Manage Server** to use this command.", ephemeral=True
            )
            return
        assert interaction.guild_id is not None

        async with db.connect() as conn:
            board = await queries.fetch_stat_board(conn, interaction.guild_id, name)

        if board is None:
            await interaction.response.send_message(
                f"No stat board named `{name}` in this server.", ephemeral=True
            )
            return

        view = _ConfirmRemoveBoardView(board_id=board.id, board_title=board.title, bot=self.bot)
        await interaction.response.send_message(
            f"Remove stat board **{board.title}** and delete its Discord message?",
            view=view,
            ephemeral=True,
        )

    @sheets_remove.autocomplete("name")
    async def _remove_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not interaction.guild_id:
            return []
        async with db.connect() as conn:
            boards = await queries.fetch_all_stat_boards(conn, interaction.guild_id)
        return [
            app_commands.Choice(name=b.title, value=b.title)
            for b in boards
            if current.lower() in b.title.lower()
        ][:25]

    # ── /sheets list ──────────────────────────────────────────────────────────

    @sheets_group.command(name="list", description="List all configured stat boards in this server")
    async def sheets_list(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "You need **Manage Server** to use this command.", ephemeral=True
            )
            return
        assert interaction.guild_id is not None

        async with db.connect() as conn:
            boards = await queries.fetch_all_stat_boards(conn, interaction.guild_id)

        if not boards:
            await interaction.response.send_message(
                "No stat boards configured. Use `/sheets add` to create one.", ephemeral=True
            )
            return

        lines: list[str] = []
        for board in boards:
            status = "✅" if board.message_id else "⚠️ (no message)"
            lines.append(
                f"**{board.title}** — <#{board.channel_id}> · "
                f"`{board.sheet_id}` · range `{board.sheet_range}` {status}"
            )

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ── /sheets refresh ───────────────────────────────────────────────────────

    @sheets_group.command(name="refresh", description="Force-refresh all stat boards in this server")
    async def sheets_refresh(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "You need **Manage Server** to use this command.", ephemeral=True
            )
            return
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)

        async with db.connect() as conn:
            boards = await queries.fetch_all_stat_boards(conn, interaction.guild_id)

        for board in boards:
            await _rerender_board(self.bot, board)

        await interaction.followup.send(
            f"Refreshed **{len(boards)}** stat board(s).", ephemeral=True
        )

    # ── 10-minute poll loop ───────────────────────────────────────────────────

    @tasks.loop(minutes=10)
    async def poll_loop(self) -> None:
        log.debug("Sheets poll: refreshing all stat boards")
        for guild in self.bot.guilds:
            async with db.connect() as conn:
                boards = await queries.fetch_all_stat_boards(conn, guild.id)
            for board in boards:
                await _rerender_board(self.bot, board)
        log.debug("Sheets poll: done")

    @poll_loop.before_loop
    async def before_poll(self) -> None:
        await self.bot.wait_until_ready()


# ── views ─────────────────────────────────────────────────────────────────────


class _ConfirmRemoveBoardView(discord.ui.View):
    def __init__(self, *, board_id: int, board_title: str, bot: commands.Bot) -> None:
        super().__init__(timeout=60)
        self._board_id = board_id
        self._board_title = board_title
        self._bot = bot

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with db.connect() as conn:
            board = await queries.fetch_stat_board_by_id(conn, self._board_id)
            if board is None:
                await interaction.response.edit_message(
                    content="Board no longer exists.", view=None
                )
                return

            if board.message_id:
                try:
                    channel = self._bot.get_channel(board.channel_id)
                    if isinstance(channel, discord.TextChannel):
                        msg = await channel.fetch_message(board.message_id)
                        await msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass

            await queries.delete_stat_board(conn, self._board_id)
            await conn.commit()

        await interaction.response.edit_message(
            content=f"Stat board **{self._board_title}** removed.", view=None
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()

    async def on_timeout(self) -> None:
        pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SheetsCog(bot))
