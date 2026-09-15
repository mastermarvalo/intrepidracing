"""
The commissioner approval queue as an interactive screen.

Previously the panel showed two counts and told the admin to go type
`/market-admin approve offer_id:<id>` — without listing the ids, so they
had to go find them. This screen lists each pending item with its terms
and gives it Approve / Reject buttons.

Two things this screen owes the commissioner, learned the hard way:

* **Offers and trades are separate queues.** Building them into one
  select and cutting it at 25 made pending trades unreachable whenever
  the offer list filled the select on its own, even though the embed
  above still listed them (G17). Each kind now gets its own paged
  select, and neither is ever silently truncated.
* **Approving is irreversible, so it needs context and a confirm.**
  Approve used to fire on the first click, sitting next to Reject, with
  nothing on screen about what the signing would do to the team's cap or
  budget (G13). It now shows payroll before/after, cap space, budget
  headroom, the driver's market value and the offer-vs-market delta,
  roster slots — and arms before it acts.

Every action routes through `bot.approvals`, the same module the slash
commands use, so the audit trail and role side-effects are identical
whichever route a commissioner takes. Context reads go through
`bot.workflow`; this module adds no database access of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import discord

from bot import approvals, workflow
from bot.market.money import format_money, format_pl
from bot.ui import base
from bot.ui.base import (
    COLOR_OK,
    COLOR_WARN,
    SELECT_MAX_OPTIONS,
    AdminOwnedView,
    BackButton,
    BackCallback,
    report_error,
    truncate_field,
)

# One page of either queue is one full select.
_QUEUE_PAGE_SIZE = SELECT_MAX_OPTIONS

# Read deeper than one page so paging has something to page through.
# `fetch_pending_queue` applies its limit to the offer and trade lists
# separately, so this is a per-kind depth, not a combined one. Four
# pages is a working compromise: deep enough that a real backlog stays
# reachable, shallow enough that the screen does not read the whole
# table to render one select.
_QUEUE_PAGE_DEPTH = 4
_QUEUE_FETCH_LIMIT = _QUEUE_PAGE_SIZE * _QUEUE_PAGE_DEPTH

# How many budget ledger rows the context read asks for. Nothing on this
# screen renders them; the summary totals are what we are after.
_BUDGET_RECENT_LIMIT = 1

_OFFER = "offer"
_TRADE = "trade"

_TYPED_FALLBACK = (
    "Typed fallback for anything past the last page: "
    "`/market-admin approve offer_id:<id>` · "
    "`/market-admin approve-trade trade_id:<id>`."
)


def page_count(total: int, page_size: int = _QUEUE_PAGE_SIZE) -> int:
    """How many pages `total` rows need — always at least one."""
    if total <= 0:
        return 1
    return (total + page_size - 1) // page_size


def page_slice(rows: list, page: int, page_size: int = _QUEUE_PAGE_SIZE) -> list:
    """One page of `rows`; an out-of-range page is empty, not an error."""
    start = max(page, 0) * page_size
    return rows[start : start + page_size]


def _page_note(kind_label: str, total: int, *, page: int, pages: int) -> str:
    """One line stating which slice of a queue is currently pickable."""
    if total == 0:
        return f"No pending {kind_label}."
    if pages == 1:
        return f"All **{total}** pending {kind_label} are pickable below."
    return (
        f"Showing **{total}** pending {kind_label} across **{pages}** pages "
        f"(page **{page + 1}**) — use the {kind_label} ◀ ▶ buttons."
    )


def build_approvals_embed(
    queue: approvals.PendingQueue,
    *,
    offer_page: int = 0,
    offer_pages: int = 1,
    trade_page: int = 0,
    trade_pages: int = 1,
) -> discord.Embed:
    """The queue itself — ids visible, so nothing has to be looked up."""
    if queue.season_id is None:
        return discord.Embed(
            title="📋 Awaiting approval",
            description="No active season, so nothing can be pending.",
            color=COLOR_WARN,
        )

    if queue.is_empty:
        return discord.Embed(
            title="📋 Awaiting approval",
            description="Queue is clear — nothing is waiting on you.",
            color=COLOR_OK,
        )

    embed = discord.Embed(
        title="📋 Awaiting approval",
        description=(
            f"**{len(queue.offers)}** contract offer(s) and "
            f"**{len(queue.trades)}** trade(s) in **{queue.season_name}**.\n"
            "Oldest first. Offers and trades have their own picker below, "
            "so a long offer queue never hides a pending trade."
        ),
        color=COLOR_WARN,
    )

    if queue.offers:
        lines = [
            f"`{o.offer_id}` · **{o.driver_name}** → {o.team_name} "
            f"(`{o.tier_code}`) · {format_money(o.salary)}/season × "
            f"{o.term_seasons}"
            for o in queue.offers
        ]
        embed.add_field(
            name="Contract offers",
            value=truncate_field("\n".join(lines)),
            inline=False,
        )

    if queue.trades:
        lines = [
            f"`{t.trade_id}` · {t.proposing_team_name} ⇄ {t.other_team_name} "
            f"· {t.item_count} contract(s)"
            for t in queue.trades
        ]
        embed.add_field(
            name="Trades", value=truncate_field("\n".join(lines)), inline=False
        )

    # State what is reachable rather than leaving the admin to infer it
    # from a select they cannot scroll past.
    picker_lines = [
        _page_note(
            "offers", len(queue.offers), page=offer_page, pages=offer_pages
        ),
        _page_note(
            "trades", len(queue.trades), page=trade_page, pages=trade_pages
        ),
    ]
    if _queue_read_is_capped(queue):
        picker_lines.append(
            f"⚠ Only the oldest {_QUEUE_FETCH_LIMIT} of each kind were "
            "read, so there may be more pending than shown."
        )
    picker_lines.append(_TYPED_FALLBACK)
    embed.add_field(
        name="Pickers",
        value=truncate_field("\n".join(picker_lines)),
        inline=False,
    )

    embed.set_footer(
        text="Equivalent commands: /market-admin approve · approve-trade"
    )
    return embed


def _queue_read_is_capped(queue: approvals.PendingQueue) -> bool:
    """
    Whether either list came back exactly at the fetch limit.

    A list that is exactly the limit long is indistinguishable from a
    longer one that got cut, so the screen says so instead of implying
    the queue is fully shown.
    """
    return (
        len(queue.offers) >= _QUEUE_FETCH_LIMIT
        or len(queue.trades) >= _QUEUE_FETCH_LIMIT
    )


# ── Offer context (what approving will actually do) ───────────────────


@dataclass(frozen=True)
class OfferContext:
    """
    The money and roster picture behind one pending offer.

    Every field is optional: this is assembled from several reads, any of
    which can legitimately have nothing to say (budgets not configured,
    no valuation published yet, team renamed). A missing value is
    rendered as an explicit "unknown" rather than a zero, because a zero
    here reads as a real cap or a real market value.
    """

    payroll_before: Decimal | None = None
    payroll_after: Decimal | None = None
    salary_cap: Decimal | None = None
    budget_balance: Decimal | None = None
    budget_headroom_after: Decimal | None = None
    budgets_enforced: bool = False
    market_value: Decimal | None = None
    has_published_valuation: bool = False
    slots_used: int | None = None
    active_driver_slots: int | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def cap_space_after(self) -> Decimal | None:
        if self.salary_cap is None or self.payroll_after is None:
            return None
        return self.salary_cap - self.payroll_after

    @property
    def over_cap(self) -> bool:
        space = self.cap_space_after
        return space is not None and space < 0

    @property
    def offer_vs_market(self) -> Decimal | None:
        """Market value minus the offer — the P/L baseline at signing."""
        if self.market_value is None:
            return None
        return self.market_value - self._salary

    @property
    def slots_full(self) -> bool:
        return (
            self.slots_used is not None
            and self.active_driver_slots is not None
            and self.slots_used >= self.active_driver_slots
        )

    _salary: Decimal = Decimal(0)


async def gather_offer_context(
    guild_id: int, offer: approvals.PendingOffer
) -> OfferContext:
    """
    Assemble the approval context for one offer from existing reads.

    Deliberately forgiving: a commissioner reviewing an offer should
    never be blocked from seeing the terms because a budget row is
    missing. Anything that cannot be resolved becomes a note on the
    embed.

    `PendingOffer` carries names, not ids, so the team is matched by name
    and the driver by (name, tier). That is a known weak join — see the
    handover notes; it wants `team_id` / `driver_id` on `PendingOffer`.
    """
    notes: list[str] = []
    payroll_before: Decimal | None = None
    team_key: str | None = None
    try:
        teams = await workflow.list_teams(guild_id)
    except workflow.WorkflowError as exc:
        notes.append(f"Payroll unavailable: {exc}")
        teams = []
    team = next((t for t in teams if t.name == offer.team_name), None)
    if team is not None:
        payroll_before = team.payroll or Decimal(0)
        team_key = team.key
    elif teams:
        notes.append(
            f"No team named **{offer.team_name}** in this guild, so the "
            "payroll and cap impact below could not be computed."
        )

    salary = offer.salary or Decimal(0)
    bonus = offer.signing_bonus or Decimal(0)
    # Matches the offer-validation matrix: the signing bonus counts
    # against the cap at signing, alongside the annual salary.
    payroll_after = (
        None if payroll_before is None else payroll_before + salary + bonus
    )

    market_value: Decimal | None = None
    has_valuation = False
    slots_used: int | None = None
    try:
        drivers = await workflow.list_drivers_in_season(guild_id)
    except workflow.WorkflowError as exc:
        notes.append(f"Driver market data unavailable: {exc}")
        drivers = []
    matches = [
        d
        for d in drivers
        if d.display_name == offer.driver_name
        and d.tier_code == offer.tier_code
    ]
    if len(matches) > 1:
        notes.append(
            f"⚠ {len(matches)} drivers named **{offer.driver_name}** in tier "
            f"`{offer.tier_code}` — the valuation below may be the wrong one."
        )
    if matches:
        market_value = matches[0].market_value
        has_valuation = market_value is not None
    elif drivers:
        notes.append(
            f"**{offer.driver_name}** is not enrolled in tier "
            f"`{offer.tier_code}`, so no market value could be read."
        )
    if drivers and team is not None:
        slots_used = sum(
            1
            for d in drivers
            if d.tier_code == offer.tier_code
            and d.active_team_name == offer.team_name
        )

    salary_cap: Decimal | None = None
    slots: int | None = None
    cfg = None
    try:
        cfg = await workflow.show_config(guild_id, tier_code=offer.tier_code)
    except workflow.WorkflowError:
        # A tier without its own override falls back to the season
        # default, the same precedence the config resolver uses.
        try:
            cfg = await workflow.show_config(guild_id)
        except workflow.WorkflowError as exc:
            notes.append(f"Cap and slot limits unavailable: {exc}")
    if cfg is not None:
        salary_cap = cfg.salary_cap
        slots = cfg.active_driver_slots

    budget_balance: Decimal | None = None
    headroom: Decimal | None = None
    enforced = False
    if team_key is not None:
        try:
            summary = await workflow.team_budget(
                guild_id=guild_id,
                team_key=team_key,
                recent_limit=_BUDGET_RECENT_LIMIT,
            )
        except workflow.WorkflowError as exc:
            notes.append(f"Budget position unavailable: {exc}")
        else:
            budget_balance = summary.balance
            enforced = bool(summary.config.enforce_budget)
            if payroll_after is not None:
                headroom = summary.balance - payroll_after

    return OfferContext(
        payroll_before=payroll_before,
        payroll_after=payroll_after,
        salary_cap=salary_cap,
        budget_balance=budget_balance,
        budget_headroom_after=headroom,
        budgets_enforced=enforced,
        market_value=market_value,
        has_published_valuation=has_valuation,
        slots_used=slots_used,
        active_driver_slots=slots,
        notes=tuple(notes),
        _salary=salary,
    )


_NO_VALUATION_WARNING = (
    "**No published valuation for this driver.** Approving now records "
    "the contract with no market value to compare it against, so the "
    "signing-time P/L baseline is lost permanently — it cannot be "
    "back-filled by a later valuation run. Publish a run for tier "
    "`{tier}` first if that baseline matters."
)


def _money_or_unknown(value: Decimal | None) -> str:
    return format_money(value) if value is not None else "— unknown"


def _build_offer_detail(
    offer: approvals.PendingOffer, context: OfferContext | None = None
) -> discord.Embed:
    """
    One offer, with the consequences of approving it stated on screen.

    `context` is optional so the terms still render if the context reads
    fail; without it the embed says the cap impact is unknown rather
    than implying there is none.
    """
    embed = discord.Embed(
        title=f"📝 Offer #{offer.offer_id}",
        description=(
            f"**{offer.driver_name}** → **{offer.team_name}** "
            f"(tier `{offer.tier_code}`)"
        ),
        color=COLOR_WARN,
    )
    embed.add_field(name="Salary", value=f"{format_money(offer.salary)}/season")
    embed.add_field(name="Term", value=f"{offer.term_seasons} season(s)")
    embed.add_field(name="Type", value=offer.contract_type)
    embed.add_field(
        name="Signing bonus", value=format_money(offer.signing_bonus)
    )
    embed.add_field(name="Kind", value=offer.offer_kind)
    embed.add_field(
        name="Submitted",
        value=discord.utils.format_dt(offer.created_at, style="R"),
    )

    if context is None:
        embed.add_field(
            name="Cap impact",
            value=(
                "— not loaded. Check the team's cap with "
                "`/market-admin budget show` before approving."
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                "Approving creates the active contract and assigns the "
                "team role."
            )
        )
        return embed

    embed.add_field(
        name=f"{offer.team_name} payroll",
        value=(
            f"{_money_or_unknown(context.payroll_before)} → "
            f"**{_money_or_unknown(context.payroll_after)}**\n"
            "(salary + signing bonus)"
        ),
        inline=False,
    )
    cap_line = f"Cap {_money_or_unknown(context.salary_cap)}"
    space = context.cap_space_after
    if space is None:
        cap_line += " · space after: — unknown"
    elif context.over_cap:
        cap_line += (
            f" · space after: **{format_money(space)}** ⚠ over the cap"
        )
    else:
        cap_line += f" · space after: **{format_money(space)}**"
    embed.add_field(name="Cap space", value=cap_line, inline=False)

    if context.budget_balance is None:
        budget_line = (
            "— unknown. Budgets may not be configured for this season."
        )
    else:
        enforced = (
            "enforced" if context.budgets_enforced else "tracked, not enforced"
        )
        budget_line = (
            f"Balance {format_money(context.budget_balance)} ({enforced}) · "
            f"headroom vs. new payroll: "
            f"{_money_or_unknown(context.budget_headroom_after)}"
        )
    embed.add_field(name="Budget", value=budget_line, inline=False)

    if context.has_published_valuation:
        delta = context.offer_vs_market
        embed.add_field(
            name="Market value",
            value=(
                f"{_money_or_unknown(context.market_value)} · offer vs. "
                f"market: "
                f"{format_pl(delta) if delta is not None else '— unknown'}\n"
                "(market − salary; negative means the team is paying a "
                "premium)"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="⚠ Market value",
            value=truncate_field(
                _NO_VALUATION_WARNING.format(tier=offer.tier_code)
            ),
            inline=False,
        )

    if context.slots_used is None or context.active_driver_slots is None:
        slot_line = "— unknown"
    else:
        slot_line = (
            f"{context.slots_used} of {context.active_driver_slots} used in "
            f"tier `{offer.tier_code}`"
        )
        if context.slots_full:
            slot_line += " ⚠ no free slot"
    embed.add_field(name="Roster slots", value=slot_line, inline=False)

    if context.notes:
        embed.add_field(
            name="Could not verify",
            value=truncate_field("\n".join(f"• {n}" for n in context.notes)),
            inline=False,
        )

    embed.set_footer(
        text=(
            "Approving creates the active contract, charges the budget, and "
            "assigns the team role. Approve asks for a confirm."
        )
    )
    return embed


def _build_trade_detail(trade: approvals.PendingTrade) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔁 Trade #{trade.trade_id}",
        description=(
            f"**{trade.proposing_team_name}** ⇄ **{trade.other_team_name}**"
        ),
        color=COLOR_WARN,
    )
    embed.add_field(name="Contracts moving", value=str(trade.item_count))
    embed.add_field(
        name="Proposed",
        value=discord.utils.format_dt(trade.created_at, style="R"),
    )
    embed.set_footer(
        text=(
            "Approving transfers the contracts and swaps team roles. "
            "Approve asks for a confirm."
        )
    )
    return embed


# ── Queue pickers: one per kind, each paged ───────────────────────────


class _OfferSelect(discord.ui.Select):
    """One page of pending offers."""

    def __init__(
        self,
        parent: ApprovalsView,
        offers: list[approvals.PendingOffer],
        *,
        row: int,
        page: int = 0,
        pages: int = 1,
        total: int = 0,
    ) -> None:
        if total == 0:
            placeholder = "No pending contract offers"
        elif pages > 1:
            placeholder = f"Review an offer… (page {page + 1}/{pages} of {total})"
        else:
            placeholder = "Review a contract offer…"
        super().__init__(
            placeholder=placeholder[:150],
            options=[
                discord.SelectOption(
                    label=(
                        f"#{o.offer_id} {o.driver_name} → {o.team_name}"
                    )[:100],
                    value=f"{_OFFER}:{o.offer_id}",
                    description=(
                        f"{format_money(o.salary)}/season × {o.term_seasons} "
                        f"· tier {o.tier_code}"
                    )[:100],
                    emoji="📝",
                )
                for o in offers[:SELECT_MAX_OPTIONS]
            ],
            disabled=not offers,
            row=row,
        )
        self._owner = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await _open_offer(interaction, self._owner, int(self.values[0].split(":")[1]))


class _TradeSelect(discord.ui.Select):
    """
    One page of pending trades.

    Its own select, not appended to the offers: sharing one select meant
    a full offer page pushed every trade past the 25-option cut and out
    of reach (G17).
    """

    def __init__(
        self,
        parent: ApprovalsView,
        trades: list[approvals.PendingTrade],
        *,
        row: int,
        page: int = 0,
        pages: int = 1,
        total: int = 0,
    ) -> None:
        if total == 0:
            placeholder = "No pending trades"
        elif pages > 1:
            placeholder = f"Review a trade… (page {page + 1}/{pages} of {total})"
        else:
            placeholder = "Review a trade…"
        super().__init__(
            placeholder=placeholder[:150],
            options=[
                discord.SelectOption(
                    label=(
                        f"#{t.trade_id} {t.proposing_team_name} ⇄ "
                        f"{t.other_team_name}"
                    )[:100],
                    value=f"{_TRADE}:{t.trade_id}",
                    description=f"{t.item_count} contract(s)"[:100],
                    emoji="🔁",
                )
                for t in trades[:SELECT_MAX_OPTIONS]
            ],
            disabled=not trades,
            row=row,
        )
        self._owner = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await _open_trade(interaction, self._owner, int(self.values[0].split(":")[1]))


class _QueuePageButton(discord.ui.Button):
    """Prev / next page for one of the two queues."""

    def __init__(self, *, kind: str, back: bool, row: int) -> None:
        label = "offers" if kind == _OFFER else "trades"
        super().__init__(
            label=f"{'Prev' if back else 'Next'} {label}",
            style=discord.ButtonStyle.secondary,
            emoji="◀" if back else "▶",
            row=row,
        )
        self._kind = kind
        self._back = back

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, ApprovalsView)
        step = -1 if self._back else 1
        if self._kind == _OFFER:
            # Wrap rather than disable at the ends: the queue shrinks as
            # items are approved, so a wrap is always in range.
            target = (view.offer_page + step) % view.offer_pages
            await view.reload(interaction, offer_page=target)
        else:
            target = (view.trade_page + step) % view.trade_pages
            await view.reload(interaction, trade_page=target)


async def _open_offer(
    interaction: discord.Interaction, parent: ApprovalsView, offer_id: int
) -> None:
    """Render the offer detail, with its cap / budget / market context."""
    await interaction.response.defer(ephemeral=True)
    offer = next(
        (o for o in parent.queue.offers if o.offer_id == offer_id), None
    )
    if offer is None:
        await parent.reload(interaction, note="That offer is no longer pending.")
        return
    context = await gather_offer_context(interaction.guild_id, offer)
    detail = _ItemView(
        kind=_OFFER,
        item_id=offer_id,
        opener_id=parent.opener_id,
        parent=parent,
        offer=offer,
        context=context,
    )
    await interaction.edit_original_response(
        embed=_build_offer_detail(offer, context), view=detail
    )


async def _open_trade(
    interaction: discord.Interaction, parent: ApprovalsView, trade_id: int
) -> None:
    trade = next(
        (t for t in parent.queue.trades if t.trade_id == trade_id), None
    )
    if trade is None:
        await parent.reload(interaction, note="That trade is no longer pending.")
        return
    detail = _ItemView(
        kind=_TRADE,
        item_id=trade_id,
        opener_id=parent.opener_id,
        parent=parent,
        trade=trade,
    )
    await interaction.response.edit_message(
        embed=_build_trade_detail(trade), view=detail
    )


class _RejectModal(base.PanelModal):
    """Rejection takes an optional note, which lands in the audit log."""

    def __init__(
        self, *, kind: str, item_id: int, parent: ApprovalsView
    ) -> None:
        label = "offer" if kind == _OFFER else "trade"
        super().__init__(title=f"Reject {label} #{item_id}")
        self._kind = kind
        self._item_id = item_id
        self._owner = parent
        self._note = discord.ui.TextInput(
            label="Reason (optional)",
            placeholder="Recorded in the audit log and shown to the team.",
            required=False,
            max_length=400,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self._note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        note = self._note.value.strip() or None
        try:
            if self._kind == _OFFER:
                await approvals.reject_offer(
                    offer_id=self._item_id,
                    actor_id=interaction.user.id,
                    note=note,
                )
            else:
                await approvals.reject_trade(
                    trade_id=self._item_id,
                    actor_id=interaction.user.id,
                    note=note,
                )
        except approvals.ApprovalError as exc:
            await report_error(interaction, str(exc))
            return

        label = "Offer" if self._kind == _OFFER else "Trade"
        await self._owner.reload(
            interaction, note=f"✅ {label} #{self._item_id} rejected."
        )


def approve_consequence_text(
    *,
    kind: str,
    item_id: int,
    offer: approvals.PendingOffer | None = None,
    trade: approvals.PendingTrade | None = None,
    context: OfferContext | None = None,
) -> str:
    """
    What the second Approve click will do, in words.

    Shown between the two clicks. Restates the terms so a mis-picked row
    is caught here rather than in the transaction log, and repeats the
    missing-valuation warning, which is the one consequence that cannot
    be undone by a void.
    """
    if kind == _TRADE:
        if trade is None:
            return (
                f"Press again to approve trade #{item_id}. This transfers "
                "the contracts and swaps the team roles."
            )
        return (
            f"Press again to approve trade #{item_id}: "
            f"**{trade.proposing_team_name}** ⇄ **{trade.other_team_name}**, "
            f"{trade.item_count} contract(s). This transfers the contracts "
            "and swaps the team roles."
        )

    if offer is None:
        return (
            f"Press again to approve offer #{item_id}. This creates the "
            "active contract and assigns the team role."
        )
    lines = [
        f"Press again to approve offer #{item_id}: **{offer.driver_name}** "
        f"→ **{offer.team_name}** at {format_money(offer.salary)}/season "
        f"× {offer.term_seasons} season(s)."
    ]
    if context is not None and context.payroll_after is not None:
        lines.append(
            f"Payroll becomes {format_money(context.payroll_after)}"
            + (
                f" (cap space {format_money(context.cap_space_after)})."
                if context.cap_space_after is not None
                else "."
            )
        )
    if context is not None and context.over_cap:
        lines.append("⚠ This takes the team over the salary cap.")
    if context is not None and context.slots_full:
        lines.append("⚠ The team has no free active-driver slot in this tier.")
    if context is not None and not context.has_published_valuation:
        lines.append(
            "⚠ No published valuation for this driver — approving loses the "
            "signing-time P/L baseline permanently."
        )
    lines.append("This creates the active contract and assigns the team role.")
    return "\n".join(lines)


class _ItemView(AdminOwnedView):
    """
    Approve / Reject for a single queue item.

    Approve arms on the first click and acts on the second, the same
    shape the valuation Publish button uses: it is irreversible, and it
    sits one button away from Reject.
    """

    def __init__(
        self,
        *,
        kind: str,
        item_id: int,
        opener_id: int,
        parent: ApprovalsView,
        offer: approvals.PendingOffer | None = None,
        trade: approvals.PendingTrade | None = None,
        context: OfferContext | None = None,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.kind = kind
        self.item_id = item_id
        self.parent = parent
        self.offer = offer
        self.trade = trade
        self.context = context
        self._armed = False
        self.add_item(BackButton(self._back, label="Back to queue"))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)

    @discord.ui.button(
        label="Approve", style=discord.ButtonStyle.success, emoji="✅"
    )
    async def approve(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._armed:
            self._armed = True
            button.label = "Confirm approval"
            button.style = discord.ButtonStyle.danger
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                approve_consequence_text(
                    kind=self.kind,
                    item_id=self.item_id,
                    offer=self.offer,
                    trade=self.trade,
                    context=self.context,
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            if self.kind == _OFFER:
                result = await approvals.approve_offer(
                    interaction.client,
                    guild=interaction.guild,
                    actor=interaction.user,
                    offer_id=self.item_id,
                )
                note = (
                    f"✅ Approved offer #{self.item_id} → contract "
                    f"`{result.contract_id}` (ref `{result.external_ref}`)."
                )
                # All three warnings, not just the role one. A signing
                # with no published value can never have its P/L
                # tracked, and a signing nobody was told about looks to
                # the league like it never happened — both are silent
                # failures unless the approver is told here.
                for warning in result.warnings:
                    note += f"\n⚠ {warning}"
            else:
                trade_result = await approvals.approve_trade(
                    guild=interaction.guild,
                    actor=interaction.user,
                    trade_id=self.item_id,
                )
                note = (
                    f"✅ Approved trade #{self.item_id}. Contracts transferred."
                )
                if trade_result.role_warnings:
                    note += "\n⚠ Role swaps had issues:\n" + "\n".join(
                        f"• {w}" for w in trade_result.role_warnings
                    )
        except approvals.ApprovalError as exc:
            await report_error(interaction, str(exc))
            return

        await self.parent.reload(interaction, note=note)

    @discord.ui.button(
        label="Reject", style=discord.ButtonStyle.danger, emoji="🚫"
    )
    async def reject(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            _RejectModal(
                kind=self.kind, item_id=self.item_id, parent=self.parent
            )
        )


class ApprovalsView(AdminOwnedView):
    """
    The queue screen. Rebuilt from the database after every action so a
    just-approved item cannot be clicked twice.

    Rows, top-down: offers select, trades select, offer paging, trade
    paging, Back. Paging rows only appear for a queue that needs them,
    which keeps the common case inside Discord's five-row budget even
    when both kinds are paging.
    """

    def __init__(
        self,
        *,
        queue: approvals.PendingQueue,
        opener_id: int,
        on_back: BackCallback,
        offer_page: int = 0,
        trade_page: int = 0,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self._on_back = on_back
        self.queue = queue

        self.offer_pages = page_count(len(queue.offers))
        self.trade_pages = page_count(len(queue.trades))
        self.offer_page = min(max(offer_page, 0), self.offer_pages - 1)
        self.trade_page = min(max(trade_page, 0), self.trade_pages - 1)

        row = 0
        self.add_item(
            _OfferSelect(
                self,
                page_slice(list(queue.offers), self.offer_page),
                row=row,
                page=self.offer_page,
                pages=self.offer_pages,
                total=len(queue.offers),
            )
        )
        row += 1
        self.add_item(
            _TradeSelect(
                self,
                page_slice(list(queue.trades), self.trade_page),
                row=row,
                page=self.trade_page,
                pages=self.trade_pages,
                total=len(queue.trades),
            )
        )
        row += 1
        if self.offer_pages > 1:
            self.add_item(_QueuePageButton(kind=_OFFER, back=True, row=row))
            self.add_item(_QueuePageButton(kind=_OFFER, back=False, row=row))
            row += 1
        if self.trade_pages > 1:
            self.add_item(_QueuePageButton(kind=_TRADE, back=True, row=row))
            self.add_item(_QueuePageButton(kind=_TRADE, back=False, row=row))
            row += 1
        self.add_item(BackButton(on_back, row=row))

    def embed(self) -> discord.Embed:
        """The queue embed matching this view's current pages."""
        return build_approvals_embed(
            self.queue,
            offer_page=self.offer_page,
            offer_pages=self.offer_pages,
            trade_page=self.trade_page,
            trade_pages=self.trade_pages,
        )

    async def reload(
        self,
        interaction: discord.Interaction,
        *,
        note: str | None = None,
        offer_page: int | None = None,
        trade_page: int | None = None,
    ) -> None:
        """
        Re-read the queue and re-render, keeping both pages unless told
        otherwise.

        Always a fresh read rather than mutating the cached list, because
        an approval can change more than the item acted on — a trade
        approval can resolve offers for the same contract, which also
        means the page an admin was on may no longer exist.
        """
        queue = await approvals.fetch_pending_queue(
            interaction.guild_id, limit=_QUEUE_FETCH_LIMIT
        )
        view = ApprovalsView(
            queue=queue,
            opener_id=self.opener_id,
            on_back=self._on_back,
            offer_page=self.offer_page if offer_page is None else offer_page,
            trade_page=self.trade_page if trade_page is None else trade_page,
        )
        embed = view.embed()

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
        if note:
            await interaction.followup.send(note, ephemeral=True)


async def open_approvals(
    interaction: discord.Interaction, *, opener_id: int, on_back: BackCallback
) -> None:
    """Entry point used by the panel's Approvals button."""
    queue = await approvals.fetch_pending_queue(
        interaction.guild_id, limit=_QUEUE_FETCH_LIMIT
    )
    view = ApprovalsView(queue=queue, opener_id=opener_id, on_back=on_back)
    await interaction.response.edit_message(embed=view.embed(), view=view)
