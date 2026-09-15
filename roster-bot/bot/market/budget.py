"""
Team budget engine — pure functions over plain data.

The SPENDING CAP and the TEAM BUDGET are two different constraints:

  * The cap (`league_config.salary_cap`) is a league RULE — the most any
    team may commit to payroll, identical for everyone.
  * The budget is MONEY — what a team actually has. It differs by team
    because it is earned (prize money, race earnings) and lost
    (penalties, no-shows, retirements), and unspent budget rolls into
    the next season when the league enables it.

A signing must clear both. A team may hold more money than the cap; it
simply cannot spend past the cap. See `bot/contracts/rules.py`
(`cap_headroom_ok`, `budget_headroom_ok`) for the two checks.

This module computes the automatic budget entries a round of results
produces. It has no side effects, no DB access, and no `discord` import:
`workflow.import_round` fetches inputs, calls `charges_for_round`, and
persists the outputs. Every rate comes from `budget_config` rows and is
passed in — the ADR-001 magic-number guard scans this module.

Sign convention: credits are positive, debits are negative. Penalty
rates are stored positive in `budget_config` (so a commissioner never
edits a sign) and negated here, once, at the single place that turns a
rate into a ledger amount.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from bot.market.money import round_money

_ZERO = Decimal(0)

# Ledger kinds this engine emits. These are `budget_entry_kinds.code`
# values; the migration seeds them and the FK rejects anything else.
KIND_RACE_EARNINGS = "race_earnings"
KIND_DNF_PENALTY = "dnf_penalty"
KIND_DNS_PENALTY = "dns_penalty"
KIND_INCIDENT_PENALTY = "incident_penalty"

RESULT_KINDS: tuple[str, ...] = (
    KIND_RACE_EARNINGS,
    KIND_DNF_PENALTY,
    KIND_DNS_PENALTY,
    KIND_INCIDENT_PENALTY,
)


# ── Input types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BudgetConfig:
    """
    Resolved `budget_config` row for a (season, tier). All money in $M.
    Penalty rates are POSITIVE magnitudes; this module applies the sign.
    """

    season_id: int
    enforce_budget: bool
    rollover_enabled: bool
    opening_budget: Decimal
    earnings_per_point: Decimal
    dnf_penalty: Decimal
    dns_penalty: Decimal
    penalty_per_incident_pt: Decimal
    tier_id: int | None = None
    id: int | None = None
    # Whether salary is actually taken from cash race by race. DEFAULT
    # TRUE in the DB and here: escrow only engages at signings that
    # happen after migration 015, so an existing roster is untouched
    # either way, and defaulting on means the feature works without a
    # config trip first. Set FALSE to keep the commitment-only model.
    escrow_enabled: bool = True


@dataclass(frozen=True)
class ResultFacts:
    """
    The subset of one `race_results` row the budget cares about, plus
    the team to charge. `team_id` is None when the driver had no active
    contract at import time — a free agent's DNF costs nobody anything,
    and the caller reports these so the commissioner can see them.
    """

    race_result_id: int
    driver_id: int
    team_id: int | None
    finish_position: int | None
    dnf: bool
    dns: bool
    incident_points: Decimal


# ── Output type ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BudgetCharge:
    """One ledger row to write. `amount` is already signed."""

    race_result_id: int
    driver_id: int
    team_id: int
    kind: str
    amount: Decimal
    detail: dict[str, str]


# ── Engine ──────────────────────────────────────────────────────────────


def charges_for_round(
    facts: Sequence[ResultFacts],
    cfg: BudgetConfig,
    points_by_position: Mapping[int, Decimal],
) -> tuple[list[BudgetCharge], list[ResultFacts]]:
    """
    Turn one round's results into signed budget entries.

    Returns `(charges, unattributed)` where `unattributed` lists the
    results that had no team to charge. Deterministic: same inputs in
    the same order give the same outputs in the same order.

    Rules, each independent and each skipped when its rate is zero so a
    league can switch any one of them off from config:

      * race_earnings   +earnings_per_point × points for the finish
      * dnf_penalty     −dnf_penalty when `dnf`
      * dns_penalty     −dns_penalty when `dns`
      * incident_penalty −penalty_per_incident_pt × incident_points

    A DNS is not also a DNF (the ingest layer records them as distinct
    outcomes), and a retirement earns no points because it has no
    classified finish, so the rules never double-charge one fact.
    """
    charges: list[BudgetCharge] = []
    unattributed: list[ResultFacts] = []

    for f in facts:
        if f.team_id is None:
            unattributed.append(f)
            continue

        if f.finish_position is not None and cfg.earnings_per_point > _ZERO:
            points = points_by_position.get(f.finish_position, _ZERO)
            if points > _ZERO:
                amount = round_money(cfg.earnings_per_point * points)
                if amount > _ZERO:
                    charges.append(
                        _charge(
                            f, KIND_RACE_EARNINGS, amount,
                            {
                                "finish_position": str(f.finish_position),
                                "points": str(points),
                                "earnings_per_point": str(cfg.earnings_per_point),
                            },
                        )
                    )

        if f.dnf and cfg.dnf_penalty > _ZERO:
            charges.append(
                _charge(
                    f, KIND_DNF_PENALTY, -round_money(cfg.dnf_penalty),
                    {"dnf_penalty": str(cfg.dnf_penalty)},
                )
            )

        if f.dns and cfg.dns_penalty > _ZERO:
            charges.append(
                _charge(
                    f, KIND_DNS_PENALTY, -round_money(cfg.dns_penalty),
                    {"dns_penalty": str(cfg.dns_penalty)},
                )
            )

        if f.incident_points > _ZERO and cfg.penalty_per_incident_pt > _ZERO:
            amount = round_money(cfg.penalty_per_incident_pt * f.incident_points)
            if amount > _ZERO:
                charges.append(
                    _charge(
                        f, KIND_INCIDENT_PENALTY, -amount,
                        {
                            "incident_points": str(f.incident_points),
                            "penalty_per_incident_pt": str(cfg.penalty_per_incident_pt),
                        },
                    )
                )

    return charges, unattributed


def available_to_spend(
    balance: Decimal,
    effective_payroll: Decimal,
    *,
    escrow_enabled: bool = False,
) -> Decimal:
    """
    Budget headroom: what a team can still commit this season.

    **Without escrow** payroll is not debited from the ledger — contracts
    are already the source of truth for what is committed, and
    duplicating them into the budget ledger would risk drift. Instead the
    balance is compared against payroll, exactly as the cap is. This can
    go negative after a penalty lands on a team that had already spent to
    its limit; that team is then frozen out of signings until it earns
    its way back.

    **With escrow** salary genuinely leaves the balance, one race at a
    time, so subtracting payroll as well would charge every team twice —
    once in cash and once again as a phantom commitment. Cash becomes the
    single source of truth and the balance is returned as it stands.

    A consequence worth stating plainly, because it is a real change in
    exposure: under escrow this figure no longer reserves anything for
    the *unescrowed* remainder of a term. A team can commit to a payroll
    it will not be able to fund later in the season and only discover it
    when a race-night charge takes the balance negative. The cap still
    limits total annual commitment, which is the backstop, but it is a
    commitment gate and not a solvency test. Reserving the remaining
    term would be the stricter rule; it is deliberately not implemented
    here, and is the first thing to revisit if teams start running dry
    mid-season.

    `escrow_enabled` defaults to False so any caller that has not
    resolved the budget config keeps the pre-escrow behaviour rather than
    silently loosening the check.
    """
    if escrow_enabled:
        return round_money(balance)
    return round_money(balance - effective_payroll)


def rollover_amount(
    balance: Decimal,
    effective_payroll: Decimal,
    *,
    escrow_enabled: bool = False,
) -> Decimal:
    """
    What carries into the next season: the unspent portion of the budget.

    Uses the same headroom arithmetic as `available_to_spend` so the two
    can never disagree about what "unspent" means. A negative headroom
    rolls over as debt — a team that finished the season underwater
    starts the next one paying it off.

    Under escrow this is the balance itself: salary has already been
    taken race by race, so there is no payroll left to net off. Carrying
    `balance - payroll` in that mode would deduct a season of salary a
    second time on the way into the new season.
    """
    return available_to_spend(
        balance, effective_payroll, escrow_enabled=escrow_enabled
    )


# ── internals ────────────────────────────────────────────────────────────


def _charge(
    f: ResultFacts, kind: str, amount: Decimal, detail: dict[str, str]
) -> BudgetCharge:
    assert f.team_id is not None
    return BudgetCharge(
        race_result_id=f.race_result_id,
        driver_id=f.driver_id,
        team_id=f.team_id,
        kind=kind,
        amount=amount,
        detail=detail,
    )
