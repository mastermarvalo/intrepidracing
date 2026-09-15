"""
Contracts desk as an interactive screen (consolidation Screen 10).

The Team Principal's home: who is on the books, what they cost, what cap
and budget room is left, which offers are outstanding, and where the
dead money came from. Every driver, contract and offer is picked from a
database-backed select, so the nine `/contract …` commands' `offer_id`,
`contract_id`, `team` and `tier` arguments are never typed
(CONSOLIDATION.md design rule 1).

Ownership is `OwnedView`, not `AdminOwnedView`: the desk belongs to the
principal of a team, which is how `bot/cogs/contracts.py`
(`_authorised_for_team`) gates the commands it mirrors — admins with
Manage Server may drive any team, a principal only their own. A member
who is neither gets an explanatory screen rather than a silent empty
list.

**The desk is read-only, and that is a gap, not a choice.**
`bot/workflow.py` exposes no offer, counter, withdraw, release or buyout
operation — the transitions live in `bot/contracts/service.py` and are
reached only from the cog, which needs a live `discord.Interaction` and
a `conn`. Panels may not import a cog and this module may not add to
`workflow.py`, so each action here ends in a **prepared route**: a card
that re-reads the current state, shows the validation the action will
face, and prints the exact slash command with the identifiers already
filled in for copying (CONSOLIDATION.md: "Ids in messages stay
copyable"). Nothing on this screen writes. The precise workflow
signatures needed to make these buttons act are listed in
`audit/REPORT_desk_screens.md` (G1).

Paging, money formatting and the "page X of Y + total" statement are
shared with `bot/ui/market_screen.py` so the two screens cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import discord

from bot import db, queries, workflow
from bot.market import budget as budget_engine
from bot.market.money import format_money
from bot.ui import base
from bot.ui.base import (
    COLOR_INFO,
    COLOR_OK,
    COLOR_WARN,
    BackButton,
    BackCallback,
    OwnedView,
    report_error,
)
from bot.ui.market_screen import (
    DESCRIPTION_LIMIT,
    UNKNOWN,
    edit_screen,
    money_or_unknown,
    page_field,
    page_slice,
    pl_or_unknown,
)

DESK_FOOTER = (
    "Read-only desk · typed routes: /contract offer · offers · withdraw · "
    "status · release · buyout"
)

NOT_WRITTEN = (
    "**Nothing was written.** No offer, contract, cap, budget, dead-money "
    "or Discord role changed — this panel only read the database."
)


# ── state ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeskTeam:
    team_id: int
    key: str
    name: str
    color: int | None
    principal_role_id: int | None


@dataclass(frozen=True)
class OfferRow:
    offer_id: int
    driver_id: int
    driver_name: str
    member_id: int | None
    state: str
    kind: str
    salary: Decimal
    term_seasons: int
    term_races: int | None
    expires_at: datetime | None
    market_value: Decimal | None
    validation: dict


@dataclass(frozen=True)
class ContractRow:
    contract_id: int
    driver_id: int
    driver_name: str
    member_id: int | None
    contract_value: Decimal
    term_seasons: int
    season_index: int
    term_races: int | None
    races_served_before: int | None
    contract_type: str
    market_value: Decimal | None
    tier_code: str | None


@dataclass(frozen=True)
class DeskState:
    """One team's whole contract position, re-read before every render."""

    season_name: str | None
    season_id: int | None
    team: DeskTeam
    payroll: Decimal
    dead_money: Decimal
    dead_rows: list
    cap: Decimal | None
    slots_used: int
    slots_total: int | None
    min_salary: Decimal | None
    max_salary: Decimal | None
    min_term_races: int | None
    max_term_races: int | None
    offer_ttl_hours: int | None
    free_agency_open: bool | None
    balance: Decimal | None
    budgets_enforced: bool
    escrow_enabled: bool
    offers: list[OfferRow]
    contracts: list[ContractRow]

    @property
    def effective_payroll(self) -> Decimal:
        return self.payroll + self.dead_money

    @property
    def cap_space(self) -> Decimal | None:
        if self.cap is None:
            return None
        return self.cap - self.effective_payroll

    @property
    def available_budget(self) -> Decimal | None:
        if self.balance is None:
            return None
        return budget_engine.available_to_spend(
            self.balance,
            self.effective_payroll,
            escrow_enabled=self.escrow_enabled,
        )

    @property
    def seats_free(self) -> int | None:
        if self.slots_total is None:
            return None
        return self.slots_total - self.slots_used


def _team_ref(team) -> DeskTeam:
    return DeskTeam(
        team_id=team.id,
        key=team.key,
        name=team.name,
        color=team.color,
        principal_role_id=team.principal_role_id,
    )


async def resolve_desk_teams(
    guild_id: int, role_ids: set[int], *, admin: bool
) -> list[DeskTeam]:
    """
    Teams this member may act for, by the cog's own rule.

    Admins get every team; everyone else gets the teams whose
    `principal_role_id` they hold. `workflow.list_teams` does not carry
    `principal_role_id`, so the role check reads `queries.fetch_all_teams`
    directly (gap G2).
    """
    async with db.connect() as conn:
        teams = await queries.fetch_all_teams(conn, guild_id)
    if admin:
        return [_team_ref(t) for t in sorted(teams, key=lambda t: t.name.casefold())]
    return [
        _team_ref(t)
        for t in sorted(teams, key=lambda t: t.name.casefold())
        if t.principal_role_id is not None and t.principal_role_id in role_ids
    ]


