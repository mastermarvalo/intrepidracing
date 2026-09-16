"""
Market browse as an interactive screen (consolidation Screen 7).

The only panel screen every league member may open, so it extends
`OwnedView` and never `AdminOwnedView`: it performs no writes at all and
gates nothing on Manage Server.

It replaces the seven `/market` commands, all of which take a free-text
`tier`, team `name` or member argument today, which is why "No tier `T!`"
is the usual first result. Here the tier, the driver and the team all
come from a database read rendered as a select, so no identifier is ever
typed (CONSOLIDATION.md design rule 1).

Renderers are reused from `bot/market/render.py` and
`bot/contracts/render.py` wherever they exist — per the spec this screen
is wiring, not new formatting. Two exceptions, both deliberate:

  * the **driver card** is built here. `market_render.render_driver_card`
    reads `latest["rank_in_tier"]`, but the only row `/market driver`
    can give it comes from `queries.fetch_driver_valuation_history`,
    which does not select that column — so the existing command raises
    `KeyError` for any driver who has ever been valued (see
    `audit/REPORT_desk_screens.md`, defect D1). The card here also shows
    contract, P/L and status, which that renderer cannot.
  * every list gets an explicit **page X of Y + total** field, because a
    footer reading `Page 2/7` does not tell a reader how many drivers
    were left out (`G16`).

Reads with no `bot/workflow.py` wrapper (market tables, movers, tier P/L,
cross-tier top, per-driver history, drivers in a tier) are composed here
from `bot.queries` inside one connection, matching what
`bot/ui/money_screen.py` already does for the money reads it needed.
They are read-only, and every render re-reads them rather than reusing a
snapshot taken when the screen opened (design rule 6).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import discord

from bot import db, limits, queries, workflow
from bot.contracts import render as contract_render
from bot.market import budget as budget_engine
from bot.market import render as market_render
from bot.market.money import format_money, format_pl
from bot.ui.base import (
    COLOR_INFO,
    COLOR_WARN,
    SELECT_MAX_OPTIONS,
    BackButton,
    BackCallback,
    OwnedView,
    report_error,
    truncate_field,
)

# A driver with no recorded earnings reads as $0.00M, which here is a
# true zero (nothing paid yet), not an unknown.
_CARD_NO_EARNINGS = Decimal("0")

# Ceiling on the earnings leaderboard read, matching `/market earnings`.
EARNINGS_MAX_ROWS = 500

# A select holds 25 options, so every picker pages at 25 and says so.
ITEMS_PER_PAGE = SELECT_MAX_OPTIONS

# The market *table* keeps the house page size (two lines per driver, ten
# drivers a page — CLAUDE.md §4) so this screen and `/market view` show
# the same page 3. Only the pickers page at 25.
TABLE_PER_PAGE = limits.MARKET_PAGE_SIZE

# Discord's embed ceilings, used when appending a field to an embed a
# shared renderer already closed off.
EMBED_TOTAL_LIMIT = limits.EMBED_TOTAL_MAX
DESCRIPTION_LIMIT = limits.EMBED_DESCRIPTION_MAX

# Every absent number renders as this. Never 0: a driver with no
# published run has an *unknown* market value, and 0 would read as
# "worthless" and quietly poison any P/L the reader computed by eye.
UNKNOWN = "— unknown"
NEEDS_RUN = "— unknown (needs a published run)"

VIEW_TABLE = "table"
VIEW_MOVERS = "movers"
VIEW_SURPLUS = "surplus"
VIEW_UNDERWATER = "underwater"
VIEW_DASHBOARD = "dashboard"
VIEW_EARNINGS = "earnings"

_VIEW_LABELS = {
    VIEW_TABLE: "Market table",
    VIEW_MOVERS: "Movers",
    VIEW_SURPLUS: "Surplus (best P/L)",
    VIEW_UNDERWATER: "Underwater (worst P/L)",
    VIEW_DASHBOARD: "Cross-tier dashboard",
    VIEW_EARNINGS: "Career earnings leaderboard",
}

_TIER_VIEWS = (VIEW_TABLE, VIEW_MOVERS, VIEW_SURPLUS, VIEW_UNDERWATER)

# Views that page. Earnings joins the market table here; the cross-tier
# dashboard and the two P/L lists are fixed-length by design.
_PAGED_VIEWS = (VIEW_TABLE, VIEW_EARNINGS)

# Career earnings are guild-wide and outlive any one season, so this
# view must render in the off-season when `season_id` is None — it is
# the one view that does not need an active season.
_SEASONLESS_VIEWS = (VIEW_EARNINGS,)

_FOOTER = (
    "Read-only · equivalent commands: /market view · movers · driver · "
    "team · surplus · underwater · dashboard · earnings"
)


# ── paging helpers (shared with the contracts and trades desks) ──────


def page_count(total: int, *, per_page: int = ITEMS_PER_PAGE) -> int:
    """Pages needed for `total` items — at least one, so empty is page 1/1."""
    if total <= 0:
        return 1
    return -(-total // per_page)


def page_slice(
    items: list, page: int, *, per_page: int = ITEMS_PER_PAGE
) -> tuple[list, int, int]:
    """`(visible, clamped_page, pages)` for a zero-indexed page."""
    pages = page_count(len(items), per_page=per_page)
    page = min(max(page, 0), pages - 1)
    start = page * per_page
    return items[start : start + per_page], page, pages


def page_field(
    embed: discord.Embed,
    *,
    shown: int,
    page: int,
    pages: int,
    total: int,
    noun: str,
    per_page: int = ITEMS_PER_PAGE,
) -> None:
    """
    State the window explicitly: which slice, which page, and the total.

    Added even when there is only one page. A reader cannot tell a
    complete list from a truncated one by looking at it, and the whole
    point of `G16` is that "top 25 of 400" was indistinguishable from
    "all 25".
    """
    first = page * per_page + 1 if total else 0
    last = first + shown - 1 if shown else 0
    if total == 0:
        value = f"No {noun} to show (page 1 of 1, {total} total)."
    else:
        value = (
            f"Showing {noun} {first}–{last} of {total} "
            f"(page {page + 1} of {pages}). Nothing is hidden: use the "
            "page buttons to reach the rest."
        )
    _add_field(embed, name="Page", value=value)


def _add_field(embed: discord.Embed, *, name: str, value: str) -> None:
    """
    Append a field without pushing the embed past Discord's 6000 total.

    The shared renderers call their own `_enforce_total` before
    returning, so anything added afterwards has to keep its own budget.
    """
    value = truncate_field(value)
    if len(embed) + len(name) + len(value) > EMBED_TOTAL_LIMIT:
        return
    embed.add_field(name=name, value=value, inline=False)


def money_or_unknown(value: Decimal | None, *, missing: str = UNKNOWN) -> str:
    """Money as the league writes it, or an explicit unknown — never 0."""
    if value is None:
        return missing
    return format_money(value)


def pl_or_unknown(
    market: Decimal | None, contract: Decimal | None, *, missing: str = UNKNOWN
) -> str:
    if market is None or contract is None:
        return missing
    return format_pl(market - contract)


# ── state ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TierRef:
    tier_id: int
    code: str
    label: str
    rank_order: int
    role_id: int | None
    accent_color: int | None


@dataclass(frozen=True)
class TeamRef:
    team_id: int
    key: str
    name: str
    payroll: Decimal


@dataclass(frozen=True)
class MarketState:
    """Season, tiers and teams — the spine every sub-view navigates."""

    season_name: str | None
    season_id: int | None
    tiers: list[TierRef]
    teams: list[TeamRef]

    def tier(self, code: str) -> TierRef | None:
        for tier in self.tiers:
            if tier.code == code:
                return tier
        return None

    def team(self, key: str) -> TeamRef | None:
        for team in self.teams:
            if team.key == key:
                return team
        return None


@dataclass(frozen=True)
class TierMarket:
    """One tier's published market, read fresh for each render."""

    tier: TierRef
    round_label: str | None
    published: bool
    rows: list
    risers: list
    fallers: list
    pl_rows: list
    driver_count: int


