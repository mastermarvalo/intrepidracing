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
from typing import NamedTuple

import discord
from discord.ext import commands

from bot import db, limits, queries
from bot.contracts import render as contract_render
from bot.market import render as market_render
from bot.models import MarketBoard

log = logging.getLogger(__name__)


class BoardRefresh(NamedTuple):
    """
    What one refresh pass actually did (G20).

    `refresh_board` used to return `None` and log permission failures at
    warning level, so a caller could only re-read `message_id` — which
    distinguishes "never posted" from "posted", but not "posted earlier,
    and today's edit was rejected". Commands replied ✅ regardless.

    `posted` means a message exists in the channel now. `action` names
    the path taken so a caller can tell a silent failure from a success.
    """

    board_id: int
    posted: bool
    action: str


#: Every value `BoardRefresh.action` can take. Callers that render an
#: outcome should handle all of them; `ACTION_LABELS` keeps the wording
#: in one place.
ACTION_SENT = "sent"
ACTION_EDITED = "edited"
ACTION_MESSAGE_MISSING = "message_missing"
ACTION_SKIPPED_NO_CHANNEL = "skipped_no_channel"
ACTION_SKIPPED_FORBIDDEN = "skipped_forbidden"
ACTION_SKIPPED_NO_DATA = "skipped_no_data"

ACTION_LABELS = {
    ACTION_SENT: "posted for the first time",
    ACTION_EDITED: "updated in place",
    ACTION_MESSAGE_MISSING: "its message was deleted — will repost next refresh",
    ACTION_SKIPPED_NO_CHANNEL: "its channel is gone or not visible to the bot",
    ACTION_SKIPPED_FORBIDDEN: "the bot lacks permission in that channel",
    ACTION_SKIPPED_NO_DATA: "there is nothing to show yet",
}

#: Actions that mean the refresh did not do what the operator asked.
FAILED_ACTIONS = (
    ACTION_MESSAGE_MISSING,
    ACTION_SKIPPED_NO_CHANNEL,
    ACTION_SKIPPED_FORBIDDEN,
)


def describe_refresh(outcome: BoardRefresh) -> str:
    """One human line for a refresh outcome."""
    label = ACTION_LABELS.get(outcome.action, outcome.action)
    return f"Board `{outcome.board_id}` — {label}"


async def refresh_all_boards(bot: commands.Bot) -> list[BoardRefresh]:
    """
    Refresh every market board across every guild the bot is in.

    Returns one outcome per board so callers can report honestly.
    """
    out: list[BoardRefresh] = []
    for guild in bot.guilds:
        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, guild.id)
            if season is None:
                continue
            boards = await queries.fetch_market_boards_in_season(conn, season.id)
        for board in boards:
            out.append(await refresh_board(bot, board))
    return out


async def refresh_boards_for_tier(
    bot: commands.Bot,
    *,
    guild_id: int,
    season_id: int,
    tier_id: int | None,
) -> list[BoardRefresh]:
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
    return [
        await refresh_board(bot, board)
        for board in list(tier_boards) + list(cross)
    ]


async def refresh_board(
    bot: commands.Bot, board: MarketBoard
) -> BoardRefresh:
    """
    Rebuild `board`'s embed and edit the stored message. Clears
    `message_id` if the message is gone; still does not raise on
    permission errors, so one broken channel doesn't take out the
    refresh pass for the rest — but now reports what happened instead of
    letting the caller assume success (G20).
    """
    embed = await build_board_embed(board)
    if embed is None:
        return BoardRefresh(board.id, False, ACTION_SKIPPED_NO_DATA)
    channel = bot.get_channel(board.channel_id)
    if not isinstance(channel, discord.TextChannel):
        log.warning(
            "Board %s: channel %s unavailable", board.id, board.channel_id
        )
        return BoardRefresh(board.id, False, ACTION_SKIPPED_NO_CHANNEL)

    if board.message_id is None:
        try:
            msg = await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning("Board %s: no permission to post in channel", board.id)
            return BoardRefresh(board.id, False, ACTION_SKIPPED_FORBIDDEN)
        async with db.connect() as conn:
            await queries.set_market_board_message_id(conn, board.id, msg.id)
        return BoardRefresh(board.id, True, ACTION_SENT)

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
        return BoardRefresh(board.id, False, ACTION_MESSAGE_MISSING)
    except discord.Forbidden:
        log.warning(
            "Board %s: no permission to edit message in channel %s",
            board.id, board.channel_id,
        )
        # The stale message is still sitting in the channel showing old
        # numbers. `message_id IS NOT NULL` would read as healthy, which
        # is exactly the lie G20 was about.
        return BoardRefresh(board.id, True, ACTION_SKIPPED_FORBIDDEN)
    return BoardRefresh(board.id, True, ACTION_EDITED)


async def build_board_embed(board: MarketBoard) -> discord.Embed | None:
    """
    Return the current embed for `board.kind`, or None if the kind is
    unknown or the season behind it has gone. Every kind the board
    picker offers (market, movers, dashboard, surplus, underwater) is
    handled below.
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
        if board.kind == "surplus":
            return await _build_pl_embed(conn, board, top_first=True)
        if board.kind == "underwater":
            return await _build_pl_embed(conn, board, top_first=False)
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


async def _build_pl_embed(
    conn, board: MarketBoard, *, top_first: bool
) -> discord.Embed | None:
    if board.tier_id is None:
        return None
    tier = await queries.fetch_tier_by_id(conn, board.tier_id)
    if tier is None:
        return None
    rows = await queries.fetch_tier_contracts_with_market(conn, tier.id)
    round_label = await conn.fetchval(
        """
        SELECT round_label FROM valuation_runs
        WHERE tier_id = $1 AND published
        ORDER BY published_at DESC NULLS LAST, created_at DESC
        LIMIT 1
        """,
        tier.id,
    )
    title = (
        f"Surplus — {tier.label}" if top_first
        else f"Underwater — {tier.label}"
    )
    return contract_render.render_pl_table(
        title=title,
        color=tier.accent_color,
        rows=rows,
        top_first=top_first,
        round_label=round_label,
    )
