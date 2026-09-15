"""
Team budget operations against an open connection.

Sits between the pure engine (`bot/market/budget.py`) and the callers
that have a `Connection` in hand — `workflow.py` for slash commands and
panel screens, the offer/trade flows in `bot/cogs/` and
`bot/contracts/service.py` for enforcement. Like `driver_ops`, nothing
here imports discord; everything is testable against the real
`pg_conn_migrated` fixture.

Two ideas to keep straight:

  * The SPENDING CAP (`league_config.salary_cap`) is a league rule and
    is the same for every team. It is not touched here.
  * The BUDGET is a team's own money. It starts at `opening_budget`,
    rises with prize money and race earnings, falls with penalties, and
    (when enabled) rolls into the next season.

A team may have more budget than the cap. It may never commit payroll
above EITHER number.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import asyncpg

from bot import queries
from bot.market import budget as budget_engine

KIND_OPENING = "opening_balance"
KIND_ROLLOVER = "rollover"
KIND_PRIZE = "prize_money"
KIND_ADJUSTMENT = "adjustment"

# Kinds a commissioner may write by hand. Automatic kinds (race
# earnings, penalties) are reserved for the ingest path so the audit
# trail can always tell a stewarding outcome from a manual award.
MANUAL_KINDS: tuple[str, ...] = (KIND_PRIZE, KIND_ADJUSTMENT)


class BudgetError(Exception):
    """Raised for a budget operation the caller should surface as-is."""


@dataclass(frozen=True)
class BudgetSnapshot:
    """
    Everything a signing or trade needs to know about one team's money.

    `available` = balance − effective payroll. It is what the team can
    still commit; it goes negative when penalties land on a team that
    had already spent to its budget.
    """

    season_id: int
    team_id: int
    config: budget_engine.BudgetConfig
    balance: Decimal
    effective_payroll: Decimal
    available: Decimal
    opened_now: bool = False


@dataclass(frozen=True)
class RoundBudgetOutcome:
    """What one round's import did to team budgets."""

    round_id: int
    entries_written: int
    corrections_written: int
    total_credited: Decimal
    total_debited: Decimal
    unattributed_driver_ids: tuple[int, ...]
    enforced: bool = True


@dataclass(frozen=True)
class RolloverLine:
    team_id: int
    team_name: str
    from_balance: Decimal
    from_payroll: Decimal
    carried: Decimal
    skipped_reason: str | None = None


# ── Opening balances ─────────────────────────────────────────────────────


async def ensure_opening_balance(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    team_id: int,
    cfg: budget_engine.BudgetConfig,
    actor_id: int | None = None,
) -> bool:
    """
    Credit `opening_budget` once, the first time a team's budget matters
    in a season. Idempotent via the partial unique index on
    (team, season) WHERE kind = 'opening_balance'.

    This is what makes the upgrade non-breaking for a league already
    mid-season: nobody has to run a backfill; the first offer or import
    after deploy opens every team at the configured amount.

    Returns True if a row was written.
    """
    if await queries.budget_entry_exists(conn, team_id, season_id, KIND_OPENING):
        return False
    if cfg.opening_budget == 0:
        return False
    await queries.insert_budget_entry(
        conn,
        season_id=season_id,
        team_id=team_id,
        kind=KIND_OPENING,
        amount=cfg.opening_budget,
        detail={"opening_budget": str(cfg.opening_budget), "config_id": str(cfg.id)},
        actor_id=actor_id,
    )
    return True


async def snapshot(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int | None,
    team_id: int,
    actor_id: int | None = None,
) -> BudgetSnapshot | None:
    """
    The team's budget position for enforcement or display.

    Returns None when budgets are not configured (no `budget_config` row
    for the season) or are switched off (`enforce_budget = FALSE`). The
    offer rule treats None as "not enforced" and passes.
    """
    cfg = await queries.fetch_budget_config(conn, season_id, tier_id)
    if cfg is None or not cfg.enforce_budget:
        return None
    opened = await ensure_opening_balance(
        conn, season_id=season_id, team_id=team_id, cfg=cfg, actor_id=actor_id,
    )
    balance = await queries.fetch_budget_balance(conn, team_id, season_id)
    payroll = await queries.fetch_team_effective_payroll(conn, team_id, season_id)
    return BudgetSnapshot(
        season_id=season_id,
        team_id=team_id,
        config=cfg,
        balance=balance,
        effective_payroll=payroll,
        available=budget_engine.available_to_spend(
            balance, payroll, escrow_enabled=cfg.escrow_enabled,
        ),
        opened_now=opened,
    )