@dataclass(frozen=True)
class DriverCard:
    display_name: str
    tier_label: str
    accent_color: int | None
    status: str
    round_label: str | None
    market_value: Decimal | None
    delta: Decimal | None
    rank_in_tier: int | None
    capped: bool
    contract_value: Decimal | None
    contract_team: str | None
    contract_term: str | None
    open_offers: int
    history: list
    # Lifetime salary credited to this member across every season.
    # Defaulted so existing constructions stay valid; it is a record of
    # what they have been paid, not a balance they can spend.
    career_earnings: Decimal = _CARD_NO_EARNINGS


async def load_market_state(guild_id: int) -> MarketState:
    """Read the active season, its tiers and the guild's teams."""
    tiers = await workflow.list_tiers(guild_id)
    teams = await workflow.list_teams(guild_id)
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
    return MarketState(
        season_name=season.name if season else None,
        season_id=season.id if season else None,
        tiers=sorted(
            (
                TierRef(
                    tier_id=t.id,
                    code=t.code,
                    label=t.label,
                    rank_order=t.rank_order,
                    role_id=t.tier_role_id,
                    accent_color=t.accent_color,
                )
                for t in tiers
            ),
            key=lambda t: (t.rank_order, t.code),
        ),
        teams=[
            TeamRef(team_id=t.team_id, key=t.key, name=t.name, payroll=t.payroll)
            for t in teams
        ],
    )


def default_tier_code(tiers: list[TierRef], role_ids: set[int]) -> str | None:
    """
    The viewer's own tier when they hold a tier role, else the top tier.

    Pure so the defaulting rule is testable without a guild: a Tier 2
    driver opening the market should land on Tier 2, not on whatever tier
    happens to sort first.
    """
    for tier in tiers:
        if tier.role_id is not None and tier.role_id in role_ids:
            return tier.code
    return tiers[0].code if tiers else None


def role_ids_of(interaction: discord.Interaction) -> set[int]:
    roles = getattr(interaction.user, "roles", None) or []
    return {getattr(r, "id", 0) for r in roles}


