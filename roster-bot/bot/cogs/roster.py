"""
/roster command group.

Admin commands (list, create, edit, remove, config) require Manage Server,
enforced in-handler. Sign/drop require Manage Server or the team's principal
role. View and freeagents are open to everyone.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot import db, flow, queries
from bot.render import build_embed, build_fa_embed

log = logging.getLogger(__name__)


def _is_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.manage_guild


def _has_role(interaction: discord.Interaction, role_id: int) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    return any(r.id == role_id for r in interaction.user.roles)


class RosterCog(commands.Cog):
    roster = app_commands.Group(
        name="roster",
        description="Manage team rosters",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /roster list ──────────────────────────────────────────────────────────

    @roster.command(name="list", description="List all configured teams in this server")
    async def roster_list(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "You need **Manage Server** to use this command.", ephemeral=True
            )
            return
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

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ── /roster remove ────────────────────────────────────────────────────────

    @roster.command(name="remove", description="Delete a team config and its roster message")
    @app_commands.describe(name="Team identifier (e.g. redbull)")
    async def roster_remove(self, interaction: discord.Interaction, name: str) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "You need **Manage Server** to use this command.", ephemeral=True
            )
            return
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

    # ── /roster create ────────────────────────────────────────────────────────

    @roster.command(name="create", description="Create a new team roster")
    @app_commands.describe(name="Team identifier (e.g. redbull)")
    async def roster_create(self, interaction: discord.Interaction, name: str) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "You need **Manage Server** to use this command.", ephemeral=True
            )
            return
        await flow.start_create(interaction, team_key=name.lower())

    # ── /roster edit ──────────────────────────────────────────────────────────

    @roster.command(name="edit", description="Edit an existing team roster")
    @app_commands.describe(name="Team identifier (e.g. redbull)")
    async def roster_edit(self, interaction: discord.Interaction, name: str) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "You need **Manage Server** to use this command.", ephemeral=True
            )
            return
        await flow.start_edit(interaction, team_key=name.lower())

    # ── /roster config ────────────────────────────────────────────────────────

    @roster.command(name="config", description="Configure roster settings for this server")
    async def roster_config(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "You need **Manage Server** to use this command.", ephemeral=True
            )
            return
        assert interaction.guild_id is not None
        async with db.connect() as conn:
            config = await queries.fetch_guild_config(conn, interaction.guild_id)

        current = (
            f"Current free agent role: <@&{config.free_agent_role_id}>"
            if config.free_agent_role_id
            else "No free agent role set yet."
        )
        view = _SetFreeAgentView(guild_id=interaction.guild_id)
        await interaction.response.send_message(
            f"**Roster Config**\n{current}\n\nPick the Free Agent role:",
            view=view,
            ephemeral=True,
        )

    # ── /roster view ──────────────────────────────────────────────────────────

    @roster.command(name="view", description="View a team's current roster")
    @app_commands.describe(name="Team identifier (e.g. redbull)")
    async def roster_view(self, interaction: discord.Interaction, name: str) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None

        async with db.connect() as conn:
            team = await queries.fetch_team(conn, interaction.guild_id, name.lower())

        if team is None:
            await interaction.response.send_message(
                f"No team named `{name}`.", ephemeral=True
            )
            return

        embed = build_embed(team, list(interaction.guild.members))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /roster sign ──────────────────────────────────────────────────────────

    @roster.command(name="sign", description="Sign a player to a team")
    @app_commands.describe(name="Team identifier (e.g. redbull)", member="Player to sign")
    async def roster_sign(
        self, interaction: discord.Interaction, name: str, member: discord.Member
    ) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None

        async with db.connect() as conn:
            team = await queries.fetch_team(conn, interaction.guild_id, name.lower())
            config = await queries.fetch_guild_config(conn, interaction.guild_id)

        if team is None:
            await interaction.response.send_message(f"No team named `{name}`.", ephemeral=True)
            return

        authorized = _is_admin(interaction) or (
            team.principal_role_id is not None and _has_role(interaction, team.principal_role_id)
        )
        if not authorized:
            await interaction.response.send_message(
                "You don't have permission to sign players for this team.", ephemeral=True
            )
            return

        team_role = interaction.guild.get_role(team.team_role_id)
        if team_role is None:
            await interaction.response.send_message(
                "Team role not found — the role may have been deleted.", ephemeral=True
            )
            return

        to_add = [team_role]
        to_remove: list[discord.Role] = []

        if config.free_agent_role_id:
            fa_role = interaction.guild.get_role(config.free_agent_role_id)
            if fa_role and fa_role in member.roles:
                to_remove.append(fa_role)

        try:
            await member.add_roles(*to_add, reason=f"Signed to {team.name} by {interaction.user}")
            if to_remove:
                await member.remove_roles(
                    *to_remove, reason=f"Signed to {team.name} by {interaction.user}"
                )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to manage roles. "
                "Make sure my role is above the team roles in Server Settings → Roles.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ {member.mention} signed to **{team.name}**.", ephemeral=True
        )

    # ── /roster drop ──────────────────────────────────────────────────────────

    @roster.command(name="drop", description="Drop a player from a team")
    @app_commands.describe(name="Team identifier (e.g. redbull)", member="Player to drop")
    async def roster_drop(
        self, interaction: discord.Interaction, name: str, member: discord.Member
    ) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None

        async with db.connect() as conn:
            team = await queries.fetch_team(conn, interaction.guild_id, name.lower())
            config = await queries.fetch_guild_config(conn, interaction.guild_id)

        if team is None:
            await interaction.response.send_message(f"No team named `{name}`.", ephemeral=True)
            return

        authorized = _is_admin(interaction) or (
            team.principal_role_id is not None and _has_role(interaction, team.principal_role_id)
        )
        if not authorized:
            await interaction.response.send_message(
                "You don't have permission to drop players from this team.", ephemeral=True
            )
            return

        team_role = interaction.guild.get_role(team.team_role_id)
        to_remove = [r for r in [team_role] if r and r in member.roles]
        to_add: list[discord.Role] = []

        if config.free_agent_role_id:
            fa_role = interaction.guild.get_role(config.free_agent_role_id)
            if fa_role and fa_role not in member.roles:
                to_add.append(fa_role)

        try:
            if to_remove:
                await member.remove_roles(
                    *to_remove, reason=f"Dropped from {team.name} by {interaction.user}"
                )
            if to_add:
                await member.add_roles(
                    *to_add, reason=f"Dropped from {team.name} by {interaction.user}"
                )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to manage roles. "
                "Make sure my role is above the team roles in Server Settings → Roles.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ {member.mention} dropped from **{team.name}**.", ephemeral=True
        )

    # ── /roster freeagents ────────────────────────────────────────────────────

    @roster.command(
        name="freeagents",
        description="View free agents, or post a live board to a channel",
    )
    @app_commands.describe(channel="Admin: post the live free-agent board here and keep it updated")
    async def roster_freeagents(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None

        async with db.connect() as conn:
            config = await queries.fetch_guild_config(conn, interaction.guild_id)

        if not config.free_agent_role_id:
            await interaction.response.send_message(
                "No free agent role configured. Run `/roster config` first.", ephemeral=True
            )
            return

        embed = build_fa_embed(interaction.guild, config.free_agent_role_id)

        if channel is not None:
            if not _is_admin(interaction):
                await interaction.response.send_message(
                    "You need **Manage Server** to post the free agent board.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            msg = await channel.send(embed=embed)
            async with db.connect() as conn:
                await queries.upsert_fa_channel(conn, interaction.guild_id, channel.id)
                await queries.set_fa_message_id(conn, interaction.guild_id, msg.id)
                await conn.commit()
            await interaction.followup.send(
                f"✅ Free agent board posted to {channel.mention} and will stay updated.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


# ── views ─────────────────────────────────────────────────────────────────────


class _SetFreeAgentView(discord.ui.View):
    def __init__(self, guild_id: int) -> None:
        super().__init__(timeout=120)
        self._guild_id = guild_id
        self._sel = discord.ui.RoleSelect(
            placeholder="Select the Free Agent role…", min_values=1, max_values=1
        )
        self._sel.callback = self._on_select
        self.add_item(self._sel)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        role = self._sel.values[0]
        async with db.connect() as conn:
            await queries.upsert_guild_config(conn, self._guild_id, role.id)
            await conn.commit()
        self.stop()
        await interaction.response.edit_message(
            content=f"✅ Free Agent role set to {role.mention}.", view=None
        )


class _ConfirmRemoveView(discord.ui.View):
    def __init__(self, *, team_id: int, team_name: str, bot: commands.Bot) -> None:
        super().__init__(timeout=60)
        self._team_id = team_id
        self._team_name = team_name
        self._bot = bot

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with db.connect() as conn:
            team = await queries.fetch_team_by_id(conn, self._team_id)
            if team is None:
                await interaction.response.edit_message(content="Team no longer exists.", view=None)
                return

            if team.message_id:
                try:
                    channel = self._bot.get_channel(team.channel_id)
                    if isinstance(channel, discord.TextChannel):
                        msg = await channel.fetch_message(team.message_id)
                        await msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass

            await queries.delete_team(conn, self._team_id)
            await conn.commit()

        await interaction.response.edit_message(
            content=f"**{self._team_name}** removed.", view=None
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()

    async def on_timeout(self) -> None:
        pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RosterCog(bot))
