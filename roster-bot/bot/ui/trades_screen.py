"""
Trades desk as an interactive screen (consolidation Screen 11).

`/trade propose` asks a principal for `my_team`, `other_team`,
`my_contract_id` and `their_contract_id` — four identifiers, two of them
database primary keys that appear in no channel a principal can read.
This screen turns all four into selects: your team, their team, then one
of your active contracts and one of theirs, each option showing the
driver, the salary and the market value so the trade can be judged where
it is assembled.

Like the contracts desk this screen is **read-only**, for the same
reason: `bot/workflow.py` has no trade operation at all (the transitions
live in `bot/contracts/service.py` and are reached only from
`bot/cogs/trades.py`). Every path therefore ends in a prepared route —
a card built from a fresh read that shows both teams' cap and budget
position after the swap and prints the exact command with the ids filled
in. Nothing here writes. Required workflow signatures are listed in
`audit/REPORT_desk_screens.md` (G1).

Team resolution, the cap-sheet loader, paging and the money formatting
are imported from `bot/ui/contracts_screen.py` and
`bot/ui/market_screen.py` so the two desks always agree about what a
team's cap space is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import discord

from bot import db, queries
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
from bot.ui.contracts_screen import (
    NOT_WRITTEN,
    ContractRow,
    DeskState,
    DeskTeam,
    build_no_team_embed,
    expiry_text,
    load_desk_state,
    resolve_desk_teams,
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

TRADES_FOOTER = (
    "Read-only desk · typed routes: /trade propose · accept · decline · "
    "withdraw · status"
)

# `/trade propose` moves exactly one contract each way. The picker
# enforces the same shape rather than offering a basket the command
# cannot express (gap G5).
ITEMS_PER_SIDE = 1


# ── state ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TradeSide:
    contract_id: int
    driver_name: str
    contract_value: Decimal
    market_value: Decimal | None
    term_seasons: int
    season_index: int
    from_team_id: int
    from_team_name: str


@dataclass(frozen=True)
class TradeRow:
    trade_id: int
    state: str
    proposing_team_id: int
    proposing_team_name: str
    other_team_id: int
    other_team_name: str
    message: str | None
    expires_at: datetime | None
    items: list[TradeSide]

    def leaving(self, team_id: int) -> list[TradeSide]:
        return [i for i in self.items if i.from_team_id == team_id]

    def arriving(self, team_id: int) -> list[TradeSide]:
        return [i for i in self.items if i.from_team_id != team_id]

    def counterparty_id(self, team_id: int) -> int:
        return (
            self.other_team_id
            if team_id == self.proposing_team_id
            else self.proposing_team_id
        )


async def load_open_trades(team_id: int) -> list[TradeRow]:
    """
    Every open trade touching this team, with both sides expanded.

    Open means one of `queries.OPEN_TRADE_STATES`; settled and rejected
    trades belong to the History screen, and that boundary is stated on
    the list embed so an empty desk is never mistaken for a lost trade.
    """
    async with db.connect() as conn:
        trades = await queries.fetch_open_trades_for_team(conn, team_id)
        out: list[TradeRow] = []
        team_names: dict[int, str] = {}

        async def name_of(tid: int) -> str:
            if tid not in team_names:
                team = await queries.fetch_team_by_id(conn, tid)
                team_names[tid] = team.name if team else UNKNOWN
            return team_names[tid]

        for trade in trades:
            items: list[TradeSide] = []
            for item in await queries.fetch_trade_items(conn, trade.id):
                contract = await queries.fetch_contract_by_id(conn, item.contract_id)
                if contract is None:
                    continue
                driver = await queries.fetch_driver_by_id(conn, contract.driver_id)
                market = await queries.fetch_latest_published_valuation(
                    conn, contract.driver_id
                )
                items.append(
                    TradeSide(
                        contract_id=contract.id,
                        driver_name=driver.display_name if driver else UNKNOWN,
                        contract_value=contract.contract_value,
                        market_value=market,
                        term_seasons=contract.term_seasons,
                        season_index=contract.season_index,
                        from_team_id=item.from_team_id,
                        from_team_name=await name_of(item.from_team_id),
                    )
                )
            out.append(
                TradeRow(
                    trade_id=trade.id,
                    state=trade.state,
                    proposing_team_id=trade.proposing_team_id,
                    proposing_team_name=await name_of(trade.proposing_team_id),
                    other_team_id=trade.other_team_id,
                    other_team_name=await name_of(trade.other_team_id),
                    message=trade.message,
                    expires_at=trade.expires_at,
                    items=items,
                )
            )
    return out


async def load_team_contracts(guild_id: int, team: DeskTeam) -> list[ContractRow]:
    """The team's active contracts, reusing the contracts desk loader."""
    state = await load_desk_state(guild_id, team)
    return state.contracts


