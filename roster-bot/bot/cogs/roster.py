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

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

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

    # ── /roster create ────────────────────────────────────────────────────────

    @roster.command(name="create", description="Create a new team roster")
    @app_commands.describe(name="Team identifier (e.g. redbull)")
    async def roster_create(self, interaction: discord.Interaction, name: str) -> None:
        await flow.start_create(interaction, team_key=name.lower())

    # ── /roster edit ──────────────────────────────────────────────────────────

    @roster.command(name="edit", description="Edit an existing team roster")
    @app_commands.describe(name="Team identifier (e.g. redbull)")
    async def roster_edit(self, interaction: discord.Interaction, name: str) -> None:
        await flow.start_edit(interaction, team_key=name.lower())

    # ── /roster config ────────────────────────────────────────────────────────

    @roster.command(name="config", description="Configure roster settings for this server")
    async def roster_config(self, interaction: discord.Interaction) -> None:
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

    @roster.command(name="freeagents", description="List free agents who have a tier role")
    async def roster_freeagents(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None

        async with db.connect() as conn:
            config = await queries.fetch_guild_config(conn, interaction.guild_id)

        if not config.free_agent_role_id:
            await interaction.response.send_message(
                "No free agent role configured. Run `/roster config` first.", ephemeral=True
            )
            return

        fa_role = interaction.guild.get_role(config.free_agent_role_id)
        if fa_role is None:
            await interaction.response.send_message(
                "The configured free agent role no longer exists. "
                "Run `/roster config` to update it.",
                ephemeral=True,
            )
            return

        # Find members who have the free agent role AND at least one tier role.
        # Tier roles are detected by name starting with "Tier " (case-insensitive).
        tier_roles = {
            r for r in interaction.guild.roles
            if r.name.strip().lower().startswith("tier ")
        }

        # Build a map: tier role → list of free agent members who hold it
        by_tier: dict[discord.Role, list[discord.Member]] = {}
        for member in interaction.guild.members:
            if fa_role not in member.roles:
                continue
            for role in member.roles:
                if role in tier_roles:
                    by_tier.setdefault(role, []).append(member)

        if not by_tier:
            await interaction.response.send_message(
                "No free agents with tier roles found.", ephemeral=True
            )
            return

        # Sort tiers by the number in their name (e.g. "Tier 1", "Tier 2 🔥")
        def tier_sort_key(role: discord.Role) -> int:
            parts = role.name.strip().split()
            return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 999

        embed = discord.Embed(title="Free Agents", color=discord.Color.green())
        for role in sorted(by_tier, key=tier_sort_key):
            mentions = " ".join(m.mention for m in by_tier[role])
            embed.add_field(name=role.name, value=mentions, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── views ─────────────────────────────────────────────────────────────────────


class _SetFreeAgentView(discord.ui.View):
    def __init__(self, guild_id: int) -> None:
        super().__init__(timeout=120)
        self._guild_id = guild_id
        sel = discord.ui.RoleSelect(
            placeholder="Select the Free Agent role…", min_values=1, max_values=1
        )
        sel.callback = self._on_select
        self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        sel: discord.ui.RoleSelect = self.children[0]  # type: ignore[assignment]
        role = sel.values[0]
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