async def _latest_published_label(guild_id: int, tier_code: str) -> tuple[str | None, bool]:
    """
    `(round_label, published)` for the tier's newest run.

    `workflow.list_valuations` returns newest first, so the first
    published entry is the run the market table is showing. The label is
    part of the title on purpose: a stale board misread as current is the
    failure this screen exists to prevent.
    """
    try:
        runs = await workflow.list_valuations(guild_id, tier_code=tier_code)
    except workflow.WorkflowError:
        return (None, False)
    for run in runs:
        if run.published:
            return (run.round_label, True)
    return (None, False)


async def load_tier_market(guild_id: int, tier: TierRef) -> TierMarket:
    """Everything the four tier-scoped views need, in one connection."""
    round_label, published = await _latest_published_label(guild_id, tier.code)
    async with db.connect() as conn:
        rows = list(await queries.fetch_market_table_for_tier(conn, tier.tier_id))
        risers, fallers = await queries.fetch_movers_for_tier(
            conn, tier.tier_id, limits.MOVERS_PER_DIRECTION
        )
        pl_rows = list(
            await queries.fetch_tier_contracts_with_market(conn, tier.tier_id)
        )
        drivers = await queries.fetch_drivers_in_tier(conn, tier.tier_id)
    return TierMarket(
        tier=tier,
        round_label=round_label,
        published=published,
        rows=rows,
        risers=list(risers),
        fallers=list(fallers),
        pl_rows=pl_rows,
        driver_count=len(drivers),
    )


async def load_tier_drivers(tier: TierRef) -> list:
    async with db.connect() as conn:
        return list(await queries.fetch_drivers_in_tier(conn, tier.tier_id))


async def load_driver_card(guild_id: int, driver_id: int) -> DriverCard | None:
    """
    One driver's whole market position, including contract and P/L.

    Returns None when the row has gone (a re-tier or a void between the
    select being rendered and clicked), so the caller can say so instead
    of rendering zeroes.
    """
    async with db.connect() as conn:
        driver = await queries.fetch_driver_by_id(conn, driver_id)
        if driver is None:
            return None
        tier = await queries.fetch_tier_by_id(conn, driver.tier_id)
        history = list(
            await queries.fetch_driver_valuation_history(
                conn, driver_id, limits.DRIVER_TREND_ENTRIES
            )
        )
        table = await queries.fetch_market_table_for_tier(conn, driver.tier_id)
        contract = await queries.fetch_active_contract_for_driver(conn, driver_id)
        team = (
            await queries.fetch_team_by_id(conn, contract.team_id)
            if contract is not None
            else None
        )
        offers = await queries.fetch_open_offers_for_driver(conn, driver_id)
        career = await queries.fetch_career_earnings(
            conn, driver.member_id, guild_id
        )

    latest = next(
        (row for row in table if row["driver_id"] == driver_id), None
    )
    term = None
    if contract is not None:
        term = f"season {contract.season_index} of {contract.term_seasons}"
    tier_code = tier.code if tier else None
    round_label, _published = (
        await _latest_published_label(guild_id, tier_code)
        if tier_code
        else (None, False)
    )
    return DriverCard(
        display_name=driver.display_name,
        tier_label=tier.label if tier else UNKNOWN,
        accent_color=tier.accent_color if tier else None,
        status=driver.status,
        round_label=round_label,
        market_value=latest["market_value"] if latest else None,
        delta=latest["delta"] if latest else None,
        rank_in_tier=latest["rank_in_tier"] if latest else None,
        capped=bool(latest["capped"]) if latest else False,
        contract_value=contract.contract_value if contract else None,
        contract_team=team.name if team else None,
        contract_term=term,
        open_offers=len(offers),
        history=history,
        career_earnings=career,
    )


@dataclass(frozen=True)
class TeamCapSheet:
    team_name: str
    color: int | None
    rows: list
    payroll: Decimal
    cap: Decimal | None
    slots_used: int
    slots_total: int | None
    dead_money: Decimal
    dead_rows: list
    balance: Decimal | None
    budgets_enforced: bool
    escrow_enabled: bool


async def load_team_cap_sheet(guild_id: int, team_key: str) -> TeamCapSheet | None:
    """One team's cap sheet, read live. Missing config stays None."""
    async with db.connect() as conn:
        team = await queries.fetch_team(conn, guild_id, team_key.lower())
        if team is None:
            return None
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            return None
        cfg = await queries.fetch_league_config_row(conn, season.id, None)
        rows = list(await queries.fetch_team_cap_sheet_rows(conn, team.id))
        payroll = await queries.fetch_team_payroll(conn, team.id)
        slots_used = await queries.fetch_team_active_slot_count(conn, team.id)
        dead_total = await queries.fetch_dead_money_total(conn, team.id, season.id)
        dead_rows = list(
            await queries.fetch_dead_money_for_team(conn, team.id, season.id)
        )
        budget_cfg = await queries.fetch_budget_config(conn, season.id, None)
        balance = (
            await queries.fetch_budget_balance(conn, team.id, season.id)
            if budget_cfg is not None
            else None
        )
    return TeamCapSheet(
        team_name=team.name,
        color=team.color,
        rows=rows,
        payroll=payroll,
        cap=cfg.salary_cap if cfg else None,
        slots_used=slots_used,
        slots_total=cfg.active_driver_slots if cfg else None,
        dead_money=dead_total,
        dead_rows=dead_rows,
        balance=balance,
        budgets_enforced=bool(budget_cfg and budget_cfg.enforce_budget),
        escrow_enabled=bool(budget_cfg and budget_cfg.escrow_enabled),
    )


