"""
Background tasks: role-change events and the 15-minute safety poll.

Two things trigger a roster re-render:

1. on_member_update  — fires whenever a member's roles change.  We check if any
   of the changed roles belong to a team and re-render just those teams.  If the
   FA role or any tier role changed, the FA board is also re-rendered.  If a team
   role was gained or lost, a transaction announcement is posted.

2. poll_loop  — runs every 15 minutes regardless.  Catches anything missed while
   the bot was offline, or events Discord dropped.  Also compares an in-memory
   roster snapshot to detect sign/drop changes that happened during downtime.

Both paths call _rerender() and _rerender_fa(), the only places that edit messages.
"""

import logging

import discord
from discord.ext import commands, tasks

from bot import db, queries
from bot.models import GuildConfig, Team
from bot.render import (
    build_fa_embed,
    build_flair_embed,
    build_roster_embeds,
    build_transaction_embed,
    roster_flair_file,
)

log = logging.getLogger(__name__)

# team_id -> frozenset of member IDs currently holding that team's role
_snapshots: dict[int, frozenset[int]] = {}
_snapshots_initialized = False


class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.poll_loop.start()

    def cog_unload(self) -> None:
        self.poll_loop.cancel()

    # ── event-driven update ───────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        before_ids = {r.id for r in before.roles}
        after_ids = {r.id for r in after.roles}
        changed_ids = before_ids.symmetric_difference(after_ids)
        if not changed_ids:
            return

        added_ids = after_ids - before_ids
        removed_ids = before_ids - after_ids

        async with db.connect() as conn:
            teams = await queries.fetch_all_teams(conn, after.guild.id)
            config = await queries.fetch_guild_config(conn, after.guild.id)

        for team in teams:
            team_role_ids = {team.team_role_id} | {s.slot_role_id for s in team.slots}
            if team_role_ids & changed_ids:
                await _rerender(self.bot, after.guild, team)

            if team.team_role_id in added_ids:
                await _announce_transaction(self.bot, team, after, "signed", config)
                _snapshots[team.id] = _snapshots.get(team.id, frozenset()) | {after.id}
            elif team.team_role_id in removed_ids:
                await _announce_transaction(self.bot, team, after, "dropped", config)
                _snapshots[team.id] = _snapshots.get(team.id, frozenset()) - {after.id}

        if config.fa_channel_id and config.fa_message_id and config.free_agent_role_id:
            changed_roles = [after.guild.get_role(rid) for rid in changed_ids]
            fa_relevant = config.free_agent_role_id in changed_ids or any(
                r and r.name.strip().lower().startswith("tier ") for r in changed_roles
            )
            if fa_relevant:
                await _rerender_fa(self.bot, after.guild, config)

    # ── 15-minute safety poll ─────────────────────────────────────────────────

    @tasks.loop(minutes=15)
    async def poll_loop(self) -> None:
        global _snapshots_initialized
        log.debug("Poll: refreshing all rosters")
        for guild in self.bot.guilds:
            async with db.connect() as conn:
                teams = await queries.fetch_all_teams(conn, guild.id)
                config = await queries.fetch_guild_config(conn, guild.id)

            for team in teams:
                team_role = guild.get_role(team.team_role_id)
                current_ids: frozenset[int] = (
                    frozenset(m.id for m in team_role.members) if team_role else frozenset()
                )

                if _snapshots_initialized:
                    prev_ids = _snapshots.get(team.id, frozenset())
                    for member_id in current_ids - prev_ids:
                        member = guild.get_member(member_id)
                        if member:
                            await _announce_transaction(self.bot, team, member, "signed", config)
                    for member_id in prev_ids - current_ids:
                        member = guild.get_member(member_id)
                        if member:
                            await _announce_transaction(self.bot, team, member, "dropped", config)

                _snapshots[team.id] = current_ids
                await _rerender(self.bot, guild, team)

            await _rerender_fa(self.bot, guild, config)

        _snapshots_initialized = True
        log.debug("Poll: done")

    @poll_loop.before_loop
    async def before_poll(self) -> None:
        await self.bot.wait_until_ready()


# ── shared helpers ────────────────────────────────────────────────────────────


async def _announce_transaction(
    bot: commands.Bot,
    team: Team,
    member: discord.Member,
    action: str,
    config: GuildConfig,
) -> None:
    async with db.connect() as conn:
        await queries.log_transaction(
            conn, team.guild_id, team.id, member.id, member.display_name, action
        )

    if not config.transactions_channel_id:
        return
    channel = bot.get_channel(config.transactions_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    embed = build_transaction_embed(team, member, action)  # type: ignore[arg-type]
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.warning(
            "No permission to post transaction in channel %s",
            config.transactions_channel_id,
        )


async def _rerender(bot: commands.Bot, guild: discord.Guild, team) -> None:  # type: ignore[type-arg]
    if team.message_id is None:
        return

    channel = bot.get_channel(team.channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        log.warning("Channel %s for team %s is unavailable", team.channel_id, team.key)
        return

    members = list(guild.members)
    flair = roster_flair_file(team)
    embeds = [build_flair_embed(team.color)] + build_roster_embeds(team, members)

    try:
        if isinstance(channel, discord.TextChannel):
            msg = await channel.fetch_message(team.message_id)
        else:  # ForumChannel — message_id == thread_id
            thread = bot.get_channel(team.message_id)
            if not isinstance(thread, discord.Thread):
                thread = await bot.fetch_channel(team.message_id)
            msg = await thread.fetch_message(team.message_id)
        await msg.edit(embeds=embeds, attachments=[flair])
    except discord.NotFound:
        log.warning("Roster message for team %s was deleted — clearing message_id", team.key)
        async with db.connect() as conn:
            await queries.set_message_id(conn, team.id, None)
    except discord.Forbidden:
        log.warning("No permission to edit roster message in channel %s", team.channel_id)


async def _rerender_fa(bot: commands.Bot, guild: discord.Guild, config) -> None:  # type: ignore[type-arg]
    if not (config.fa_channel_id and config.fa_message_id and config.free_agent_role_id):
        return

    channel = bot.get_channel(config.fa_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    embed = build_fa_embed(guild, config.free_agent_role_id)
    try:
        msg = await channel.fetch_message(config.fa_message_id)
        await msg.edit(embed=embed)
    except discord.NotFound:
        log.warning("FA board message was deleted — clearing fa_message_id")
        async with db.connect() as conn:
            await queries.set_fa_message_id(conn, guild.id, None)
    except discord.Forbidden:
        log.warning("No permission to edit FA board in channel %s", config.fa_channel_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventsCog(bot))
