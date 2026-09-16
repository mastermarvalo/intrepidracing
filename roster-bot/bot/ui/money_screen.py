"""
Teams & money as an interactive screen (consolidation Screen 5).

Seeded from `setup_screen._TeamsView`, which listed teams and payroll and
nothing else. Everything a commissioner needs to answer "can this team
afford that driver" lived in four typed commands (`/market-admin budget
show|award|adjust|config`, `admin adjust-cap`) and one read-only
`/market team`. This screen puts the whole money position of every team
on one surface and routes each write through `bot.workflow`.

Two numbers, deliberately kept apart (CLAUDE.md §3, money invariant 9):

  * the CAP is a league rule, identical for every team, measured on
    committed contract value plus dead money;
  * the BUDGET is the team's own money, and only bites when
    `budget_config.enforce_budget` is on.

`cap_adjustment` ledger rows are **enforced** (G18, fixed 2026-09-16).
`rules.cap_headroom_ok` adds the net of a team's rows to the league cap
and validates every signing against that effective ceiling, so the rows
are both the audit trail and the mechanism. They used to be writers with
no reader, while two screens promised enforcement was coming.

Reads that have no `workflow` wrapper yet (per-team dead money, active
slot count, budget balance) are composed here from `bot.queries` inside a
single connection — see `load_money_state`. They are read-only and the
screen re-reads them before every render and before every write.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import discord

from bot import db, queries, workflow
from bot.market import budget as budget_engine
from bot.market import budget_ops
from bot.ui import base
from bot.ui.base import (
    COLOR_INFO,
    COLOR_OK,
    COLOR_WARN,
    SELECT_MAX_OPTIONS,
    AdminOwnedView,
    BackButton,
    BackCallback,
    report_error,
    truncate_field,
)

# One page of teams is small enough that four lines per team still fit
# the embed description, and the same window feeds the select, so the
# list and the picker can never disagree about what is on screen.
TEAMS_PER_PAGE = 8

_NO_ROUND = "__none__"
_SCOPE_SEASON = "__season__"
_FILTER_ALL = "__all__"
_FILTER_OVER_CAP = "__over_cap__"
_FILTER_NEGATIVE = "__negative__"

_CREDIT = "credit"
_DEBIT = "debit"
_ZERO = Decimal("0")

_CAP_NOTE = (
    "Cap adjustments **change what this team may spend.** A positive "
    "delta grants extra room above the league cap, a negative one docks "
    "it, and every signing is validated against the adjusted ceiling."
)



# ── state ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TeamMoneyRow:
    """One team's whole money position, as displayed."""

    team_id: int
    key: str
    name: str
    payroll: Decimal
    dead_money: Decimal
    effective_payroll: Decimal
    cap: Decimal
    slots_used: int
    slots_total: int
    balance: Decimal | None
    available: Decimal | None
    #: Season-to-date total of this team's cap adjustments. Positive
    #: grants room above the league cap, negative docks it.
    cap_adjustment: Decimal = Decimal("0")

    @property
    def effective_cap(self) -> Decimal:
        """
        What this team may actually commit.

        G18 made adjustments enforceable in `bot/contracts/rules.py`.
        This screen went on showing the unadjusted league cap, so a
        team granted relief still read as having no room while the
        offer check let the signing through.
        """
        return self.cap + self.cap_adjustment

    @property
    def cap_space(self) -> Decimal:
        return self.effective_cap - self.effective_payroll

    @property
    def over_cap(self) -> bool:
        return self.cap_space < 0

    @property
    def negative_headroom(self) -> bool:
        return self.available is not None and self.available < 0


@dataclass(frozen=True)
class MoneyState:
    """Everything the screen renders, read in one transaction."""

    season_name: str | None
    season_id: int | None
    cap: Decimal
    budgets_configured: bool
    budgets_enforced: bool
    rollover_enabled: bool
    rows: list[TeamMoneyRow]

    def row(self, key: str) -> TeamMoneyRow | None:
        for row in self.rows:
            if row.key == key:
                return row
        return None


async def load_money_state(guild_id: int) -> MoneyState:
    """
    Re-read every money figure for the active season.

    Called on open, after every write, and again inside each confirm step
    so a preview is never shown from a snapshot taken when the screen was
    opened (G1). Read-only: unlike `budget_ops.snapshot` it never credits
    an opening balance as a side effect of being looked at.
    """
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            return MoneyState(
                season_name=None,
                season_id=None,
                cap=Decimal("0"),
                budgets_configured=False,
                budgets_enforced=False,
                rollover_enabled=False,
                rows=[],
            )
        league_cfg = await queries.fetch_league_config_row(conn, season.id, None)
        cap = league_cfg.salary_cap if league_cfg else Decimal("0")
        slots_total = league_cfg.active_driver_slots if league_cfg else 0
        budget_cfg = await queries.fetch_budget_config(conn, season.id, None)

        rows: list[TeamMoneyRow] = []
        for team in await queries.fetch_all_teams(conn, guild_id):
            payroll = await queries.fetch_team_payroll(conn, team.id)
            dead = await queries.fetch_dead_money_total(conn, team.id, season.id)
            slots = await queries.fetch_team_active_slot_count(conn, team.id)
            adjustment = await queries.fetch_cap_adjustment_total(
                conn, season.id, team.id
            )
            balance: Decimal | None = None
            available: Decimal | None = None
            if budget_cfg is not None:
                balance = await queries.fetch_budget_balance(conn, team.id, season.id)
                available = budget_engine.available_to_spend(balance, payroll + dead)
            rows.append(
                TeamMoneyRow(
                    team_id=team.id,
                    key=team.key,
                    name=team.name,
                    payroll=payroll,
                    dead_money=dead,
                    effective_payroll=payroll + dead,
                    cap=cap,
                    slots_used=slots,
                    slots_total=slots_total,
                    balance=balance,
                    available=available,
                    cap_adjustment=adjustment,
                )
            )

    rows.sort(key=lambda r: (-r.effective_payroll, r.name.casefold()))
    return MoneyState(
        season_name=season.name,
        season_id=season.id,
        cap=cap,
        budgets_configured=budget_cfg is not None,
        budgets_enforced=bool(budget_cfg and budget_cfg.enforce_budget),
        rollover_enabled=bool(budget_cfg and budget_cfg.rollover_enabled),
        rows=rows,
    )