# ── rendering ────────────────────────────────────────────────────────


def _title(state: MarketState, tier: TierRef | None, round_label: str | None) -> str:
    season = state.season_name or "no active season"
    if tier is None:
        return f"📈 Market — {season}"
    return f"📈 Market — {tier.label} · {round_label or 'no published round'}"


def build_no_season_embed() -> discord.Embed:
    return discord.Embed(
        title="📈 Market",
        description=(
            "No active season, so there is no market to browse.\n\n"
            "A commissioner creates and activates one from **/league → "
            "Setup → Season**."
        ),
        color=COLOR_INFO,
    )


def build_no_tiers_embed(state: MarketState) -> discord.Embed:
    return discord.Embed(
        title=_title(state, None, None),
        description=(
            f"**{state.season_name}** has no tiers yet, and every market "
            "value is computed inside a single tier (CLAUDE.md §3: markets "
            "are strictly tier-isolated).\n\n"
            "A commissioner adds one from **/league → Setup → Tiers**."
        ),
        color=COLOR_INFO,
    )


def build_table_embed(
    state: MarketState, market: TierMarket, *, page: int
) -> discord.Embed:
    """The paged market table, plus the page/total statement."""
    embed = market_render.render_market_page(
        tier_label=market.tier.label,
        round_label=market.round_label,
        accent_color=market.tier.accent_color,
        drivers=market.rows,
        page=page + 1,
    )
    embed.title = _title(state, market.tier, market.round_label)
    visible, page, pages = page_slice(
        market.rows, page, per_page=TABLE_PER_PAGE
    )
    page_field(
        embed,
        shown=len(visible),
        page=page,
        pages=pages,
        total=len(market.rows),
        noun="drivers",
        per_page=TABLE_PER_PAGE,
    )
    if not market.published:
        _add_field(
            embed,
            name="No published run",
            value=(
                f"`{market.tier.code}` has {market.driver_count} enrolled "
                f"driver(s) but no published valuation, so every value is "
                f"{NEEDS_RUN}. Nothing here is zero — it is unknown."
            ),
        )
    elif market.driver_count > len(market.rows):
        _add_field(
            embed,
            name="Drivers missing from this run",
            value=(
                f"{market.driver_count - len(market.rows)} driver(s) in "
                f"`{market.tier.code}` are enrolled but absent from the "
                f"published run **{market.round_label}**, so their value "
                f"is {NEEDS_RUN}."
            ),
        )
    embed.set_footer(text=_FOOTER)
    return embed


def build_movers_embed(state: MarketState, market: TierMarket) -> discord.Embed:
    embed = market_render.render_movers(
        tier_label=market.tier.label,
        round_label=market.round_label,
        accent_color=market.tier.accent_color,
        risers=market.risers,
        fallers=market.fallers,
    )
    embed.title = f"📈 Movers — {market.tier.label}"
    _add_field(
        embed,
        name="Window",
        value=(
            f"Top {limits.MOVERS_PER_DIRECTION} in each direction from "
            f"**{market.round_label or 'no published round'}** — "
            f"{len(market.risers)} riser(s) and {len(market.fallers)} "
            "faller(s) shown. A driver who held their value appears in "
            "neither list; that is not the same as a fall to zero."
        ),
    )
    embed.set_footer(text=_FOOTER)
    return embed


def build_pl_embed(
    state: MarketState, market: TierMarket, *, best_first: bool
) -> discord.Embed:
    """Surplus / underwater. Same renderer as `/market surplus`."""
    kind = "Surplus" if best_first else "Underwater"
    embed = contract_render.render_pl_table(
        title=f"{kind} — {market.tier.label}",
        color=market.tier.accent_color,
        rows=market.pl_rows,
        top_first=best_first,
        round_label=market.round_label,
    )
    priced = [r for r in market.pl_rows if r["market_value"] is not None]
    unpriced = len(market.pl_rows) - len(priced)
    shown = min(len(priced), limits.MARKET_PAGE_SIZE)
    _add_field(
        embed,
        name="Page",
        value=(
            f"Showing {shown} of {len(priced)} priced contract(s) "
            f"(page 1 of 1 — the renderer ranks by P/L and keeps the top "
            f"{limits.MARKET_PAGE_SIZE}). Open **Driver…** for anyone not "
            "listed here."
        ),
    )
    if unpriced:
        _add_field(
            embed,
            name="Excluded",
            value=(
                f"{unpriced} active contract(s) in `{market.tier.code}` have "
                f"no published market value, so their P/L is {NEEDS_RUN} and "
                "they are left out rather than counted as 0."
            ),
        )
    embed.set_footer(text=_FOOTER)
    return embed