async def load_other_teams(
    guild_id: int, exclude_team_id: int
) -> list[DeskTeam]:
    """Every other team in the guild — a trade partner need not be yours."""
    async with db.connect() as conn:
        teams = await queries.fetch_all_teams(conn, guild_id)
    return [
        DeskTeam(
            team_id=t.id,
            key=t.key,
            name=t.name,
            color=t.color,
            principal_role_id=t.principal_role_id,
        )
        for t in sorted(teams, key=lambda t: t.name.casefold())
        if t.id != exclude_team_id
    ]


# ── cap maths (pure, so it is testable without a guild) ──────────────


@dataclass(frozen=True)
class SwapEffect:
    team_name: str
    payroll_before: Decimal
    payroll_after: Decimal
    cap: Decimal | None
    cap_space_before: Decimal | None
    cap_space_after: Decimal | None
    over_cap: bool | None
    budget_available_after: Decimal | None
    budget_blocks: bool | None
    seats_free: int | None


def evaluate_swap(
    state: DeskState, *, out_value: Decimal, in_value: Decimal
) -> SwapEffect:
    """
    One side of a 1-for-1 swap: payroll, cap space and budget after it.

    Seats do not change in a 1-for-1 trade, so the seat count is carried
    through unchanged rather than recomputed. Where the cap or the
    budget is not configured the result is None and renders as
    "— unknown": a missing cap is not an infinite one.
    """
    payroll_after = state.payroll - out_value + in_value
    effective_after = payroll_after + state.dead_money
    cap_space_after = None if state.cap is None else state.cap - effective_after
    budget_after = None
    if state.balance is not None:
        budget_after = budget_engine.available_to_spend(
            state.balance, effective_after, escrow_enabled=state.escrow_enabled
        )
    return SwapEffect(
        team_name=state.team.name,
        payroll_before=state.payroll,
        payroll_after=payroll_after,
        cap=state.cap,
        cap_space_before=state.cap_space,
        cap_space_after=cap_space_after,
        over_cap=None if cap_space_after is None else cap_space_after < 0,
        budget_available_after=budget_after,
        budget_blocks=(
            None
            if budget_after is None or not state.budgets_enforced
            else budget_after < 0
        ),
        seats_free=state.seats_free,
    )


def swap_lines(effect: SwapEffect) -> list[str]:
    lines = [
        f"**{effect.team_name}**",
        f"Payroll: {format_money(effect.payroll_before)} → "
        f"{format_money(effect.payroll_after)}",
        f"Cap space: {money_or_unknown(effect.cap_space_before)} → "
        f"{money_or_unknown(effect.cap_space_after)}",
    ]
    if effect.over_cap is None:
        lines.append(
            f"Cap check: {UNKNOWN} — this season has no `league_config` row, "
            "so no cap can be tested (not the same as passing)"
        )
    elif effect.over_cap:
        lines.append("Cap check: ⛔ over the cap after the swap")
    else:
        lines.append("Cap check: ✅ within the cap")
    if effect.budget_available_after is None:
        lines.append("Budget check: not configured, so only the cap applies")
    elif effect.budget_blocks is None:
        lines.append(
            "Budget check: recorded but not enforced — available after "
            f"{format_money(effect.budget_available_after)}"
        )
    elif effect.budget_blocks:
        lines.append(
            "Budget check: ⛔ budget would go negative "
            f"({format_money(effect.budget_available_after)})"
        )
    else:
        lines.append(
            "Budget check: ✅ available after "
            f"{format_money(effect.budget_available_after)}"
        )
    lines.append(
        f"Seats: {effect.seats_free if effect.seats_free is not None else UNKNOWN}"
        " free, unchanged by a 1-for-1 swap"
    )
    return lines


# ── rendering ────────────────────────────────────────────────────────


def build_desk_embed(state: DeskState, trades: list[TradeRow]) -> discord.Embed:
    if state.season_id is None:
        return discord.Embed(
            title=f"🔁 Trades — {state.team.name}",
            description=(
                "No active season, so there is nothing to trade and no cap "
                "to trade against."
            ),
            color=COLOR_INFO,
        )
    incoming = [t for t in trades if t.proposing_team_id != state.team.team_id]
    outgoing = [t for t in trades if t.proposing_team_id == state.team.team_id]
    embed = discord.Embed(
        title=f"🔁 Trades — {state.team.name}",
        description=(
            f"**{state.season_name}** · {len(incoming)} awaiting your "
            f"answer · {len(outgoing)} you proposed\n"
            f"Payroll {format_money(state.payroll)} · cap space "
            f"{money_or_unknown(state.cap_space)} · "
            f"{len(state.contracts)} tradeable contract(s)"
        ),
        color=(
            discord.Colour(state.team.color)
            if state.team.color is not None
            else COLOR_INFO
        ),
    )
    embed.add_field(
        name="Scope",
        value=(
            "Open states only: "
            + ", ".join(f"`{s}`" for s in queries.OPEN_TRADE_STATES)
            + ". Executed, declined and withdrawn trades are in the History "
            "screen, not lost."
        ),
        inline=False,
    )
    embed.add_field(
        name="Shape",
        value=(
            f"`/trade propose` moves exactly {ITEMS_PER_SIDE} contract each "
            "way, so the picker offers one from each side. A multi-player "
            "package is not expressible today (gap G5)."
        ),
        inline=False,
    )
    embed.set_footer(text=TRADES_FOOTER)
    return embed


