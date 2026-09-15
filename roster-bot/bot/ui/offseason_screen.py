"""
The offseason as a gated wizard (consolidation Screen 6).

Rolling a league over is a nine-step sequence whose order is load-bearing
and, until now, written down nowhere the commissioner would see it (G21).
Doing it out of order leaves the new season with no contracts and no
budgets while free agency is open, so offers land against a season whose
deals have not arrived yet.

The order, and why each step is where it is:

  1. Close free agency — nothing new may be signed mid-rollover.
  2. Create the new season, seeded with a preset (G2: a season with no
     rules can neither price nor sign anyone).
  3. Activate it — every read switches immediately, and no contracts
     exist in it until step 4.
  4. Carry contracts over, **oldest past season first**: a season is only
     clickable once every older one is resolved.
  5. Roll budgets over.
  6. Award any outstanding prize money.
  7. Sync drivers from the tier roles.
  8. Baseline valuation per tier, then publish, so every driver has a
     market value before the first offer (G11).
  9. Reopen free agency.

Each step reads its own state live, previews before it writes, and is
disabled until the step above it is complete. Nothing here is typed: the
past-season pickers are selects built from the database.

Completion is derived from the database, not remembered, with two
exceptions that a database cannot answer: a step the commissioner
deliberately skipped, and a step they ran in this session whose live
signal is a toggle another step flips back (steps 1 and 9 are the same
flag in opposite directions). Both are recorded on the view.

Reads with no `workflow` wrapper yet (unresolved contracts per past
season, rollover progress, prize totals) are composed here from
`bot.queries` inside one connection; they are read-only and re-run before
every render and every confirm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import discord

from bot import db, queries, workflow
from bot.contracts import carryover
from bot.market import budget as budget_engine
from bot.market import budget_ops, driver_ops
from bot.ui import base, money_screen
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

STEP_CLOSE_FA = "close_fa"
STEP_CREATE = "create_season"
STEP_ACTIVATE = "activate"
STEP_CARRY = "carry_over"
STEP_ROLLOVER = "rollover"
STEP_PRIZE = "prize"
STEP_SYNC = "sync_drivers"
STEP_BASELINE = "baseline"
STEP_REOPEN_FA = "reopen_fa"

STEP_ORDER: tuple[str, ...] = (
    STEP_CLOSE_FA,
    STEP_CREATE,
    STEP_ACTIVATE,
    STEP_CARRY,
    STEP_ROLLOVER,
    STEP_PRIZE,
    STEP_SYNC,
    STEP_BASELINE,
    STEP_REOPEN_FA,
)

STEP_TITLES: dict[str, str] = {
    STEP_CLOSE_FA: "Close free agency",
    STEP_CREATE: "Create the new season",
    STEP_ACTIVATE: "Activate the new season",
    STEP_CARRY: "Carry contracts over",
    STEP_ROLLOVER: "Roll budgets over",
    STEP_PRIZE: "Award outstanding prize money",
    STEP_SYNC: "Sync drivers from tier roles",
    STEP_BASELINE: "Baseline valuation, then publish",
    STEP_REOPEN_FA: "Reopen free agency",
}

STEP_BUTTON_LABELS: dict[str, str] = {
    STEP_CLOSE_FA: "1 Close FA",
    STEP_CREATE: "2 Create season",
    STEP_ACTIVATE: "3 Activate",
    STEP_CARRY: "4 Carry over",
    STEP_ROLLOVER: "5 Rollover",
    STEP_PRIZE: "6 Prize money",
    STEP_SYNC: "7 Sync drivers",
    STEP_BASELINE: "8 Baseline values",
    STEP_REOPEN_FA: "9 Reopen FA",
}

_PRESET_F1 = "f1"
_BASELINE_PREFIX = "Baseline"



# ── state ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PastSeason:
    """A season other than the active one, with what it still owes."""

    season_id: int
    name: str
    created_at: datetime
    unresolved_contracts: int
    budget_rows: int


@dataclass(frozen=True)
class TierProgress:
    code: str
    label: str
    driver_count: int
    has_role: bool
    has_published_valuation: bool


@dataclass(frozen=True)
class OffseasonState:
    active_season_name: str | None
    active_season_id: int | None
    newest_season_name: str | None
    newest_is_active: bool
    season_count: int
    free_agency_open: bool
    has_config: bool
    past_seasons: list[PastSeason]
    teams_total: int
    teams_rolled_over: int
    rollover_enabled: bool
    prize_money_total: Decimal
    tiers: list[TierProgress]

    @property
    def pending_carry(self) -> list[PastSeason]:
        """Past seasons still holding active contracts, oldest first."""
        return [s for s in self.past_seasons if s.unresolved_contracts]

    @property
    def rollover_sources(self) -> list[PastSeason]:
        """Past seasons that have any budget history to carry forward."""
        return [s for s in self.past_seasons if s.budget_rows]


async def load_offseason_state(guild_id: int) -> OffseasonState:
    """Read every signal the checklist shows, in one transaction."""
    async with db.connect() as conn:
        seasons = await queries.fetch_all_seasons(conn, guild_id)
        seasons_oldest_first = sorted(seasons, key=lambda s: (s.created_at, s.id))
        active = await queries.fetch_active_season(conn, guild_id)
        newest = seasons_oldest_first[-1] if seasons_oldest_first else None

        if active is None:
            return OffseasonState(
                active_season_name=None,
                active_season_id=None,
                newest_season_name=newest.name if newest else None,
                newest_is_active=False,
                season_count=len(seasons),
                free_agency_open=False,
                has_config=False,
                past_seasons=[],
                teams_total=0,
                teams_rolled_over=0,
                rollover_enabled=False,
                prize_money_total=Decimal("0"),
                tiers=[],
            )

        cfg = await queries.fetch_league_config_row(conn, active.id, None)
        teams = await queries.fetch_all_teams(conn, guild_id)

        past: list[PastSeason] = []
        for season in seasons_oldest_first:
            if season.id == active.id:
                continue
            contracts = await queries.fetch_active_contracts_for_season(
                conn, season.id
            )
            balances = await queries.fetch_budget_balances_for_season(conn, season.id)
            past.append(
                PastSeason(
                    season_id=season.id,
                    name=season.name,
                    created_at=season.created_at,
                    unresolved_contracts=len(contracts),
                    budget_rows=len(balances),
                )
            )

        rolled = 0
        prize_total = Decimal("0")
        for team in teams:
            if await queries.budget_entry_exists(
                conn, team.id, active.id, budget_ops.KIND_ROLLOVER
            ):
                rolled += 1
            totals = await queries.fetch_budget_totals_by_kind(conn, team.id, active.id)
            prize_total += totals.get(budget_ops.KIND_PRIZE, Decimal("0"))

        budget_cfg = await queries.fetch_budget_config(conn, active.id, None)
        tiers: list[TierProgress] = []
        for tier in await queries.fetch_all_tiers(conn, active.id):
            drivers = await queries.fetch_drivers_in_tier(conn, tier.id)
            tiers.append(
                TierProgress(
                    code=tier.code,
                    label=tier.label,
                    driver_count=len(drivers),
                    has_role=tier.tier_role_id is not None,
                    has_published_valuation=(
                        await queries.tier_has_published_valuation(conn, tier.id)
                    ),
                )
            )

    return OffseasonState(
        active_season_name=active.name,
        active_season_id=active.id,
        newest_season_name=newest.name if newest else None,
        newest_is_active=bool(newest and newest.id == active.id),
        season_count=len(seasons),
        free_agency_open=bool(cfg and cfg.free_agency_open),
        has_config=cfg is not None,
        past_seasons=past,
        teams_total=len(teams),
        teams_rolled_over=rolled,
        rollover_enabled=bool(budget_cfg and budget_cfg.rollover_enabled),
        prize_money_total=prize_total,
        tiers=tiers,
    )


# ── step derivation ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Step:
    number: int
    key: str
    title: str
    state_line: str
    done: bool
    skipped_reason: str | None
    enabled: bool

    @property
    def complete(self) -> bool:
        return self.done or self.skipped_reason is not None

    @property
    def mark(self) -> str:
        if self.skipped_reason is not None:
            return "⏭"
        return "✅" if self.done else "⬜"


@dataclass
class WizardProgress:
    """
    What the database cannot tell us.

    `ran` holds steps completed in this session, which matters for the
    free-agency pair: step 9 reopens the flag step 1 closed, so after
    step 9 the live signal for step 1 reads as undone.
    """

    ran: set[str] = field(default_factory=set)
    skipped: dict[str, str] = field(default_factory=dict)


def _state_lines(state: OffseasonState) -> dict[str, str]:
    fa = "open" if state.free_agency_open else "closed"
    pending = state.pending_carry
    if pending:
        oldest = pending[0]
        carry_line = (
            f"{len(pending)} past season(s) still hold active contracts; "
            f"oldest is **{oldest.name}** with {oldest.unresolved_contracts}"
        )
    else:
        carry_line = "no past season holds an active contract"
    if state.teams_total == 0:
        rollover_line = "no teams yet, so there is nothing to roll over"
    else:
        rollover_line = (
            f"{state.teams_rolled_over}/{state.teams_total} team(s) have a "
            f"rollover row · rollover is "
            f"{'enabled' if state.rollover_enabled else 'DISABLED'} for this season"
        )
    tier_bits = [
        f"`{t.code}` {t.driver_count} driver(s)"
        + ("" if t.has_role else ", no role")
        + (", valued" if t.has_published_valuation else ", **no published value**")
        for t in state.tiers
    ]
    tier_line = " · ".join(tier_bits) if tier_bits else "no tiers in this season"
    return {
        STEP_CLOSE_FA: f"free agency is **{fa}**",
        STEP_CREATE: (
            f"{state.season_count} season(s) exist · newest is "
            f"**{state.newest_season_name or 'none'}**"
        ),
        STEP_ACTIVATE: (
            f"active season is **{state.active_season_name or 'none'}**"
            + ("" if state.newest_is_active else " — the newest season is NOT active")
        ),
        STEP_CARRY: carry_line,
        STEP_ROLLOVER: rollover_line,
        STEP_PRIZE: (
            f"prize money written this season: "
            f"{money_screen.format_money(state.prize_money_total)}"
        ),
        STEP_SYNC: tier_line,
        STEP_BASELINE: tier_line,
        STEP_REOPEN_FA: f"free agency is **{fa}**",
    }


def _derived_done(state: OffseasonState) -> dict[str, bool]:
    tiers = state.tiers
    return {
        STEP_CLOSE_FA: not state.free_agency_open,
        STEP_CREATE: state.season_count > 1,
        STEP_ACTIVATE: state.newest_is_active and state.season_count > 1,
        STEP_CARRY: not state.pending_carry,
        STEP_ROLLOVER: (
            state.teams_total == 0
            or state.teams_rolled_over >= state.teams_total
            or not state.rollover_sources
        ),
        STEP_PRIZE: state.prize_money_total != 0,
        STEP_SYNC: bool(tiers) and all(t.driver_count > 0 for t in tiers),
        STEP_BASELINE: bool(tiers) and all(t.has_published_valuation for t in tiers),
        STEP_REOPEN_FA: state.free_agency_open,
    }


def build_steps(state: OffseasonState, progress: WizardProgress) -> list[Step]:
    """
    The nine steps with live state, completion and gating.

    Gating is strictly sequential: a step is enabled only once every step
    above it is complete, where complete means done in the database,
    already run in this session, or explicitly skipped with a reason.
    """
    derived = _derived_done(state)
    lines = _state_lines(state)
    steps: list[Step] = []
    prior_complete = True
    for number, key in enumerate(STEP_ORDER, start=1):
        done = derived[key] or key in progress.ran
        skipped = progress.skipped.get(key)
        steps.append(
            Step(
                number=number,
                key=key,
                title=STEP_TITLES[key],
                state_line=lines[key],
                done=done,
                skipped_reason=skipped,
                enabled=prior_complete and state.active_season_id is not None,
            )
        )
        prior_complete = prior_complete and (done or skipped is not None)
    return steps


def build_offseason_embed(
    state: OffseasonState, steps: list[Step]
) -> discord.Embed:
    """Pure render of the gated checklist."""
    old = state.pending_carry[0].name if state.pending_carry else (
        state.past_seasons[-1].name if state.past_seasons else "—"
    )
    title = f"🔄 Offseason — {old} → {state.active_season_name or 'no active season'}"
    if state.active_season_id is None:
        return discord.Embed(
            title="🔄 Offseason",
            description=(
                "No active season. The wizard rolls one season into another, "
                "so start in **Setup → Season**."
            ),
            color=COLOR_INFO,
        )

    lines: list[str] = []
    for step in steps:
        lines.append(f"{step.mark} **{step.number}. {step.title}**")
        lines.append(f"    {step.state_line}")
        if step.skipped_reason:
            lines.append(f"    ⏭ skipped: {step.skipped_reason}")
        elif not step.enabled and not step.done:
            lines.append("    🔒 locked until the step above is done or skipped")
    lines.append("")
    lines.append(
        "Order matters: activating a season switches every read to it, and "
        "no contracts or budgets exist there until steps 4 and 5 have run."
    )
    if not state.has_config:
        lines.append(
            "⚠ This season has no league config row — seed it in **Setup → "
            "Cap & rules** before signing anyone."
        )
    remaining = [s for s in steps if not s.complete]
    return discord.Embed(
        title=title,
        description=truncate_field("\n".join(lines), base.EMBED_FIELD_LIMIT * 4),
        color=COLOR_WARN if remaining else COLOR_OK,
    ).set_footer(
        text=(
            "Equivalent commands: /market-admin config free-agency · season "
            "create|activate|carry-over · budget rollover|award · driver "
            "sync-all · valuation run|publish"
        )
    )


# ── previews (read-only, composed from existing engine functions) ─────


@dataclass(frozen=True)
class CarryPreview:
    from_season_name: str
    to_season_name: str
    to_carry: int
    to_expire: int
    carried_value: Decimal
    expiring_value: Decimal
    over_cap: list[tuple[str, Decimal, Decimal]]
    salary_cap: Decimal


async def preview_carry_over(guild_id: int, from_season_name: str) -> CarryPreview:
    """
    What `workflow.carry_over_contracts` would do, without writing.

    Reproduces the engine's own decision with the engine's own pure
    predicate (`carryover.continues_next_season`) rather than a second
    copy of the rule. Projected payroll is today's active payroll minus
    what expires, because a carried row replaces its predecessor at the
    same value while an expiring row simply stops counting.
    """
    async with db.connect() as conn:
        to_season = await queries.fetch_active_season(conn, guild_id)
        if to_season is None:
            raise workflow.WorkflowError("No active season to carry into.")
        from_season = await queries.fetch_season_by_name(
            conn, guild_id, from_season_name
        )
        if from_season is None:
            raise workflow.WorkflowError(f"No season named `{from_season_name}`.")
        cfg = await queries.fetch_league_config_row(conn, to_season.id, None)
        cap = cfg.salary_cap if cfg else Decimal("0")

        contracts = await queries.fetch_active_contracts_for_season(
            conn, from_season.id
        )
        carry = [c for c in contracts if carryover.continues_next_season(c)]
        expire = [c for c in contracts if not carryover.continues_next_season(c)]
        expiring_by_team: dict[int, Decimal] = {}
        for contract in expire:
            expiring_by_team[contract.team_id] = (
                expiring_by_team.get(contract.team_id, Decimal("0"))
                + contract.contract_value
            )
        touched = {c.team_id for c in carry} | set(expiring_by_team)
        over: list[tuple[str, Decimal, Decimal]] = []
        for team_id in sorted(touched):
            team = await queries.fetch_team_by_id(conn, team_id)
            if team is None:
                continue
            payroll = await queries.fetch_team_payroll(conn, team_id)
            projected = payroll - expiring_by_team.get(team_id, Decimal("0"))
            if cap and projected > cap:
                over.append((team.name, projected, cap))

    return CarryPreview(
        from_season_name=from_season.name,
        to_season_name=to_season.name,
        to_carry=len(carry),
        to_expire=len(expire),
        carried_value=sum((c.contract_value for c in carry), Decimal("0")),
        expiring_value=sum((c.contract_value for c in expire), Decimal("0")),
        over_cap=over,
        salary_cap=cap,
    )


def build_carry_preview_embed(preview: CarryPreview) -> discord.Embed:
    lines = [
        f"**{preview.from_season_name} → {preview.to_season_name}**",
        "",
        f"Contracts to carry: **{preview.to_carry}** "
        f"({money_screen.format_money(preview.carried_value)} of payroll)",
        f"Contracts to expire: **{preview.to_expire}** "
        f"({money_screen.format_money(preview.expiring_value)} released)",
    ]
    if preview.over_cap:
        lines.append("")
        lines.append(f"⚠ Over the {money_screen.format_money(preview.salary_cap)} cap afterwards:")
        for name, projected, cap in preview.over_cap:
            lines.append(
                f"• {name} — {money_screen.format_money(projected)} "
                f"(over by {money_screen.format_money(projected - cap)})"
            )
        lines.append(
            "Carried payroll is an existing obligation: it is reported "
            "against the cap, never blocked."
        )
    lines += [
        "",
        "**Confirming writes contracts.** Every expiring driver becomes a "
        "free agent and their team role is dropped; carried rows are copied "
        "verbatim with the signing bonus set to zero, because it was already "
        "paid on the original deal. Contract states are terminal, so this "
        "cannot be undone from the panel.",
        "A row the engine cannot carry stays active in the old season and is "
        "reported again next run — re-running is safe.",
    ]
    return discord.Embed(
        title="🔄 Confirm carry-over",
        description=truncate_field("\n".join(lines)),
        color=COLOR_WARN,
    )


@dataclass(frozen=True)
class RolloverPreviewLine:
    team_name: str
    balance: Decimal
    season_payroll: Decimal
    carried: Decimal
    already_rolled: bool


@dataclass(frozen=True)
class RolloverPreview:
    from_season_name: str
    to_season_name: str
    enabled: bool
    lines: list[RolloverPreviewLine]


async def preview_rollover(guild_id: int, from_season_name: str) -> RolloverPreview:
    """
    Per-team carried balances, using the engine's own arithmetic.

    `budget_engine.rollover_amount` is the same pure function
    `budget_ops.rollover` calls, and the payroll is the source season's
    pinned figure, so the preview cannot disagree with the write. Escrow
    is read from the SOURCE season for the same reason the engine does:
    that is the season whose balance already had salary debited.
    """
    async with db.connect() as conn:
        to_season = await queries.fetch_active_season(conn, guild_id)
        if to_season is None:
            raise workflow.WorkflowError("No active season to roll into.")
        from_season = await queries.fetch_season_by_name(
            conn, guild_id, from_season_name
        )
        if from_season is None:
            raise workflow.WorkflowError(f"No season named `{from_season_name}`.")
        to_cfg = await queries.fetch_budget_config(conn, to_season.id, None)
        from_cfg = await queries.fetch_budget_config(conn, from_season.id, None)
        from_escrow = from_cfg.escrow_enabled if from_cfg is not None else False
        lines: list[RolloverPreviewLine] = []
        for team in await queries.fetch_all_teams(conn, guild_id):
            balance = await queries.fetch_budget_balance(conn, team.id, from_season.id)
            payroll = await queries.fetch_team_season_payroll(
                conn, team.id, from_season.id
            )
            lines.append(
                RolloverPreviewLine(
                    team_name=team.name,
                    balance=balance,
                    season_payroll=payroll,
                    carried=budget_engine.rollover_amount(
                        balance, payroll, escrow_enabled=from_escrow
                    ),
                    already_rolled=await queries.budget_entry_exists(
                        conn, team.id, to_season.id, budget_ops.KIND_ROLLOVER
                    ),
                )
            )
    return RolloverPreview(
        from_season_name=from_season.name,
        to_season_name=to_season.name,
        enabled=bool(to_cfg and to_cfg.rollover_enabled),
        lines=lines,
    )


def build_rollover_preview_embed(preview: RolloverPreview) -> discord.Embed:
    lines = [f"**{preview.from_season_name} → {preview.to_season_name}**", ""]
    if not preview.enabled:
        lines.append(
            "⛔ Rollover is **disabled** for this season, so the write will "
            "be refused. Turn it on in **Teams & money → Budget settings** "
            "first."
        )
        lines.append("")
    for line in preview.lines:
        suffix = " · already rolled over, will be skipped" if line.already_rolled else ""
        lines.append(
            f"• **{line.team_name}** — balance {money_screen.format_money(line.balance)} "
            f"− payroll {money_screen.format_money(line.season_payroll)} = "
            f"**{money_screen.format_money(line.carried)}**{suffix}"
        )
    if not preview.lines:
        lines.append("No teams, so nothing would be written.")
    lines += [
        "",
        "A negative figure carries as debt: that team starts the new season "
        "paying it off. Each team is written once — the ledger is "
        "append-only and a second run skips anyone already rolled over.",
    ]
    return discord.Embed(
        title="🔄 Confirm budget rollover",
        description=truncate_field("\n".join(lines)),
        color=COLOR_WARN,
    )


# ── confirm plumbing ─────────────────────────────────────────────────


class _ConfirmView(AdminOwnedView):
    """Preview plus an explicit Confirm, restating the consequence."""

    def __init__(
        self,
        *,
        opener_id: int,
        confirm_label: str,
        on_confirm,
        on_cancel: BackCallback,
        disabled: bool = False,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self._on_confirm = on_confirm
        self.expiry_hint = "Nothing was written — this step was never confirmed."
        self.add_item(_ConfirmButton(confirm_label, disabled=disabled))
        self.add_item(BackButton(on_cancel, label="Cancel", row=1))

    async def run(self, interaction: discord.Interaction) -> None:
        await self._on_confirm(interaction)


class _ConfirmButton(discord.ui.Button):
    def __init__(self, label: str, *, disabled: bool) -> None:
        super().__init__(
            label=label,
            style=discord.ButtonStyle.danger,
            emoji="✅",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, _ConfirmView)
        await view.run(interaction)


async def _render(
    interaction: discord.Interaction, *, embed: discord.Embed, view: AdminOwnedView
) -> None:
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)
    await view.bind_message(interaction)


# ── main view ────────────────────────────────────────────────────────


class OffseasonView(AdminOwnedView):
    """The checklist: nine gated step buttons, a skip select and Back."""

    def __init__(
        self,
        *,
        state: OffseasonState,
        opener_id: int,
        on_back: BackCallback,
        progress: WizardProgress | None = None,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.state = state
        self.on_back = on_back
        self.progress = progress or WizardProgress()
        self.steps = build_steps(state, self.progress)
        for index, step in enumerate(self.steps):
            self.add_item(_StepButton(step, row=index // 5))
        incomplete = [s for s in self.steps if not s.complete]
        if incomplete:
            self.add_item(_SkipSelect(incomplete))
        self.add_item(BackButton(on_back, row=3))

    def step(self, key: str) -> Step:
        return next(s for s in self.steps if s.key == key)

    async def reload(
        self, interaction: discord.Interaction, *, note: str | None = None
    ) -> None:
        """Re-read the whole state, then rebuild the checklist."""
        state = await load_offseason_state(interaction.guild_id)
        view = OffseasonView(
            state=state,
            opener_id=self.opener_id,
            on_back=self.on_back,
            progress=self.progress,
        )
        await _render(
            interaction, embed=build_offseason_embed(state, view.steps), view=view
        )
        if note:
            await interaction.followup.send(note, ephemeral=True)

    async def mark_ran(
        self, interaction: discord.Interaction, key: str, *, note: str
    ) -> None:
        self.progress.ran.add(key)
        await self.reload(interaction, note=note)


class _StepButton(discord.ui.Button):
    def __init__(self, step: Step, *, row: int) -> None:
        super().__init__(
            label=f"{step.mark} {STEP_BUTTON_LABELS[step.key]}"[:80],
            style=(
                discord.ButtonStyle.primary
                if step.enabled and not step.complete
                else discord.ButtonStyle.secondary
            ),
            row=row,
            disabled=not step.enabled,
        )
        self.step_key = step.key

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, OffseasonView)
        await _open_step(interaction, view, self.step_key)


class _SkipSelect(discord.ui.Select):
    """Record why a step is being skipped, so the gap is explained later."""

    def __init__(self, incomplete: list[Step]) -> None:
        super().__init__(
            placeholder="Skip a step (records why)…",
            options=[
                discord.SelectOption(
                    label=f"{s.number}. {s.title}"[:100],
                    value=s.key,
                    description=s.state_line[:100],
                )
                for s in incomplete[:SELECT_MAX_OPTIONS]
            ],
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, OffseasonView)
        await interaction.response.send_modal(_SkipModal(view, self.values[0]))


class _SkipModal(base.PanelModal, title="Skip this step"):
    def __init__(self, view: OffseasonView, key: str) -> None:
        super().__init__()
        self._owner = view
        self._key = key
        self._reason = discord.ui.TextInput(
            label="Why is this step being skipped?",
            placeholder="e.g. no budgets in this league",
            max_length=200,
        )
        self.add_item(self._reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = str(self._reason.value).strip()
        if not reason:
            await report_error(interaction, "A skipped step needs a reason.")
            return
        await interaction.response.defer(ephemeral=True)
        self._owner.progress.skipped[self._key] = reason
        await self._owner.reload(
            interaction,
            note=(
                f"⏭ Skipped **{STEP_TITLES[self._key]}** — {reason}. The "
                "reason is kept for this panel session only; it is not "
                "written to the database."
            ),
        )


async def _open_step(
    interaction: discord.Interaction, view: OffseasonView, key: str
) -> None:
    handlers = {
        STEP_CLOSE_FA: _step_free_agency,
        STEP_REOPEN_FA: _step_free_agency,
        STEP_CREATE: _step_create_season,
        STEP_ACTIVATE: _step_activate,
        STEP_CARRY: _step_carry_over,
        STEP_ROLLOVER: _step_rollover,
        STEP_PRIZE: _step_prize,
        STEP_SYNC: _step_sync_drivers,
        STEP_BASELINE: _step_baseline,
    }
    await handlers[key](interaction, view, key)


# ── step 1 / 9: free agency ──────────────────────────────────────────


async def _step_free_agency(
    interaction: discord.Interaction, view: OffseasonView, key: str
) -> None:
    closing = key == STEP_CLOSE_FA
    state = await load_offseason_state(interaction.guild_id)
    if closing:
        consequence = (
            "Closing free agency refuses every new free-agent offer with "
            "`free_agency_closed` until step 9 reopens it. Offers already "
            "pending are untouched and still need approving — check the "
            "Approvals screen before carrying contracts over."
        )
    else:
        consequence = (
            "Reopening free agency lets teams submit offers again. Do this "
            "last: an offer made before step 8 has no market value to price "
            "against, and approving it loses the driver's P/L baseline "
            "permanently."
        )
    embed = discord.Embed(
        title=f"🔄 {'Close' if closing else 'Reopen'} free agency",
        description=(
            f"Free agency is currently **{'open' if state.free_agency_open else 'closed'}"
            f"** in **{state.active_season_name}**.\n\n{consequence}"
        ),
        color=COLOR_WARN,
    )

    async def _do(confirm: discord.Interaction) -> None:
        await confirm.response.defer(ephemeral=True)
        try:
            await workflow.set_free_agency(
                guild_id=confirm.guild_id, is_open=not closing
            )
        except workflow.WorkflowError as exc:
            await report_error(confirm, str(exc))
            return
        await view.mark_ran(
            confirm,
            key,
            note=(
                f"✅ Free agency is now **{'closed' if closing else 'open'}**. "
                "Nothing was announced publicly."
            ),
        )

    confirm_view = _ConfirmView(
        opener_id=view.opener_id,
        confirm_label="Close it" if closing else "Reopen it",
        on_confirm=_do,
        on_cancel=view.reload,
    )
    await _render(interaction, embed=embed, view=confirm_view)


# ── step 2: create the new season ────────────────────────────────────


async def _step_create_season(
    interaction: discord.Interaction, view: OffseasonView, key: str
) -> None:
    await interaction.response.send_modal(_CreateSeasonModal(view))


class _CreateSeasonModal(base.PanelModal, title="Create the new season"):
    def __init__(self, view: OffseasonView) -> None:
        super().__init__()
        self._owner = view
        self._name = discord.ui.TextInput(
            label="New season name",
            placeholder="e.g. Season 8",
            max_length=80,
        )
        self.add_item(self._name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = str(self._name.value).strip()
        if not name:
            await report_error(interaction, "A season needs a name.")
            return
        embed = discord.Embed(
            title="🔄 Confirm: create the new season",
            description=(
                f"**{name}** will be created and seeded with the **F1 "
                "preset** — tiers, statuses, points table, valuation factors, "
                "budget config and a league config row with the $145.00M "
                "default cap.\n\n"
                "The preset is not optional here: a season with no rules can "
                "neither price a driver nor accept an offer.\n\n"
                "Creating does **not** activate it — every read stays on the "
                "current season until step 3."
            ),
            color=COLOR_WARN,
        )

        async def _do(confirm: discord.Interaction) -> None:
            await confirm.response.defer(ephemeral=True)
            try:
                created = await workflow.create_season(
                    guild_id=confirm.guild_id, name=name, preset=_PRESET_F1
                )
            except workflow.WorkflowError as exc:
                await report_error(confirm, str(exc))
                return
            await self._owner.mark_ran(
                confirm,
                STEP_CREATE,
                note=(
                    f"✅ Created **{created.name}** (preset seeded: "
                    f"{created.preset_seeded}). It is not active yet — "
                    "step 3 does that."
                ),
            )

        confirm_view = _ConfirmView(
            opener_id=self._owner.opener_id,
            confirm_label="Create it",
            on_confirm=_do,
            on_cancel=self._owner.reload,
        )
        await _render(interaction, embed=embed, view=confirm_view)


# ── step 3: activate ─────────────────────────────────────────────────


async def _step_activate(
    interaction: discord.Interaction, view: OffseasonView, key: str
) -> None:
    seasons = await workflow.list_seasons(interaction.guild_id)
    ordered = sorted(seasons, key=lambda s: (s.created_at, s.id), reverse=True)
    if not ordered:
        await report_error(interaction, "No seasons exist yet.")
        return
    picker = _ActivateView(view, ordered)
    embed = discord.Embed(
        title="🔄 Activate a season — pick it from the list",
        description=(
            "Newest first. Season names are never typed here, so the "
            "free-text mismatch that silently activates nothing cannot "
            "happen."
        ),
        color=COLOR_INFO,
    )
    await _render(interaction, embed=embed, view=picker)


class _ActivateView(AdminOwnedView):
    def __init__(self, parent: OffseasonView, seasons: list) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.seasons = seasons
        self.page = 0
        self._build()

    def _build(self) -> None:
        self.clear_items()
        window, self.page, pages = _paginate(self.seasons, self.page)
        self.add_item(_ActivateSelect(self, window))
        if pages > 1:
            self.add_item(
                _SeasonPageButton(self, -1, disabled=self.page == 0, row=1)
            )
            self.add_item(
                _SeasonPageButton(self, 1, disabled=self.page >= pages - 1, row=1)
            )
        self.add_item(BackButton(self.parent.reload, label="Cancel", row=2))

    async def repage(self, interaction: discord.Interaction, step: int) -> None:
        self.page += step
        self._build()
        await _render(
            interaction,
            embed=discord.Embed(
                title="🔄 Activate a season — pick it from the list",
                description=f"Page {self.page + 1}",
                color=COLOR_INFO,
            ),
            view=self,
        )


def _paginate(items: list, page: int) -> tuple[list, int, int]:
    pages = max(1, -(-len(items) // SELECT_MAX_OPTIONS))
    page = max(0, min(page, pages - 1))
    start = page * SELECT_MAX_OPTIONS
    return items[start : start + SELECT_MAX_OPTIONS], page, pages


class _SeasonPageButton(discord.ui.Button):
    def __init__(
        self, owner: _ActivateView, step: int, *, disabled: bool, row: int
    ) -> None:
        super().__init__(
            label="Newer" if step < 0 else "Older",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self._owner = owner
        self._step = step

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._owner.repage(interaction, self._step)


class _ActivateSelect(discord.ui.Select):
    def __init__(self, owner: _ActivateView, window: list) -> None:
        super().__init__(
            placeholder="Activate which season?",
            options=[
                discord.SelectOption(
                    label=s.name[:100],
                    value=s.name[:100],
                    description=("active now" if s.is_active else None),
                )
                for s in window
            ],
        )
        self._owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        name = self.values[0]
        parent = self._owner.parent
        embed = discord.Embed(
            title=f"🔄 Confirm: activate {name}",
            description=(
                f"Activating **{name}** switches **every read** — payroll, "
                "cap, market, boards, approvals — to it immediately.\n\n"
                "Until step 4 runs, **no contracts exist in it**: multi-season "
                "deals stay in the old season and every team looks empty. "
                "Unspent budget is missing until step 5.\n\n"
                "Only one season is active at a time, so this deactivates the "
                "current one."
            ),
            color=COLOR_WARN,
        )

        async def _do(confirm: discord.Interaction) -> None:
            await confirm.response.defer(ephemeral=True)
            try:
                await workflow.activate_season(guild_id=confirm.guild_id, name=name)
            except workflow.WorkflowError as exc:
                await report_error(confirm, str(exc))
                return
            await parent.mark_ran(
                confirm,
                STEP_ACTIVATE,
                note=(
                    f"✅ **{name}** is active. Next: step 4 carries contracts "
                    "over (oldest past season first), then step 5 rolls "
                    "budgets over."
                ),
            )

        confirm_view = _ConfirmView(
            opener_id=parent.opener_id,
            confirm_label="Activate it",
            on_confirm=_do,
            on_cancel=parent.reload,
        )
        await _render(interaction, embed=embed, view=confirm_view)


# ── step 4: carry contracts over ─────────────────────────────────────


async def _step_carry_over(
    interaction: discord.Interaction, view: OffseasonView, key: str
) -> None:
    state = await load_offseason_state(interaction.guild_id)
    pending = state.pending_carry
    if not pending:
        await view.reload(
            interaction,
            note=(
                "Nothing to carry: no past season holds an active contract."
            ),
        )
        return
    picker = _CarryView(view, pending)
    await _render(
        interaction, embed=build_carry_queue_embed(pending), view=picker
    )


def build_carry_queue_embed(pending: list[PastSeason]) -> discord.Embed:
    """
    The queue of past seasons, oldest first and only the oldest clickable.

    Carrying a newer season first would move a driver into the new season
    on their newer deal and then leave the older row behind as active
    payroll forever, so the order is enforced here rather than documented.
    """
    lines = []
    for index, season in enumerate(pending):
        marker = "▶" if index == 0 else "🔒"
        suffix = "" if index == 0 else " — waits for the season above"
        lines.append(
            f"{marker} **{season.name}** — {season.unresolved_contracts} "
            f"active contract(s){suffix}"
        )
    lines.append("")
    lines.append(
        "Oldest season first, and only the oldest is selectable. Each one "
        "shows a preview before anything is written."
    )
    return discord.Embed(
        title="🔄 Carry contracts over",
        description=truncate_field("\n".join(lines)),
        color=COLOR_INFO,
    )


class _CarryView(AdminOwnedView):
    def __init__(self, parent: OffseasonView, pending: list[PastSeason]) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.pending = pending
        self.add_item(_CarrySelect(self, pending))
        self.add_item(BackButton(parent.reload, label="Back", row=1))


class _CarrySelect(discord.ui.Select):
    """
    Only the oldest pending season is a real option.

    Later seasons appear disabled-by-absence rather than as clickable
    options that would then be refused, so the ordering rule is visible in
    the component itself.
    """

    def __init__(self, owner: _CarryView, pending: list[PastSeason]) -> None:
        oldest = pending[0]
        super().__init__(
            placeholder=f"Carry over {oldest.name} (oldest first)",
            options=[
                discord.SelectOption(
                    label=oldest.name[:100],
                    value=oldest.name[:100],
                    description=(
                        f"{oldest.unresolved_contracts} active contract(s)"
                    )[:100],
                )
            ],
        )
        self._owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        name = self.values[0]
        parent = self._owner.parent
        await interaction.response.defer(ephemeral=True)
        try:
            preview = await preview_carry_over(interaction.guild_id, name)
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return

        async def _do(confirm: discord.Interaction) -> None:
            await confirm.response.defer(ephemeral=True)
            try:
                report = await workflow.carry_over_contracts(
                    guild_id=confirm.guild_id,
                    actor_id=confirm.user.id,
                    from_season_name=name,
                )
            except workflow.WorkflowError as exc:
                await report_error(confirm, str(exc))
                return
            outcome = report.outcome
            await parent.mark_ran(
                confirm,
                STEP_CARRY,
                note=(
                    f"✅ {report.from_season_name} → {report.to_season_name}: "
                    f"carried **{outcome.carried}**, expired "
                    f"**{outcome.expired}**, skipped **{outcome.skipped}**. "
                    "Skipped rows stay active in the old season and will be "
                    "offered again. Nothing was announced publicly."
                ),
            )

        confirm_view = _ConfirmView(
            opener_id=parent.opener_id,
            confirm_label="Carry them over",
            on_confirm=_do,
            on_cancel=parent.reload,
        )
        await _render(
            interaction, embed=build_carry_preview_embed(preview), view=confirm_view
        )


# ── step 5: budget rollover ──────────────────────────────────────────


async def _step_rollover(
    interaction: discord.Interaction, view: OffseasonView, key: str
) -> None:
    state = await load_offseason_state(interaction.guild_id)
    sources = state.rollover_sources or state.past_seasons
    if not sources:
        await view.reload(
            interaction,
            note="No past season has any budget history to roll over.",
        )
        return
    picker = _RolloverView(view, sources)
    embed = discord.Embed(
        title="🔄 Roll budgets over — pick the source season",
        description=(
            "Rollover is order-independent from carry-over: it reads each "
            "team's pinned payroll for the source season, so it gives the "
            "same answer before or after step 4."
        ),
        color=COLOR_INFO,
    )
    await _render(interaction, embed=embed, view=picker)


class _RolloverView(AdminOwnedView):
    def __init__(self, parent: OffseasonView, sources: list[PastSeason]) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.add_item(_RolloverSelect(self, sources))
        self.add_item(BackButton(parent.reload, label="Back", row=1))


class _RolloverSelect(discord.ui.Select):
    def __init__(self, owner: _RolloverView, sources: list[PastSeason]) -> None:
        window, _page, _pages = _paginate(list(reversed(sources)), 0)
        super().__init__(
            placeholder="Roll budgets from which season?",
            options=[
                discord.SelectOption(
                    label=s.name[:100],
                    value=s.name[:100],
                    description=f"{s.budget_rows} team(s) with budget rows"[:100],
                )
                for s in window
            ],
        )
        self._owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        name = self.values[0]
        parent = self._owner.parent
        await interaction.response.defer(ephemeral=True)
        try:
            preview = await preview_rollover(interaction.guild_id, name)
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return

        async def _do(confirm: discord.Interaction) -> None:
            await confirm.response.defer(ephemeral=True)
            try:
                lines = await workflow.rollover_budgets(
                    guild_id=confirm.guild_id,
                    actor_id=confirm.user.id,
                    from_season_name=name,
                )
            except workflow.WorkflowError as exc:
                await report_error(confirm, str(exc))
                return
            written = [line for line in lines if line.skipped_reason is None]
            await parent.mark_ran(
                confirm,
                STEP_ROLLOVER,
                note=(
                    f"✅ Rolled over **{len(written)}** team(s) from {name}; "
                    f"{len(lines) - len(written)} skipped (already rolled or "
                    "nothing unspent). Contracts were not touched."
                ),
            )

        confirm_view = _ConfirmView(
            opener_id=parent.opener_id,
            confirm_label="Roll it over",
            on_confirm=_do,
            on_cancel=parent.reload,
            disabled=not preview.enabled,
        )
        await _render(
            interaction,
            embed=build_rollover_preview_embed(preview),
            view=confirm_view,
        )


# ── step 6: prize money ──────────────────────────────────────────────


async def _step_prize(
    interaction: discord.Interaction, view: OffseasonView, key: str
) -> None:
    """
    Hand off to Teams & money, which owns the award flow.

    Prize money is per team and needs the balance table next to it; the
    wizard marks the step run when the commissioner comes back.
    """

    async def _back(back_interaction: discord.Interaction) -> None:
        await view.mark_ran(
            back_interaction,
            STEP_PRIZE,
            note=(
                "Marked step 6 as handled. Any prize money you awarded is in "
                "the team budget ledger."
            ),
        )

    await money_screen.open_money(
        interaction, on_back=_back, opener_id=view.opener_id
    )


# ── step 7: sync drivers from tier roles ─────────────────────────────


def _seeds_from_role(role: discord.Role) -> list[driver_ops.DriverSeed]:
    """Non-bot members of the role, as enrolment seeds."""
    return [
        driver_ops.DriverSeed(member_id=m.id, display_name=m.display_name)
        for m in role.members
        if not m.bot
    ]


def _resolve_tier_roles(
    guild: discord.Guild, tiers: list[TierProgress], role_ids: dict[str, int | None]
) -> tuple[dict[str, list[driver_ops.DriverSeed]], dict[str, str]]:
    seeds: dict[str, list[driver_ops.DriverSeed]] = {}
    skipped: dict[str, str] = {}
    for tier in tiers:
        role_id = role_ids.get(tier.code)
        if role_id is None:
            skipped[tier.code] = "no Discord role set"
            continue
        role = guild.get_role(role_id)
        if role is None:
            skipped[tier.code] = f"role id {role_id} not in guild"
            continue
        seeds[tier.code] = _seeds_from_role(role)
    return seeds, skipped


async def _step_sync_drivers(
    interaction: discord.Interaction, view: OffseasonView, key: str
) -> None:
    await interaction.response.defer(ephemeral=True)
    tiers = await workflow.list_tiers(interaction.guild_id)
    role_ids = {t.code: t.tier_role_id for t in tiers}
    state = await load_offseason_state(interaction.guild_id)
    assert interaction.guild is not None
    seeds, skipped = _resolve_tier_roles(interaction.guild, state.tiers, role_ids)

    lines = []
    for tier in state.tiers:
        if tier.code in skipped:
            lines.append(f"• `{tier.code}` — skipped: {skipped[tier.code]}")
        else:
            lines.append(
                f"• `{tier.code}` — {len(seeds[tier.code])} member(s) in the "
                f"role, {tier.driver_count} already enrolled"
            )
    embed = discord.Embed(
        title="🔄 Confirm: sync drivers from tier roles",
        description=truncate_field(
            "\n".join(
                lines
                + [
                    "",
                    "Enrolling creates a driver row per member per tier and "
                    "leaves existing rows alone; a member in two tiers keeps "
                    "two independent rows. Nobody is removed — a driver who "
                    "left the role stays enrolled with their history.",
                ]
            )
        ),
        color=COLOR_WARN,
    )

    async def _do(confirm: discord.Interaction) -> None:
        await confirm.response.defer(ephemeral=True)
        try:
            reports = await workflow.sync_drivers_all_tiers(
                guild_id=confirm.guild_id,
                actor_id=confirm.user.id,
                seeds_by_tier_code=seeds,
                skipped_by_tier_code=skipped,
            )
        except workflow.WorkflowError as exc:
            await report_error(confirm, str(exc))
            return
        created = sum(r.created for r in reports)
        await view.mark_ran(
            confirm,
            STEP_SYNC,
            note=(
                f"✅ Sync complete: **{created}** driver row(s) created across "
                f"{len(reports)} tier(s). No Discord roles were changed."
            ),
        )

    confirm_view = _ConfirmView(
        opener_id=view.opener_id,
        confirm_label="Sync them",
        on_confirm=_do,
        on_cancel=view.reload,
    )
    await _render(interaction, embed=embed, view=confirm_view)


# ── step 8: baseline valuation, then publish ─────────────────────────


def baseline_round_label(season_name: str) -> str:
    """
    The label a baseline run is filed under.

    A label with no imported round under it produces an empty-observation
    run — no movement — which is exactly what a pre-season baseline is.
    """
    return f"{_BASELINE_PREFIX} {season_name}"


async def _step_baseline(
    interaction: discord.Interaction, view: OffseasonView, key: str
) -> None:
    await interaction.response.defer(ephemeral=True)
    state = await load_offseason_state(interaction.guild_id)
    if not state.tiers:
        await report_error(
            interaction, "No tiers in this season — add them in Setup first."
        )
        return
    label = baseline_round_label(str(state.active_season_name))
    runs: list[tuple[str, int, int]] = []
    failures: list[str] = []
    for tier in state.tiers:
        try:
            outcome = await workflow.run_valuation(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id,
                tier=tier.code,
                round_label=label,
            )
        except workflow.WorkflowError as exc:
            failures.append(f"• `{tier.code}` — {exc}")
            continue
        runs.append((tier.code, outcome.run_id, len(outcome.outcomes)))

    lines = [f"Round label: **{label}**", ""]
    for code, run_id, count in runs:
        lines.append(f"• `{code}` — run `{run_id}`, {count} driver(s) priced")
    lines += failures
    if not runs:
        lines.append("Nothing could be priced, so there is nothing to publish.")
    lines += [
        "",
        "These are **dry runs**: the values are stored but not live, and no "
        "board shows them yet. Publishing makes them the drivers' market "
        "values and refreshes every board that displays them — the previous "
        "published run stays in history but stops being current.",
    ]
    embed = discord.Embed(
        title="🔄 Baseline valuation — preview before publishing",
        description=truncate_field("\n".join(lines)),
        color=COLOR_WARN,
    )

    async def _do(confirm: discord.Interaction) -> None:
        await confirm.response.defer(ephemeral=True)
        published: list[str] = []
        stale_boards = False
        for code, run_id, _count in runs:
            try:
                outcome = await workflow.publish_valuation(
                    confirm.client, guild_id=confirm.guild_id, run_id=run_id
                )
            except workflow.WorkflowError as exc:
                failures.append(f"• `{code}` — {exc}")
                continue
            published.append(code)
            stale_boards = stale_boards or not outcome.boards_refreshed
        note = (
            f"✅ Published baseline values for: "
            f"{', '.join(f'`{c}`' for c in published) or 'nothing'}."
        )
        if stale_boards:
            note += (
                " One or more boards did not redraw — the values are live and "
                "the 15-minute poll will heal the boards."
            )
        await view.mark_ran(confirm, STEP_BASELINE, note=note)

    confirm_view = _ConfirmView(
        opener_id=view.opener_id,
        confirm_label="Publish them",
        on_confirm=_do,
        on_cancel=view.reload,
        disabled=not runs,
    )
    await _render(interaction, embed=embed, view=confirm_view)


# ── entry point ──────────────────────────────────────────────────────


async def open_offseason(
    interaction: discord.Interaction,
    *,
    on_back: BackCallback,
    opener_id: int | None = None,
) -> None:
    """
    Entry point for the `/league` home screen's Offseason button.

    Mirrors `open_boards`' shape; `opener_id` defaults to the clicking
    user so the caller can pass just the back callback.
    """
    state = await load_offseason_state(interaction.guild_id)
    view = OffseasonView(
        state=state,
        opener_id=opener_id if opener_id is not None else interaction.user.id,
        on_back=on_back,
    )
    await _render(
        interaction, embed=build_offseason_embed(state, view.steps), view=view
    )
