"""
Valuation engine — pure functions over plain data.

`compute_run` takes the season's factor weights, one input row per
driver (previous market value + raw factor observations for the cycle),
and the movement caps, and returns one `DriverValuation` per driver
with a fully explainable breakdown. It has no side effects, no DB
access, and no `discord` import: the cog layer fetches inputs, hands
them to `compute_run`, and persists the outputs. That separation is
what makes the engine deterministic, testable, and safe to re-run for
dry-run previews.

Tier isolation is a caller contract, not an engine feature: the caller
passes only tier-N inputs and gets tier-N valuations back. The engine
never learns tier structure, so a Tier-2 driver appearing in the input
mathematically cannot influence a Tier-1 value.

The ADR-001 magic-number guard scans this module — every numeric
constant lives in the DB (valuation_factors, league_config) and is
passed in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from bot.market.money import round_money

_ZERO = Decimal(0)


# ── Input types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FactorWeight:
    """
    One row from `valuation_factors`. `max_contribution` caps this
    factor's absolute per-driver contribution before movement caps are
    applied (a defensive limit against a single wild observation
    dominating the whole run). `None` means uncapped.
    """

    code: str
    weight: Decimal
    max_contribution: Decimal | None = None


@dataclass(frozen=True)
class MovementCaps:
    """Per-cycle bounds on how much a driver's value may move."""

    weekly: Decimal
    exceptional: Decimal


@dataclass(frozen=True)
class DriverInput:
    """
    One driver's raw performance observations plus their pre-run market
    value. `factor_values` is keyed by the same codes as
    `FactorWeight.code`; missing codes are treated as zero, so a driver
    who did not qualify is not implicitly penalised.

    `exceptional` opts into the wider movement cap for this driver only
    — reserved for outlier weekends the commissioner marks explicitly
    in the dry-run flow.
    """

    driver_id: int
    display_name: str
    previous_value: Decimal
    factor_values: dict[str, Decimal] = field(default_factory=dict)
    exceptional: bool = False


# ── Output types ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FactorContribution:
    """One row of the audit breakdown for a single driver."""

    code: str
    raw_value: Decimal
    weight: Decimal
    max_contribution: Decimal | None
    contribution: Decimal
    clipped: bool


@dataclass(frozen=True)
class DriverValuation:
    driver_id: int
    display_name: str
    previous_value: Decimal
    market_value: Decimal
    delta: Decimal
    rank_in_tier: int
    capped: bool
    cap_applied: Decimal
    breakdown: tuple[FactorContribution, ...]


# ── Engine ──────────────────────────────────────────────────────────────


def compute_run(
    factors: Sequence[FactorWeight],
    drivers: Sequence[DriverInput],
    caps: MovementCaps,
) -> list[DriverValuation]:
    """
    Compute per-driver valuations for a single (season, tier) cycle.

    Contract:
      * Deterministic — same inputs (in the same order) always yield
        the same outputs (in the same order).
      * Pure — no I/O, no globals mutated, no ambient state consulted.
      * Tier-isolated — the engine has no notion of tier; callers pass
        one tier's inputs at a time.
      * Decimal end to end — all inputs and outputs are Decimal; the
        one rounding boundary is `money.round_money`.

    Ranking: results are sorted by market_value descending, ties broken
    by driver_id ascending for stability. `rank_in_tier` is 1-based and
    reflects that order.
    """
    unsorted: list[DriverValuation] = []
    for driver in drivers:
        breakdown = tuple(
            _contribution(factor, driver.factor_values.get(factor.code, _ZERO))
            for factor in factors
        )
        raw_delta = sum((c.contribution for c in breakdown), _ZERO)

        cap = caps.exceptional if driver.exceptional else caps.weekly
        clipped_delta, capped = _clip(raw_delta, cap)
        market_value = round_money(driver.previous_value + clipped_delta)
        # Recompute delta from the rounded market value so `previous +
        # delta == market_value` holds in the persisted row (avoids a
        # sub-cent tail on the delta after rounding the sum).
        delta = round_money(market_value - driver.previous_value)

        unsorted.append(
            DriverValuation(
                driver_id=driver.driver_id,
                display_name=driver.display_name,
                previous_value=round_money(driver.previous_value),
                market_value=market_value,
                delta=delta,
                rank_in_tier=0,  # replaced below after sorting
                capped=capped,
                cap_applied=cap,
                breakdown=breakdown,
            )
        )

    ordered = sorted(unsorted, key=_rank_key)
    ranked: list[DriverValuation] = []
    for index, v in enumerate(ordered, start=1):
        ranked.append(
            DriverValuation(
                driver_id=v.driver_id,
                display_name=v.display_name,
                previous_value=v.previous_value,
                market_value=v.market_value,
                delta=v.delta,
                rank_in_tier=index,
                capped=v.capped,
                cap_applied=v.cap_applied,
                breakdown=v.breakdown,
            )
        )
    return ranked


def breakdown_to_json(breakdown: Sequence[FactorContribution]) -> list[dict[str, object]]:
    """
    Serialise a breakdown into the shape stored in
    `driver_valuations.breakdown` (JSONB). Decimals go out as strings
    to preserve exact precision on the DB round-trip.
    """
    return [
        {
            "code": c.code,
            "raw_value": str(c.raw_value),
            "weight": str(c.weight),
            "max_contribution": (
                str(c.max_contribution) if c.max_contribution is not None else None
            ),
            "contribution": str(c.contribution),
            "clipped": c.clipped,
        }
        for c in breakdown
    ]


# ── internals ────────────────────────────────────────────────────────────


def _contribution(factor: FactorWeight, raw_value: Decimal) -> FactorContribution:
    raw = factor.weight * raw_value
    clipped = False
    if factor.max_contribution is not None:
        limit = factor.max_contribution
        if raw > limit:
            raw = limit
            clipped = True
        elif raw < -limit:
            raw = -limit
            clipped = True
    return FactorContribution(
        code=factor.code,
        raw_value=raw_value,
        weight=factor.weight,
        max_contribution=factor.max_contribution,
        contribution=raw,
        clipped=clipped,
    )


def _clip(value: Decimal, cap: Decimal) -> tuple[Decimal, bool]:
    if value > cap:
        return cap, True
    if value < -cap:
        return -cap, True
    return value, False


def _rank_key(v: DriverValuation) -> tuple[Decimal, int]:
    # Higher market value first; stable tiebreak on driver_id so the
    # ranking is deterministic across runs.
    return (-v.market_value, v.driver_id)