def build_trades_list_embed(
    state: DeskState, trades: list[TradeRow], *, page: int
) -> discord.Embed:
    window, page, pages = page_slice(trades, page)
    lines = []
    for trade in window:
        direction = (
            "you proposed"
            if trade.proposing_team_id == state.team.team_id
            else "awaiting you"
        )
        out_names = ", ".join(i.driver_name for i in trade.leaving(state.team.team_id))
        in_names = ", ".join(i.driver_name for i in trade.arriving(state.team.team_id))
        lines.append(
            f"`{trade.trade_id}` {trade.state} · {direction} · "
            f"out: {out_names or 'nothing'} · in: {in_names or 'nothing'} · "
            f"expires {expiry_text(trade.expires_at)}"
        )
    embed = discord.Embed(
        title=f"🔁 Open trades — {state.team.name}",
        description=base.truncate_field(
            "\n".join(lines) or "*No open trades.*", DESCRIPTION_LIMIT
        ),
        color=COLOR_INFO,
    )
    page_field(
        embed,
        shown=len(window),
        page=page,
        pages=pages,
        total=len(trades),
        noun="trades",
    )
    embed.set_footer(text="Pick a trade for both sides' cap impact")
    return embed


def _side_lines(sides: list[TradeSide]) -> str:
    if not sides:
        return "*nothing*"
    return "\n".join(
        f"• {s.driver_name} — {format_money(s.contract_value)} · "
        f"season {s.season_index} of {s.term_seasons} · market "
        f"{money_or_unknown(s.market_value)} · P/L "
        f"{pl_or_unknown(s.market_value, s.contract_value)} · contract "
        f"`{s.contract_id}`"
        for s in sides
    )


def build_trade_detail_embed(
    state: DeskState,
    trade: TradeRow,
    mine: SwapEffect,
    theirs: SwapEffect | None,
    *,
    armed: bool,
) -> discord.Embed:
    """One trade, both sides' cap impact, and what answering it will do."""
    my_id = state.team.team_id
    i_proposed = trade.proposing_team_id == my_id
    embed = discord.Embed(
        title=f"🔁 Trade {trade.trade_id} — {trade.state}",
        description=(
            f"{trade.proposing_team_name} → {trade.other_team_name}\n"
            f"{'You proposed this.' if i_proposed else 'Awaiting your answer.'} "
            f"Expires {expiry_text(trade.expires_at)}."
            + (f"\n\n> {trade.message}" if trade.message else "")
        ),
        color=COLOR_WARN if armed else COLOR_INFO,
    )
    embed.add_field(
        name="Leaving your team", value=_side_lines(trade.leaving(my_id)), inline=False
    )
    embed.add_field(
        name="Arriving at your team",
        value=_side_lines(trade.arriving(my_id)),
        inline=False,
    )
    embed.add_field(
        name="Your cap after the swap", value="\n".join(swap_lines(mine)), inline=False
    )
    if theirs is not None:
        embed.add_field(
            name="Their cap after the swap",
            value="\n".join(swap_lines(theirs)),
            inline=False,
        )
    else:
        embed.add_field(
            name="Their cap after the swap",
            value=(
                f"{UNKNOWN} — the counterparty's cap sheet could not be read "
                "on this pass. Their side is validated by the service layer "
                "when the trade executes."
            ),
            inline=False,
        )
    if armed:
        embed.add_field(
            name="Confirm what you are about to do",
            value=(
                "Pressing **Show route** does not answer this trade — this "
                "panel cannot write (gap G1). It reveals the exact "
                "command.\n\n"
                "**When you run it:** accepting moves both contracts and "
                "both payrolls at once and cannot be undone from the panel; "
                "declining or withdrawing closes the trade permanently and "
                "the other principal is notified. Neither touches Discord "
                "team roles — those are reassigned separately — and neither "
                "cancels any contract offer outstanding to the drivers "
                "involved."
            ),
            inline=False,
        )
    embed.add_field(name="State", value=NOT_WRITTEN, inline=False)
    embed.set_footer(text=TRADES_FOOTER)
    return embed