def build_dashboard_embed(
    state: MarketState, rows: list, per_tier: int = limits.DASHBOARD_PER_TIER
) -> discord.Embed:
    embed = market_render.render_dashboard(
        season_name=state.season_name or "no active season", tier_rows=rows
    )
    embed.title = f"📈 Dashboard — {state.season_name or 'no active season'}"
    tiers_with_rows = {r["tier_code"] for r in rows}
    silent = [t.code for t in state.tiers if t.code not in tiers_with_rows]
    _add_field(
        embed,
        name="Scope",
        value=(
            f"Top {per_tier} per tier, {len(rows)} row(s) across "
            f"{len(tiers_with_rows)} of {len(state.tiers)} tier(s). "
            "**Display only** — values never interact across tiers "
            "(CLAUDE.md §3, money invariant 1)."
            + (
                f"\nNo published run yet for: {', '.join(sorted(silent))}."
                if silent
                else ""
            )
        ),
    )
    embed.set_footer(text=_FOOTER)
    return embed


def build_driver_card_embed(card: DriverCard) -> discord.Embed:
    """
    Driver card: market, movement, rank, contract, P/L, trend, status.

    Written here rather than reusing `market_render.render_driver_card`
    for the two reasons in the module docstring — that renderer needs a
    `rank_in_tier` its caller cannot supply, and it still says contracts
    "land in Phase 4".
    """
    lines = [
        f"Tier: {card.tier_label}  |  Status: {card.status or UNKNOWN}",
        f"Round: {card.round_label or 'no published round'}",
        "",
        f"Market: {money_or_unknown(card.market_value, missing=NEEDS_RUN)}",
        f"Week: {_arrow(card.delta)} "
        f"{format_pl(card.delta) if card.delta is not None else UNKNOWN}"
        + ("  ⚠ capped" if card.capped else ""),
        f"Rank in tier: {card.rank_in_tier if card.rank_in_tier else UNKNOWN}",
        "",
        f"Contract: {money_or_unknown(card.contract_value)}"
        + (f"  ({card.contract_team})" if card.contract_team else "  (no active deal)"),
        f"Term: {card.contract_term or UNKNOWN}",
        f"P/L: {pl_or_unknown(card.market_value, card.contract_value)}",
        f"Open offers: {card.open_offers}",
        "",
        f"Career earnings: {format_money(card.career_earnings)}",
    ]
    if card.market_value is None:
        lines.append("")
        lines.append(
            "No published valuation for this driver, so market value and "
            "P/L are unknown — not zero. Publish a run to fill them in."
        )
    embed = discord.Embed(
        title=f"📇 {card.display_name}",
        description=truncate_field("\n".join(lines), DESCRIPTION_LIMIT),
        color=(
            discord.Colour(card.accent_color)
            if card.accent_color is not None
            else COLOR_INFO
        ),
    )
    if card.history:
        trend = "\n".join(
            f"• {h['round_label'] or 'unlabelled'} — "
            f"{money_or_unknown(h['market_value'])} "
            f"({format_pl(h['delta']) if h['delta'] is not None else UNKNOWN})"
            for h in card.history
        )
        _add_field(embed, name=f"Trend (last {len(card.history)})", value=trend)
    else:
        _add_field(
            embed,
            name="Trend",
            value="No published runs for this driver yet.",
        )
    embed.set_footer(text="Read-only · equivalent command: /market driver")
    return embed


def _arrow(delta: Decimal | None) -> str:
    if delta is None:
        return "•"
    if delta > 0:
        return "▲"
    if delta < 0:
        return "▼"
    return "•"


def build_cap_sheet_embed(sheet: TeamCapSheet) -> discord.Embed:
    """
    The team's cap sheet, via the same renderer `/market team` uses.

    `render_cap_sheet` requires a cap and a slot maximum; when the season
    has no `league_config` row there is no cap to render, so we say that
    instead of substituting 0 and reporting a fictional cap space.
    """
    if sheet.cap is None or sheet.slots_total is None:
        return discord.Embed(
            title=f"💼 Cap sheet — {sheet.team_name}",
            description=(
                f"Payroll is {format_money(sheet.payroll)} across "
                f"{sheet.slots_used} active contract(s), but this season has "
                "no `league_config` row, so the cap, cap space and seat "
                f"count are {UNKNOWN}.\n\n"
                "A commissioner seeds them from **Setup → Cap & rules**."
            ),
            color=COLOR_WARN,
        )
    embed = contract_render.render_cap_sheet(
        team_name=sheet.team_name,
        color=sheet.color,
        contracts_with_market=sheet.rows,
        payroll=sheet.payroll,
        salary_cap=sheet.cap,
        active_slots_used=sheet.slots_used,
        active_slots_max=sheet.slots_total,
        dead_money=sheet.dead_money,
        dead_money_rows=[
            {"amount": r.amount, "note": r.note} for r in sheet.dead_rows
        ],
        budget_balance=sheet.balance,
        # D7: the screen already knew the escrow mode; the renderer did
        # not, so it subtracted payroll a second time under escrow.
        escrow_enabled=sheet.escrow_enabled,
    )
    embed.title = f"💼 Cap sheet — {sheet.team_name}"
    unpriced = sum(1 for r in sheet.rows if r["market_value"] is None)
    notes = [
        f"Contracts shown: {len(sheet.rows)} (all active rows — page 1 of 1, "
        "nothing omitted).",
        "Cap compliance is measured on **contract** value; market value only "
        "drives P/L (CLAUDE.md §3, money invariant 3).",
    ]
    if unpriced:
        notes.append(
            f"{unpriced} driver(s) have no published market value, shown as "
            f"`—`: that is {UNKNOWN}, not 0, and their P/L is omitted."
        )
    if sheet.balance is None:
        notes.append(
            "Budgets are not configured this season, so the budget block is "
            "absent rather than showing a 0 balance."
        )
    elif not sheet.budgets_enforced:
        notes.append(
            "Budgets are recorded but **not enforced**: only the cap blocks a "
            "signing today."
        )
    else:
        available = budget_engine.available_to_spend(
            sheet.balance,
            sheet.payroll + sheet.dead_money,
            escrow_enabled=sheet.escrow_enabled,
        )
        notes.append(
            "Budgets are **enforced**: available to spend "
            f"{format_money(available)}, and a signing must clear both this "
            "and the cap."
        )
        if sheet.escrow_enabled:
            notes.append(
                "Escrow is on, so salary leaves the balance race by race and "
                "payroll is **not** also reserved. The `Available to spend` "
                "line in the budget block above subtracts payroll anyway and "
                "is therefore too low — the figure in this field is the one "
                "`bot/market/budget.available_to_spend` enforces (defect D7)."
            )
    _add_field(embed, name="Reading this sheet", value="\n".join(notes))
    embed.set_footer(text="Read-only · equivalent commands: /market team, budget show")
    return embed


