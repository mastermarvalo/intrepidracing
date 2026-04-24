import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


class RosterBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        # guild_members is privileged — must be enabled in the Discord dev portal
        # under Bot > Privileged Gateway Intents > Server Members Intent
        intents.members = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

    async def setup_hook(self) -> None:
        await self.load_extension("bot.cogs.roster")
        # Global sync — commands appear in all servers but propagation takes up to 1h.
        # For faster dev iteration, call tree.sync(guild=discord.Object(id=YOUR_GUILD_ID)).
        await self.tree.sync()
        log.info("Command tree synced")

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Ready — logged in as %s (ID: %s)", self.user, self.user.id)


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set in environment")
    async with RosterBot() as bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