# ── Manual awards ────────────────────────────────────────────────────────


async def award(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int | None,
    team_id: int,
    kind: str,
    amount: Decimal,
    note: str,
    actor_id: int,
) -> Decimal:
    """
    Commissioner-written entry: prize money (credit) or an adjustment
    (either sign). Returns the new balance.

    Prize money is how a league seeds unequal starting positions — for
    example, paying out last season's constructors' standings into the
    new season's budgets — without touching `opening_budget`, which
    stays equal for everyone so the audit trail shows exactly who was
    awarded what.
    """
    if kind not in MANUAL_KINDS:
        raise BudgetError(
            f"`{kind}` is not a manual budget kind. Use one of: "
            + ", ".join(MANUAL_KINDS)
        )
    if amount == 0:
        raise BudgetError("Amount must be non-zero.")
    if kind == KIND_PRIZE and amount < 0:
        raise BudgetError("Prize money is a credit; use `adjustment` to debit.")
    if not note.strip():
        raise BudgetError("Budget awards require a note for the audit trail.")
    cfg = await queries.fetch_budget_config(conn, season_id, tier_id)
    if cfg is None:
        raise BudgetError(
            "Budgets are not configured for this season. Run "
            "`/market-admin budget config` first."
        )
    await ensure_opening_balance(
        conn, season_id=season_id, team_id=team_id, cfg=cfg, actor_id=actor_id,
    )
    await queries.insert_budget_entry(
        conn,
        season_id=season_id,
        team_id=team_id,
        kind=kind,
        amount=amount,
        note=note.strip(),
        detail={"note": note.strip()},
        actor_id=actor_id,
    )
    return await queries.fetch_budget_balance(conn, team_id, season_id)


# ── Results → budget ─────────────────────────────────────────────────────


async def apply_round_charges(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int,
    round_id: int,
    actor_id: int | None,
) -> RoundBudgetOutcome:
    """
    Write the budget consequences of one round's results.

    Idempotent and correction-safe: the engine computes what SHOULD be
    charged per (result, team, kind); this diffs that against what the
    ledger already holds for the round and writes only the difference.
    An identical re-import writes nothing. A re-import where the
    stewards removed a DNF writes a single positive `dnf_penalty` row
    flagged `is_correction`, netting the earlier charge to zero.

    Drivers with no active contract are reported, not charged.
    """
    cfg = await queries.fetch_budget_config(conn, season_id, tier_id)
    if cfg is None or not cfg.enforce_budget:
        return RoundBudgetOutcome(
            round_id=round_id, entries_written=0, corrections_written=0,
            total_credited=Decimal(0), total_debited=Decimal(0),
            unattributed_driver_ids=(), enforced=False,
        )

    facts = await queries.fetch_result_facts_for_round(conn, round_id)
    scores = await queries.fetch_position_scores(conn, season_id)
    points_by_position = {s.position: s.points for s in scores}

    charges, unattributed = budget_engine.charges_for_round(
        facts, cfg, points_by_position
    )
    desired: dict[tuple[int, int, str], budget_engine.BudgetCharge] = {
        (c.race_result_id, c.team_id, c.kind): c for c in charges
    }
    existing = await queries.fetch_result_charge_net(conn, round_id)

    written = 0
    corrections = 0
    credited = Decimal(0)
    debited = Decimal(0)

    for key in sorted(set(desired) | set(existing)):
        race_result_id, team_id, kind = key
        want = desired[key].amount if key in desired else Decimal(0)
        have = existing.get(key, Decimal(0))
        delta = want - have
        if delta == 0:
            continue
        is_correction = key in existing
        detail = dict(desired[key].detail) if key in desired else {}
        if is_correction:
            detail["previous_net"] = str(have)
            detail["new_total"] = str(want)
        await ensure_opening_balance(
            conn, season_id=season_id, team_id=team_id, cfg=cfg, actor_id=actor_id,
        )
        await queries.insert_budget_entry(
            conn,
            season_id=season_id,
            team_id=team_id,
            kind=kind,
            amount=delta,
            race_result_id=race_result_id,
            round_id=round_id,
            detail=detail,
            actor_id=actor_id,
            is_correction=is_correction,
        )
        written += 1
        if is_correction:
            corrections += 1
        if delta > 0:
            credited += delta
        else:
            debited += -delta

    return RoundBudgetOutcome(
        round_id=round_id,
        entries_written=written,
        corrections_written=corrections,
        total_credited=credited,
        total_debited=debited,
        unattributed_driver_ids=tuple(f.driver_id for f in unattributed),
    )


