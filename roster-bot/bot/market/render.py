"""
Market surface renderers — pure functions returning discord.Embed
objects.

No DB access, no interaction dispatch, no side effects. Callers hand
in already-fetched rows (or domain dataclasses) and get back an embed
ready to send. This is the same separation `bot/render.py` uses for
the roster surfaces and it keeps every layout invariant testable
without spinning up a mock Discord.

Layout — Discord wraps long lines badly on mobile, so per CLAUDE.md §5
every driver renders on two lines rather than as columns:

    1. Verstappen
       Market: $20.75M  |  Week: ▲ $1.25M  ⚠

Discord protocol limits (embed field 1024 chars, total 6000 chars,
description 4096 chars) and rendering choices (page size, movers per
direction) live in `bot/limits.py` — the ADR-001 magic-number guard
scans this module, so anything numeric here is a variable, a
per-iteration index (0/1/-1 allowed), or an import from `bot.limits`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping, Sequence

import discord

from bot import limits
from bot.market.money import format_money, format_pl

# The (0, 1, -1) allowlist covers the few unavoidable numeric literals
# below: the initial page number, page arithmetic, and colour fallback.


# ── public entry points ──────────────────────────────────────────────


def paginate(items: Sequence, page: int) -> tuple[list, int, int]:
    """
    Slice `items` for the requested 1-indexed page. Returns
    (page_items, page_clamped, total_pages). If items is empty,
    total_pages is 1 and page_items is empty — the empty state still
    renders as "page 1 of 1".
    """
    if not items:
        return ([], 1, 1)
    size = limits.MARKET_PAGE_SIZE
    total = (len(items) + size - 1) // size
    page_clamped = max(1, min(page, total))
    start = (page_clamped - 1) * size
    return (list(items[start:start + size]), page_clamped, total)


def render_market_page(
    *,
    tier_label: str,
    round_label: str | None,
    accent_color: int | None,
    drivers: Sequence[Mapping],
    page: int,
) -> discord.Embed:
    """
    Paginated market table for a tier. `drivers` rows must include
    display_name, market_value, delta, rank_in_tier, capped.
    """
    page_rows, page_clamped, total_pages = paginate(drivers, page)
    embed = discord.Embed(
        title=f"Market — {tier_label}",
        colour=_colour(accent_color),
    )
    if not drivers:
        embed.description = _empty_tier_body()
    else:
        lines: list[str] = []
        for row in page_rows:
            lines.extend(_driver_two_line(
                rank=row["rank_in_tier"],
                name=row["display_name"],
                market_value=row["market_value"],
                delta=row["delta"],
                capped=row["capped"],
            ))
        embed.description = _clip(
            "\n".join(lines), limits.EMBED_DESCRIPTION_MAX
        )
    footer = _footer_bits(round_label, page_clamped, total_pages, len(drivers))
    embed.set_footer(text=footer)
    _enforce_total(embed)
    return embed


def render_movers(
    *,
    tier_label: str,
    round_label: str | None,
    accent_color: int | None,
    risers: Sequence[Mapping],
    fallers: Sequence[Mapping],
) -> discord.Embed:
    """Top risers/fallers side-by-side (as embed fields, not columns)."""
    embed = discord.Embed(
        title=f"Movers — {tier_label}",
        colour=_colour(accent_color),
    )
    if not risers and not fallers:
        embed.description = (
            "No movement to report — either no published run yet, or "
            "every driver held their previous value."
        )
    else:
        embed.add_field(
            name="▲ Risers",
            value=_clip(_movers_field(risers, "▲") or "*none*",
                        limits.EMBED_FIELD_VALUE_MAX),
            inline=False,
        )
        embed.add_field(
            name="▼ Fallers",
            value=_clip(_movers_field(fallers, "▼") or "*none*",
                        limits.EMBED_FIELD_VALUE_MAX),
            inline=False,
        )
    footer = round_label or "No round label"
    embed.set_footer(text=footer)
    _enforce_total(embed)
    return embed


def render_driver_card(
    *,
    display_name: str,
    tier_label: str,
    accent_color: int | None,
    latest: Mapping | None,
    history: Sequence[Mapping],
) -> discord.Embed:
    """
    Driver card: current market value, movement, tier, trend of last N
    published runs. Contract/P/L block is intentionally absent — that
    lights up in Phase 4 when contracts land; showing a placeholder
    line here makes the missing information obvious rather than
    surprising.
    """
    embed = discord.Embed(
        title=display_name,
        colour=_colour(accent_color),
    )
    embed.add_field(name="Tier", value=tier_label, inline=True)
    if latest is None:
        embed.add_field(
            name="Market",
            value="No published valuation yet.",
            inline=False,
        )
    else:
        embed.add_field(
            name="Market",
            value=format_money(latest["market_value"]),
            inline=True,
        )
        embed.add_field(
            name="Week",
            value=(
                f"{_delta_arrow(latest['delta'])} {format_pl(latest['delta'])}"
                + ("  ⚠ capped" if latest["capped"] else "")
            ),
            inline=True,
        )
        embed.add_field(
            name="Rank in tier",
            value=str(latest["rank_in_tier"]),
            inline=True,
        )
    embed.add_field(
        name="Contract",
        value="—  (contracts land in Phase 4)",
        inline=False,
    )
    trend = _trend_field(history)
    if trend:
        embed.add_field(
            name=f"Trend (last {len(history)})",
            value=_clip(trend, limits.EMBED_FIELD_VALUE_MAX),
            inline=False,
        )
    _enforce_total(embed)
    return embed


def render_dashboard(
    *,
    season_name: str,
    tier_rows: Sequence[Mapping],
) -> discord.Embed:
    """
    Cross-tier summary. `tier_rows` is a flat list joined with tier
    metadata (tier_code, tier_label, tier_rank_order, accent_color);
    we group and render one field per tier so tier isolation is
    obvious visually as well as mathematically.
    """
    embed = discord.Embed(
        title=f"Market Dashboard — {season_name}",
        description=(
            "Top-of-tier snapshot. Values are per-tier and do not "
            "interact across tiers."
        ),
    )
    if not tier_rows:
        embed.description = (
            "No published valuations yet. Run "
            "`/market-admin valuation run` per tier, then publish."
        )
        _enforce_total(embed)
        return embed

    grouped: dict[str, list[Mapping]] = {}
    tier_meta: dict[str, tuple[str, int]] = {}
    for row in tier_rows:
        code = row["tier_code"]
        grouped.setdefault(code, []).append(row)
        tier_meta[code] = (row["tier_label"], row["tier_rank_order"])

    for code in sorted(grouped, key=lambda c: tier_meta[c][1]):
        rows = grouped[code]
        label = tier_meta[code][0]
        value_lines: list[str] = []
        for row in rows[:limits.DASHBOARD_PER_TIER]:
            value_lines.extend(_driver_two_line(
                rank=row["rank_in_tier"],
                name=row["display_name"],
                market_value=row["market_value"],
                delta=row["delta"],
                capped=row["capped"],
            ))
        embed.add_field(
            name=label,
            value=_clip("\n".join(value_lines) or "*no drivers*",
                        limits.EMBED_FIELD_VALUE_MAX),
            inline=False,
        )
    _enforce_total(embed)
    return embed


# ── shared building blocks ───────────────────────────────────────────


def _driver_two_line(
    *,
    rank: int,
    name: str,
    market_value: Decimal,
    delta: Decimal,
    capped: bool,
) -> list[str]:
    cap_flag = "  ⚠ capped" if capped else ""
    line1 = f"{rank}. {name}"
    line2 = (
        f"   Market: {format_money(market_value)}  |  "
        f"Week: {_delta_arrow(delta)} {format_pl(delta)}{cap_flag}"
    )
    return [_bound(line1), _bound(line2)]


def _movers_field(rows: Iterable[Mapping], arrow: str) -> str:
    lines: list[str] = []
    for row in rows:
        cap_flag = "  ⚠" if row["capped"] else ""
        lines.append(_bound(
            f"{row['display_name']} — {format_money(row['market_value'])} · "
            f"{arrow} {format_pl(row['delta'])}{cap_flag}"
        ))
    return "\n".join(lines)


def _trend_field(history: Sequence[Mapping]) -> str:
    if not history:
        return ""
    parts: list[str] = []
    for row in history:
        label = row["round_label"]
        value = format_money(row["market_value"])
        delta = format_pl(row["delta"]) if row["delta"] is not None else ""
        arrow = _delta_arrow(row["delta"]) if row["delta"] is not None else "•"
        parts.append(_bound(f"{label}: {value} ({arrow} {delta})"))
    return "\n".join(parts)


def _footer_bits(
    round_label: str | None, page: int, total: int, driver_count: int
) -> str:
    round_bit = round_label or "No round label"
    return f"{round_bit}  ·  Page {page}/{total}  ·  {driver_count} driver(s)"


def _empty_tier_body() -> str:
    return (
        "No drivers in this tier's most recent published run.\n"
        "Publish a valuation to populate this board."
    )


def _delta_arrow(delta: Decimal | None) -> str:
    if delta is None or delta == Decimal(0):
        return "•"
    if delta > Decimal(0):
        return "▲"
    return "▼"


def _colour(accent: int | None) -> discord.Colour:
    if accent is None:
        return discord.Colour.blurple()
    return discord.Colour(accent)


def _bound(line: str) -> str:
    """
    Soft-truncate a rendered line to `RENDER_LINE_WIDTH` characters so
    mobile clients don't wrap awkwardly. The tests assert this bound,
    so keeping it in one place keeps the invariant honest.
    """
    if len(line) <= limits.RENDER_LINE_WIDTH:
        return line
    return line[:limits.RENDER_LINE_WIDTH - 1] + "…"


def _clip(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap - 1] + "…"


def _enforce_total(embed: discord.Embed) -> None:
    """
    Discord rejects an embed whose total serialised text exceeds 6000
    characters. When we hit that ceiling we truncate the last field's
    value with a warning suffix rather than raising — the operational
    outcome (a slightly clipped board) is much better than a silently
    dropped update.
    """
    total = _embed_char_count(embed)
    if total <= limits.EMBED_TOTAL_MAX:
        return
    if not embed.fields:
        overshoot = total - limits.EMBED_TOTAL_MAX
        if embed.description:
            new_len = max(0, len(embed.description) - overshoot - 1)
            embed.description = embed.description[:new_len] + "…"
        return
    last_field = embed.fields[-1]
    overshoot = total - limits.EMBED_TOTAL_MAX
    new_value_len = max(0, len(last_field.value or "") - overshoot - 1)
    embed.set_field_at(
        len(embed.fields) - 1,
        name=last_field.name,
        value=((last_field.value or "")[:new_value_len] + "…"),
        inline=last_field.inline,
    )


def _embed_char_count(embed: discord.Embed) -> int:
    total = 0
    if embed.title:
        total += len(embed.title)
    if embed.description:
        total += len(embed.description)
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    if embed.author and embed.author.name:
        total += len(embed.author.name)
    for field in embed.fields:
        total += len(field.name or "") + len(field.value or "")
    return total
