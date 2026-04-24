"""
/roster command group.

All subcommands require Manage Server, enforced via default_permissions on the
group. Discord hides these commands from users who lack the permission, and
rejects invocations server-side even if someone bypasses the UI.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot import db, flow, queries

log = logging.getLogger(__name__)


class RosterCog(commands.Cog):
    roster = app_commands.Group(
        name="roster",
        description="Manage team rosters",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /roster list ──────────────────────────────────────────────────────────

    @roster.command(name="list", description="List all configured teams in this server")
    async def roster_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        async with db.connect() as conn:
            teams = await queries.fetch_all_teams(conn, interaction.guild_id)

        if not teams:
            await interaction.response.send_message(
                "No teams configured yet. Use `/roster create <name>` to add one.",
                ephemeral=True,
            )
            return

        lines: list[str] = []
        for team in teams:
            status = "✅" if team.message_id else "⚠️ (no roster message)"
            channel = f"<#{team.channel_id}>"
            lines.append(f"**{team.key}** — {team.name} · {channel} {status}")

        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )

    # ── /roster remove ────────────────────────────────────────────────────────

    @roster.command(name="remove", description="Delete a team config and its roster message")
    @app_commands.describe(name="Team identifier (e.g. redbull)")
    async def roster_remove(self, interaction: discord.Interaction, name: str) -> None:
        assert interaction.guild_id is not None
        async with db.connect() as conn:
            team = await queries.fetch_team(conn, interaction.guild_id, name)

        if team is None:
            await interaction.response.send_message(
                f"No team named `{name}` in this server.", ephemeral=True
            )
            return

        view = _ConfirmRemoveView(team_id=team.id, team_name=team.name, bot=self.bot)
        await interaction.response.send_message(
            f"Remove **{team.name}** (`{team.key}`) and delete its roster message?",
            view=view,
            ephemeral=True,
        )

    # ── /roster create & edit (stubs until step 5/6) ─────────────────────────

    @roster.command(name="create", description="Create a new team roster")
    @app_commands.describe(name="Team identifier (e.g. redbull)")
    async def roster_create(self, interaction: discord.Interaction, name: str) -> None:
        await flow.start_create(interaction, team_key=name.lower())

    @roster.command(name="edit", description="Edit an existing team roster")
    @app_commands.describe(name="Team identifier (e.g. redbull)")
    async def roster_edit(self, interaction: discord.Interaction, name: str) -> None:
        await flow.start_edit(interaction, team_key=name.lower())


class _ConfirmRemoveView(discord.ui.View):
    """Ephemeral confirmation for /roster remove."""

    def __init__(self, *, team_id: int, team_name: str, bot: commands.Bot) -> None:
        super().__init__(timeout=60)
        self._team_id = team_id
        self._team_name = team_name
        self._bot = bot

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        async with db.connect() as conn:
            team = await queries.fetch_team_by_id(conn, self._team_id)
            if team is None:
                await interaction.response.edit_message(
                    content="Team no longer exists.", view=None
                )
                return

            # Try to delete the roster message before removing the DB record.
            if team.message_id:
                try:
                    channel = self._bot.get_channel(team.channel_id)
                    if isinstance(channel, discord.TextChannel):
                        msg = await channel.fetch_message(team.message_id)
                        await msg.delete()
                except discord.NotFound:
                    pass  # already gone — fine
                except discord.Forbidden:
                    log.warning(
                        "No permission to delete roster message %s in channel %s",
                        team.message_id,
                        team.channel_id,
                    )

            await queries.delete_team(conn, self._team_id)
            await conn.commit()

        await interaction.response.edit_message(
            content=f"**{self._team_name}** removed.", view=None
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()

    async def on_timeout(self) -> None:
        # View has already been sent as ephemeral; we can't edit it after timeout
        # without a stored interaction reference. Silently expire.
        pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RosterCog(bot))
