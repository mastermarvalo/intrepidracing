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
        await self._sync_commands()

    async def _sync_commands(self) -> None:
        """
        Publish the command tree to Discord.

        A bare `tree.sync()` is a *global* sync, and Discord propagates
        those lazily — up to an hour. For a single-server league that is
        the wrong trade: every new command looks broken for an hour
        after deploy, which is indistinguishable from a bug and sends
        you hunting through logs for a problem that does not exist.

        Setting `DISCORD_GUILD_ID` syncs to that one guild instead,
        where Discord applies the change immediately. Left unset, the
        behaviour is the old global sync, so an existing deployment
        keeps working untouched.
        """
        guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        if not guild_id:
            await self.tree.sync()
            log.info(
                "Command tree synced globally (%d commands). Discord may "
                "take up to an hour to show new or changed commands. Set "
                "DISCORD_GUILD_ID to your server ID for instant syncs.",
                len(self.tree.get_commands()),
            )
            return

        try:
            target = discord.Object(id=int(guild_id))
        except ValueError:
            log.error(
                "DISCORD_GUILD_ID=%r is not a number — falling back to a "
                "global sync. Copy the ID from Discord with Developer Mode "
                "on: right-click the server, Copy Server ID.",
                guild_id,
            )
            await self.tree.sync()
            return

        # Mirror the globally-declared commands onto the guild, then sync
        # the guild. Without the copy, the guild sync would publish an
        # empty tree and every command would vanish from that server.
        self.tree.copy_global_to(guild=target)
        try:
            synced = await self.tree.sync(guild=target)
        except discord.Forbidden:
            log.error(
                "Not allowed to sync commands to guild %s — the bot is "
                "probably not in that server, or was invited without the "
                "applications.commands scope. Re-invite it with that "
                "scope. Falling back to a global sync.",
                guild_id,
            )
            await self.tree.sync()
            return
        log.info(
            "Command tree synced to guild %s (%d commands) — available "
            "immediately.",
            guild_id,
            len(synced),
        )

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
