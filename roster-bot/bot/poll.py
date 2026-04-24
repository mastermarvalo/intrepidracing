"""
15-minute safety poll.

Re-renders every team in every guild the bot can see.  This catches role
changes that arrived while the bot was offline, and any events that Discord
dropped.  The render path is identical to the event-driven path.
"""

import logging

from discord.ext import commands, tasks

from bot import db, queries
from bot.events import _rerender

log = logging.getLogger(__name__)


class PollCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.poll_loop.start()

    def cog_unload(self) -> None:
        self.poll_loop.cancel()

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
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PollCog(bot))
