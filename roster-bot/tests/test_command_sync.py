"""
Publishing the command tree.

A global sync takes up to an hour to reach Discord, so a command added
by a deploy looks broken in the meantime. Setting `DISCORD_GUILD_ID`
targets one guild, which applies immediately. These tests pin the
choice, the fallbacks, and the one mistake that would wipe every
command from the server.
"""

import discord
import pytest

from bot import main


class _Tree:
    def __init__(self) -> None:
        self.global_syncs = 0
        self.guild_syncs: list[int] = []
        self.copied_to: list[int] = []
        self.raises: Exception | None = None

    def get_commands(self, **kw):
        return []

    def copy_global_to(self, *, guild):
        self.copied_to.append(guild.id)

    async def sync(self, *, guild=None):
        if guild is None:
            self.global_syncs += 1
            return []
        if self.raises is not None:
            raise self.raises
        self.guild_syncs.append(guild.id)
        return []


class _Bot:
    """Just enough of RosterBot to exercise the sync decision."""

    def __init__(self) -> None:
        self.tree = _Tree()

    _sync_commands = main.RosterBot._sync_commands


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    return _Bot()


async def test_without_a_guild_id_it_syncs_globally(bot):
    await bot._sync_commands()
    assert bot.tree.global_syncs == 1
    assert bot.tree.guild_syncs == []


async def test_a_guild_id_targets_that_guild_instead(bot, monkeypatch):
    monkeypatch.setenv("DISCORD_GUILD_ID", "555")
    await bot._sync_commands()
    assert bot.tree.guild_syncs == [555]
    assert bot.tree.global_syncs == 0, "a guild sync should not also go global"


async def test_the_global_tree_is_copied_before_the_guild_sync(bot, monkeypatch):
    # Syncing a guild without copying first publishes an empty tree and
    # every command disappears from that server. This is the whole bug
    # this test exists to prevent.
    monkeypatch.setenv("DISCORD_GUILD_ID", "555")
    await bot._sync_commands()
    assert bot.tree.copied_to == [555]


async def test_surrounding_whitespace_does_not_defeat_it(bot, monkeypatch):
    monkeypatch.setenv("DISCORD_GUILD_ID", "  555  ")
    await bot._sync_commands()
    assert bot.tree.guild_syncs == [555]


async def test_an_empty_guild_id_is_treated_as_unset(bot, monkeypatch):
    # A commented-out or blank line in .env should not be read as a
    # guild called "".
    monkeypatch.setenv("DISCORD_GUILD_ID", "   ")
    await bot._sync_commands()
    assert bot.tree.global_syncs == 1


async def test_a_nonsense_guild_id_falls_back_rather_than_crashing(
    bot, monkeypatch
):
    # Pasting a server *name* instead of its ID must not stop the bot
    # from booting with a working command tree.
    monkeypatch.setenv("DISCORD_GUILD_ID", "my-server")
    await bot._sync_commands()
    assert bot.tree.global_syncs == 1
    assert bot.tree.guild_syncs == []


async def test_a_forbidden_guild_sync_falls_back_to_global(bot, monkeypatch):
    # Bot invited without the applications.commands scope, or not in
    # that server at all. Commands should still reach Discord.
    monkeypatch.setenv("DISCORD_GUILD_ID", "555")
    bot.tree.raises = discord.Forbidden(
        _Resp(), "missing applications.commands"
    )
    await bot._sync_commands()
    assert bot.tree.global_syncs == 1


class _Resp:
    status = 403
    reason = "Forbidden"
