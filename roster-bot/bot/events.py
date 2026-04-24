"""
Background tasks: role-change events and the 15-minute safety poll.

Two things trigger a roster re-render:

1. on_member_update  — fires whenever a member's roles change.  We check if any
   of the changed roles belong to a team and re-render just those teams.

2. poll_loop  — runs every 15 minutes regardless.  Catches anything missed while
   the bot was offline, or events Discord dropped.

Both paths call _rerender(), which is the only place that edits a roster message.
"""

import logging

import discord
from discord.ext import commands, tasks

from bot import db, queries
from bot.render import build_embed

log = logging.getLogger(__name__)


class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.poll_loop.start()

    def cog_unload(self) -> None:
        self.poll_loop.cancel()

    # ── event-driven update ───────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        # symmetric_difference gives us roles that were added OR removed
        before_ids = {r.id for r in before.roles}
        after_ids = {r.id for r in after.roles}
        changed_ids = before_ids.symmetric_difference(after_ids)
        if not changed_ids:
            return  # something else changed (nickname, etc.) — nothing to do

        async with db.connect() as conn:
            teams = await queries.fetch_all_teams(conn, after.guild.id)

        # Only re-render teams that care about the changed roles
        for team in teams:
            team_role_ids = {team.team_role_id} | {s.slot_role_id for s in team.slots}
            if team_role_ids & changed_ids:  # set intersection — any overlap?
                await _rerender(self.bot, after.guild, team)

    # ── 15-minute safety poll ─────────────────────────────────────────────────

    @tasks.loop(minutes=15)
    async def poll_loop(self) -> None:
        log.debug("Poll: refreshing all rosters")
        for guild in self.bot.guilds:
            async with db.connect() as conn:
                teams = await queries.fetch_all_teams(conn, guild.id)
            for team in teams:
                await _rerender(self.bot, guild, team)
        log.debug("Poll: done")

    @poll_loop.before_loop
    async def before_poll(self) -> None:
        # Don't start polling until the bot has finished connecting
        await self.bot.wait_until_ready()


# ── shared render helper ──────────────────────────────────────────────────────


async def _rerender(bot: commands.Bot, guild: discord.Guild, team) -> None:  # type: ignore[type-arg]
    """Rebuild the roster embed and edit the stored Discord message."""
    if team.message_id is None:
        return  # No message posted yet — nothing to update

    channel = bot.get_channel(team.channel_id)
    if not isinstance(channel, discord.TextChannel):
        log.warning("Channel %s for team %s is unavailable", team.channel_id, team.key)
        return

    embed = build_embed(team, list(guild.members))
    try:
        msg = await channel.fetch_message(team.message_id)
        await msg.edit(embed=embed)
    except discord.NotFound:
        # The roster message was deleted manually — clear the stored ID.
        # /roster list will show this team as broken.
        log.warning("Roster message for team %s was deleted — clearing message_id", team.key)
        async with db.connect() as conn:
            await queries.set_message_id(conn, team.id, None)
            await conn.commit()
    except discord.Forbidden:
        log.warning("No permission to edit roster message in channel %s", team.channel_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventsCog(bot))