def build_trade_route_embed(state: DeskState, trade: TradeRow) -> discord.Embed:
    my_id = state.team.team_id
    i_proposed = trade.proposing_team_id == my_id
    if i_proposed:
        route = (
            f"/trade withdraw my_team: {state.team.key} "
            f"trade_id: {trade.trade_id} note: <reason>"
        )
        which = (
            "You proposed this trade, so yours is the withdraw route. Only "
            f"**{trade.other_team_name}** can accept or decline it."
        )
    else:
        route = (
            f"/trade accept my_team: {state.team.key} "
            f"trade_id: {trade.trade_id}\n"
            f"/trade decline my_team: {state.team.key} "
            f"trade_id: {trade.trade_id} note: <reason>"
        )
        which = (
            f"**{trade.proposing_team_name}** proposed this, so you may "
            "accept or decline. They can still withdraw it before you "
            "answer."
        )
    embed = discord.Embed(
        title=f"🔁 Route ready — trade {trade.trade_id}",
        description=(
            f"Re-read just now and still `{trade.state}`, expiring "
            f"{expiry_text(trade.expires_at)}."
        ),
        color=COLOR_OK,
    )
    embed.add_field(name="Copy this", value=f"```\n{route}\n```", inline=False)
    embed.add_field(name="Who can do what", value=which, inline=False)
    embed.add_field(
        name="State",
        value=(
            NOT_WRITTEN
            + f"\nThe trade is still `{trade.state}` and your payroll is "
            f"still {format_money(state.payroll)}."
        ),
        inline=False,
    )
    embed.add_field(
        name="Approval",
        value=(
            "An accepted trade may still need commissioner approval before "
            "it executes, depending on this league's configuration — the "
            "state after accepting tells you which."
        ),
        inline=False,
    )
    embed.set_footer(text=TRADES_FOOTER)
    return embed


def build_propose_review_embed(
    *,
    mine_state: DeskState,
    theirs_state: DeskState,
    my_row: ContractRow,
    their_row: ContractRow,
    mine: SwapEffect,
    theirs: SwapEffect,
) -> discord.Embed:
    route = (
        "/trade propose\n"
        f"  my_team: {mine_state.team.key}\n"
        f"  other_team: {theirs_state.team.key}\n"
        f"  my_contract_id: {my_row.contract_id}\n"
        f"  their_contract_id: {their_row.contract_id}\n"
        "  ttl_hours: <24 | 48 | 72 | 168>\n"
        "  message: <optional note>"
    )
    embed = discord.Embed(
        title="🔁 Prepared trade",
        description=(
            f"**{mine_state.team.name}** sends **{my_row.driver_name}** "
            f"({format_money(my_row.contract_value)}) and receives "
            f"**{their_row.driver_name}** "
            f"({format_money(their_row.contract_value)}) from "
            f"**{theirs_state.team.name}**."
        ),
        color=COLOR_INFO,
    )
    embed.add_field(
        name="Out",
        value=_side_lines(
            [
                TradeSide(
                    contract_id=my_row.contract_id,
                    driver_name=my_row.driver_name,
                    contract_value=my_row.contract_value,
                    market_value=my_row.market_value,
                    term_seasons=my_row.term_seasons,
                    season_index=my_row.season_index,
                    from_team_id=mine_state.team.team_id,
                    from_team_name=mine_state.team.name,
                )
            ]
        ),
        inline=False,
    )
    embed.add_field(
        name="In",
        value=_side_lines(
            [
                TradeSide(
                    contract_id=their_row.contract_id,
                    driver_name=their_row.driver_name,
                    contract_value=their_row.contract_value,
                    market_value=their_row.market_value,
                    term_seasons=their_row.term_seasons,
                    season_index=their_row.season_index,
                    from_team_id=theirs_state.team.team_id,
                    from_team_name=theirs_state.team.name,
                )
            ]
        ),
        inline=False,
    )
    embed.add_field(name="Your side", value="\n".join(swap_lines(mine)), inline=False)
    embed.add_field(name="Their side", value="\n".join(swap_lines(theirs)), inline=False)
    tiers_differ = (
        my_row.tier_code is not None
        and their_row.tier_code is not None
        and my_row.tier_code != their_row.tier_code
    )
    embed.add_field(
        name="Tier",
        value=(
            f"Your driver is in `{my_row.tier_code or UNKNOWN}`, theirs in "
            f"`{their_row.tier_code or UNKNOWN}`."
            + (
                " These differ, and market values are only comparable inside "
                "a tier, so the P/L figures above are not a like-for-like "
                "comparison."
                if tiers_differ
                else ""
            )
        ),
        inline=False,
    )
    embed.add_field(name="Copy this", value=f"```\n{route}\n```", inline=False)
    embed.add_field(
        name="State",
        value=(
            NOT_WRITTEN
            + "\nNo trade row exists yet; nothing is offered to "
            f"{theirs_state.team.name} until you run that command. The cap "
            "figures are this screen's reading of season-scope config — the "
            "service layer re-validates at propose and again at execute, "
            "and its per-tier resolution can be stricter (gap G4)."
        ),
        inline=False,
    )
    embed.set_footer(text=TRADES_FOOTER)
    return embed


