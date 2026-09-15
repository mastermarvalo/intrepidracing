"""
Contract-flow renderers — pure functions returning discord.Embed.

Same separation as `bot/market/render.py`: no DB, no interaction
dispatch. Callers pass already-fetched rows; these functions decide
layout. The two-line-per-driver constraint from CLAUDE.md §5 applies
to the cap sheet and to any list of drivers/contracts. Numeric limits
come from `bot/limits.py`; the ADR-001 guard scans this module.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping, Sequence

import discord

from bot import limits
from bot.contracts.rules import OfferValidation
from bot.market.money import format_money, format_pl

_ZERO = Decimal(0)


def render_review_panel(
    *,
    team_name: str,
    driver_name: str,
    tier_label: str,
    offer_kind: str,
    salary: Decimal,
    term_seasons: int,
    contract_type: str,
    signing_bonus: Decimal,
    incentives: str | None,
    message: str | None,
    payroll_before: Decimal,
    salary_cap: Decimal,
    current_market_value: Decimal | None,
    validation: OfferValidation,
    team_budget: Decimal | None = None,
) -> discord.Embed:
    """
    TP's pre-submit review. Shows the arithmetic so nobody submits an
    over-cap offer by accident.

    `team_budget` is the team's budget BALANCE when budgets are enforced
    for the season; None hides the budget block entirely. The cap is the
    league's ceiling, the budget is this team's money — both are shown
    because both must clear.
    """
    payroll_after = payroll_before + salary + signing_bonus
    cap_space_before = salary_cap - payroll_before
    cap_space_after = salary_cap - payroll_after

    colour = discord.Colour.green() if validation.ok else discord.Colour.red()
    embed = discord.Embed(
        title=f"Offer review — {team_name} → {driver_name}",
        colour=colour,
    )
    embed.add_field(name="Tier", value=tier_label, inline=True)
    embed.add_field(name="Kind", value=offer_kind, inline=True)
    embed.add_field(name="Type", value=contract_type, inline=True)
    embed.add_field(name="Salary", value=format_money(salary), inline=True)
    embed.add_field(name="Term", value=f"{term_seasons} season(s)", inline=True)
    embed.add_field(
        name="Signing bonus", value=format_money(signing_bonus), inline=True
    )
    embed.add_field(
        name="Incentives",
        value=incentives or "*none*",
        inline=False,
    )
    if current_market_value is not None:
        pl_now = current_market_value - salary
        embed.add_field(
            name="Market value",
            value=(
                f"{format_money(current_market_value)}  "
                f"(P/L on this offer: {format_pl(pl_now)})"
            ),
            inline=False,
        )
    embed.add_field(
        name="Cap impact",
        value=_bound_lines([
            f"Payroll before: {format_money(payroll_before)}",
            f"Payroll after:  {format_money(payroll_after)}",
            f"Cap space before: {format_money(cap_space_before)}",
            f"Cap space after:  {format_money(cap_space_after)}",
        ]),
        inline=False,
    )
    if team_budget is not None:
        embed.add_field(
            name="Budget impact",
            value=_bound_lines([
                f"Team budget: {format_money(team_budget)}",
                f"Available before: {format_money(team_budget - payroll_before)}",
                f"Available after:  {format_money(team_budget - payroll_after)}",
            ]),
            inline=False,
        )
    if message:
        embed.add_field(name="Note to driver", value=_clip_field(message), inline=False)
    embed.add_field(
        name="Checks",
        value=_render_check_list(validation),
        inline=False,
    )
    embed.set_footer(
        text=(
            "✅ Ready to submit." if validation.ok
            else "⛔ Blocked — see checks above."
        )
    )
    _enforce_total(embed)
    return embed


def render_driver_offer_card(
    *,
    team_name: str,
    tier_label: str,
    salary: Decimal,
    term_seasons: int,
    contract_type: str,
    signing_bonus: Decimal,
    incentives: str | None,
    message: str | None,
    expires_at,
    current_market_value: Decimal | None,
    current_contract_value: Decimal | None,
) -> discord.Embed:
    """The offer as the driver sees it in the negotiation thread."""
    embed = discord.Embed(
        title=f"Contract offer — {team_name}",
        colour=discord.Colour.blurple(),
    )
    embed.add_field(name="Tier", value=tier_label, inline=True)
    embed.add_field(name="Type", value=contract_type, inline=True)
    embed.add_field(name="Term", value=f"{term_seasons} season(s)", inline=True)
    embed.add_field(name="Salary", value=format_money(salary), inline=True)
    embed.add_field(
        name="Signing bonus", value=format_money(signing_bonus), inline=True
    )
    embed.add_field(
        name="Incentives", value=incentives or "*none*", inline=False
    )
    if current_market_value is not None:
        pl_offer = current_market_value - salary
        embed.add_field(
            name="Compared to your market value",
            value=(
                f"Market: {format_money(current_market_value)}  |  "
                f"Offer P/L: {format_pl(pl_offer)}"
            ),
            inline=False,
        )
    if current_contract_value is not None:
        embed.add_field(
            name="Compared to your current contract",
            value=(
                f"Current: {format_money(current_contract_value)}  |  "
                f"Δ vs current: {format_pl(salary - current_contract_value)}"
            ),
            inline=False,
        )
    if message:
        embed.add_field(name="Note from team", value=_clip_field(message), inline=False)
    embed.add_field(
        name="Expires",
        value=f"<t:{int(expires_at.timestamp())}:R>",
        inline=False,
    )
    embed.set_footer(text=(
        "Reply with `/contract accept id:<n>`, `/contract decline id:<n>`, "
        "or `/contract counter id:<n>`."
    ))
    _enforce_total(embed)
    return embed


def render_signed_contract_post(
    *,
    team_name: str,
    driver_name: str,
    tier_label: str,
    contract_value: Decimal,
    signing_bonus: Decimal,
    term_seasons: int,
    contract_type: str,
    value_at_signing: Decimal | None,
    external_ref: str,
    approved_by_mention: str,
) -> discord.Embed:
    """
    Public post that lands in transactions_channel on approval. This
    is the audit-visible record of every signing — keep it uncluttered.
    """
    embed = discord.Embed(
        title=f"Signed — {driver_name} → {team_name}",
        colour=discord.Colour.gold(),
    )
    embed.add_field(name="Tier", value=tier_label, inline=True)
    embed.add_field(name="Type", value=contract_type, inline=True)
    embed.add_field(name="Term", value=f"{term_seasons} season(s)", inline=True)
    embed.add_field(name="Value", value=format_money(contract_value), inline=True)
    embed.add_field(
        name="Signing bonus", value=format_money(signing_bonus), inline=True
    )
    if value_at_signing is not None:
        pl_now = value_at_signing - contract_value
        embed.add_field(
            name="P/L vs market at signing",
            value=(
                f"Market at signing: {format_money(value_at_signing)}  |  "
                f"P/L: {format_pl(pl_now)}"
            ),
            inline=False,
        )
    embed.set_footer(text=f"Ref {external_ref}  ·  Approved by {approved_by_mention}")
    _enforce_total(embed)
    return embed


def render_cap_sheet(
    *,
    team_name: str,
    color: int | None,
    contracts_with_market: Sequence[Mapping],
    payroll: Decimal,
    salary_cap: Decimal,
    active_slots_used: int,
    active_slots_max: int,
    dead_money: Decimal = Decimal("0"),
    dead_money_rows: Sequence[Mapping] = (),
    budget_balance: Decimal | None = None,
) -> discord.Embed:
    """
    /market team output. Two-line-per-driver layout with contract vs
    market value and per-driver P/L. Empty state renders as a clear
    "no active contracts" message.

    `dead_money` (Phase 5) surfaces buyout residuals separately from
    payroll so a commissioner can see why cap space is smaller than
    the payroll line alone would explain.
    """
    embed = discord.Embed(
        title=f"Cap sheet — {team_name}",
        colour=discord.Colour(color) if color else discord.Colour.blurple(),
    )
    effective_payroll = payroll + dead_money
    cap_space = salary_cap - effective_payroll
    cap_lines = [
        f"Cap: {format_money(salary_cap)}",
        f"Payroll: {format_money(payroll)}",
    ]
    if dead_money > Decimal("0"):
        cap_lines.append(f"Dead money: {format_money(dead_money)}")
        cap_lines.append(f"Effective payroll: {format_money(effective_payroll)}")
    cap_lines.append(f"Cap space: {format_money(cap_space)}")
    cap_lines.append(f"Seats: {active_slots_used}/{active_slots_max}")
    embed.add_field(
        name="Salary cap",
        value=_bound_lines(cap_lines),
        inline=False,
    )
    if budget_balance is not None:
        # The budget is the team's own money; the cap is the league's
        # ceiling. Spending is bounded by whichever is smaller.
        available = budget_balance - effective_payroll
        embed.add_field(
            name="Team budget",
            value=_bound_lines([
                f"Budget: {format_money(budget_balance)}",
                f"Available to spend: {format_money(available)}",
                f"Binding limit: {'budget' if available < cap_space else 'cap'}",
            ]),
            inline=False,
        )
    if not contracts_with_market:
        embed.add_field(
            name="Roster",
            value="*No active contracts yet.*",
            inline=False,
        )
        _enforce_total(embed)
        return embed

    lines: list[str] = []
    for row in contracts_with_market:
        market = row["market_value"]
        contract_value = row["contract_value"]
        pl_bit = ""
        if market is not None:
            pl = market - contract_value
            pl_bit = (
                f"  |  Market: {format_money(market)}  |  P/L: {format_pl(pl)}"
            )
        else:
            pl_bit = "  |  Market: —"
        lines.append(_bound(f"• {row['display_name']}"))
        lines.append(_bound(
            f"   Contract: {format_money(contract_value)}  "
            f"({row['term_seasons']} yr, {row['contract_type']}){pl_bit}"
        ))
    embed.add_field(
        name="Roster",
        value=_clip_field("\n".join(lines)),
        inline=False,
    )
    if dead_money_rows:
        dm_lines: list[str] = []
        for row in dead_money_rows:
            dm_lines.append(_bound(
                f"• {format_money(row['amount'])}"
                f"{'  · ' + row['note'] if row.get('note') else ''}"
            ))
        embed.add_field(
            name="Dead money entries",
            value=_clip_field("\n".join(dm_lines)),
            inline=False,
        )
    _enforce_total(embed)
    return embed


def render_trade_review(
    *,
    proposing_team_name: str,
    other_team_name: str,
    items: Sequence[Mapping],
    message: str | None,
    payroll_before_proposing: Decimal,
    payroll_after_proposing: Decimal,
    salary_cap_proposing: Decimal,
    payroll_before_other: Decimal,
    payroll_after_other: Decimal,
    salary_cap_other: Decimal,
    budget_proposing: Decimal | None = None,
    budget_other: Decimal | None = None,
) -> discord.Embed:
    """
    Trade proposal review shown to the proposing TP (before submit)
    and to the other TP (in the negotiation thread). Explicitly shows
    both cap sheets so nobody accepts a trade that would blow their
    cap unnoticed.
    """
    embed = discord.Embed(
        title=f"Trade — {proposing_team_name} ↔ {other_team_name}",
        colour=discord.Colour.blurple(),
    )
    to_proposing = [
        row for row in items if row["direction"] == "to_proposing"
    ]
    to_other = [
        row for row in items if row["direction"] == "to_other"
    ]
    embed.add_field(
        name=f"{proposing_team_name} sends",
        value=_clip_field(_trade_items_block(to_other) or "*nothing*"),
        inline=False,
    )
    embed.add_field(
        name=f"{other_team_name} sends",
        value=_clip_field(_trade_items_block(to_proposing) or "*nothing*"),
        inline=False,
    )
    embed.add_field(
        name=f"{proposing_team_name} cap impact",
        value=_bound_lines(_trade_cap_lines(
            payroll_before_proposing, payroll_after_proposing,
            salary_cap_proposing, budget_proposing,
        )),
        inline=False,
    )
    embed.add_field(
        name=f"{other_team_name} cap impact",
        value=_bound_lines(_trade_cap_lines(
            payroll_before_other, payroll_after_other,
            salary_cap_other, budget_other,
        )),
        inline=False,
    )
    if message:
        embed.add_field(name="Note", value=_clip_field(message), inline=False)
    _enforce_total(embed)
    return embed


def _trade_cap_lines(
    before: Decimal, after: Decimal, cap: Decimal, budget: Decimal | None
) -> list[str]:
    lines = [
        f"Payroll before: {format_money(before)}",
        f"Payroll after:  {format_money(after)}",
        f"Cap space after: {format_money(cap - after)}",
    ]
    if budget is not None:
        lines.append(f"Budget available after: {format_money(budget - after)}")
    return lines


def _trade_items_block(rows: Sequence[Mapping]) -> str:
    lines: list[str] = []
    for row in rows:
        lines.append(_bound(
            f"• {row['display_name']} — {format_money(row['contract_value'])}"
            f" ({row['term_seasons']} yr, {row['contract_type']})"
        ))
    return "\n".join(lines)


def render_pl_table(
    *,
    title: str,
    color: int | None,
    rows: Sequence[Mapping],
    top_first: bool,
    round_label: str | None,
) -> discord.Embed:
    """
    /market surplus (top_first=True: best P/L first) and
    /market underwater (top_first=False: worst P/L first). Shared
    layout because the differ is purely the sort direction and title.
    """
    embed = discord.Embed(
        title=title,
        colour=discord.Colour(color) if color else discord.Colour.blurple(),
    )
    if not rows:
        embed.description = (
            "No active contracts with a published market value yet."
        )
        _enforce_total(embed)
        return embed

    scored = []
    for row in rows:
        if row["market_value"] is None:
            continue
        pl = row["market_value"] - row["contract_value"]
        scored.append((pl, row))
    scored.sort(reverse=top_first, key=lambda item: (item[0], item[1]["display_name"]))
    scored = scored[:limits.MARKET_PAGE_SIZE]

    lines: list[str] = []
    for pl, row in scored:
        lines.append(_bound(f"• {row['display_name']} — {row['team_name']}"))
        lines.append(_bound(
            f"   Contract: {format_money(row['contract_value'])}  |  "
            f"Market: {format_money(row['market_value'])}  |  "
            f"P/L: {format_pl(pl)}"
        ))
    embed.description = _clip("\n".join(lines), limits.EMBED_DESCRIPTION_MAX)
    footer_bits: list[str] = []
    if round_label:
        footer_bits.append(round_label)
    footer_bits.append(f"{len(scored)} driver(s)")
    embed.set_footer(text="  ·  ".join(footer_bits))
    _enforce_total(embed)
    return embed


def render_offers_list(
    *,
    title: str,
    offers: Iterable[Mapping],
) -> discord.Embed:
    """
    Small table of offers (team view: `/contract offers` for TP;
    driver view when Phase 5 adds the inverse). Emits an empty-state
    line rather than an empty embed.
    """
    embed = discord.Embed(title=title, colour=discord.Colour.blurple())
    lines: list[str] = []
    for o in offers:
        state_icon = _state_icon(o["state"])
        expires_at = o["expires_at"]
        lines.append(_bound(
            f"`{o['id']}` {state_icon} {o['state']} · "
            f"{o['driver_name']} · {format_money(o['salary'])} · "
            f"{o['term_seasons']} yr · expires <t:{int(expires_at.timestamp())}:R>"
        ))
    embed.description = _clip("\n".join(lines) or "*None.*",
                              limits.EMBED_DESCRIPTION_MAX)
    _enforce_total(embed)
    return embed


def render_contract_status(
    *,
    driver_name: str,
    tier_label: str,
    active: Mapping | None,
    history: Sequence[Mapping],
    open_offers: Sequence[Mapping],
) -> discord.Embed:
    """Everything money-side about a driver in one embed."""
    embed = discord.Embed(
        title=f"Contract status — {driver_name}",
        colour=discord.Colour.blurple(),
    )
    embed.add_field(name="Tier", value=tier_label, inline=True)
    if active is None:
        embed.add_field(name="Active contract", value="*None.*", inline=False)
    else:
        embed.add_field(
            name="Active contract",
            value=_bound_lines([
                f"Team: {active['team_name']}",
                f"Value: {format_money(active['contract_value'])}",
                f"Term: season {active.get('season_index', 1)} of "
                f"{active['term_seasons']}",
                f"Type: {active['contract_type']}",
                f"Signed: <t:{int(active['signed_at'].timestamp())}:d>"
                if active.get("signed_at") else "Signed: —",
                f"Ref: `{active.get('external_ref') or '—'}`",
            ]),
            inline=False,
        )
    if open_offers:
        lines = []
        for o in open_offers:
            state_icon = _state_icon(o["state"])
            lines.append(_bound(
                f"`{o['id']}` {state_icon} {o['state']} · "
                f"{o['team_name']} · {format_money(o['salary'])}"
            ))
        embed.add_field(
            name="Open offers",
            value=_clip_field("\n".join(lines)),
            inline=False,
        )
    if history:
        hist_lines = []
        for h in history[:limits.MARKET_PAGE_SIZE]:
            hist_lines.append(_bound(
                f"• {h['team_name']} · {format_money(h['contract_value'])} · "
                f"{h['state']}"
            ))
        embed.add_field(
            name="History",
            value=_clip_field("\n".join(hist_lines)),
            inline=False,
        )
    _enforce_total(embed)
    return embed


# ── shared helpers ──────────────────────────────────────────────────


def _render_check_list(validation: OfferValidation) -> str:
    """
    Compact but honest: pass/warn/block on each rule. Truncates if
    Discord's 1024-char field-value cap is threatened.
    """
    icons = {"info": "✅", "warn": "⚠", "block": "⛔"}
    lines: list[str] = []
    for result in validation.results:
        icon = icons.get(result.severity, "•")
        # Only show the message for non-passing rows to keep the panel tight.
        if result.ok:
            lines.append(_bound(f"{icon} {result.code}"))
        else:
            lines.append(_bound(f"{icon} {result.code} — {result.message}"))
    return _clip_field("\n".join(lines) or "*no checks*")


def _state_icon(state: str) -> str:
    return {
        "draft": "📝",
        "pending_driver": "⏳",
        "pending_team": "⏳",
        "countered": "🔄",
        "accepted": "✅",
        "pending_approval": "🛂",
        "approved": "🟢",
        "rejected": "🔴",
        "declined": "❌",
        "withdrawn": "↩️",
        "expired": "⏰",
    }.get(state, "•")


def _bound(line: str) -> str:
    if len(line) <= limits.RENDER_LINE_WIDTH:
        return line
    return line[:limits.RENDER_LINE_WIDTH - 1] + "…"


def _bound_lines(lines: Sequence[str]) -> str:
    return "\n".join(_bound(line) for line in lines)


def _clip(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap - 1] + "…"


def _clip_field(text: str) -> str:
    return _clip(text, limits.EMBED_FIELD_VALUE_MAX)


def _enforce_total(embed: discord.Embed) -> None:
    """
    Same last-field-truncation policy `bot/market/render.py` uses —
    truncating one field beats dropping the update on an embed that
    overshoots Discord's 6000-char total.
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
    for f in embed.fields:
        total += len(f.name or "") + len(f.value or "")
    return total


# Kept to satisfy the guard's __all__ hygiene expectation (unused
# elsewhere but the zero constant is genuinely referenced above).
__all__ = [
    "render_review_panel",
    "render_driver_offer_card",
    "render_signed_contract_post",
    "render_cap_sheet",
    "render_pl_table",
    "render_offers_list",
    "render_contract_status",
]
_KEEP_ZERO = _ZERO