async def load_desk_state(guild_id: int, team: DeskTeam) -> DeskState:
    """
    Read the team's cap sheet, offers and contracts in one connection.

    Config is read at season scope (`tier_id=None`). A team can hold
    contracts in several tiers, so there is no single tier override to
    apply here; per-tier caps are resolved by the service layer when an
    offer is validated, and that difference is called out on the
    prepared-route cards (gap G4).
    """
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            return DeskState(
                season_name=None,
                season_id=None,
                team=team,
                payroll=Decimal("0"),
                dead_money=Decimal("0"),
                dead_rows=[],
                cap=None,
                slots_used=0,
                slots_total=None,
                min_salary=None,
                max_salary=None,
                min_term_races=None,
                max_term_races=None,
                offer_ttl_hours=None,
                free_agency_open=None,
                balance=None,
                budgets_enforced=False,
                escrow_enabled=False,
                offers=[],
                contracts=[],
            )
        cfg = await queries.fetch_league_config_row(conn, season.id, None)
        payroll = await queries.fetch_team_payroll(conn, team.team_id)
        slots_used = await queries.fetch_team_active_slot_count(conn, team.team_id)
        dead_total = await queries.fetch_dead_money_total(
            conn, team.team_id, season.id
        )
        dead_rows = list(
            await queries.fetch_dead_money_for_team(conn, team.team_id, season.id)
        )
        budget_cfg = await queries.fetch_budget_config(conn, season.id, None)
        balance = (
            await queries.fetch_budget_balance(conn, team.team_id, season.id)
            if budget_cfg is not None
            else None
        )
        raw_offers = await queries.fetch_open_offers_for_team(conn, team.team_id)
        offers: list[OfferRow] = []
        for offer in raw_offers:
            driver = await queries.fetch_driver_by_id(conn, offer.driver_id)
            market = await queries.fetch_latest_published_valuation(
                conn, offer.driver_id
            )
            offers.append(
                OfferRow(
                    offer_id=offer.id,
                    driver_id=offer.driver_id,
                    driver_name=driver.display_name if driver else UNKNOWN,
                    member_id=driver.member_id if driver else None,
                    state=offer.state,
                    kind=offer.offer_kind,
                    salary=offer.salary,
                    term_seasons=offer.term_seasons,
                    term_races=getattr(offer, "term_races", None),
                    expires_at=offer.expires_at,
                    market_value=market,
                    validation=offer.validation or {},
                )
            )
        rows = await queries.fetch_team_cap_sheet_rows(conn, team.team_id)
        contracts: list[ContractRow] = []
        for row in rows:
            full = await queries.fetch_contract_by_id(conn, row["contract_id"])
            driver = await queries.fetch_driver_by_id(conn, row["driver_id"])
            tier = (
                await queries.fetch_tier_by_id(conn, full.tier_id)
                if full is not None
                else None
            )
            contracts.append(
                ContractRow(
                    contract_id=row["contract_id"],
                    driver_id=row["driver_id"],
                    driver_name=row["display_name"],
                    member_id=driver.member_id if driver else None,
                    contract_value=row["contract_value"],
                    term_seasons=row["term_seasons"],
                    season_index=full.season_index if full else 1,
                    term_races=getattr(full, "term_races", None),
                    races_served_before=getattr(full, "races_served_before", None),
                    contract_type=row["contract_type"],
                    market_value=row["market_value"],
                    tier_code=tier.code if tier else None,
                )
            )
    return DeskState(
        season_name=season.name,
        season_id=season.id,
        team=team,
        payroll=payroll,
        dead_money=dead_total,
        dead_rows=dead_rows,
        cap=cfg.salary_cap if cfg else None,
        slots_used=slots_used,
        slots_total=cfg.active_driver_slots if cfg else None,
        min_salary=cfg.min_salary if cfg else None,
        max_salary=cfg.max_salary if cfg else None,
        min_term_races=cfg.min_term_races if cfg else None,
        max_term_races=cfg.max_term_races if cfg else None,
        offer_ttl_hours=cfg.offer_ttl_hours if cfg else None,
        free_agency_open=cfg.free_agency_open if cfg else None,
        balance=balance,
        budgets_enforced=bool(budget_cfg and budget_cfg.enforce_budget),
        escrow_enabled=bool(budget_cfg and budget_cfg.escrow_enabled),
        offers=offers,
        contracts=contracts,
    )


@dataclass(frozen=True)
class DriverPick:
    driver_id: int
    display_name: str
    member_id: int | None
    status: str
    tier_code: str
    tier_label: str
    market_value: Decimal | None
    contracted_to: str | None


async def load_driver_picks(guild_id: int, tier_code: str) -> list[DriverPick]:
    """
    Every driver in one tier with market value and current team.

    Free agents sort first — that is who a principal is usually shopping
    for — then contracted drivers, each labelled with their team so an
    offer to a signed driver is a deliberate act.
    """
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            return []
        tier = await queries.fetch_tier(conn, season.id, tier_code)
        if tier is None:
            return []
        drivers = await queries.fetch_drivers_in_tier(conn, tier.id)
        picks: list[DriverPick] = []
        for driver in drivers:
            contract = await queries.fetch_active_contract_for_driver(conn, driver.id)
            team = (
                await queries.fetch_team_by_id(conn, contract.team_id)
                if contract is not None
                else None
            )
            market = await queries.fetch_latest_published_valuation(conn, driver.id)
            picks.append(
                DriverPick(
                    driver_id=driver.id,
                    display_name=driver.display_name,
                    member_id=driver.member_id,
                    status=driver.status,
                    tier_code=tier.code,
                    tier_label=tier.label,
                    market_value=market,
                    contracted_to=team.name if team else None,
                )
            )
    picks.sort(
        key=lambda p: (
            p.contracted_to is not None,
            -(p.market_value or Decimal("-1")),
            p.display_name.casefold(),
        )
    )
    return picks


@dataclass(frozen=True)
class DriverStatus:
    display_name: str
    tier_label: str
    status: str
    market_value: Decimal | None
    active: tuple | None
    history: list[tuple]
    offers: list[tuple]


async def load_driver_status(driver_id: int) -> DriverStatus | None:
    """
    One driver's contract, history and open offers, with team names.

    Team names are resolved here rather than by handing these rows to
    `contract_render.render_contract_status`: that renderer indexes
    `active["team_name"]` and `h["team_name"]`, which the `Contract`
    dataclass `bot.queries` returns does not support at all, so calling
    it with them raises `TypeError` (defect D4).
    """
    async with db.connect() as conn:
        driver = await queries.fetch_driver_by_id(conn, driver_id)
        if driver is None:
            return None
        tier = await queries.fetch_tier_by_id(conn, driver.tier_id)
        market = await queries.fetch_latest_published_valuation(conn, driver_id)
        active = await queries.fetch_active_contract_for_driver(conn, driver_id)
        history = list(
            await queries.fetch_contract_history_for_driver(conn, driver_id)
        )
        offers = list(await queries.fetch_open_offers_for_driver(conn, driver_id))
        wanted = {c.team_id for c in history} | {o.team_id for o in offers}
        if active is not None:
            wanted.add(active.team_id)
        names: dict[int, str] = {}
        for team_id in wanted:
            team = await queries.fetch_team_by_id(conn, team_id)
            names[team_id] = team.name if team else UNKNOWN
    return DriverStatus(
        display_name=driver.display_name,
        tier_label=tier.label if tier else UNKNOWN,
        status=driver.status,
        market_value=market,
        active=(
            (active, names.get(active.team_id, UNKNOWN))
            if active is not None
            else None
        ),
        history=[(c, names.get(c.team_id, UNKNOWN)) for c in history],
        offers=[(o, names.get(o.team_id, UNKNOWN)) for o in offers],
    )