# ── generic paged picker ─────────────────────────────────────────────


class _PagedPicker(OwnedView):
    """
    One paged select over a list that is re-read on every render.

    Used for the trade partner and both contract sides. Re-reading
    matters here: a contract released while the principal was choosing
    must vanish from the next page rather than be offered and then fail.
    """

    def __init__(
        self,
        *,
        opener_id: int,
        title: str,
        description: str,
        noun: str,
        placeholder: str,
        load,
        option_of,
        on_pick,
        on_back: BackCallback,
        back_label: str = "Back",
        page: int = 0,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.title = title
        self.description = description
        self.noun = noun
        self.placeholder = placeholder
        self._load = load
        self._option_of = option_of
        self._on_pick = on_pick
        self._on_back = on_back
        self.back_label = back_label
        self.page = page
        self.items: list = []

    async def render(self, interaction: discord.Interaction) -> None:
        self.items = await self._load()
        window, self.page, pages = page_slice(self.items, self.page)
        self.clear_items()
        if window:
            self.add_item(_PickerSelect(self, window))
        if pages > 1:
            self.add_item(_PickerPage(self, -1, disabled=self.page == 0, row=1))
            self.add_item(
                _PickerPage(self, 1, disabled=self.page >= pages - 1, row=1)
            )
        self.add_item(BackButton(self._back, label=self.back_label, row=2))
        embed = discord.Embed(
            title=self.title,
            description=self.description,
            color=COLOR_INFO,
        )
        page_field(
            embed,
            shown=len(window),
            page=self.page,
            pages=pages,
            total=len(self.items),
            noun=self.noun,
        )
        if not self.items:
            embed.add_field(
                name="Nothing to pick",
                value=(
                    f"No {self.noun} are available on this read. Nothing was "
                    "written; use Back and try again."
                ),
                inline=False,
            )
        embed.set_footer(text=TRADES_FOOTER)
        await edit_screen(interaction, embed=embed, view=self)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self._on_back(interaction)

    async def pick(self, interaction: discord.Interaction, value: str) -> None:
        chosen = next(
            (i for i in self.items if str(self._option_of(i).value) == value), None
        )
        if chosen is None:
            await report_error(
                interaction,
                f"That choice is no longer available — reloading the {self.noun}.",
            )
            await self.render(interaction)
            return
        await self._on_pick(interaction, chosen)


class _PickerSelect(discord.ui.Select):
    def __init__(self, owner: _PagedPicker, window: list) -> None:
        super().__init__(
            placeholder=owner.placeholder,
            options=[owner._option_of(item) for item in window],
            row=0,
        )
        self._owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.pick(interaction, self.values[0])


class _PickerPage(discord.ui.Button):
    def __init__(
        self, owner: _PagedPicker, step: int, *, disabled: bool, row: int
    ) -> None:
        super().__init__(
            label="Prev page" if step < 0 else "Next page",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self._owner = owner
        self._step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        self._owner.page += self._step
        await self._owner.render(interaction)


def _team_option(team: DeskTeam) -> discord.SelectOption:
    return discord.SelectOption(label=team.name[:100], value=str(team.team_id))


def _contract_option(row: ContractRow) -> discord.SelectOption:
    return discord.SelectOption(
        label=f"{row.driver_name} — {format_money(row.contract_value)}"[:100],
        value=str(row.contract_id),
        description=(
            f"market {money_or_unknown(row.market_value)} · "
            f"season {row.season_index}/{row.term_seasons} · "
            f"{row.tier_code or UNKNOWN}"
        )[:100],
    )


# ── main view ────────────────────────────────────────────────────────


class TradesView(OwnedView):
    """Team select · open trades · propose · back."""

    def __init__(
        self,
        *,
        teams: list[DeskTeam],
        state: DeskState,
        trades: list[TradeRow],
        opener_id: int,
        on_back: BackCallback,
        admin: bool,
        team_page: int = 0,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.teams = teams
        self.state = state
        self.trades = trades
        self.on_back = on_back
        self.admin = admin
        self.team_page = team_page
        self.expiry_hint = (
            "Nothing was written — this desk only reads. Typed routes: "
            "`/trade propose`, `/trade accept`, `/trade decline`, "
            "`/trade withdraw`, `/trade status`."
        )
        window, self.team_page, pages = page_slice(teams, team_page)
        if len(teams) > 1 and window:
            self.add_item(_MyTeamSelect(window, current=state.team.team_id))
        if pages > 1:
            self.add_item(_MyTeamPage(-1, disabled=self.team_page == 0, row=1))
            self.add_item(_MyTeamPage(1, disabled=self.team_page >= pages - 1, row=1))
        has_season = state.season_id is not None
        self.add_item(_OpenTradesButton(enabled=has_season and bool(trades), row=2))
        self.add_item(
            _ProposeButton(enabled=has_season and bool(state.contracts), row=2)
        )
        self.add_item(BackButton(on_back, row=3))

    async def reload(
        self, interaction: discord.Interaction, *, note: str | None = None
    ) -> None:
        state = await load_desk_state(interaction.guild_id, self.state.team)
        trades = await load_open_trades(state.team.team_id)
        view = TradesView(
            teams=self.teams,
            state=state,
            trades=trades,
            opener_id=self.opener_id,
            on_back=self.on_back,
            admin=self.admin,
            team_page=self.team_page,
        )
        await edit_screen(
            interaction, embed=build_desk_embed(state, trades), view=view
        )
        if note:
            await interaction.followup.send(note, ephemeral=True)


class _MyTeamSelect(discord.ui.Select):
    def __init__(self, window: list[DeskTeam], *, current: int) -> None:
        super().__init__(
            placeholder="Trading for which team?",
            options=[
                discord.SelectOption(
                    label=t.name[:100],
                    value=str(t.team_id),
                    default=t.team_id == current,
                )
                for t in window
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, TradesView)
        chosen = next(
            (t for t in view.teams if t.team_id == int(self.values[0])), None
        )
        if chosen is None:
            await report_error(interaction, "That team is no longer available.")
            return
        state = await load_desk_state(interaction.guild_id, chosen)
        trades = await load_open_trades(chosen.team_id)
        new_view = TradesView(
            teams=view.teams,
            state=state,
            trades=trades,
            opener_id=view.opener_id,
            on_back=view.on_back,
            admin=view.admin,
            team_page=view.team_page,
        )
        await edit_screen(
            interaction, embed=build_desk_embed(state, trades), view=new_view
        )


class _MyTeamPage(discord.ui.Button):
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
        assert isinstance(view, TradesView)
        view.team_page += self.step
        await view.reload(interaction)


# ── open trades ──────────────────────────────────────────────────────


class _OpenTradesButton(discord.ui.Button):
    def __init__(self, *, enabled: bool, row: int) -> None:
        super().__init__(
            label="Open trades…",
            style=discord.ButtonStyle.primary,
            emoji="🔁",
            row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, TradesView)
        await _TradesListView(parent=view).render(interaction)


class _TradesListView(OwnedView):
    def __init__(self, *, parent: TradesView, page: int = 0) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.page = page
        self.state = parent.state
        self.trades: list[TradeRow] = []

    async def render(self, interaction: discord.Interaction) -> None:
        self.state = await load_desk_state(interaction.guild_id, self.state.team)
        self.trades = await load_open_trades(self.state.team.team_id)
        window, self.page, pages = page_slice(self.trades, self.page)
        self.clear_items()
        if window:
            self.add_item(_TradeSelect(self, window))
        if pages > 1:
            self.add_item(_TradePage(self, -1, disabled=self.page == 0, row=1))
            self.add_item(_TradePage(self, 1, disabled=self.page >= pages - 1, row=1))
        self.add_item(BackButton(self._back, label="Back to desk", row=2))
        await edit_screen(
            interaction,
            embed=build_trades_list_embed(self.state, self.trades, page=self.page),
            view=self,
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)


class _TradePage(discord.ui.Button):
    def __init__(
        self, owner: _TradesListView, step: int, *, disabled: bool, row: int
    ) -> None:
        super().__init__(
            label="Prev trades" if step < 0 else "More trades",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self._owner = owner
        self._step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        self._owner.page += self._step
        await self._owner.render(interaction)


class _TradeSelect(discord.ui.Select):
    def __init__(self, owner: _TradesListView, window: list[TradeRow]) -> None:
        super().__init__(
            placeholder="Which trade?",
            options=[
                discord.SelectOption(
                    label=(
                        f"{t.proposing_team_name} → {t.other_team_name}"
                    )[:100],
                    value=str(t.trade_id),
                    description=(
                        f"{t.state} · {expiry_text(t.expires_at)}"
                    )[:100],
                )
                for t in window
            ],
        )
        self._owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        owner = self._owner
        view = _TradeDetailView(listing=owner, trade_id=int(self.values[0]))
        await view.refresh(interaction)


async def _effects_for(
    guild_id: int, state: DeskState, trade: TradeRow
) -> tuple[SwapEffect, SwapEffect | None]:
    """Both sides' cap impact, read fresh for the counterparty too."""
    my_id = state.team.team_id
    out_value = sum(
        (i.contract_value for i in trade.leaving(my_id)), Decimal("0")
    )
    in_value = sum(
        (i.contract_value for i in trade.arriving(my_id)), Decimal("0")
    )
    mine = evaluate_swap(state, out_value=out_value, in_value=in_value)
    other_id = trade.counterparty_id(my_id)
    async with db.connect() as conn:
        other = await queries.fetch_team_by_id(conn, other_id)
    if other is None:
        return (mine, None)
    other_ref = DeskTeam(
        team_id=other.id,
        key=other.key,
        name=other.name,
        color=other.color,
        principal_role_id=other.principal_role_id,
    )
    other_state = await load_desk_state(guild_id, other_ref)
    theirs = evaluate_swap(other_state, out_value=in_value, in_value=out_value)
    return (mine, theirs)


class _TradeDetailView(OwnedView):
    """
    One trade with a two-click arm/confirm before the route is revealed.

    The panel writes nothing, but accepting, declining or withdrawing is
    irreversible, so the first click restates exactly what the revealed
    command will do and the second re-reads the trade before printing it.
    """

    def __init__(self, *, listing: _TradesListView, trade_id: int) -> None:
        super().__init__(opener_id=listing.opener_id)
        self.listing = listing
        self.trade_id = trade_id
        self.armed = False
        self.expiry_hint = (
            "Nothing was written — the confirm step was never pressed, and "
            "this panel could not have answered the trade in any case."
        )
        self.add_item(_TradeArmButton(self))
        self.add_item(BackButton(self._back, label="Back to trades"))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.listing.render(interaction)

    async def refresh(
        self, interaction: discord.Interaction, *, note: str | None = None
    ) -> None:
        state = await load_desk_state(
            interaction.guild_id, self.listing.state.team
        )
        trades = await load_open_trades(state.team.team_id)
        trade = next((t for t in trades if t.trade_id == self.trade_id), None)
        if trade is None:
            await report_error(
                interaction,
                f"Trade `{self.trade_id}` is no longer open — it was "
                "answered, withdrawn or expired. Reloading the list.",
            )
            await self.listing.render(interaction)
            return
        mine, theirs = await _effects_for(interaction.guild_id, state, trade)
        if self.armed:
            embed = build_trade_route_embed(state, trade)
        else:
            embed = build_trade_detail_embed(
                state, trade, mine, theirs, armed=False
            )
        await edit_screen(interaction, embed=embed, view=self)
        if note:
            await interaction.followup.send(note, ephemeral=True)


class _TradeArmButton(discord.ui.Button):
    def __init__(self, owner: _TradeDetailView) -> None:
        super().__init__(
            label="Prepare answer route",
            style=discord.ButtonStyle.secondary,
            emoji="🔁",
        )
        self._owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        owner = self._owner
        if not owner.armed:
            state = await load_desk_state(
                interaction.guild_id, owner.listing.state.team
            )
            trades = await load_open_trades(state.team.team_id)
            trade = next(
                (t for t in trades if t.trade_id == owner.trade_id), None
            )
            if trade is None:
                await report_error(
                    interaction,
                    f"Trade `{owner.trade_id}` is no longer open.",
                )
                await owner.listing.render(interaction)
                return
            mine, theirs = await _effects_for(interaction.guild_id, state, trade)
            owner.armed = True
            self.label = "Show route"
            self.style = discord.ButtonStyle.danger
            await edit_screen(
                interaction,
                embed=build_trade_detail_embed(
                    state, trade, mine, theirs, armed=True
                ),
                view=owner,
            )
            await interaction.followup.send(
                "Read the confirm note: the command this reveals settles or "
                "kills the trade for real. Nothing has been written yet — "
                "the trade is still open and both payrolls are unchanged.",
                ephemeral=True,
            )
            return
        self.disabled = True
        await owner.refresh(
            interaction,
            note=(
                "Route revealed from a fresh read. Still unchanged: the "
                "trade's state, both teams' payrolls and caps, every "
                "contract involved, and all Discord team roles."
            ),
        )


# ── propose flow ─────────────────────────────────────────────────────


class _ProposeButton(discord.ui.Button):
    def __init__(self, *, enabled: bool, row: int) -> None:
        super().__init__(
            label="Propose trade…",
            style=discord.ButtonStyle.success,
            emoji="📝",
            row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, TradesView)
        await _open_partner_picker(interaction, view)


async def _open_partner_picker(
    interaction: discord.Interaction, view: TradesView
) -> None:
    guild_id = interaction.guild_id
    mine = view.state.team

    async def load():
        return await load_other_teams(guild_id, mine.team_id)

    async def on_pick(inner: discord.Interaction, team: DeskTeam) -> None:
        await _open_my_contract_picker(inner, view, team)

    picker = _PagedPicker(
        opener_id=view.opener_id,
        title="🔁 Propose a trade — partner",
        description=(
            "Pick the team you want to trade with. Every team in the guild "
            "is offered; you do not have to be its principal to propose, "
            "only to answer."
        ),
        noun="teams",
        placeholder="Which team?",
        load=load,
        option_of=_team_option,
        on_pick=on_pick,
        on_back=view.reload,
        back_label="Back to desk",
    )
    await picker.render(interaction)


async def _open_my_contract_picker(
    interaction: discord.Interaction, view: TradesView, partner: DeskTeam
) -> None:
    guild_id = interaction.guild_id
    mine = view.state.team

    async def load():
        return await load_team_contracts(guild_id, mine)

    async def on_pick(inner: discord.Interaction, row: ContractRow) -> None:
        await _open_their_contract_picker(inner, view, partner, row)

    picker = _PagedPicker(
        opener_id=view.opener_id,
        title=f"🔁 Propose a trade — you send ({mine.name})",
        description=(
            f"Pick the contract leaving **{mine.name}** for "
            f"**{partner.name}**. Salary, market value, term and tier are "
            "shown so the trade can be judged here."
        ),
        noun="contracts",
        placeholder="Which of your contracts?",
        load=load,
        option_of=_contract_option,
        on_pick=on_pick,
        on_back=lambda inner: _open_partner_picker(inner, view),
        back_label="Back to partner",
    )
    await picker.render(interaction)


async def _open_their_contract_picker(
    interaction: discord.Interaction,
    view: TradesView,
    partner: DeskTeam,
    my_row: ContractRow,
) -> None:
    guild_id = interaction.guild_id

    async def load():
        return await load_team_contracts(guild_id, partner)

    async def on_pick(inner: discord.Interaction, their_row: ContractRow) -> None:
        mine_state = await load_desk_state(inner.guild_id, view.state.team)
        theirs_state = await load_desk_state(inner.guild_id, partner)
        fresh_mine = next(
            (c for c in mine_state.contracts if c.contract_id == my_row.contract_id),
            None,
        )
        fresh_theirs = next(
            (
                c
                for c in theirs_state.contracts
                if c.contract_id == their_row.contract_id
            ),
            None,
        )
        if fresh_mine is None or fresh_theirs is None:
            await report_error(
                inner,
                "One of those contracts is no longer active — nothing was "
                "written. Start the trade again.",
            )
            await view.reload(inner)
            return
        mine_effect = evaluate_swap(
            mine_state,
            out_value=fresh_mine.contract_value,
            in_value=fresh_theirs.contract_value,
        )
        theirs_effect = evaluate_swap(
            theirs_state,
            out_value=fresh_theirs.contract_value,
            in_value=fresh_mine.contract_value,
        )
        leaf = _LeafView(
            opener_id=view.opener_id,
            back=lambda again: _open_their_contract_picker(
                again, view, partner, my_row
            ),
            label="Back to their contracts",
        )
        await edit_screen(
            interaction=inner,
            embed=build_propose_review_embed(
                mine_state=mine_state,
                theirs_state=theirs_state,
                my_row=fresh_mine,
                their_row=fresh_theirs,
                mine=mine_effect,
                theirs=theirs_effect,
            ),
            view=leaf,
        )

    picker = _PagedPicker(
        opener_id=view.opener_id,
        title=f"🔁 Propose a trade — you receive ({partner.name})",
        description=(
            f"Pick the contract coming back from **{partner.name}** in "
            f"exchange for **{my_row.driver_name}** "
            f"({format_money(my_row.contract_value)})."
        ),
        noun="contracts",
        placeholder="Which of their contracts?",
        load=load,
        option_of=_contract_option,
        on_pick=on_pick,
        on_back=lambda inner: _open_my_contract_picker(inner, view, partner),
        back_label="Back to your contracts",
    )
    await picker.render(interaction)


class _LeafView(OwnedView):
    """Terminal card: one embed and a back button."""

    def __init__(self, *, opener_id: int, back, label: str = "Back") -> None:
        super().__init__(opener_id=opener_id)
        self._back = back
        self.add_item(BackButton(self._go, label=label))

    async def _go(self, interaction: discord.Interaction) -> None:
        await self._back(interaction)


# ── entry point ──────────────────────────────────────────────────────


async def open_trades(
    interaction: discord.Interaction,
    *,
    on_back: BackCallback,
    opener_id: int | None = None,
) -> None:
    """
    Entry point for the `/league` home screen's Trades button.

    Same authority rule as the contracts desk: principals get their own
    teams, Manage Server gets all of them, anyone else gets the
    explanatory screen.
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
        view = _LeafView(opener_id=opener, back=on_back)
        await edit_screen(
            interaction,
            embed=build_no_team_embed(admin=admin, teams_exist=bool(all_teams)),
            view=view,
        )
        return
    state = await load_desk_state(interaction.guild_id, teams[0])
    trades = await load_open_trades(teams[0].team_id)
    view = TradesView(
        teams=teams,
        state=state,
        trades=trades,
        opener_id=opener,
        on_back=on_back,
        admin=admin,
    )
    await edit_screen(interaction, embed=build_desk_embed(state, trades), view=view)
