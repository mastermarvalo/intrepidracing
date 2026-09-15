import asyncio
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from bot import db

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
        await db.init()
        self.tree.on_error = self._on_app_command_error
        await self.load_extension("bot.cogs.roster")
        await self.load_extension("bot.cogs.sheets")
        await self.load_extension("bot.cogs.admin_market")
        await self.load_extension("bot.cogs.market")
        await self.load_extension("bot.cogs.contracts")
        await self.load_extension("bot.cogs.trades")
        # Loaded last so /help sees every other cog's commands when it
        # walks the tree.
        await self.load_extension("bot.cogs.panel")
        await self.load_extension("bot.events")
        # Global sync — commands appear in all servers but propagation takes up to 1h.
        # For faster dev iteration, call tree.sync(guild=discord.Object(id=YOUR_GUILD_ID)).
        await self.tree.sync()
        log.info("Command tree synced")

    async def _on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """
        Last resort for a slash command that raised something unexpected.

        Each cog catches its own domain errors; anything else used to
        surface as "The application did not respond" (or nothing at all,
        once the command had deferred) with the traceback visible only in
        the log. A wrong answer the owner can see beats a silent one.
        """
        original = getattr(error, "original", error)
        log.exception("Unhandled app command error", exc_info=original)

        note = (
            f"❌ Something went wrong: `{type(original).__name__}`.\n"
            "This was not an expected failure, so the league may be in a "
            "partly-changed state — check `/league` before retrying. The "
            "details are in the bot log."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(note, ephemeral=True)
            else:
                await interaction.response.send_message(note, ephemeral=True)
        except discord.HTTPException as exc:
            log.debug("Could not deliver the error notice: %s", exc)

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