# ── rendering ────────────────────────────────────────────────────────


def expiry_text(when: datetime | None) -> str:
    if when is None:
        return UNKNOWN
    delta = when - datetime.now(UTC)
    hours = int(delta.total_seconds() // 3600)
    if hours < 0:
        return f"expired ({when:%Y-%m-%d %H:%M} UTC)"
    return f"{hours}h left ({when:%Y-%m-%d %H:%M} UTC)"


def _term_text(row: ContractRow | OfferRow) -> str:
    races = row.term_races
    if isinstance(row, ContractRow):
        seasons = f"season {row.season_index} of {row.term_seasons}"
    else:
        seasons = f"{row.term_seasons} season(s)"
    if races is None:
        return f"{seasons}, races {UNKNOWN}"
    return f"{seasons} · {races} race(s) total"


def build_no_team_embed(*, admin: bool, teams_exist: bool) -> discord.Embed:
    if not teams_exist:
        body = (
            "This guild has no teams yet, so there is no contract desk to "
            "open. A commissioner adds them from **/league → Setup → "
            "Teams**."
        )
    elif admin:
        body = (
            "No team is registered against your roles, but you hold Manage "
            "Server, so every team should have been offered. If this screen "
            "is empty the teams table came back empty on this read — try "
            "**Back** and reopen."
        )
    else:
        body = (
            "You are not a Team Principal for any team, so there is nothing "
            "to manage here.\n\n"
            "Principal access comes from a team's principal role. A "
            "commissioner assigns it; the same rule gates `/contract offer` "
            "and `/trade propose`, so this is not a panel-only "
            "restriction.\n\n"
            "**Drivers:** your own offers arrive by DM and are answered with "
            "`/contract accept`, `/contract decline` or `/contract counter`. "
            "Your market page is on the Market screen."
        )
    return discord.Embed(title="📋 Contracts desk", color=COLOR_INFO, description=body)


def build_desk_embed(state: DeskState) -> discord.Embed:
    """Header: cap, budget, seats, free agency, offers, dead money."""
    if state.season_id is None:
        return discord.Embed(
            title=f"📋 Contracts — {state.team.name}",
            description=(
                "No active season, so there is no cap, no budget and no "
                "roster to show. A commissioner activates one from "
                "**/league → Setup → Season**."
            ),
            color=COLOR_INFO,
        )
    cap_lines = [
        f"Payroll: {format_money(state.payroll)}",
        f"Dead money: {format_money(state.dead_money)}",
        f"Effective payroll: {format_money(state.effective_payroll)}",
        f"Cap: {money_or_unknown(state.cap)}",
        f"Cap space: {money_or_unknown(state.cap_space)}",
    ]
    if state.cap is None:
        cap_lines.append(
            "No `league_config` row for this season, so the cap and cap "
            "space are unknown rather than 0 — Setup → Cap & rules seeds it."
        )
    seats = (
        f"{state.slots_used}/{state.slots_total}"
        if state.slots_total is not None
        else f"{state.slots_used}/{UNKNOWN}"
    )
    budget_lines = []
    if state.balance is None:
        budget_lines.append(
            "Budgets are not configured this season — no balance exists, "
            "which is not the same as a balance of 0."
        )
    else:
        budget_lines.append(f"Balance: {format_money(state.balance)}")
        budget_lines.append(
            f"Available to spend: {money_or_unknown(state.available_budget)}"
        )
        budget_lines.append(
            "Enforced: "
            + ("yes — a signing must clear the budget too" if state.budgets_enforced
               else "no — only the cap blocks a signing")
        )
        budget_lines.append(
            "Escrow: " + ("on (salary leaves cash race by race)"
                          if state.escrow_enabled else "off (commitment only)")
        )
    fa = (
        UNKNOWN
        if state.free_agency_open is None
        else ("open" if state.free_agency_open else "closed")
    )
    embed = discord.Embed(
        title=f"📋 Contracts — {state.team.name}",
        description=(
            f"**{state.season_name}** · seats {seats} · free agency {fa}\n"
            f"{len(state.offers)} open offer(s) · "
            f"{len(state.contracts)} active contract(s)"
        ),
        color=(
            discord.Colour(state.team.color)
            if state.team.color is not None
            else COLOR_INFO
        ),
    )
    embed.add_field(name="Salary cap", value="\n".join(cap_lines), inline=False)
    embed.add_field(name="Team budget", value="\n".join(budget_lines), inline=False)
    if state.dead_rows:
        embed.add_field(
            name=f"Dead money ({len(state.dead_rows)} row(s))",
            value=base.truncate_field(
                "\n".join(
                    f"• {format_money(r.amount)} — {r.note or 'no note'}"
                    for r in state.dead_rows
                )
            ),
            inline=False,
        )
    rules = [
        f"Salary band: {money_or_unknown(state.min_salary)} – "
        f"{money_or_unknown(state.max_salary)}",
        f"Term band: {state.min_term_races or UNKNOWN} – "
        f"{state.max_term_races or UNKNOWN} race(s)",
        f"Default offer TTL: {state.offer_ttl_hours or UNKNOWN} hour(s)",
        "Read at season scope; a tier override can be stricter (gap G4).",
    ]
    embed.add_field(name="Offer rules", value="\n".join(rules), inline=False)
    embed.set_footer(text=DESK_FOOTER)
    return embed


def build_offers_embed(state: DeskState, *, page: int) -> discord.Embed:
    """
    Paged list of the team's open offers.

    Built here rather than with `contract_render.render_offers_list`,
    which formats the deadline as `<t:{int(expires_at.timestamp())}:R>`
    and raises `AttributeError` on an offer whose `expires_at` is NULL —
    a state the column allows (defect D3).
    """
    window, page, pages = page_slice(state.offers, page)
    lines = [
        f"`{o.offer_id}` {o.state} · {o.driver_name} · "
        f"{format_money(o.salary)} · {_term_text(o)} · "
        f"expires {expiry_text(o.expires_at)}"
        for o in window
    ]
    embed = discord.Embed(
        title=f"\U0001f4e8 Open offers \u2014 {state.team.name}",
        description=base.truncate_field(
            "\n".join(lines) or "*No open offers.*", DESCRIPTION_LIMIT
        ),
        color=COLOR_INFO,
    )
    page_field(
        embed,
        shown=len(window),
        page=page,
        pages=pages,
        total=len(state.offers),
        noun="offers",
    )
    embed.add_field(
        name="States shown",
        value=(
            "Open states only: "
            + ", ".join(f"`{s}`" for s in queries.OPEN_OFFER_STATES)
            + ". Accepted, declined, withdrawn and expired offers are not "
            "listed here; the History screen keeps them."
        ),
        inline=False,
    )
    embed.set_footer(text="Pick an offer for its detail and withdraw route")
    return embed


def build_offer_detail_embed(state: DeskState, offer: OfferRow) -> discord.Embed:
    lines = [
        f"Driver: **{offer.driver_name}**",
        f"State: `{offer.state}` · kind `{offer.kind}`",
        f"Salary: {format_money(offer.salary)}",
        f"Term: {_term_text(offer)}",
        f"Market value: {money_or_unknown(offer.market_value)}",
        f"Offer vs market: {pl_or_unknown(offer.salary, offer.market_value)}",
        f"Expires: {expiry_text(offer.expires_at)}",
        "",
        f"If signed, payroll would go {format_money(state.payroll)} → "
        f"{format_money(state.payroll + offer.salary)} against a cap of "
        f"{money_or_unknown(state.cap)}.",
    ]
    if state.cap is not None:
        after = state.cap - (state.effective_payroll + offer.salary)
        lines.append(
            f"Cap space after signing: {format_money(after)}"
            + ("  ⚠ over the cap" if after < 0 else "")
        )
    embed = discord.Embed(
        title=f"📨 Offer {offer.offer_id} — {offer.driver_name}",
        description="\n".join(lines),
        color=COLOR_WARN if offer.state == "countered" else COLOR_INFO,
    )
    if offer.validation:
        embed.add_field(
            name="Validation recorded at submit",
            value=base.truncate_field(
                "\n".join(f"• {k}: {v}" for k, v in offer.validation.items())
            ),
            inline=False,
        )
    embed.add_field(
        name="Typed route",
        value=(
            f"Withdraw: `/contract withdraw offer_id: {offer.offer_id}`\n"
            "The driver answers with `/contract accept`, `/contract decline` "
            "or `/contract counter` — a principal cannot accept on their "
            "behalf."
        ),
        inline=False,
    )
    embed.add_field(name="State", value=NOT_WRITTEN, inline=False)
    embed.set_footer(text=DESK_FOOTER)
    return embed


def build_offer_route_embed(state: DeskState, pick: DriverPick) -> discord.Embed:
    """
    Prepared `/contract offer`: every check it will face, then the route.

    Salary and term are not asked for here. `/contract offer` collects
    them in its own modal after the command runs, and duplicating that
    modal would mean this panel validated numbers the service layer would
    then re-validate against a config it, not this screen, resolves.
    """
    checks: list[str] = []
    if state.free_agency_open is None:
        checks.append(f"❓ Free agency flag is {UNKNOWN} (no config row)")
    elif state.free_agency_open:
        checks.append("✅ Free agency is open")
    else:
        checks.append("⛔ Free agency is **closed** — the offer will be rejected")
    if pick.contracted_to is None:
        checks.append("✅ Driver has no active contract")
    else:
        checks.append(
            f"⚠ Driver is under contract to **{pick.contracted_to}** — a "
            "trade, release or buyout has to come first"
        )
    seats = state.seats_free
    if seats is None:
        checks.append(f"❓ Seat count is {UNKNOWN} (no config row)")
    elif seats > 0:
        checks.append(f"✅ {seats} seat(s) free")
    else:
        checks.append("⛔ No free seat — release or trade someone first")
    if state.cap_space is None:
        checks.append(f"❓ Cap space is {UNKNOWN} (no config row)")
    else:
        checks.append(f"ℹ Cap space to work with: {format_money(state.cap_space)}")
    if state.balance is None:
        checks.append("ℹ Budgets not configured, so only the cap applies")
    elif state.budgets_enforced:
        checks.append(
            f"ℹ Budget available: {money_or_unknown(state.available_budget)} "
            "— the binding limit is whichever is smaller"
        )
    else:
        checks.append("ℹ Budget recorded but not enforced")
    mention = f"<@{pick.member_id}>" if pick.member_id else UNKNOWN
    route = (
        "/contract offer\n"
        f"  team: {state.team.key}\n"
        f"  tier: {pick.tier_code}\n"
        f"  driver: {mention}\n"
        "  offer_kind: <New signing | Extension | Trade-and-sign>\n"
        f"  ttl_hours: <24 | 48 | 72 | 168>   (league default "
        f"{state.offer_ttl_hours or UNKNOWN}h)"
    )
    embed = discord.Embed(
        title=f"📝 Prepared offer — {pick.display_name}",
        description=(
            f"Market value {money_or_unknown(pick.market_value)} · "
            f"{pick.tier_label} · status `{pick.status}`\n"
            f"Salary band {money_or_unknown(state.min_salary)} – "
            f"{money_or_unknown(state.max_salary)}, term "
            f"{state.min_term_races or UNKNOWN}–"
            f"{state.max_term_races or UNKNOWN} race(s). Salary and term are "
            "entered in the command's own modal."
        ),
        color=COLOR_INFO,
    )
    embed.add_field(name="Pre-flight", value="\n".join(checks), inline=False)
    embed.add_field(name="Copy this", value=f"```\n{route}\n```", inline=False)
    embed.add_field(
        name="State",
        value=(
            NOT_WRITTEN
            + "\nThe checks above are this screen's reading of season-scope "
            "config; `bot/contracts/rules.py` re-runs them at submit and is "
            "the authority."
        ),
        inline=False,
    )
    embed.set_footer(text=DESK_FOOTER)
    return embed


def dead_money_if_released(row: ContractRow, state: DeskState) -> Decimal:
    """
    Residual a release leaves behind under the current service rules.

    `/contract release` records no dead money — it ends the deal and
    frees the whole salary. `/contract buyout` records exactly the
    amount the principal types. So the honest answer for a release is
    zero residual, and for a buyout it is "whatever you enter, and it
    stays on the cap for the rest of the season". Both are stated
    rather than guessed at.
    """
    del row, state
    return Decimal("0")


def build_release_preview_embed(
    state: DeskState, row: ContractRow, *, armed: bool
) -> discord.Embed:
    payroll_after = state.payroll - row.contract_value
    cap_after = None if state.cap is None else state.cap - (
        payroll_after + state.dead_money
    )
    lines = [
        f"Driver: **{row.driver_name}** ({row.tier_code or UNKNOWN})",
        f"Contract: {format_money(row.contract_value)} · {_term_text(row)}",
        f"Type: `{row.contract_type}`",
        f"Market value: {money_or_unknown(row.market_value)}",
        f"P/L: {pl_or_unknown(row.market_value, row.contract_value)}",
        "",
        f"Payroll {format_money(state.payroll)} → {format_money(payroll_after)}",
        f"Dead money stays at {format_money(state.dead_money)} for a release; "
        "a buyout adds the amount you type.",
        f"Cap space would become {money_or_unknown(cap_after)}",
    ]
    embed = discord.Embed(
        title=f"🚪 Release or buy out — {row.driver_name}",
        description="\n".join(lines),
        color=COLOR_WARN if armed else COLOR_INFO,
    )
    if armed:
        embed.add_field(
            name="Confirm what you are about to do",
            value=(
                "Pressing **Show route** does not release anyone — this "
                "panel cannot write (gap G1). It reveals the exact command "
                "to run.\n\n"
                "**When you run that command:** the contract ends "
                "immediately and cannot be undone from the panel; a buyout "
                "additionally writes dead money that sits on this season's "
                "cap; the ledger records you as the actor; the driver's tier "
                "and enrolment are untouched; open offers to that driver are "
                "**not** cancelled; and `/contract release` attempts to drop "
                "the Discord team role but treats failure as non-fatal, so "
                "check the role afterwards."
            ),
            inline=False,
        )
    embed.add_field(name="State", value=NOT_WRITTEN, inline=False)
    embed.set_footer(text=DESK_FOOTER)
    return embed


def build_release_route_embed(
    state: DeskState, row: ContractRow
) -> discord.Embed:
    remaining = row.contract_value
    route = (
        f"/contract release contract_id: {row.contract_id} note: <reason>\n"
        f"/contract buyout contract_id: {row.contract_id} "
        f"buyout_m: <amount, e.g. {remaining}> note: <reason>"
    )
    embed = discord.Embed(
        title=f"🚪 Route ready — {row.driver_name}",
        description=(
            f"Contract `{row.contract_id}` for **{row.driver_name}** at "
            f"{format_money(row.contract_value)}, re-read just now and still "
            "active."
        ),
        color=COLOR_OK,
    )
    embed.add_field(name="Copy one of these", value=f"```\n{route}\n```", inline=False)
    embed.add_field(
        name="Which one",
        value=(
            "**Release** frees the full salary and records no dead money.\n"
            "**Buyout** frees the salary but charges the amount you type as "
            "dead money against this season's cap, so cap space rises by "
            f"{format_money(row.contract_value)} minus that amount."
        ),
        inline=False,
    )
    embed.add_field(
        name="State",
        value=(
            NOT_WRITTEN
            + f"\nPayroll is still {format_money(state.payroll)} and dead "
            f"money still {format_money(state.dead_money)}."
        ),
        inline=False,
    )
    embed.set_footer(text=DESK_FOOTER)
    return embed


def build_status_embed(status: DriverStatus) -> discord.Embed:
    """Driver-side view: active deal, open offers, every past row."""
    if status.active is None:
        active_lines = ["*No active contract — free agent.*"]
    else:
        contract, team_name = status.active
        active_lines = [
            f"Team: {team_name}",
            f"Value: {format_money(contract.contract_value)}",
            f"Term: season {contract.season_index} of {contract.term_seasons}"
            + (
                f" · {contract.term_races} race(s) total"
                if getattr(contract, "term_races", None) is not None
                else f" · races {UNKNOWN}"
            ),
            f"Type: `{contract.contract_type}`",
            f"Contract id: `{contract.id}`",
            f"P/L vs market: "
            f"{pl_or_unknown(status.market_value, contract.contract_value)}",
        ]
    embed = discord.Embed(
        title=f"\U0001f50e Contract status \u2014 {status.display_name}",
        description=(
            f"{status.tier_label} · status `{status.status}` · market value "
            f"{money_or_unknown(status.market_value)}"
        ),
        color=COLOR_INFO,
    )
    embed.add_field(
        name="Active contract", value="\n".join(active_lines), inline=False
    )
    if status.offers:
        embed.add_field(
            name=f"Open offers ({len(status.offers)})",
            value=base.truncate_field(
                "\n".join(
                    f"`{o.id}` {o.state} · {team} · {format_money(o.salary)} · "
                    f"{o.term_seasons} season(s) · "
                    f"expires {expiry_text(o.expires_at)}"
                    for o, team in status.offers
                )
            ),
            inline=False,
        )
    else:
        embed.add_field(name="Open offers", value="*None.*", inline=False)
    if status.history:
        embed.add_field(
            name=f"History ({len(status.history)} row(s))",
            value=base.truncate_field(
                "\n".join(
                    f"• `{c.id}` {team} · {format_money(c.contract_value)} · "
                    f"`{c.state}`"
                    for c, team in status.history
                )
            ),
            inline=False,
        )
    embed.add_field(
        name="Scope",
        value=(
            f"{len(status.history)} contract row(s) and "
            f"{len(status.offers)} open offer(s) — the complete set for this "
            "driver, page 1 of 1. One row per season served, so a "
            "multi-season deal appears several times (contracts are carried "
            "over, not mutated)."
        ),
        inline=False,
    )
    embed.add_field(name="State", value=NOT_WRITTEN, inline=False)
    embed.set_footer(text="Read-only · equivalent command: /contract status")
    return embed


# ── views ────────────────────────────────────────────────────────────


class ContractsView(OwnedView):
    """Team select · offers · new offer · release · driver status."""

    def __init__(
        self,
        *,
        teams: list[DeskTeam],
        state: DeskState,
        opener_id: int,
        on_back: BackCallback,
        admin: bool,
        team_page: int = 0,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.teams = teams
        self.state = state
        self.on_back = on_back
        self.admin = admin
        self.team_page = team_page
        self.expiry_hint = (
            "Nothing was written — this desk only reads. Typed routes: "
            "`/contract offers`, `/contract offer`, `/contract withdraw`, "
            "`/contract release`, `/contract buyout`."
        )
        window, self.team_page, pages = page_slice(teams, team_page)
        if len(teams) > 1 and window:
            self.add_item(_TeamSelect(window, current=state.team.key))
        if pages > 1:
            self.add_item(_TeamPageButton(-1, disabled=self.team_page == 0, row=1))
            self.add_item(
                _TeamPageButton(1, disabled=self.team_page >= pages - 1, row=1)
            )
        has_season = state.season_id is not None
        self.add_item(_OffersButton(enabled=has_season and bool(state.offers), row=2))
        self.add_item(_NewOfferButton(enabled=has_season, row=2))
        self.add_item(
            _ReleaseButton(enabled=has_season and bool(state.contracts), row=2)
        )
        self.add_item(_StatusButton(enabled=has_season, row=2))
        self.add_item(BackButton(on_back, row=3))

    async def reload(
        self, interaction: discord.Interaction, *, note: str | None = None
    ) -> None:
        state = await load_desk_state(interaction.guild_id, self.state.team)
        view = ContractsView(
            teams=self.teams,
            state=state,
            opener_id=self.opener_id,
            on_back=self.on_back,
            admin=self.admin,
            team_page=self.team_page,
        )
        await edit_screen(interaction, embed=build_desk_embed(state), view=view)
        if note:
            await interaction.followup.send(note, ephemeral=True)


class _TeamSelect(discord.ui.Select):
    def __init__(self, window: list[DeskTeam], *, current: str) -> None:
        super().__init__(
            placeholder="Which team?",
            options=[
                discord.SelectOption(
                    label=t.name[:100], value=t.key, default=t.key == current
                )
                for t in window
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, ContractsView)
        chosen = next((t for t in view.teams if t.key == self.values[0]), None)
        if chosen is None:
            await report_error(interaction, "That team is no longer available.")
            return
        state = await load_desk_state(interaction.guild_id, chosen)
        new_view = ContractsView(
            teams=view.teams,
            state=state,
            opener_id=view.opener_id,
            on_back=view.on_back,
            admin=view.admin,
            team_page=view.team_page,
        )
        await edit_screen(interaction, embed=build_desk_embed(state), view=new_view)


class _TeamPageButton(discord.ui.Button):
    def __init__(self, step: int, *, disabled: bool, row: int) -> None:
        super().__init__(
            label="Prev teams" if step < 0 else "More teams",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self.step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, ContractsView)
        view.team_page += self.step
        await view.reload(interaction)


class _OffersButton(discord.ui.Button):
    def __init__(self, *, enabled: bool, row: int) -> None:
        super().__init__(
            label="Open offers…",
            style=discord.ButtonStyle.primary,
            emoji="📨",
            row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, ContractsView)
        await _OffersView(parent=view).render(interaction)


class _OffersView(OwnedView):
    def __init__(self, *, parent: ContractsView, page: int = 0) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.page = page
        self.state = parent.state

    async def render(self, interaction: discord.Interaction) -> None:
        self.state = await load_desk_state(interaction.guild_id, self.state.team)
        window, self.page, pages = page_slice(self.state.offers, self.page)
        self.clear_items()
        if window:
            self.add_item(_OfferSelect(self, window))
        if pages > 1:
            self.add_item(_OfferPageButton(self, -1, disabled=self.page == 0, row=1))
            self.add_item(
                _OfferPageButton(self, 1, disabled=self.page >= pages - 1, row=1)
            )
        self.add_item(BackButton(self._back, label="Back to desk", row=2))
        await edit_screen(
            interaction,
            embed=build_offers_embed(self.state, page=self.page),
            view=self,
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)


class _OfferPageButton(discord.ui.Button):
    def __init__(
        self, owner: _OffersView, step: int, *, disabled: bool, row: int
    ) -> None:
        super().__init__(
            label="Prev offers" if step < 0 else "More offers",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self._owner = owner
        self._step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        self._owner.page += self._step
        await self._owner.render(interaction)


class _OfferSelect(discord.ui.Select):
    def __init__(self, owner: _OffersView, window: list[OfferRow]) -> None:
        super().__init__(
            placeholder="Which offer?",
            options=[
                discord.SelectOption(
                    label=f"{o.driver_name} — {format_money(o.salary)}"[:100],
                    value=str(o.offer_id),
                    description=f"{o.state} · {expiry_text(o.expires_at)}"[:100],
                )
                for o in window
            ],
        )
        self._owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        owner = self._owner
        state = await load_desk_state(interaction.guild_id, owner.state.team)
        offer_id = int(self.values[0])
        offer = next((o for o in state.offers if o.offer_id == offer_id), None)
        if offer is None:
            await report_error(
                interaction,
                f"Offer `{offer_id}` is no longer open — it was answered or "
                "expired between this list being drawn and your click. "
                "Reloading.",
            )
            await owner.render(interaction)
            return
        view = _DetailView(back=owner.render, opener_id=owner.opener_id)
        await edit_screen(
            interaction, embed=build_offer_detail_embed(state, offer), view=view
        )


class _DetailView(OwnedView):
    """Shared leaf view: one embed and a back button."""

    def __init__(self, *, back, opener_id: int, label: str = "Back") -> None:
        super().__init__(opener_id=opener_id)
        self._back = back
        self.add_item(BackButton(self._go, label=label))

    async def _go(self, interaction: discord.Interaction) -> None:
        await self._back(interaction)


# ── new offer flow ───────────────────────────────────────────────────


class _NewOfferButton(discord.ui.Button):
    def __init__(self, *, enabled: bool, row: int) -> None:
        super().__init__(
            label="New offer…",
            style=discord.ButtonStyle.success,
            emoji="📝",
            row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, ContractsView)
        await _DriverPickerView(
            parent=view, purpose="offer"
        ).render(interaction)


class _StatusButton(discord.ui.Button):
    def __init__(self, *, enabled: bool, row: int) -> None:
        super().__init__(
            label="Driver status…",
            style=discord.ButtonStyle.secondary,
            emoji="🔎",
            row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, ContractsView)
        await _DriverPickerView(
            parent=view, purpose="status"
        ).render(interaction)


class _DriverPickerView(OwnedView):
    """Tier select then paged driver select. Used for offers and status."""

    def __init__(
        self,
        *,
        parent: ContractsView,
        purpose: str,
        tier_code: str | None = None,
        page: int = 0,
    ) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.purpose = purpose
        self.tier_code = tier_code
        self.page = page
        self.picks: list[DriverPick] = []

    async def render(self, interaction: discord.Interaction) -> None:
        tiers = await workflow.list_tiers(interaction.guild_id)
        tiers = sorted(tiers, key=lambda t: (t.rank_order, t.code))
        if self.tier_code is None and tiers:
            self.tier_code = tiers[0].code
        self.picks = (
            await load_driver_picks(interaction.guild_id, self.tier_code)
            if self.tier_code
            else []
        )
        window, self.page, pages = page_slice(self.picks, self.page)
        self.clear_items()
        tier_window, _tp, tier_pages = page_slice(tiers, 0)
        if tier_window:
            self.add_item(_PickerTierSelect(self, tier_window))
        if window:
            self.add_item(_PickerDriverSelect(self, window))
        if pages > 1:
            self.add_item(_PickerPageButton(self, -1, disabled=self.page == 0, row=2))
            self.add_item(
                _PickerPageButton(self, 1, disabled=self.page >= pages - 1, row=2)
            )
        self.add_item(BackButton(self._back, label="Back to desk", row=3))
        title = (
            "📝 Pick a driver to offer" if self.purpose == "offer"
            else "🔎 Pick a driver"
        )
        free = sum(1 for p in self.picks if p.contracted_to is None)
        embed = discord.Embed(
            title=title,
            description=(
                f"Tier `{self.tier_code or UNKNOWN}` · {free} free agent(s) "
                f"of {len(self.picks)} driver(s). Free agents are listed "
                "first, then drivers under contract, each labelled with "
                "their team. Names come from the database — nothing to type."
            ),
            color=COLOR_INFO,
        )
        page_field(
            embed,
            shown=len(window),
            page=self.page,
            pages=pages,
            total=len(self.picks),
            noun="drivers",
        )
        if tier_pages > 1:
            embed.add_field(
                name="Tiers",
                value=(
                    f"Showing the first {len(tier_window)} of {len(tiers)} "
                    "tiers in the select; the rest are reachable from the "
                    "Market screen (this guild has more tiers than one "
                    "select holds)."
                ),
                inline=False,
            )
        await edit_screen(interaction, embed=embed, view=self)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)


class _PickerTierSelect(discord.ui.Select):
    def __init__(self, owner: _DriverPickerView, window: list) -> None:
        super().__init__(
            placeholder="Which tier?",
            options=[
                discord.SelectOption(
                    label=f"{t.label} (`{t.code}`)"[:100],
                    value=t.code,
                    default=t.code == owner.tier_code,
                )
                for t in window
            ],
            row=0,
        )
        self._owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        self._owner.tier_code = self.values[0]
        self._owner.page = 0
        await self._owner.render(interaction)


class _PickerPageButton(discord.ui.Button):
    def __init__(
        self, owner: _DriverPickerView, step: int, *, disabled: bool, row: int
    ) -> None:
        super().__init__(
            label="Prev drivers" if step < 0 else "More drivers",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self._owner = owner
        self._step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        self._owner.page += self._step
        await self._owner.render(interaction)


class _PickerDriverSelect(discord.ui.Select):
    def __init__(self, owner: _DriverPickerView, window: list[DriverPick]) -> None:
        super().__init__(
            placeholder="Which driver?",
            options=[
                discord.SelectOption(
                    label=p.display_name[:100],
                    value=str(p.driver_id),
                    description=(
                        f"{money_or_unknown(p.market_value)} · "
                        + (p.contracted_to or "free agent")
                    )[:100],
                )
                for p in window
            ],
            row=1,
        )
        self._owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        owner = self._owner
        driver_id = int(self.values[0])
        if owner.purpose == "status":
            status = await load_driver_status(driver_id)
            if status is None:
                await report_error(
                    interaction, "That driver row has gone \u2014 reloading."
                )
                await owner.render(interaction)
                return
            view = _DetailView(
                back=owner.render, opener_id=owner.opener_id, label="Back to drivers"
            )
            await edit_screen(
                interaction, embed=build_status_embed(status), view=view
            )
            return
        state = await load_desk_state(interaction.guild_id, owner.parent.state.team)
        picks = await load_driver_picks(interaction.guild_id, owner.tier_code)
        pick = next((p for p in picks if p.driver_id == driver_id), None)
        if pick is None:
            await report_error(
                interaction,
                "That driver is no longer in this tier — reloading the list.",
            )
            await owner.render(interaction)
            return
        view = _DetailView(
            back=owner.render, opener_id=owner.opener_id, label="Back to drivers"
        )
        await edit_screen(
            interaction, embed=build_offer_route_embed(state, pick), view=view
        )


# ── release / buyout flow ────────────────────────────────────────────


class _ReleaseButton(discord.ui.Button):
    def __init__(self, *, enabled: bool, row: int) -> None:
        super().__init__(
            label="Release / buy out…",
            style=discord.ButtonStyle.danger,
            emoji="🚪",
            row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, ContractsView)
        await _ReleaseView(parent=view).render(interaction)


class _ReleaseView(OwnedView):
    def __init__(self, *, parent: ContractsView, page: int = 0) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.page = page
        self.state = parent.state

    async def render(self, interaction: discord.Interaction) -> None:
        self.state = await load_desk_state(interaction.guild_id, self.state.team)
        window, self.page, pages = page_slice(self.state.contracts, self.page)
        self.clear_items()
        if window:
            self.add_item(_ContractSelect(self, window))
        if pages > 1:
            self.add_item(
                _ContractPageButton(self, -1, disabled=self.page == 0, row=1)
            )
            self.add_item(
                _ContractPageButton(self, 1, disabled=self.page >= pages - 1, row=1)
            )
        self.add_item(BackButton(self._back, label="Back to desk", row=2))
        embed = discord.Embed(
            title=f"🚪 Active contracts — {self.state.team.name}",
            description=(
                "Pick a contract to see what releasing or buying it out "
                "would do to payroll, dead money and cap space. Nothing is "
                "released from here."
            ),
            color=COLOR_INFO,
        )
        page_field(
            embed,
            shown=len(window),
            page=self.page,
            pages=pages,
            total=len(self.state.contracts),
            noun="contracts",
        )
        await edit_screen(interaction, embed=embed, view=self)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)


class _ContractPageButton(discord.ui.Button):
    def __init__(
        self, owner: _ReleaseView, step: int, *, disabled: bool, row: int
    ) -> None:
        super().__init__(
            label="Prev contracts" if step < 0 else "More contracts",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self._owner = owner
        self._step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        self._owner.page += self._step
        await self._owner.render(interaction)


class _ContractSelect(discord.ui.Select):
    def __init__(self, owner: _ReleaseView, window: list[ContractRow]) -> None:
        super().__init__(
            placeholder="Which contract?",
            options=[
                discord.SelectOption(
                    label=(
                        f"{c.driver_name} — {format_money(c.contract_value)}"
                    )[:100],
                    value=str(c.contract_id),
                    description=_term_text(c)[:100],
                )
                for c in window
            ],
        )
        self._owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        owner = self._owner
        state = await load_desk_state(interaction.guild_id, owner.state.team)
        contract_id = int(self.values[0])
        row = next(
            (c for c in state.contracts if c.contract_id == contract_id), None
        )
        if row is None:
            await report_error(
                interaction,
                f"Contract `{contract_id}` is no longer active — reloading.",
            )
            await owner.render(interaction)
            return
        view = _ReleaseConfirmView(picker=owner, contract_id=contract_id)
        await edit_screen(
            interaction,
            embed=build_release_preview_embed(state, row, armed=False),
            view=view,
        )


class _ReleaseConfirmView(OwnedView):
    """
    Two-click arm/confirm before the release route is revealed.

    The panel cannot write, so nothing here is irreversible *yet* — but
    the command it hands over is, so the same arm/confirm shape guards
    it: the first click restates the consequences (dead money, ledger
    actor, offers not cancelled, best-effort role drop), the second
    re-reads the contract and prints the command.
    """

    def __init__(self, *, picker: _ReleaseView, contract_id: int) -> None:
        super().__init__(opener_id=picker.opener_id)
        self.picker = picker
        self.contract_id = contract_id
        self.armed = False
        self.expiry_hint = (
            "Nothing was written — the confirm step was never pressed, and "
            "this panel could not have released anyone in any case."
        )
        self.add_item(_ReleaseArmButton(self))
        self.add_item(BackButton(self._back, label="Back to contracts"))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.picker.render(interaction)

    async def refresh(self, interaction: discord.Interaction) -> None:
        state = await load_desk_state(interaction.guild_id, self.picker.state.team)
        row = next(
            (c for c in state.contracts if c.contract_id == self.contract_id), None
        )
        if row is None:
            await report_error(
                interaction,
                f"Contract `{self.contract_id}` is no longer active — it was "
                "ended elsewhere. Reloading the list.",
            )
            await self.picker.render(interaction)
            return
        if not self.armed:
            await edit_screen(
                interaction,
                embed=build_release_preview_embed(state, row, armed=False),
                view=self,
            )
            return
        await edit_screen(
            interaction, embed=build_release_route_embed(state, row), view=self
        )


class _ReleaseArmButton(discord.ui.Button):
    def __init__(self, owner: _ReleaseConfirmView) -> None:
        super().__init__(
            label="Prepare release route",
            style=discord.ButtonStyle.secondary,
            emoji="🚪",
        )
        self._owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        owner = self._owner
        if not owner.armed:
            state = await load_desk_state(
                interaction.guild_id, owner.picker.state.team
            )
            row = next(
                (c for c in state.contracts if c.contract_id == owner.contract_id),
                None,
            )
            if row is None:
                await report_error(
                    interaction,
                    f"Contract `{owner.contract_id}` is no longer active.",
                )
                await owner.picker.render(interaction)
                return
            owner.armed = True
            self.label = "Show route"
            self.style = discord.ButtonStyle.danger
            await edit_screen(
                interaction,
                embed=build_release_preview_embed(state, row, armed=True),
                view=owner,
            )
            await interaction.followup.send(
                "Read the confirm note: the command this reveals ends the "
                "contract for real, a buyout writes dead money against this "
                "season's cap, and nothing has been written yet.",
                ephemeral=True,
            )
            return
        self.disabled = True
        await owner.refresh(interaction)
        await interaction.followup.send(
            "Route revealed from a fresh read. Still unchanged: the "
            "contract, payroll, dead money, the driver's tier and every "
            "open offer.",
            ephemeral=True,
        )


# ── entry point ──────────────────────────────────────────────────────


async def open_contracts(
    interaction: discord.Interaction,
    *,
    on_back: BackCallback,
    opener_id: int | None = None,
) -> None:
    """
    Entry point for the `/league` home screen's Contracts button.

    Opens on the first team the viewer may act for. A member with no
    principal role and no Manage Server gets the explanatory screen
    rather than an empty desk.
    """
    opener = opener_id if opener_id is not None else interaction.user.id
    admin = base.is_admin(interaction)
    role_ids = {
        getattr(r, "id", 0) for r in (getattr(interaction.user, "roles", None) or [])
    }
    teams = await resolve_desk_teams(interaction.guild_id, role_ids, admin=admin)
    if not teams:
        async with db.connect() as conn:
            all_teams = await queries.fetch_all_teams(conn, interaction.guild_id)
        view = _DetailView(back=on_back, opener_id=opener)
        await edit_screen(
            interaction,
            embed=build_no_team_embed(admin=admin, teams_exist=bool(all_teams)),
            view=view,
        )
        return
    state = await load_desk_state(interaction.guild_id, teams[0])
    view = ContractsView(
        teams=teams,
        state=state,
        opener_id=opener,
        on_back=on_back,
        admin=admin,
    )
    await edit_screen(interaction, embed=build_desk_embed(state), view=view)