# ── Season rollover ──────────────────────────────────────────────────────


async def rollover(
    conn: asyncpg.Connection,
    *,
    from_season_id: int,
    to_season_id: int,
    teams: list[tuple[int, str]],
    actor_id: int,
) -> list[RolloverLine]:
    """
    Carry each team's unspent budget from one season into the next as a
    `rollover` row. Idempotent per (team, target season) via the partial
    unique index; a team already rolled over is reported as skipped.

    "Unspent" = balance − the payroll that season actually carried
    (`queries.fetch_team_season_payroll`: rows signed or carried into the
    source season, whether they are still active, were carried on, or
    expired at its end, plus that season's dead money). Pinning the
    payroll to the source season's own rows means this number is the
    same whether contract carry-over has already run or not. A team that
    finished underwater carries a negative rollover.

    Requires `rollover_enabled` on the TARGET season's config: the league
    that is starting decides whether it honours the past. Contracts are
    carried separately by `bot.contracts.carryover`; this is money only.
    """
    if from_season_id == to_season_id:
        raise BudgetError("Source and target seasons must differ.")
    to_cfg = await queries.fetch_budget_config(conn, to_season_id, None)
    if to_cfg is None:
        raise BudgetError(
            "The target season has no budget config. Seed the preset or run "
            "`/market-admin budget config` there first."
        )
    if not to_cfg.rollover_enabled:
        raise BudgetError("Rollover is disabled for the target season.")

    # Whether salary was taken in cash is a property of the season being
    # carried FROM, not the one being carried into: that is the season
    # whose balance already had its escrow debits applied. Reading the
    # target's flag here would net off a season of payroll that the
    # source season never charged (or charge it twice). A source season
    # with no config at all is treated as commitment-only, matching how
    # every season behaved before migration 015.
    from_cfg = await queries.fetch_budget_config(conn, from_season_id, None)
    from_escrow = from_cfg.escrow_enabled if from_cfg is not None else False

    lines: list[RolloverLine] = []
    for team_id, team_name in teams:
        if await queries.budget_entry_exists(conn, team_id, to_season_id, KIND_ROLLOVER):
            lines.append(
                RolloverLine(
                    team_id=team_id, team_name=team_name,
                    from_balance=Decimal(0), from_payroll=Decimal(0),
                    carried=Decimal(0), skipped_reason="already rolled over",
                )
            )
            continue
        from_balance = await queries.fetch_budget_balance(conn, team_id, from_season_id)
        from_payroll = await queries.fetch_team_season_payroll(
            conn, team_id, from_season_id
        )
        carried = budget_engine.rollover_amount(
            from_balance, from_payroll, escrow_enabled=from_escrow,
        )
        # The opening balance for the new season is credited alongside
        # the rollover so the target season's first row is never a
        # bare rollover with no baseline underneath it.
        await ensure_opening_balance(
            conn, season_id=to_season_id, team_id=team_id, cfg=to_cfg, actor_id=actor_id,
        )
        if carried == 0:
            lines.append(
                RolloverLine(
                    team_id=team_id, team_name=team_name,
                    from_balance=from_balance, from_payroll=from_payroll,
                    carried=carried, skipped_reason="nothing unspent",
                )
            )
            continue
        await queries.insert_budget_entry(
            conn,
            season_id=to_season_id,
            team_id=team_id,
            kind=KIND_ROLLOVER,
            amount=carried,
            from_season_id=from_season_id,
            detail={
                "from_season_id": str(from_season_id),
                "from_balance": str(from_balance),
                "from_effective_payroll": str(from_payroll),
                "escrow_enabled": str(from_escrow),
            },
            actor_id=actor_id,
        )
        lines.append(
            RolloverLine(
                team_id=team_id, team_name=team_name,
                from_balance=from_balance, from_payroll=from_payroll,
                carried=carried,
            )
        )
    return lines