# ── rendering ────────────────────────────────────────────────────────


def format_money(value: Decimal | None) -> str:
    """Money as the league writes it: `$12.50M`, or an em dash for none."""
    if value is None:
        return "—"
    return f"${value:,.2f}M"


_m = format_money


def _signed(value: Decimal) -> str:
    sign = "+" if value >= 0 else "−"
    return f"{sign}{_m(abs(value))}"


def budgets_header(state: MoneyState) -> str:
    """The one line that decides how the rest of the screen should be read."""
    if not state.budgets_configured:
        return (
            "🟡 **Budgets: not configured** for this season — every team is "
            "measured on the cap alone. Budget settings can turn them on."
        )
    if not state.budgets_enforced:
        return (
            "🟡 **Budgets: recorded but NOT enforced** — balances below are "
            "informational; signings and trades clear the cap only."
        )
    rollover = "on" if state.rollover_enabled else "off"
    return (
        "🟢 **Budgets: enforced** — every signing and trade must clear both "
        f"the cap and the team's balance. Rollover is **{rollover}**."
    )


def paginate_rows(
    rows: list[TeamMoneyRow], page: int
) -> tuple[list[TeamMoneyRow], int, int]:
    """`(window, page, total_pages)` with the page clamped into range."""
    total_pages = max(1, -(-len(rows) // TEAMS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    start = page * TEAMS_PER_PAGE
    return rows[start : start + TEAMS_PER_PAGE], page, total_pages


def filter_rows(rows: list[TeamMoneyRow], team_filter: str) -> list[TeamMoneyRow]:
    if team_filter == _FILTER_OVER_CAP:
        return [r for r in rows if r.over_cap]
    if team_filter == _FILTER_NEGATIVE:
        return [r for r in rows if r.negative_headroom]
    return list(rows)


def build_money_embed(
    state: MoneyState, *, page: int = 0, team_filter: str = _FILTER_ALL
) -> discord.Embed:
    """The team-by-team money table. Pure: no DB, no interaction."""
    title = f"💰 Teams & money — {state.season_name or 'no active season'}"
    if state.season_id is None:
        return discord.Embed(
            title=title,
            description=(
                "No active season. Open **Setup → Season** to create and "
                "activate one; payroll, cap and budgets are all season-scoped."
            ),
            color=COLOR_INFO,
        )
    if not state.rows:
        return discord.Embed(
            title=title,
            description=(
                f"{budgets_header(state)}\n\n"
                "No teams yet. Create one with `/roster create` — team "
                "membership still lives on Discord roles."
            ),
            color=COLOR_INFO,
        )

    shown = filter_rows(state.rows, team_filter)
    window, page, total_pages = paginate_rows(shown, page)
    lines = [budgets_header(state), ""]
    if not window:
        lines.append("No team matches this filter.")
    for row in window:
        flag = " ⚠ over cap" if row.over_cap else ""
        lines.append(f"**{row.name}** (`{row.key}`){flag}")
        lines.append(
            f"    Payroll: {_m(row.payroll)}  |  Dead: {_m(row.dead_money)}"
            f"  |  Cap space: {_signed(row.cap_space)}"
        )
        lines.append(
            f"    Budget: {_m(row.balance)}  |  Available: "
            f"{_m(row.available)}  |  Slots: {row.slots_used}/{row.slots_total}"
        )
    lines.append("")
    lines.append(
        f"Cap {_m(state.cap)} · {len(shown)} of {len(state.rows)} team(s) "
        f"· page {page + 1}/{total_pages}"
    )
    lines.append(_CAP_NOTE)

    over = [r for r in state.rows if r.over_cap]
    embed = discord.Embed(
        title=title,
        description=truncate_field("\n".join(lines), base.EMBED_FIELD_LIMIT * 4),
        color=COLOR_WARN if over else COLOR_OK,
    )
    embed.set_footer(
        text=(
            "Equivalent commands: /market-admin budget show · award · adjust "
            "· config · admin adjust-cap"
        )
    )
    return embed


def build_cap_sheet_embed(row: TeamMoneyRow, state: MoneyState) -> discord.Embed:
    """One team's cap sheet: the arithmetic, spelled out."""
    lines = [
        f"Payroll (active contract value): {_m(row.payroll)}",
        f"Dead money this season: {_m(row.dead_money)}",
        f"**Charged against the cap: {_m(row.effective_payroll)}**",
        f"Cap: {_m(row.cap)} → cap space {_signed(row.cap_space)}",
        "",
        f"Budget balance: {_m(row.balance)}",
        f"Available to spend: {_m(row.available)}",
        f"Active slots used: {row.slots_used} of {row.slots_total}",
    ]
    if row.balance is None:
        lines.append(
            "Budgets are not configured for this season, so the balance "
            "lines are blank rather than zero."
        )
    elif not state.budgets_enforced:
        lines.append(
            "Budgets are recorded but not enforced: only the cap blocks a "
            "signing today."
        )
    embed = discord.Embed(
        title=f"💰 {row.name} — cap sheet",
        description=truncate_field("\n".join(lines)),
        color=COLOR_WARN if row.over_cap or row.negative_headroom else COLOR_OK,
    )
    embed.set_footer(text="Read-only view · equivalent: /market team, budget show")
    return embed


def _parse_amount(raw: str) -> Decimal:
    """
    Read a dollars-in-millions figure, keeping the sign the user typed.

    Raises ValueError with user-facing text so each caller can surface it
    through `report_error` rather than inventing its own message.
    """
    text = str(raw).strip().replace("$", "").replace(",", "")
    text = text.rstrip("Mm").strip().lstrip("+")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Could not read `{raw}` as an amount in $M.") from exc
    return value


# ── shared confirm step ──────────────────────────────────────────────


class _ConfirmView(AdminOwnedView):
    """
    A preview embed plus an explicit Confirm.

    Every write on this screen is an append-only ledger row, so nothing
    here can be undone by pressing a button again — which is exactly why
    the consequence is restated before the write, not after it.
    """

    def __init__(
        self,
        *,
        opener_id: int,
        confirm_label: str,
        on_confirm,
        on_cancel: BackCallback,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self._on_confirm = on_confirm
        self.expiry_hint = (
            "Nothing was written — the confirm step was never pressed."
        )
        self.add_item(_ConfirmButton(confirm_label))
        self.add_item(BackButton(on_cancel, label="Cancel", row=1))

    async def run(self, interaction: discord.Interaction) -> None:
        await self._on_confirm(interaction)


class _ConfirmButton(discord.ui.Button):
    def __init__(self, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.danger, emoji="✅")

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, _ConfirmView)
        await view.run(interaction)


async def _show_confirm(
    interaction: discord.Interaction,
    *,
    view: _ConfirmView,
    embed: discord.Embed,
) -> None:
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)
    await view.bind_message(interaction)


# ── main view ────────────────────────────────────────────────────────


class MoneyView(AdminOwnedView):
    """Team money table with a picker, filter, paging and the write actions."""

    def __init__(
        self,
        *,
        state: MoneyState,
        opener_id: int,
        on_back: BackCallback,
        page: int = 0,
        team_filter: str = _FILTER_ALL,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.state = state
        self.on_back = on_back
        shown = filter_rows(state.rows, team_filter)
        window, page, total_pages = paginate_rows(shown, page)
        self.page = page
        self.team_filter = team_filter
        self.total_pages = total_pages

        if window:
            self.add_item(_TeamSelect(window))
        if state.rows:
            self.add_item(_FilterSelect(team_filter))
        if total_pages > 1:
            self.add_item(_PageButton(-1, disabled=page == 0, row=2))
            self.add_item(
                _PageButton(1, disabled=page >= total_pages - 1, row=2)
            )
        self.add_item(_AwardButton(row=3, enabled=bool(state.rows)))
        self.add_item(_AdjustBudgetButton(row=3, enabled=bool(state.rows)))
        self.add_item(_BudgetSettingsButton(row=3, enabled=state.season_id is not None))
        self.add_item(_CapAdjustButton(row=3, enabled=bool(state.rows)))
        self.add_item(BackButton(on_back, row=4))

    async def reload(
        self,
        interaction: discord.Interaction,
        *,
        note: str | None = None,
        page: int | None = None,
    ) -> None:
        """Re-read the state, rebuild, then say what happened."""
        state = await load_money_state(interaction.guild_id)
        view = MoneyView(
            state=state,
            opener_id=self.opener_id,
            on_back=self.on_back,
            page=self.page if page is None else page,
            team_filter=self.team_filter,
        )
        embed = build_money_embed(
            state, page=view.page, team_filter=view.team_filter
        )
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
        await view.bind_message(interaction)
        if note:
            await interaction.followup.send(note, ephemeral=True)


class _TeamSelect(discord.ui.Select):
    """Open a team's cap sheet. Options are always the visible page."""

    def __init__(self, window: list[TeamMoneyRow]) -> None:
        super().__init__(
            placeholder="Cap sheet for which team?",
            options=[
                discord.SelectOption(
                    label=f"{r.name} · payroll {_m(r.payroll)}"[:100],
                    value=r.key,
                    description=(
                        f"cap space {_signed(r.cap_space)} · available "
                        f"{_m(r.available)}"
                    )[:100],
                )
                for r in window[:SELECT_MAX_OPTIONS]
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, MoneyView)
        state = await load_money_state(interaction.guild_id)
        row = state.row(self.values[0])
        if row is None:
            await report_error(
                interaction, "That team no longer exists — reloading the list."
            )
            await view.reload(interaction)
            return
        sheet = _CapSheetView(parent=view, team_key=row.key)
        await interaction.response.edit_message(
            embed=build_cap_sheet_embed(row, state), view=sheet
        )
        await sheet.bind_message(interaction)


class _CapSheetView(AdminOwnedView):
    def __init__(self, *, parent: MoneyView, team_key: str) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.team_key = team_key
        self.add_item(BackButton(self._back, label="Back to teams"))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)


class _FilterSelect(discord.ui.Select):
    def __init__(self, current: str) -> None:
        options = [
            discord.SelectOption(
                label="All teams", value=_FILTER_ALL, default=current == _FILTER_ALL
            ),
            discord.SelectOption(
                label="Over the cap",
                value=_FILTER_OVER_CAP,
                default=current == _FILTER_OVER_CAP,
            ),
            discord.SelectOption(
                label="Negative budget headroom",
                value=_FILTER_NEGATIVE,
                default=current == _FILTER_NEGATIVE,
            ),
        ]
        super().__init__(placeholder="Filter teams…", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, MoneyView)
        view.team_filter = self.values[0]
        await view.reload(interaction, page=0)


class _PageButton(discord.ui.Button):
    def __init__(self, step: int, *, disabled: bool, row: int) -> None:
        super().__init__(
            label="Prev" if step < 0 else "Next",
            style=discord.ButtonStyle.secondary,
            emoji="◀" if step < 0 else "▶",
            disabled=disabled,
            row=row,
        )
        self.step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, MoneyView)
        await view.reload(interaction, page=view.page + self.step)


# ── award prize money ────────────────────────────────────────────────


class _AwardButton(discord.ui.Button):
    def __init__(self, *, row: int, enabled: bool) -> None:
        super().__init__(
            label="Award prize money",
            style=discord.ButtonStyle.success,
            emoji="🏆",
            row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, MoneyView)
        flow = _AwardFlow(parent=view)
        await flow.start(interaction)


class _AwardFlow(AdminOwnedView):
    """
    Round → team → amount → preview → confirm.

    The round is context for the audit note, not a computation: nothing in
    `bot/market/` derives a prize table from a round, so the amount is
    the commissioner's. Race earnings are a separate, automatic kind
    written at import time.
    """

    def __init__(self, *, parent: MoneyView) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.round_label: str | None = None
        self.team_key: str | None = None
        self.rounds: list[workflow.RaceRoundSummary] = []
        self.round_page = 0

    async def start(self, interaction: discord.Interaction) -> None:
        self.rounds = await workflow.list_rounds(interaction.guild_id)
        await self.render(interaction)

    async def render(self, interaction: discord.Interaction) -> None:
        self.clear_items()
        # Most recent round first: `list_race_rounds` is ascending, so the
        # default (the latest import) is the LAST element (CLAUDE.md §12).
        newest_first = list(reversed(self.rounds))
        window, page, pages = _paginate(newest_first, self.round_page)
        self.round_page = page
        self.add_item(_AwardRoundSelect(self, window))
        if pages > 1:
            self.add_item(_RoundPageButton(self, -1, disabled=page == 0, row=1))
            self.add_item(
                _RoundPageButton(self, 1, disabled=page >= pages - 1, row=1)
            )
        self.add_item(BackButton(self._cancel, label="Cancel", row=2))
        embed = discord.Embed(
            title="🏆 Award prize money — step 1 of 3",
            description=(
                "Pick the round this prize relates to (it goes into the audit "
                "note), or award it outside any round.\n\n"
                f"Rounds are listed newest first · page {page + 1}/{pages}"
            ),
            color=COLOR_INFO,
        )
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)
        await self.bind_message(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)

    async def pick_round(
        self, interaction: discord.Interaction, label: str | None
    ) -> None:
        self.round_label = label
        state = await load_money_state(interaction.guild_id)
        self.clear_items()
        self.add_item(_AwardTeamSelect(self, state.rows[:SELECT_MAX_OPTIONS]))
        self.add_item(BackButton(self._cancel, label="Cancel", row=1))
        embed = discord.Embed(
            title="🏆 Award prize money — step 2 of 3",
            description=(
                f"Round: **{label or 'not tied to a round'}**\n\n"
                "Pick the team to credit. One team per award, so the ledger "
                "shows exactly who was paid what."
            ),
            color=COLOR_INFO,
        )
        await interaction.response.edit_message(embed=embed, view=self)