# ── main view ────────────────────────────────────────────────────────


async def build_earnings_embed(guild_id: int, *, page: int) -> discord.Embed:
    """
    Career-earnings leaderboard for the panel.

    Reuses the command renderer so the panel and `/market earnings` can
    never disagree, then adds this screen's explicit "page X of Y +
    total" field on top (design rule: a bare `Page 2/7` footer does not
    tell a reader how many drivers were left out — G16).

    Live Discord display names are not resolved here: `build_embed` is
    handed a guild id, not an interaction, and the stored driver name is
    a correct fallback. The `<@id>` last resort still renders as a name.
    """
    async with db.connect() as conn:
        rows = await queries.fetch_earnings_leaderboard(
            conn, guild_id=guild_id, season_id=None, limit=EARNINGS_MAX_ROWS
        )
    embed = market_render.render_earnings_leaderboard(
        rows=rows, page=page + 1, season_label=None
    )
    pages = page_count(len(rows), per_page=limits.EARNINGS_PAGE_SIZE)
    _add_field(
        embed,
        name="Showing",
        value=(
            f"Page {min(page + 1, pages)} of {pages} · "
            f"{len(rows)} driver(s) with earnings"
        ),
    )
    return embed


class MarketView(OwnedView):
    """
    View select · tier select · page buttons · driver and team pickers.

    Opener-locked but not admin-gated: nothing here writes, so a league
    member browsing their own tier is exactly the intended use.
    """

    def __init__(
        self,
        *,
        state: MarketState,
        opener_id: int,
        on_back: BackCallback,
        view_kind: str = VIEW_TABLE,
        tier_code: str | None = None,
        page: int = 0,
        tier_page: int = 0,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.state = state
        self.on_back = on_back
        self.view_kind = view_kind
        self.tier_code = tier_code
        self.page = page
        self.tier_page = tier_page
        self.expiry_hint = (
            "Nothing was in flight — this screen only reads. The typed "
            "routes are `/market view`, `movers`, `driver`, `team`, "
            "`surplus`, `underwater` and `dashboard`."
        )

        self.add_item(_ViewSelect(view_kind))
        tier_window, self.tier_page, tier_pages = page_slice(
            state.tiers, tier_page
        )
        if tier_window:
            self.add_item(_TierSelect(tier_window, current=tier_code))
        if tier_pages > 1:
            self.add_item(
                _TierPageButton(-1, disabled=self.tier_page == 0, row=2)
            )
            self.add_item(
                _TierPageButton(
                    1, disabled=self.tier_page >= tier_pages - 1, row=2
                )
            )
        # The market table pages only once a tier is chosen; earnings
        # are guild-wide and page immediately.
        pages_here = view_kind in _PAGED_VIEWS and (
            view_kind != VIEW_TABLE or tier_code is not None
        )
        if pages_here:
            self.add_item(_PageButton(-1, disabled=page == 0, row=3))
            self.add_item(_PageButton(1, row=3))
        self.add_item(
            _DriversButton(row=4, enabled=tier_code is not None)
        )
        self.add_item(_TeamsButton(row=4, enabled=bool(state.teams)))
        self.add_item(BackButton(on_back, row=4))

    # -- rendering -----------------------------------------------------

    async def render(
        self, interaction: discord.Interaction, *, note: str | None = None
    ) -> None:
        """
        Re-read and redraw. Never renders from the opening snapshot: a
        publish between two clicks must show up on the second one.
        """
        state = await load_market_state(interaction.guild_id)
        view = MarketView(
            state=state,
            opener_id=self.opener_id,
            on_back=self.on_back,
            view_kind=self.view_kind,
            tier_code=self.tier_code,
            page=self.page,
            tier_page=self.tier_page,
        )
        embed = await view.build_embed(interaction.guild_id)
        await edit_screen(interaction, embed=embed, view=view)
        if note:
            await interaction.followup.send(note, ephemeral=True)

    async def build_embed(self, guild_id: int) -> discord.Embed:
        state = self.state
        if self.view_kind in _SEASONLESS_VIEWS:
            return await build_earnings_embed(guild_id, page=self.page)
        if state.season_id is None:
            return build_no_season_embed()
        if not state.tiers:
            return build_no_tiers_embed(state)
        if self.view_kind == VIEW_DASHBOARD:
            async with db.connect() as conn:
                rows = list(
                    await queries.fetch_cross_tier_top(
                        conn, state.season_id, limits.DASHBOARD_PER_TIER
                    )
                )
            return build_dashboard_embed(state, rows)

        tier = state.tier(self.tier_code) if self.tier_code else None
        if tier is None:
            return discord.Embed(
                title=_title(state, None, None),
                description=(
                    "Pick a tier below. Values are computed inside one tier "
                    "only, so there is no all-tiers market table — the "
                    "**Cross-tier dashboard** view is the display-only "
                    "summary."
                ),
                color=COLOR_INFO,
            )
        market = await load_tier_market(guild_id, tier)
        if self.view_kind == VIEW_MOVERS:
            return build_movers_embed(state, market)
        if self.view_kind == VIEW_SURPLUS:
            return build_pl_embed(state, market, best_first=True)
        if self.view_kind == VIEW_UNDERWATER:
            return build_pl_embed(state, market, best_first=False)
        return build_table_embed(state, market, page=self.page)


async def edit_screen(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed,
    view: discord.ui.View,
) -> None:
    """Edit in place whether or not the interaction was already answered."""
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)
    if isinstance(view, OwnedView):
        await view.bind_message(interaction)


