"""
Rendering and lifecycle for Google Sheets live stat boards.

Extracted from `bot/cogs/sheets.py` so the `/sheets` command group and
the League panel's **Stat boards** screen drive exactly the same code.
Two copies of forum-thread reuse and message-edit fallback would drift.

This is not `bot/market/boards.py`. Those are market boards the bot
renders from its own data; these mirror an external Google Sheet.
"""

import logging

import discord
from discord.ext import commands

from bot import db, queries, sheets
from bot.queries import set_stat_board_forum_thread_id

log = logging.getLogger(__name__)


async def _get_forum_thread(
    bot: commands.Bot, channel: discord.ForumChannel, thread_id: int
) -> discord.Thread | None:
    thread = bot.get_channel(thread_id)
    if isinstance(thread, discord.Thread):
        return thread
    try:
        thread = await bot.fetch_channel(thread_id)
        return thread if isinstance(thread, discord.Thread) else None
    except (discord.NotFound, discord.Forbidden):
        return None


async def _rerender_board(bot: commands.Bot, board) -> None:  # type: ignore[type-arg]
    embed = await sheets.fetch_and_build_embed(board.title, board.sheet_id, board.sheet_range)

    channel = bot.get_channel(board.channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        log.warning("Stat board %r: channel %s unavailable", board.title, board.channel_id)
        return

    needs_post = board.message_id is None

    # Try to edit the existing message
    if not needs_post:
        try:
            if isinstance(channel, discord.TextChannel):
                msg = await channel.fetch_message(board.message_id)
                await msg.edit(embed=embed)
                return
            else:
                thread = (
                    await _get_forum_thread(bot, channel, board.forum_thread_id)
                    if board.forum_thread_id
                    else None
                )
                if thread is not None:
                    msg = await thread.fetch_message(board.message_id)
                    await msg.edit(embed=embed)
                    return
                needs_post = True
        except discord.NotFound:
            log.info("Stat board %r: message gone — re-posting", board.title)
            needs_post = True
        except discord.Forbidden:
            log.warning(
                "Stat board %r: no permission to edit in channel %s",
                board.title, board.channel_id,
            )
            return

    # Post (or re-post): for forum channels reuse the existing thread where possible
    try:
        if isinstance(channel, discord.ForumChannel):
            thread = (
                await _get_forum_thread(bot, channel, board.forum_thread_id)
                if board.forum_thread_id
                else None
            )
            if thread is not None:
                msg = await thread.send(embed=embed)
            else:
                thread, msg = await channel.create_thread(name=board.title, embed=embed)
            async with db.connect() as conn:
                await queries.set_stat_board_message_id(conn, board.id, msg.id)
                await set_stat_board_forum_thread_id(conn, board.id, thread.id)
        else:
            msg = await channel.send(embed=embed)
            async with db.connect() as conn:
                await queries.set_stat_board_message_id(conn, board.id, msg.id)
    except discord.Forbidden:
        log.warning(
            "Stat board %r: no permission to post in channel %s",
            board.title, board.channel_id,
        )
        return
    log.info("Stat board %r: posted as message %s", board.title, msg.id)


async def list_stat_boards(guild_id: int):
    """Every stat board configured in a server."""
    async with db.connect() as conn:
        return await queries.fetch_all_stat_boards(conn, guild_id)


async def refresh_stat_boards(bot: commands.Bot, guild_id: int) -> int:
    """Re-render every stat board. Returns how many were attempted."""
    boards = await list_stat_boards(guild_id)
    for board in boards:
        await _rerender_board(bot, board)
    return len(boards)


async def remove_stat_board(bot: commands.Bot, board_id: int) -> None:
    """
    Delete a stat board's Discord message, then its row.

    The row goes regardless of whether the message could be deleted —
    the database is what the bot is authoritative over.
    """
    async with db.connect() as conn:
        board = await queries.fetch_stat_board_by_id(conn, board_id)
        if board is not None and board.message_id:
            channel = bot.get_channel(board.channel_id)
            thread = (
                await _get_forum_thread(bot, channel, board.forum_thread_id)
                if isinstance(channel, discord.ForumChannel)
                and board.forum_thread_id
                else None
            )
            target = thread if thread is not None else channel
            if target is not None:
                try:
                    msg = await target.fetch_message(board.message_id)
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden, AttributeError):
                    log.debug(
                        "Stat board %s: message could not be deleted",
                        board_id, exc_info=True,
                    )
        await queries.delete_stat_board(conn, board_id)


async def create_stat_board(
    bot: commands.Bot,
    *,
    guild_id: int,
    title: str,
    sheet_id: str,
    sheet_range: str,
    channel_id: int,
) -> bool:
    """
    Create a stat board and post its first render.

    Returns True when the first read of the sheet FAILED. The board is
    still created in that case — the posted message explains the error —
    because a wrong URL or an unshared sheet is fixable without
    rebuilding the board.

    Text channels only; the `/sheets add` command additionally supports
    forum channels with an explicit thread, which a panel channel picker
    cannot express.
    """
    embed = await sheets.fetch_and_build_embed(title, sheet_id, sheet_range)
    fetch_failed = embed.color == discord.Color.red()

    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise ValueError(
            "That channel is not a text channel the bot can see."
        )

    try:
        msg = await channel.send(embed=embed)
    except discord.Forbidden as exc:
        raise ValueError(
            f"The bot cannot post in <#{channel_id}>. Give it "
            "**Send Messages** and **Embed Links** there, then try again."
        ) from exc

    async with db.connect() as conn:
        board_id = await queries.insert_stat_board(
            conn, guild_id, title, sheet_id, sheet_range, channel_id
        )
        await queries.set_stat_board_message_id(conn, board_id, msg.id)

    return fetch_failed
