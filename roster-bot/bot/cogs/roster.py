import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)


class RosterCog(commands.Cog):
    """All /roster subcommands live here. Steps 4-6 will fill these in."""

    roster = app_commands.Group(name="roster", description="Manage team rosters")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @roster.command(name="list", description="List all configured teams in this server")
    async def roster_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("(stub — coming in step 4)", ephemeral=True)

    @roster.command(name="remove", description="Delete a team and its roster message")
    @app_commands.describe(name="Team identifier, e.g. redbull")
    async def roster_remove(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.send_message("(stub — coming in step 4)", ephemeral=True)

    @roster.command(name="create", description="Create a new team roster")
    @app_commands.describe(name="Team identifier, e.g. redbull")
    async def roster_create(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.send_message("(stub — coming in step 5)", ephemeral=True)

    @roster.command(name="edit", description="Edit an existing team roster")
    @app_commands.describe(name="Team identifier, e.g. redbull")
    async def roster_edit(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.send_message("(stub — coming in step 6)", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RosterCog(bot))