class _ViewSelect(discord.ui.Select):
    def __init__(self, current: str) -> None:
        super().__init__(
            placeholder="Which view?",
            options=[
                discord.SelectOption(
                    label=label, value=code, default=code == current
                )
                for code, label in _VIEW_LABELS.items()
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, MarketView)
        view.view_kind = self.values[0]
        view.page = 0
        if view.view_kind in _TIER_VIEWS and view.tier_code is None:
            view.tier_code = default_tier_code(
                view.state.tiers, role_ids_of(interaction)
            )
        await view.render(interaction)


class _TierSelect(discord.ui.Select):
    def __init__(self, window: list[TierRef], *, current: str | None) -> None:
        super().__init__(
            placeholder="Which tier?",
            options=[
                discord.SelectOption(
                    label=f"{t.label} (`{t.code}`)"[:100],
                    value=t.code,
                    default=t.code == current,
                )
                for t in window
            ],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, MarketView)
        view.tier_code = self.values[0]
        view.page = 0
        await view.render(interaction)


class _TierPageButton(discord.ui.Button):
    def __init__(self, step: int, *, disabled: bool = False, row: int) -> None:
        super().__init__(
            label="Prev tiers" if step < 0 else "More tiers",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self.step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, MarketView)
        view.tier_page += self.step
        await view.render(interaction)


class _PageButton(discord.ui.Button):
    def __init__(self, step: int, *, disabled: bool = False, row: int) -> None:
        super().__init__(
            label="Prev page" if step < 0 else "Next page",
            style=discord.ButtonStyle.secondary,
            emoji="◀" if step < 0 else "▶",
            disabled=disabled,
            row=row,
        )
        self.step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, MarketView)
        view.page = max(0, view.page + self.step)
        await view.render(interaction)


# ── driver browser ───────────────────────────────────────────────────


class _DriversButton(discord.ui.Button):
    def __init__(self, *, row: int, enabled: bool) -> None:
        super().__init__(
            label="Driver…",
            style=discord.ButtonStyle.primary,
            emoji="📇",
            row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, MarketView)
        tier = view.state.tier(view.tier_code) if view.tier_code else None
        if tier is None:
            await report_error(interaction, "Pick a tier first.")
            return
        browser = _DriverBrowserView(parent=view, tier=tier)
        await browser.render(interaction)


class _DriverBrowserView(OwnedView):
    """Paged driver picker for one tier — 25 a page, total always stated."""

    def __init__(
        self, *, parent: MarketView, tier: TierRef, page: int = 0
    ) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.tier = tier
        self.page = page
        self.drivers: list = []

    async def render(self, interaction: discord.Interaction) -> None:
        self.drivers = await load_tier_drivers(self.tier)
        window, self.page, pages = page_slice(self.drivers, self.page)
        self.clear_items()
        if window:
            self.add_item(_DriverSelect(self, window))
        if pages > 1:
            self.add_item(
                _DriverPageButton(self, -1, disabled=self.page == 0, row=1)
            )
            self.add_item(
                _DriverPageButton(
                    self, 1, disabled=self.page >= pages - 1, row=1
                )
            )
        self.add_item(BackButton(self._back, label="Back to market", row=2))
        embed = discord.Embed(
            title=f"📇 Drivers — {self.tier.label}",
            description=(
                "Pick a driver for their card: market value, movement, "
                "contract and P/L. Every name comes from the database, so "
                "there is no id or display name to type."
            ),
            color=COLOR_INFO,
        )
        page_field(
            embed,
            shown=len(window),
            page=self.page,
            pages=pages,
            total=len(self.drivers),
            noun="drivers",
        )
        await edit_screen(interaction, embed=embed, view=self)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.render(interaction)