def _paginate(items: list, page: int) -> tuple[list, int, int]:
    pages = max(1, -(-len(items) // SELECT_MAX_OPTIONS))
    page = max(0, min(page, pages - 1))
    start = page * SELECT_MAX_OPTIONS
    return items[start : start + SELECT_MAX_OPTIONS], page, pages


class _RoundPageButton(discord.ui.Button):
    def __init__(
        self, flow: _AwardFlow, step: int, *, disabled: bool, row: int
    ) -> None:
        super().__init__(
            label="Prev rounds" if step < 0 else "More rounds",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self._flow = flow
        self._step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        self._flow.round_page += self._step
        await self._flow.render(interaction)


class _AwardRoundSelect(discord.ui.Select):
    def __init__(self, flow: _AwardFlow, window: list) -> None:
        options = [
            discord.SelectOption(
                label="Not tied to a round",
                value=_NO_ROUND,
                description="e.g. a standings payout for the whole season",
            )
        ]
        for r in window[: SELECT_MAX_OPTIONS - 1]:
            options.append(
                discord.SelectOption(
                    label=f"{r.tier_code} · {r.round_label}"[:100],
                    value=f"{r.tier_code}|{r.round_label}"[:100],
                    description=f"{r.result_count} result rows"[:100],
                )
            )
        super().__init__(placeholder="Which round?", options=options)
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        picked = self.values[0]
        label = None if picked == _NO_ROUND else picked.split("|", 1)[1]
        await self._flow.pick_round(interaction, label)


class _AwardTeamSelect(discord.ui.Select):
    def __init__(self, flow: _AwardFlow, rows: list[TeamMoneyRow]) -> None:
        super().__init__(
            placeholder="Credit which team?",
            options=[
                discord.SelectOption(
                    label=f"{r.name} · balance {_m(r.balance)}"[:100],
                    value=r.key,
                )
                for r in rows
            ],
        )
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        self._flow.team_key = self.values[0]
        await interaction.response.send_modal(_AwardModal(self._flow))


class _AwardModal(base.PanelModal, title="Award prize money"):
    def __init__(self, flow: _AwardFlow) -> None:
        super().__init__()
        self._flow = flow
        self._amount = discord.ui.TextInput(
            label="Amount in $M (credit, positive)",
            placeholder="e.g. 2.5",
            max_length=16,
        )
        self._note = discord.ui.TextInput(
            label="Reason (required — audit trail)",
            placeholder="e.g. P1 constructors' payout",
            max_length=200,
        )
        self.add_item(self._amount)
        self.add_item(self._note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            amount = _parse_amount(self._amount.value)
        except ValueError as exc:
            await report_error(interaction, str(exc))
            return
        if amount <= 0:
            await report_error(
                interaction,
                "Prize money is a credit — enter a positive amount, or use "
                "**Manual budget adjustment** to take money away.",
            )
            return
        await _preview_budget_write(
            interaction,
            flow_parent=self._flow.parent,
            on_cancel=self._flow._cancel,
            team_key=str(self._flow.team_key),
            amount=amount,
            kind=budget_ops.KIND_PRIZE,
            note=_award_note(self._flow.round_label, str(self._note.value)),
            title="🏆 Confirm prize money",
        )


def _award_note(round_label: str | None, reason: str) -> str:
    if round_label:
        return f"Prize money ({round_label}): {reason}"
    return f"Prize money: {reason}"


# ── manual budget adjustment ─────────────────────────────────────────


class _AdjustBudgetButton(discord.ui.Button):
    def __init__(self, *, row: int, enabled: bool) -> None:
        super().__init__(
            label="Manual budget adjustment",
            style=discord.ButtonStyle.primary,
            emoji="±",
            row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, MoneyView)
        flow = _AdjustFlow(parent=view)
        await flow.render_team_step(interaction)


class _AdjustFlow(AdminOwnedView):
    """
    Team → direction → amount → preview → confirm.

    Direction is its own step because a modal cannot hold a select and a
    typed `-2.5` is one missing character away from its own opposite. The
    modal therefore takes a magnitude and this view owns the sign.
    """

    def __init__(self, *, parent: MoneyView) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.team_key: str | None = None
        self.direction: str | None = None
        self.page = 0

    async def render_team_step(self, interaction: discord.Interaction) -> None:
        state = await load_money_state(interaction.guild_id)
        window, page, pages = _paginate(state.rows, self.page)
        self.page = page
        self.clear_items()
        self.add_item(_AdjustTeamSelect(self, window))
        if pages > 1:
            self.add_item(_AdjustPageButton(self, -1, disabled=page == 0, row=1))
            self.add_item(
                _AdjustPageButton(self, 1, disabled=page >= pages - 1, row=1)
            )
        self.add_item(BackButton(self._cancel, label="Cancel", row=2))
        embed = discord.Embed(
            title="± Manual budget adjustment — step 1 of 3",
            description=(
                "Pick the team.\n\nThis moves real money in the team budget "
                "ledger (unlike a cap adjustment, which changes nothing).\n"
                f"Teams {page + 1}/{pages}"
            ),
            color=COLOR_INFO,
        )
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)
        await self.bind_message(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)

    async def render_direction_step(self, interaction: discord.Interaction) -> None:
        self.clear_items()
        self.add_item(_DirectionSelect(self))
        self.add_item(BackButton(self._cancel, label="Cancel", row=1))
        embed = discord.Embed(
            title="± Manual budget adjustment — step 2 of 3",
            description=(
                f"Team: **{self.team_key}**\n\n"
                "Credit adds to the balance; debit takes money away. The "
                "sign is picked here, never typed."
            ),
            color=COLOR_INFO,
        )
        await interaction.response.edit_message(embed=embed, view=self)


class _AdjustPageButton(discord.ui.Button):
    def __init__(
        self, flow: _AdjustFlow, step: int, *, disabled: bool, row: int
    ) -> None:
        super().__init__(
            label="Prev teams" if step < 0 else "More teams",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self._flow = flow
        self._step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        self._flow.page += self._step
        await self._flow.render_team_step(interaction)


class _AdjustTeamSelect(discord.ui.Select):
    def __init__(self, flow: _AdjustFlow, rows: list[TeamMoneyRow]) -> None:
        super().__init__(
            placeholder="Adjust which team's budget?",
            options=[
                discord.SelectOption(
                    label=f"{r.name} · balance {_m(r.balance)}"[:100],
                    value=r.key,
                )
                for r in rows
            ],
        )
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        self._flow.team_key = self.values[0]
        await self._flow.render_direction_step(interaction)


class _DirectionSelect(discord.ui.Select):
    def __init__(self, flow: _AdjustFlow) -> None:
        super().__init__(
            placeholder="Credit or debit?",
            options=[
                discord.SelectOption(
                    label="Credit (+) — give the team money",
                    value=_CREDIT,
                ),
                discord.SelectOption(
                    label="Debit (−) — take money away",
                    value=_DEBIT,
                ),
            ],
        )
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        self._flow.direction = self.values[0]
        await interaction.response.send_modal(_AdjustModal(self._flow))


class _AdjustModal(base.PanelModal, title="Budget adjustment"):
    def __init__(self, flow: _AdjustFlow) -> None:
        super().__init__()
        self._flow = flow
        self._amount = discord.ui.TextInput(
            label="Amount in $M (no sign — set above)",
            placeholder="e.g. 2.5",
            max_length=16,
        )
        self._note = discord.ui.TextInput(
            label="Reason (required — audit trail)",
            placeholder="e.g. stewards' fine, sponsor payment",
            max_length=200,
        )
        self.add_item(self._amount)
        self.add_item(self._note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            amount = _parse_amount(self._amount.value)
        except ValueError as exc:
            await report_error(interaction, str(exc))
            return
        magnitude = abs(amount)
        if magnitude == 0:
            await report_error(interaction, "A zero adjustment writes nothing.")
            return
        signed = magnitude if self._flow.direction == _CREDIT else -magnitude
        word = "credit" if signed > 0 else "debit"
        await _preview_budget_write(
            interaction,
            flow_parent=self._flow.parent,
            on_cancel=self._flow._cancel,
            team_key=str(self._flow.team_key),
            amount=signed,
            kind=budget_ops.KIND_ADJUSTMENT,
            note=f"Manual {word}: {self._note.value}",
            title=f"± Confirm budget {word}",
        )


# ── shared preview → confirm for a budget write ──────────────────────


async def _preview_budget_write(
    interaction: discord.Interaction,
    *,
    flow_parent: MoneyView,
    on_cancel: BackCallback,
    team_key: str,
    amount: Decimal,
    kind: str,
    note: str,
    title: str,
) -> None:
    """
    Show balance-before → balance-after from a fresh read, then confirm.

    The state is read here rather than reused from the flow so a balance
    that moved while the modal was open is the one shown.
    """
    state = await load_money_state(interaction.guild_id)
    row = state.row(team_key)
    if row is None:
        await report_error(interaction, f"No team `{team_key}` any more.")
        return
    if not state.budgets_configured:
        await report_error(
            interaction,
            "Budgets are not configured for this season, so there is no "
            "ledger to write to. Use **Budget settings** first.",
        )
        return

    before = row.balance or Decimal("0")
    after = before + amount
    enforcement = (
        "Budgets are **enforced**, so this changes what the team may sign."
        if state.budgets_enforced
        else "Budgets are **not enforced**, so this records money but blocks "
        "nothing today."
    )
    embed = discord.Embed(
        title=title,
        description=truncate_field(
            "\n".join(
                [
                    f"**{row.name}** (`{row.key}`)",
                    f"Balance: {_m(before)} → **{_m(after)}**"
                    f"  ({_signed(amount)})",
                    f"Available to spend: {_m(row.available)} → "
                    f"**{_m((row.available or Decimal('0')) + amount)}**",
                    "",
                    f"Ledger kind: `{kind}` · note: {note}",
                    "",
                    "This appends a row to the team budget ledger, which is "
                    "**append-only — it cannot be edited or deleted**. A "
                    "mistake has to be corrected with an opposite entry.",
                    enforcement,
                ]
            )
        ),
        color=COLOR_WARN,
    )

    async def _do(confirm_interaction: discord.Interaction) -> None:
        await confirm_interaction.response.defer(ephemeral=True)
        try:
            balance = await workflow.award_budget(
                guild_id=confirm_interaction.guild_id,
                actor_id=confirm_interaction.user.id,
                team_key=team_key,
                kind=kind,
                amount=amount,
                note=note,
            )
        except workflow.WorkflowError as exc:
            await report_error(confirm_interaction, str(exc))
            return
        await flow_parent.reload(
            confirm_interaction,
            note=(
                f"✅ {row.name}: {_signed(amount)} written as `{kind}`. New "
                f"balance {_m(balance)}. Nothing was announced publicly and "
                "no contract changed."
            ),
        )

    view = _ConfirmView(
        opener_id=flow_parent.opener_id,
        confirm_label="Write it",
        on_confirm=_do,
        on_cancel=on_cancel,
    )
    await _show_confirm(interaction, view=view, embed=embed)


# ── budget settings ──────────────────────────────────────────────────


class _BudgetSettingsButton(discord.ui.Button):
    def __init__(self, *, row: int, enabled: bool) -> None:
        super().__init__(
            label="Budget settings",
            style=discord.ButtonStyle.secondary,
            emoji="⚙",
            row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, MoneyView)
        flow = _SettingsFlow(parent=view)
        await flow.render_scope_step(interaction)


class _SettingsFlow(AdminOwnedView):
    """
    Scope → toggles + rates.

    `enforce_budget` and `rollover_enabled` are booleans and a modal
    cannot hold a switch, so they are buttons; the five numeric rates are
    exactly `MODAL_MAX_INPUTS`, which is why they get the modal.
    """

    def __init__(self, *, parent: MoneyView) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.tier_code: str | None = None

    async def render_scope_step(self, interaction: discord.Interaction) -> None:
        tiers = await workflow.list_tier_choices(interaction.guild_id)
        self.clear_items()
        self.add_item(_ScopeSelect(self, tiers))
        self.add_item(BackButton(self._cancel, label="Cancel", row=1))
        embed = discord.Embed(
            title="⚙ Budget settings — scope",
            description=(
                "Season default applies to every tier that has no override "
                "of its own.\n\n"
                "⚠ Per-tier overrides are only partially wired league-wide "
                "(CLAUDE.md §9): a tier row is honoured when validating an "
                "offer but ignored when validating a trade. Prefer the "
                "season default until that resolver exists."
            ),
            color=COLOR_INFO,
        )
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)
        await self.bind_message(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)

    async def render_settings(self, interaction: discord.Interaction) -> None:
        cfg = await workflow.get_budget_config(
            guild_id=interaction.guild_id, tier=self.tier_code
        )
        self.clear_items()
        enforce = bool(cfg and cfg.enforce_budget)
        rollover = bool(cfg and cfg.rollover_enabled)
        escrow = bool(cfg and cfg.escrow_enabled)
        self.add_item(_ToggleButton(self, field="enforce_budget", value=enforce))
        self.add_item(_ToggleButton(self, field="rollover_enabled", value=rollover))
        # Disabled until the row exists: this toggle writes one field and
        # would have to invent an opening budget and every rate to create
        # it. Edit rates first, which is what the empty-state embed says.
        self.add_item(
            _ToggleButton(
                self, field="escrow_enabled", value=escrow, enabled=cfg is not None
            )
        )
        self.add_item(_EditRatesButton(self, row=1))
        self.add_item(BackButton(self._cancel, label="Back to teams", row=2))
        embed = _build_settings_embed(cfg, self.tier_code)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)
        await self.bind_message(interaction)


def _build_settings_embed(cfg, tier_code: str | None) -> discord.Embed:
    scope = f"tier `{tier_code}`" if tier_code else "season default"
    if cfg is None:
        return discord.Embed(
            title=f"⚙ Budget settings — {scope}",
            description=(
                "No budget config row for this scope yet, so budgets are "
                "**off**: signings and trades clear the cap only.\n\n"
                "Use **Edit rates** to create the row, then switch "
                "enforcement on."
            ),
            color=COLOR_INFO,
        )
    escrow_note = (
        "salary charged from cash race by race"
        if cfg.escrow_enabled
        else "commitment only — cash is not drawn down"
    )
    lines = [
        f"Enforce budgets: **{'on' if cfg.enforce_budget else 'off'}**",
        f"Rollover into the next season: **{'on' if cfg.rollover_enabled else 'off'}**",
        f"Escrow: **{'on' if cfg.escrow_enabled else 'off'}** — {escrow_note}",
        "",
        f"Opening budget: {_m(cfg.opening_budget)}",
        f"Earnings per point: {_m(cfg.earnings_per_point)}",
        f"DNF penalty: {_m(cfg.dnf_penalty)}",
        f"DNS penalty: {_m(cfg.dns_penalty)}",
        f"Per incident point: {_m(cfg.penalty_per_incident_pt)}",
        "",
        "Penalties are stored as positive magnitudes; the engine applies "
        "the sign. A rate of 0 switches that charge off.",
    ]
    return discord.Embed(
        title=f"⚙ Budget settings — {scope}",
        description=truncate_field("\n".join(lines)),
        color=COLOR_OK if cfg.enforce_budget else COLOR_INFO,
    ).set_footer(text="Equivalent command: /market-admin budget config")


class _ScopeSelect(discord.ui.Select):
    def __init__(self, flow: _SettingsFlow, tiers: list[tuple[str, str]]) -> None:
        options = [
            discord.SelectOption(
                label="Season default (every tier)", value=_SCOPE_SEASON
            )
        ]
        for code, label in tiers[: SELECT_MAX_OPTIONS - 1]:
            options.append(
                discord.SelectOption(label=label[:100], value=code)
            )
        super().__init__(placeholder="Which scope?", options=options)
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        picked = self.values[0]
        self._flow.tier_code = None if picked == _SCOPE_SEASON else picked
        await self._flow.render_settings(interaction)


class _ToggleButton(discord.ui.Button):
    """Flip one boolean, behind a confirm that states what changes."""

    def __init__(
        self, flow: _SettingsFlow, *, field: str, value: bool, enabled: bool = True
    ) -> None:
        label = {
            "enforce_budget": "Enforce budgets",
            "rollover_enabled": "Rollover",
            "escrow_enabled": "Escrow",
        }[field]
        super().__init__(
            label=f"{label}: {'on' if value else 'off'} → {'off' if value else 'on'}",
            style=discord.ButtonStyle.primary if value else discord.ButtonStyle.secondary,
            disabled=not enabled,
        )
        self._flow = flow
        self._field = field
        self._value = value

    async def callback(self, interaction: discord.Interaction) -> None:
        target = not self._value
        if self._field == "enforce_budget":
            consequence = (
                "Every signing and trade will have to clear the team's "
                "budget balance as well as the cap. Teams already above "
                "their balance will be blocked from signing until they earn "
                "their way back."
                if target
                else "Signings and trades will stop checking budgets "
                "entirely — balances keep being recorded, but nothing is "
                "blocked by them."
            )
        elif self._field == "rollover_enabled":
            consequence = (
                "Unspent budget from a finished season can be carried into "
                "this one by the Offseason wizard's rollover step."
                if target
                else "Rollover into this season is refused: teams start on "
                "the opening budget alone, and any unspent money is lost."
            )
        else:
            consequence = (
                "Each race will debit that race's share of salary from the "
                "team's **cash** into escrow, and the total comes back when "
                "the contract ends, adjusted for how the driver's value "
                "moved. Nothing is charged at signing and nothing reaches "
                "back: contracts already running start being charged from "
                "the next race imported, not retroactively."
                if target
                else "Race imports stop debiting salary. Contracts still "
                "count against the spending cap, but balances are no longer "
                "drawn down. Money already held on live contracts stays "
                "held and still settles when they end."
            )
        embed = discord.Embed(
            title=f"⚙ Confirm: {self._field} → {'on' if target else 'off'}",
            description=consequence,
            color=COLOR_WARN,
        )

        async def _do(confirm: discord.Interaction) -> None:
            await confirm.response.defer(ephemeral=True)
            await _save_settings(
                confirm, self._flow, **{self._field: target}
            )

        view = _ConfirmView(
            opener_id=self._flow.opener_id,
            confirm_label="Apply",
            on_confirm=_do,
            on_cancel=self._flow.render_settings,
        )
        await _show_confirm(interaction, view=view, embed=embed)


class _EditRatesButton(discord.ui.Button):
    def __init__(self, flow: _SettingsFlow, *, row: int) -> None:
        super().__init__(
            label="Edit rates", style=discord.ButtonStyle.secondary, row=row
        )
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        cfg = await workflow.get_budget_config(
            guild_id=interaction.guild_id, tier=self._flow.tier_code
        )
        state = await load_money_state(interaction.guild_id)
        await interaction.response.send_modal(
            _RatesModal(self._flow, cfg, default_opening=state.cap)
        )


class _RatesModal(base.PanelModal, title="Budget rates"):
    """The five numeric budget tunables — exactly `MODAL_MAX_INPUTS`."""

    def __init__(self, flow: _SettingsFlow, cfg, *, default_opening: Decimal) -> None:
        super().__init__()
        self._flow = flow
        zero = Decimal("0")
        self._opening = _rate_input(
            "Opening budget $M", cfg.opening_budget if cfg else default_opening
        )
        self._per_point = _rate_input(
            "Earnings per point $M", cfg.earnings_per_point if cfg else zero
        )
        self._dnf = _rate_input("DNF penalty $M", cfg.dnf_penalty if cfg else zero)
        self._dns = _rate_input("DNS penalty $M", cfg.dns_penalty if cfg else zero)
        self._incident = _rate_input(
            "Per incident point $M", cfg.penalty_per_incident_pt if cfg else zero
        )
        for item in (
            self._opening,
            self._per_point,
            self._dnf,
            self._dns,
            self._incident,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            values = {
                "opening_budget": _parse_amount(self._opening.value),
                "earnings_per_point": _parse_amount(self._per_point.value),
                "dnf_penalty": _parse_amount(self._dnf.value),
                "dns_penalty": _parse_amount(self._dns.value),
                "penalty_per_incident_pt": _parse_amount(self._incident.value),
            }
        except ValueError as exc:
            await report_error(interaction, str(exc))
            return
        if any(v < 0 for v in values.values()):
            await report_error(
                interaction,
                "Penalties are stored as positive magnitudes — enter the "
                "size of the charge, not a negative number.",
            )
            return
        await interaction.response.defer(ephemeral=True)
        await _save_settings(interaction, self._flow, **values)


def _rate_input(label: str, value: Decimal) -> discord.ui.TextInput:
    return discord.ui.TextInput(label=label, default=f"{value}", max_length=16)


async def _save_settings(
    interaction: discord.Interaction, flow: _SettingsFlow, **changes
) -> None:
    """
    Re-read the row, apply the changed fields, write the whole row back.

    Re-reading is the point: the toggles and the rates modal each only
    know about their own half, and a save built from the values the screen
    was opened with would silently revert the other half (G1, and the
    same bug `config_modal._save` fixed).
    """
    cfg = await workflow.get_budget_config(
        guild_id=interaction.guild_id, tier=flow.tier_code
    )
    state = await load_money_state(interaction.guild_id)
    zero = Decimal("0")
    current = {
        "enforce_budget": bool(cfg and cfg.enforce_budget),
        "rollover_enabled": bool(cfg and cfg.rollover_enabled),
        "opening_budget": cfg.opening_budget if cfg else state.cap,
        "earnings_per_point": cfg.earnings_per_point if cfg else zero,
        "dnf_penalty": cfg.dnf_penalty if cfg else zero,
        "dns_penalty": cfg.dns_penalty if cfg else zero,
        "penalty_per_incident_pt": cfg.penalty_per_incident_pt if cfg else zero,
    }
    current.update(changes)
    try:
        await workflow.set_budget_config(
            guild_id=interaction.guild_id, tier=flow.tier_code, **current
        )
    except workflow.WorkflowError as exc:
        await report_error(interaction, str(exc))
        return
    await flow.render_settings(interaction)
    await interaction.followup.send(
        "✅ Budget settings saved. Existing ledger rows are untouched — this "
        "changes what future imports and signings do.",
        ephemeral=True,
    )


# ── cap adjustment (audit note only) ─────────────────────────────────


class _CapAdjustButton(discord.ui.Button):
    def __init__(self, *, row: int, enabled: bool) -> None:
        super().__init__(
            label="Cap adjustment (audit note)",
            style=discord.ButtonStyle.secondary,
            emoji="📝",
            row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, MoneyView)
        flow = _CapAdjustFlow(parent=view)
        await flow.render(interaction)


class _CapAdjustFlow(AdminOwnedView):
    def __init__(self, *, parent: MoneyView) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.page = 0

    async def render(self, interaction: discord.Interaction) -> None:
        state = await load_money_state(interaction.guild_id)
        window, page, pages = _paginate(state.rows, self.page)
        self.page = page
        self.clear_items()
        self.add_item(_CapTeamSelect(self, window))
        if pages > 1:
            self.add_item(_CapPageButton(self, -1, disabled=page == 0, row=1))
            self.add_item(
                _CapPageButton(self, 1, disabled=page >= pages - 1, row=1)
            )
        self.add_item(BackButton(self._cancel, label="Cancel", row=2))
        embed = discord.Embed(
            title="📝 Cap adjustment",
            description=(
                f"{_CAP_NOTE}\n\n"
                "Use this for relief or a penalty aimed at one team. To "
                "change the ceiling for **everyone**, edit the cap in "
                "**Setup → Cap & rules**; to move a team's own money "
                "rather than its ceiling, use **Manual budget "
                "adjustment**.\n\n"
                f"Teams {page + 1}/{pages}"
            ),
            color=COLOR_INFO,
        )
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)
        await self.bind_message(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)


class _CapPageButton(discord.ui.Button):
    def __init__(
        self, flow: _CapAdjustFlow, step: int, *, disabled: bool, row: int
    ) -> None:
        super().__init__(
            label="Prev teams" if step < 0 else "More teams",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self._flow = flow
        self._step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        self._flow.page += self._step
        await self._flow.render(interaction)


class _CapTeamSelect(discord.ui.Select):
    def __init__(self, flow: _CapAdjustFlow, rows: list[TeamMoneyRow]) -> None:
        super().__init__(
            placeholder="Record a cap note for which team?",
            options=[
                discord.SelectOption(
                    label=f"{r.name} · payroll {_m(r.payroll)}"[:100],
                    value=r.key,
                )
                for r in rows
            ],
        )
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            _CapAdjustModal(self._flow, self.values[0])
        )


class _CapAdjustModal(base.PanelModal, title="Cap adjustment (audit note)"):
    def __init__(self, flow: _CapAdjustFlow, team_key: str) -> None:
        super().__init__()
        self._flow = flow
        self._team_key = team_key
        self._delta = discord.ui.TextInput(
            label="Delta in $M (sign it: -2.5 or 1.0)",
            placeholder="e.g. -2.5 or +1.0",
            max_length=16,
        )
        self._note = discord.ui.TextInput(
            label="Reason (required — audit trail)",
            placeholder="e.g. luxury-tax refund, stewards' sanction",
            max_length=200,
        )
        self.add_item(self._delta)
        self.add_item(self._note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            delta = _parse_amount(self._delta.value)
        except ValueError as exc:
            await report_error(interaction, str(exc))
            return
        if delta == 0:
            await report_error(interaction, "A zero adjustment records nothing.")
            return
        state = await load_money_state(interaction.guild_id)
        row = state.row(self._team_key)
        if row is None:
            await report_error(interaction, f"No team `{self._team_key}` any more.")
            return

        # Adjustments accumulate across a season (G18), so the owner has
        # to see what is already applied before adding to it — otherwise
        # a second "+$5M relief" silently becomes +$10M.
        existing = _ZERO
        if state.season_id is not None:
            async with db.connect() as conn:
                existing = await queries.fetch_cap_adjustment_total(
                    conn, state.season_id, row.team_id
                )

        embed = discord.Embed(
            title="📝 Confirm cap adjustment",
            description=truncate_field(
                "\n".join(
                    [
                        f"**{row.name}** (`{row.key}`) — {_signed(delta)}",
                        f"Note: {self._note.value}",
                        "",
                        f"League cap {_m(row.cap)}",
                        f"Adjustments already applied {_signed(existing)}",
                        f"**Effective cap after this change "
                        f"{_m(row.cap + existing + delta)}**",
                        "",
                        _CAP_NOTE,
                        "",
                        "The ledger is append-only, so this note cannot be "
                        "edited or deleted afterwards.",
                    ]
                )
            ),
            color=COLOR_WARN,
        )

        async def _do(confirm: discord.Interaction) -> None:
            await confirm.response.defer(ephemeral=True)
            try:
                recorded = await workflow.adjust_team_cap(
                    guild_id=confirm.guild_id,
                    actor_id=confirm.user.id,
                    team_key=row.key,
                    delta=delta,
                    note=str(self._note.value),
                )
            except workflow.WorkflowError as exc:
                await report_error(confirm, str(exc))
                return
            await self._flow.parent.reload(
                confirm,
                note=(
                    f"✅ Cap adjusted for **{row.name}**: "
                    f"{_signed(recorded)}. Effective cap is now "
                    f"{_m(row.cap + existing + recorded)} and every "
                    "signing check uses it from here. The ledger row is "
                    "append-only, so this cannot be edited or deleted — "
                    "correct it with an opposite adjustment."
                ),
            )

        view = _ConfirmView(
            opener_id=self._flow.opener_id,
            confirm_label="Record the note",
            on_confirm=_do,
            on_cancel=self._flow._cancel,
        )
        await _show_confirm(interaction, view=view, embed=embed)


# ── entry point ──────────────────────────────────────────────────────


async def open_money(
    interaction: discord.Interaction,
    *,
    on_back: BackCallback,
    opener_id: int | None = None,
) -> None:
    """
    Entry point for the `/league` home screen's Teams & money button.

    `opener_id` defaults to the clicking user, matching `open_boards`'
    shape while letting a parent screen pass its own opener through.
    """
    state = await load_money_state(interaction.guild_id)
    view = MoneyView(
        state=state,
        opener_id=opener_id if opener_id is not None else interaction.user.id,
        on_back=on_back,
    )
    embed = build_money_embed(state)
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)
    await view.bind_message(interaction)
