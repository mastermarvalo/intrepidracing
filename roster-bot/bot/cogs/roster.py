"""
/roster command group.

Admin commands (list, create, edit, remove, config) require Manage Server,
enforced in-handler. Sign/drop require Manage Server or the team's principal
role. View and freeagents are open to everyone.
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot import db, flow, queries, roster_ops
from bot.events import _rerender, _rerender_fa
from bot.render import (
    build_avatar_card,
    build_fa_embed,
    build_flair_embed,
    build_roster_embeds,
    roster_flair_file,
)

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
    @app_commands.describe(name="Team identifier (e.g. red bull)")
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
    @app_commands.describe(name="Team identifier (e.g. red bull)")
    async def roster_create(self, interaction: discord.Interaction, name: str) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "You need **Manage Server** to use this command.", ephemeral=True
            )
            return
        await flow.start_create(interaction, team_key=name.lower())

    # ── /roster edit ──────────────────────────────────────────────────────────

    @roster.command(name="edit", description="Edit an existing team roster")
    @app_commands.describe(name="Team identifier (e.g. red bull)")
    async def roster_edit(self, interaction: discord.Interaction, name: str) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "You need **Manage Server** to use this command.", ephemeral=True
            )
            return
        await flow.start_edit(interaction, team_key=name.lower())

    # ── /roster relink ────────────────────────────────────────────────────────

    @roster.command(
        name="relink",
        description="Emergency recovery: re-register a team whose DB record was lost",
    )
    @app_commands.describe(name="Team identifier (e.g. red bull)")
    async def roster_relink(self, interaction: discord.Interaction, name: str) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "You need **Manage Server** to use this command.", ephemeral=True
            )
            return
        await flow.start_relink(interaction, team_key=name.lower())

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

        fa_line = (
            f"Free Agent role: <@&{config.free_agent_role_id}>"
            if config.free_agent_role_id
            else "Free Agent role: *not set*"
        )
        tx_line = (
            f"Transactions channel: <#{config.transactions_channel_id}>"
            if config.transactions_channel_id
            else "Transactions channel: *not set*"
        )
        view = _ConfigView(guild_id=interaction.guild_id)
        await interaction.response.send_message(
            f"**Roster Config**\n{fa_line}\n{tx_line}",
            view=view,
            ephemeral=True,
        )

    # ── /roster view ──────────────────────────────────────────────────────────

    @roster.command(name="view", description="View a team's current roster")
    @app_commands.describe(name="Team identifier (e.g. red bull)")
    async def roster_view(self, interaction: discord.Interaction, name: str) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None

        async with db.connect() as conn:
            team = await queries.fetch_team(conn, interaction.guild_id, name.lower())

        if team is None:
            await interaction.response.send_message(
                f"No team named `{name}`.", ephemeral=True
            )
            return

        flair = roster_flair_file(team)
        embeds = [build_flair_embed(team.color)] + build_roster_embeds(
            team, list(interaction.guild.members)
        )
        await interaction.response.send_message(embeds=embeds, file=flair, ephemeral=True)

    # ── /roster sign ──────────────────────────────────────────────────────────

    @roster.command(name="sign", description="Sign a player to a team")
    @app_commands.describe(name="Team identifier (e.g. red bull)", member="Player to sign")
    async def roster_sign(
        self, interaction: discord.Interaction, name: str, member: discord.Member
    ) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None

        async with db.connect() as conn:
            team = await queries.fetch_team(conn, interaction.guild_id, name.lower())

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

        try:
            await roster_ops.sign_to_team(
                guild=interaction.guild,
                member=member,
                team=team,
                actor=interaction.user,
            )
        except roster_ops.RoleAssignmentError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(
            f"✅ **{member.display_name}** signed to **{team.name}**.", ephemeral=True
        )

    # ── /roster drop ──────────────────────────────────────────────────────────

    @roster.command(name="drop", description="Drop a player from a team")
    @app_commands.describe(name="Team identifier (e.g. red bull)", member="Player to drop")
    async def roster_drop(
        self, interaction: discord.Interaction, name: str, member: discord.Member
    ) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None

        async with db.connect() as conn:
            team = await queries.fetch_team(conn, interaction.guild_id, name.lower())

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

        try:
            await roster_ops.drop_from_team(
                guild=interaction.guild,
                member=member,
                team=team,
                actor=interaction.user,
            )
        except roster_ops.RoleAssignmentError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(
            f"✅ **{member.display_name}** dropped from **{team.name}**.", ephemeral=True
        )

    # ── /roster graphic ───────────────────────────────────────────────────────

    @roster.command(name="graphic", description="Generate the avatar card graphic for a team")
    @app_commands.describe(
        name="Team identifier (e.g. red bull)",
        slot="Show only one slot group (optional)",
    )
    async def roster_graphic(
        self,
        interaction: discord.Interaction,
        name: str,
        slot: str | None = None,
    ) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None

        async with db.connect() as conn:
            team = await queries.fetch_team(conn, interaction.guild_id, name.lower())

        if team is None:
            await interaction.response.send_message(f"No team named `{name}`.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        avatar_file = await build_avatar_card(
            team, list(interaction.guild.members), slot_filter=slot
        )
        if avatar_file is None:
            msg = (
                f"No members found in slot **{slot}** for **{team.name}**."
                if slot
                else f"**{team.name}** has no members assigned to slots yet."
            )
            await interaction.followup.send(msg, ephemeral=True)
            return
        await interaction.followup.send(file=avatar_file, ephemeral=True)

    @roster_graphic.autocomplete("slot")
    async def _graphic_slot_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        assert interaction.guild_id is not None
        # Pull team name from the already-typed options
        opts = interaction.data.get("options", [])  # type: ignore[union-attr]
        name_val = next(
            (o["value"] for o in opts if o.get("name") == "name"), None
        )
        if not name_val:
            return []
        async with db.connect() as conn:
            team = await queries.fetch_team(conn, interaction.guild_id, str(name_val).lower())
        if team is None:
            return []
        labels = [s.label for s in team.slots if current.lower() in s.label.lower()]
        return [app_commands.Choice(name=label, value=label) for label in labels[:25]]

    # ── /roster bulksign / bulkdrop ───────────────────────────────────────────

    @roster.command(name="bulksign", description="Sign multiple players to a team at once")
    @app_commands.describe(name="Team identifier (e.g. red bull)")
    async def roster_bulksign(self, interaction: discord.Interaction, name: str) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None

        async with db.connect() as conn:
            team = await queries.fetch_team(conn, interaction.guild_id, name.lower())

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

        view = _BulkSignDropView(team, drop=False)
        await interaction.response.send_message(
            f"**Bulk sign → {team.name}**\nSelect up to 25 players, then click **Sign**.",
            view=view,
            ephemeral=True,
        )

    @roster.command(name="bulkdrop", description="Drop multiple players from a team at once")
    @app_commands.describe(name="Team identifier (e.g. red bull)")
    async def roster_bulkdrop(self, interaction: discord.Interaction, name: str) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None

        async with db.connect() as conn:
            team = await queries.fetch_team(conn, interaction.guild_id, name.lower())

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

        view = _BulkSignDropView(team, drop=True)
        await interaction.response.send_message(
            f"**Bulk drop ← {team.name}**\nSelect up to 25 players, then click **Drop**.",
            view=view,
            ephemeral=True,
        )

    # ── /roster history ───────────────────────────────────────────────────────

    @roster.command(name="history", description="Show recent sign/drop history for a team")
    @app_commands.describe(
        name="Team identifier (e.g. red bull)",
        limit="Entries to show (default 20, max 50)",
    )
    async def roster_history(
        self, interaction: discord.Interaction, name: str, limit: int = 20
    ) -> None:
        assert interaction.guild_id is not None
        limit = max(1, min(limit, 50))

        async with db.connect() as conn:
            team = await queries.fetch_team(conn, interaction.guild_id, name.lower())
            if team is None:
                await interaction.response.send_message(
                    f"No team named `{name}`.", ephemeral=True
                )
                return
            rows = await queries.fetch_transactions(conn, interaction.guild_id, team.id, limit)

        if not rows:
            await interaction.response.send_message(
                f"No transaction history for **{team.name}** yet.", ephemeral=True
            )
            return

        lines: list[str] = []
        for row in rows:
            icon = "✅" if row["action"] == "signed" else "🔴"
            verb = "signed" if row["action"] == "signed" else "dropped"
            # asyncpg returns TIMESTAMPTZ as a tz-aware datetime
            ts = f"<t:{int(row['created_at'].timestamp())}:D>"
            lines.append(f"{icon} **{row['member_name']}** {verb} — {ts}")

        embed = discord.Embed(
            title=f"Transaction History — {team.name}",
            description="\n".join(lines),
            color=discord.Color(team.color) if team.color else discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

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
            await interaction.followup.send(
                f"✅ Free agent board posted to {channel.mention} and will stay updated.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /roster refresh ───────────────────────────────────────────────────────

    @roster.command(name="refresh", description="Force-refresh all roster embeds in this server")
    async def roster_refresh(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "You need **Manage Server** to use this command.", ephemeral=True
            )
            return
        assert interaction.guild is not None and interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)

        async with db.connect() as conn:
            teams = await queries.fetch_all_teams(conn, interaction.guild_id)
            config = await queries.fetch_guild_config(conn, interaction.guild_id)

        for team in teams:
            await _rerender(self.bot, interaction.guild, team)
        await _rerender_fa(self.bot, interaction.guild, config)

        await interaction.followup.send(
            f"✅ Refreshed **{len(teams)}** roster(s).", ephemeral=True
        )

    # ── /roster dm ────────────────────────────────────────────────────────────

    @roster.command(name="dm", description="Send a DM to all members with a specific role")
    @app_commands.describe(role="Role whose members will be DM'd", message="Message to send")
    async def roster_dm(
        self, interaction: discord.Interaction, role: discord.Role, message: str
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "You need **Manage Server** to use this command.", ephemeral=True
            )
            return

        members = [m for m in role.members if not m.bot]
        if not members:
            await interaction.response.send_message(
                f"No non-bot members have the {role.mention} role.", ephemeral=True
            )
            return

        preview = message if len(message) <= 500 else message[:497] + "…"
        view = _ConfirmDMView(role=role, message=message, members=members)
        await interaction.response.send_message(
            f"Send the following DM to **{len(members)} member(s)** with "
            f"{role.mention}?\n\n>>> {preview}",
            view=view,
            ephemeral=True,
        )


# ── views ─────────────────────────────────────────────────────────────────────


class _ConfigView(discord.ui.View):
    def __init__(self, guild_id: int) -> None:
        super().__init__(timeout=120)
        self._guild_id = guild_id

        self._role_sel = discord.ui.RoleSelect(
            placeholder="Change Free Agent role…", min_values=1, max_values=1
        )
        self._role_sel.callback = self._on_role
        self.add_item(self._role_sel)

        self._chan_sel = discord.ui.ChannelSelect(
            placeholder="Change transactions channel…",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        self._chan_sel.callback = self._on_channel
        self.add_item(self._chan_sel)

    async def _on_role(self, interaction: discord.Interaction) -> None:
        role = self._role_sel.values[0]
        async with db.connect() as conn:
            await queries.upsert_guild_config(conn, self._guild_id, role.id)
        await interaction.response.edit_message(
            content=f"✅ Free Agent role set to {role.mention}.",
        )

    async def _on_channel(self, interaction: discord.Interaction) -> None:
        channel = self._chan_sel.values[0]
        async with db.connect() as conn:
            await queries.upsert_transactions_channel(conn, self._guild_id, channel.id)
        await interaction.response.edit_message(
            content=f"✅ Transactions channel set to {channel.mention}.",
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


class _ConfirmDMView(discord.ui.View):
    def __init__(
        self, *, role: discord.Role, message: str, members: list[discord.Member]
    ) -> None:
        super().__init__(timeout=60)
        self._role = role
        self._message = message
        self._members = members

    @discord.ui.button(label="Send", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        total = len(self._members)
        await interaction.response.edit_message(
            content=f"Sending DMs… (0/{total})", view=None
        )

        sent = 0
        failed = 0
        loop = asyncio.get_running_loop()
        last_edit = loop.time()

        async def update(content: str, *, force: bool = False) -> None:
            nonlocal last_edit
            now = loop.time()
            if force or now - last_edit >= 2.0:
                await interaction.edit_original_response(content=content)
                last_edit = loop.time()

        for member in self._members:
            while True:
                try:
                    await member.send(self._message)
                    sent += 1
                    break
                except discord.Forbidden:
                    failed += 1
                    break
                except discord.RateLimited as exc:
                    secs = round(exc.retry_after)
                    log.warning("DM rate limited — waiting %ds", secs)
                    await interaction.edit_original_response(
                        content=f"⏳ Rate limited — waiting {secs}s… ({sent}/{total} sent)"
                    )
                    last_edit = loop.time()
                    await asyncio.sleep(exc.retry_after)
                except discord.HTTPException as exc:
                    if exc.status == 429:
                        try:
                            retry_after = float(exc.response.headers["Retry-After"])
                        except (KeyError, AttributeError, ValueError):
                            retry_after = 5.0
                        secs = round(retry_after)
                        log.warning("DM 429 — waiting %ds", secs)
                        await interaction.edit_original_response(
                            content=f"⏳ Rate limited — waiting {secs}s… ({sent}/{total} sent)"
                        )
                        last_edit = loop.time()
                        await asyncio.sleep(retry_after)
                    else:
                        failed += 1
                        break

            tail = f", {failed} failed" if failed else ""
            await update(f"Sending DMs… ({sent}/{total} sent{tail})")
            await asyncio.sleep(0.75)

        result = f"✅ Done — **{sent}/{total}** DMs sent to members with {self._role.mention}."
        if failed:
            result += f" **{failed}** couldn't be reached (DMs disabled or bot blocked)."
        await interaction.edit_original_response(content=result)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()

    async def on_timeout(self) -> None:
        pass


class _BulkSignDropView(discord.ui.View):
    def __init__(self, team, *, drop: bool) -> None:
        super().__init__(timeout=120)
        self._team = team
        self._drop = drop
        self._selected: list[discord.Member] = []

        verb = "drop" if drop else "sign"
        self._sel = discord.ui.UserSelect(
            placeholder=f"Select players to {verb}…",
            min_values=1,
            max_values=25,
        )
        self._sel.callback = self._on_select
        self.add_item(self._sel)

        self.confirm.label = "Drop" if drop else "Sign"
        self.confirm.style = discord.ButtonStyle.danger if drop else discord.ButtonStyle.success
        self.confirm.disabled = True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self._selected = list(self._sel.values)
        self.confirm.disabled = False
        verb = "Drop" if self._drop else "Sign"
        names = ", ".join(m.display_name for m in self._selected[:5])
        if len(self._selected) > 5:
            names += f" +{len(self._selected) - 5} more"
        await interaction.response.edit_message(
            content=f"**{verb}:** {names}\nClick **{verb}** to confirm.",
            view=self,
        )

    @discord.ui.button(label="Sign", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.defer(ephemeral=True)
        assert interaction.guild is not None

        op = roster_ops.drop_from_team if self._drop else roster_ops.sign_to_team
        verb = "Bulk drop" if self._drop else "Bulk sign"
        done: list[str] = []
        failed: list[str] = []

        for member in self._selected:
            try:
                await op(
                    guild=interaction.guild,
                    member=member,
                    team=self._team,
                    actor=interaction.user,
                    reason=f"{verb} by {interaction.user}",
                )
                done.append(member.display_name)
            except roster_ops.RoleAssignmentError:
                failed.append(member.display_name)

        verb_past = "dropped from" if self._drop else "signed to"
        result = f"✅ **{len(done)}** player(s) {verb_past} **{self._team.name}**."
        if failed:
            result += f"\n⚠️ Failed for: {', '.join(failed)} (missing role permissions)"
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)

    async def on_timeout(self) -> None:
        pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RosterCog(bot))