class _DriverPageButton(discord.ui.Button):
    def __init__(
        self, browser: _DriverBrowserView, step: int, *, disabled: bool, row: int
    ) -> None:
        super().__init__(
            label="Prev drivers" if step < 0 else "More drivers",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self._owner = browser
        self._step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        self._owner.page += self._step
        await self._owner.render(interaction)


class _DriverSelect(discord.ui.Select):
    def __init__(self, browser: _DriverBrowserView, window: list) -> None:
        super().__init__(
            placeholder="Which driver?",
            options=[
                discord.SelectOption(
                    label=d.display_name[:100],
                    value=str(d.id),
                    description=f"status: {d.status}"[:100],
                )
                for d in window
            ],
        )
        self._owner = browser

    async def callback(self, interaction: discord.Interaction) -> None:
        browser = self._owner
        card = await load_driver_card(
            interaction.guild_id, int(self.values[0])
        )
        if card is None:
            await report_error(
                interaction,
                "That driver row has gone — reloading the list.",
            )
            await browser.render(interaction)
            return
        view = _CardView(browser=browser)
        await edit_screen(interaction, embed=build_driver_card_embed(card), view=view)


class _CardView(OwnedView):
    def __init__(self, *, browser: _DriverBrowserView) -> None:
        super().__init__(opener_id=browser.opener_id)
        self._owner = browser
        self.add_item(BackButton(self._back, label="Back to drivers"))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self._owner.render(interaction)


# ── team browser ─────────────────────────────────────────────────────


class _TeamsButton(discord.ui.Button):
    def __init__(self, *, row: int, enabled: bool) -> None:
        super().__init__(
            label="Team…",
            style=discord.ButtonStyle.primary,
            emoji="💼",
            row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, MarketView)
        browser = _TeamBrowserView(parent=view)
        await browser.render(interaction)


class _TeamBrowserView(OwnedView):
    def __init__(self, *, parent: MarketView, page: int = 0) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.page = page
        self.teams: list[TeamRef] = []

    async def render(self, interaction: discord.Interaction) -> None:
        state = await load_market_state(interaction.guild_id)
        self.teams = state.teams
        window, self.page, pages = page_slice(self.teams, self.page)
        self.clear_items()
        if window:
            self.add_item(_TeamSelect(self, window))
        if pages > 1:
            self.add_item(
                _TeamPageButton(self, -1, disabled=self.page == 0, row=1)
            )
            self.add_item(
                _TeamPageButton(self, 1, disabled=self.page >= pages - 1, row=1)
            )
        self.add_item(BackButton(self._back, label="Back to market", row=2))
        embed = discord.Embed(
            title="💼 Teams",
            description=(
                "Pick a team for its cap sheet: payroll, dead money, cap "
                "space, seats and per-driver P/L."
            ),
            color=COLOR_INFO,
        )
        page_field(
            embed,
            shown=len(window),
            page=self.page,
            pages=pages,
            total=len(self.teams),
            noun="teams",
        )
        await edit_screen(interaction, embed=embed, view=self)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.render(interaction)


class _TeamPageButton(discord.ui.Button):
    def __init__(
        self, browser: _TeamBrowserView, step: int, *, disabled: bool, row: int
    ) -> None:
        super().__init__(
            label="Prev teams" if step < 0 else "More teams",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self._owner = browser
        self._step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        self._owner.page += self._step
        await self._owner.render(interaction)


class _TeamSelect(discord.ui.Select):
    def __init__(self, browser: _TeamBrowserView, window: list[TeamRef]) -> None:
        super().__init__(
            placeholder="Which team?",
            options=[
                discord.SelectOption(
                    label=f"{t.name} · payroll {format_money(t.payroll)}"[:100],
                    value=t.key,
                )
                for t in window
            ],
        )
        self._owner = browser

    async def callback(self, interaction: discord.Interaction) -> None:
        browser = self._owner
        sheet = await load_team_cap_sheet(
            interaction.guild_id, self.values[0]
        )
        if sheet is None:
            await report_error(
                interaction,
                "That team or its season has gone — reloading the list.",
            )
            await browser.render(interaction)
            return
        view = _CapSheetView(browser=browser)
        await edit_screen(interaction, embed=build_cap_sheet_embed(sheet), view=view)


class _CapSheetView(OwnedView):
    def __init__(self, *, browser: _TeamBrowserView) -> None:
        super().__init__(opener_id=browser.opener_id)
        self._owner = browser
        self.add_item(BackButton(self._back, label="Back to teams"))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self._owner.render(interaction)


# ── entry point ──────────────────────────────────────────────────────


async def open_market(
    interaction: discord.Interaction,
    *,
    on_back: BackCallback,
    opener_id: int | None = None,
) -> None:
    """
    Entry point for the `/league` home screen's Market button.

    Defaults to the viewer's own tier when they hold a tier role and to
    the top tier otherwise, on page 1, with the tier's latest published
    round in the title.
    """
    state = await load_market_state(interaction.guild_id)
    view = MarketView(
        state=state,
        opener_id=opener_id if opener_id is not None else interaction.user.id,
        on_back=on_back,
        tier_code=default_tier_code(state.tiers, role_ids_of(interaction)),
    )
    embed = await view.build_embed(interaction.guild_id)
    await edit_screen(interaction, embed=embed, view=view)
