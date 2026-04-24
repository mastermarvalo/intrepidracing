"""
on_member_update event handler.

When a member's roles change, we find every team in that guild whose
team_role_id or any slot_role_id appears in the changed role set, and
re-render those teams. This keeps the roster message in sync without polling.
"""

import logging

import discord
from discord.ext import commands

from bot import db, queries
from bot.render import build_embed

log = logging.getLogger(__name__)


class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        before_ids = {r.id for r in before.roles}
        after_ids = {r.id for r in after.roles}
        if before_ids == after_ids:
            return  # roles didn't change (e.g. nickname update)

        changed_ids = before_ids.symmetric_difference(after_ids)

        async with db.connect() as conn:
            teams = await queries.fetch_all_teams(conn, after.guild.id)

        relevant = [
            t for t in teams
            if t.team_role_id in changed_ids
            or any(s.slot_role_id in changed_ids for s in t.slots)
        ]

        for team in relevant:
            await _rerender(self.bot, after.guild, team)


async def _rerender(bot: commands.Bot, guild: discord.Guild, team) -> None:  # type: ignore[type-arg]
    """
    Re-render a team's roster embed and edit the stored message in place.
    If the message has been deleted, log a warning and clear message_id.
    """
    if team.message_id is None:
        log.debug("Team %s (%s) has no roster message — skipping re-render", team.key, team.id)
        return

    channel = bot.get_channel(team.channel_id)
    if not isinstance(channel, discord.TextChannel):
        log.warning("Channel %s for team %s is unavailable", team.channel_id, team.key)
        return

    embed = build_embed(team, list(guild.members))

    try:
        msg = await channel.fetch_message(team.message_id)
        await msg.edit(embed=embed)
    except discord.NotFound:
        log.warning(
            "Roster message %s for team %s was deleted — clearing message_id",
            team.message_id,
            team.key,
        )
        async with db.connect() as conn:
            await queries.set_message_id(conn, team.id, None)
            await conn.commit()
    except discord.Forbidden:
        log.warning(
            "No permission to edit roster message %s in channel %s",
            team.message_id,
            team.channel_id,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventsCog(bot))
