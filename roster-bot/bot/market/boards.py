"""
Self-updating market boards — the runtime side.

`bot/market/render.py` builds the embeds; this module orchestrates the
Discord side of things: fetches the data behind a board, calls the
right renderer for its `kind`, and edits the stored message in place.
If the message was deleted, the board's `message_id` is cleared and
`/market-admin board list` surfaces the row as broken — mirroring the
"idempotent render" contract `bot/render.py` uses for roster embeds
(CLAUDE.md §1).

Refresh triggers:
  - Explicit: `/market-admin board refresh`
  - After publish: called from the valuation-publish path so the market
    board reflects the new numbers immediately.
  - Safety net: the 15-minute `poll_loop` in `bot/events.py` calls
    `refresh_all_boards` so any missed event self-heals within a
    quarter hour.

The ADR-001 magic-number guard scans this module. Numeric limits come
from `bot/limits.py`; the (0, 1, -1) allowlist covers per-iteration
indices.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from bot import db, limits, queries
from bot.market import render as market_render
from bot.models import MarketBoard

log = logging.getLogger(__name__)


async def refresh_all_boards(bot: commands.Bot) -> None:
    """Refresh every market board across every guild the bot is in."""
    for guild in bot.guilds:
        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, guild.id)
            if season is None:
                continue
            boards = await queries.fetch_market_boards_in_season(conn, season.id)
        for board in boards:
            await refresh_board(bot, board)


async def refresh_boards_for_tier(
    bot: commands.Bot,
    *,
    guild_id: int,
    season_id: int,
    tier_id: int | None,
) -> None:
    """
    Refresh every board scoped to a specific tier PLUS every cross-tier
    board (dashboards) in the same season. Called after a publish so a
    dashboard picks up the new tier's numbers.
    """
    async with db.connect() as conn:
        tier_boards = await queries.fetch_market_boards_for_tier(
            conn, season_id, tier_id
        )
        cross = (
            [] if tier_id is None
            else await queries.fetch_market_boards_for_tier(conn, season_id, None)
        )
    for board in list(tier_boards) + list(cross):
        await refresh_board(bot, board)


async def refresh_board(bot: commands.Bot, board: MarketBoard) -> None:
    """
    Rebuild `board`'s embed and edit the stored message. Clears
    `message_id` if the message is gone; logs (but does not raise) on
    permission errors so one broken channel doesn't take out the
    refresh pass for the rest.
    """
    embed = await build_board_embed(board)
    if embed is None:
        return
    channel = bot.get_channel(board.channel_id)
    if not isinstance(channel, discord.TextChannel):
        log.warning(
            "Board %s: channel %s unavailable", board.id, board.channel_id
        )
        return

    if board.message_id is None:
        try:
            msg = await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning("Board %s: no permission to post in channel", board.id)
            return
        async with db.connect() as conn:
            await queries.set_market_board_message_id(conn, board.id, msg.id)
        return

    try:
        msg = await channel.fetch_message(board.message_id)
        await msg.edit(embed=embed)
    except discord.NotFound:
        log.warning(
            "Board %s: message %s was deleted; clearing message_id",
            board.id, board.message_id,
        )
        async with db.connect() as conn:
            await queries.set_market_board_message_id(conn, board.id, None)
    except discord.Forbidden:
        log.warning(
            "Board %s: no permission to edit message in channel %s",
            board.id, board.channel_id,
        )


async def build_board_embed(board: MarketBoard) -> discord.Embed | None:
    """
    Return the current embed for `board.kind`, or None if the kind is
    unknown / not yet implemented (e.g. cap/surplus/underwater land in
    Phase 4).
    """
    async with db.connect() as conn:
        season = await queries.fetch_season_by_id(conn, board.season_id)
        if season is None:
            return None
        if board.kind == "market":
            return await _build_market_embed(conn, board)
        if board.kind == "movers":
            return await _build_movers_embed(conn, board)
        if board.kind == "dashboard":
            return await _build_dashboard_embed(conn, board, season.name)
    return None


async def _build_market_embed(
    conn, board: MarketBoard
) -> discord.Embed | None:
    if board.tier_id is None:
        return None
    tier = await queries.fetch_tier_by_id(conn, board.tier_id)
    if tier is None:
        return None
    rows = await queries.fetch_market_table_for_tier(conn, tier.id)
    round_label = await conn.fetchval(
        """
        SELECT round_label FROM valuation_runs
        WHERE tier_id = $1 AND published
        ORDER BY published_at DESC NULLS LAST, created_at DESC
        LIMIT 1
        """,
        tier.id,
    )
    return market_render.render_market_page(
        tier_label=tier.label,
        round_label=round_label,
        accent_color=tier.accent_color,
        drivers=rows,
        page=board.page or 1,
    )


async def _build_movers_embed(
    conn, board: MarketBoard
) -> discord.Embed | None:
    if board.tier_id is None:
        return None
    tier = await queries.fetch_tier_by_id(conn, board.tier_id)
    if tier is None:
        return None
    risers, fallers = await queries.fetch_movers_for_tier(
        conn, tier.id, limits.MOVERS_PER_DIRECTION
    )
    round_label = await conn.fetchval(
        """
        SELECT round_label FROM valuation_runs
        WHERE tier_id = $1 AND published
        ORDER BY published_at DESC NULLS LAST, created_at DESC
        LIMIT 1
        """,
        tier.id,
    )
    return market_render.render_movers(
        tier_label=tier.label,
        round_label=round_label,
        accent_color=tier.accent_color,
        risers=risers,
        fallers=fallers,
    )


async def _build_dashboard_embed(
    conn, board: MarketBoard, season_name: str
) -> discord.Embed | None:
    rows = await queries.fetch_cross_tier_top(
        conn, board.season_id, limits.DASHBOARD_PER_TIER
    )
    return market_render.render_dashboard(
        season_name=season_name, tier_rows=rows
    )
