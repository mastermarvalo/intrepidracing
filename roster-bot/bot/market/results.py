"""
Results normalization — raw race facts to valuation observations.

`bot/market/valuation.py` computes `contribution = weight * raw_value`
and clips at `max_contribution`. That contract only behaves sensibly if
every raw observation arrives on a comparable scale, so this module is
the boundary that puts them there: every factor observation it emits is
in `[0, 1]` (or `[-1, 1]` for `form_trend`, which is signed by nature).

Two concrete bugs this exists to prevent:

  * Finishing position fed in raw. `race_finish` carries a positive
    weight, so a raw P20 would score twenty times a raw P1 — precisely
    backwards. Positions are looked up in `position_scores`, where P1
    maps to the highest score.
  * Championship points fed in raw. With a 25-point win and a per-factor
    cap below that, a win and a midfield points finish both clipped to
    the same contribution and the factor lost all resolution. Points are
    normalized against the season maximum before they reach the engine.

Like the valuation engine this module is pure: plain dataclasses in,
plain dict out, no DB access, no `discord` import, no ambient state. The
cog fetches rows, calls `build_observations`, and hands the result to
`compute_run`.

ADR-001: the guard scans this file. Every threshold lives in data —
`position_scores` rows carry the score curve, the points table, and the
`is_win` / `is_podium` / `is_pole` flags, so nothing here asks whether
`position <= 3`. Window lengths and the incident scale come from
`results_config`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

_ZERO = Decimal(0)
_ONE = Decimal(1)


# ── Factor codes ────────────────────────────────────────────────────────
# String keys matching `valuation_factors.code`. Codes are identifiers,
# not tunable business numbers, so they belong in code.

FACTOR_RACE_FINISH = "race_finish"
FACTOR_QUALI_FINISH = "quali_finish"
FACTOR_POINTS = "points_scored"
FACTOR_WINS = "wins"
FACTOR_PODIUMS = "podiums"
FACTOR_POLES = "poles"
FACTOR_FASTEST_LAPS = "fastest_laps"
FACTOR_DRIVER_OF_DAY = "driver_of_day"
FACTOR_DNF = "dnf"
FACTOR_INCIDENTS = "incidents"
FACTOR_FORM_TREND = "form_trend"
FACTOR_CONSISTENCY = "consistency"


# ── Input types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PositionScore:
    """
    One row of `position_scores`: the normalized value of finishing (or
    starting) in a given position.

    `race_score` and `quali_score` are already in `[0, 1]` — the curve
    is the league's to shape, and it is deliberately allowed to be
    non-linear so that P1 can be worth disproportionately more than P2.
    """

    position: int
    race_score: Decimal
    quali_score: Decimal
    points: Decimal
    is_win: bool = False
    is_podium: bool = False
    is_pole: bool = False


@dataclass(frozen=True)
class RoundResult:
    """
    One driver's raw facts for one round. Nothing derived, nothing
    scored — re-importing after a stewards' decision overwrites facts
    only.

    `finish_position` is None for a retirement or a no-show.
    """

    round_order: int
    driver_id: int
    finish_position: int | None = None
    grid_position: int | None = None
    dnf: bool = False
    dns: bool = False
    fastest_lap: bool = False
    driver_of_day: bool = False
    incident_points: Decimal = _ZERO


@dataclass(frozen=True)
class ResultsTuning:
    """Resolved `results_config` row for the (season, tier) being priced."""

    form_window_rounds: int
    consistency_window_rounds: int
    max_incident_points: Decimal


@dataclass(frozen=True)
class DriverObservations:
    """Factor observations for one driver, plus the exceptional-cap flag."""

    driver_id: int
    factor_values: dict[str, Decimal]
    exceptional: bool


# ── Helpers ─────────────────────────────────────────────────────────────


def _clamp_unit(value: Decimal) -> Decimal:
    """Clamp to [0, 1]. Observations outside the unit interval would
    silently defeat the per-factor caps downstream."""
    if value < _ZERO:
        return _ZERO
    if value > _ONE:
        return _ONE
    return value


def _flag(condition: bool) -> Decimal:
    """Boolean observation as a Decimal, so the engine sees one type."""
    return _ONE if condition else _ZERO


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return _ZERO
    return sum(values, _ZERO) / Decimal(len(values))


def _race_score_for(
    result: RoundResult, scores: Mapping[int, PositionScore]
) -> Decimal:
    """
    The normalized race score for one result.

    A retirement or a no-show scores zero rather than being skipped: not
    finishing is a real performance signal, and the dedicated `dnf`
    factor applies the penalty on top.
    """
    if result.dnf or result.dns or result.finish_position is None:
        return _ZERO
    row = scores.get(result.finish_position)
    # A position past the end of the curve (an unusually large field)
    # scores zero rather than raising — a missing lookup row should not
    # take down a whole valuation run.
    return row.race_score if row is not None else _ZERO


def _history_scores(
    history: Sequence[RoundResult],
    scores: Mapping[int, PositionScore],
    window: int,
) -> list[Decimal]:
    """Race scores for the most recent `window` rounds, oldest first."""
    if window <= 0:
        return []
    ordered = sorted(history, key=lambda r: r.round_order)
    return [_race_score_for(r, scores) for r in ordered[-window:]]


# ── Derived factors ─────────────────────────────────────────────────────


def compute_form_trend(
    history: Sequence[RoundResult],
    scores: Mapping[int, PositionScore],
    window: int,
) -> Decimal:
    """
    Signed momentum in `[-1, 1]`: recent-window mean race score minus the
    mean of everything before it.

    Returns zero when there is no prior baseline to compare against, so a
    driver's first round is neutral rather than counted as a collapse.
    """
    if window <= 0 or not history:
        return _ZERO
    ordered = sorted(history, key=lambda r: r.round_order)
    if len(ordered) <= window:
        return _ZERO
    recent = ordered[-window:]
    prior = ordered[:-window]
    recent_mean = _mean([_race_score_for(r, scores) for r in recent])
    prior_mean = _mean([_race_score_for(r, scores) for r in prior])
    delta = recent_mean - prior_mean
    if delta > _ONE:
        return _ONE
    if delta < -_ONE:
        return -_ONE
    return delta


def compute_consistency(
    history: Sequence[RoundResult],
    scores: Mapping[int, PositionScore],
    window: int,
) -> Decimal:
    """
    Steadiness in `[0, 1]`, as one minus the mean absolute deviation of
    recent race scores.

    Mean absolute deviation rather than standard deviation on purpose:
    MAD needs no squaring or square root, so the whole path stays exact
    Decimal arithmetic with no float boundary. A driver with a single
    round has nothing to vary yet and scores zero rather than a
    misleading perfect 1.
    """
    values = _history_scores(history, scores, window)
    if len(values) <= 1:
        return _ZERO
    mean = _mean(values)
    deviation = _mean([abs(v - mean) for v in values])
    return _clamp_unit(_ONE - deviation)


def is_exceptional_weekend(
    result: RoundResult, scores: Mapping[int, PositionScore]
) -> bool:
    """
    Whether this drive earns the wider `exceptional_move_cap`.

    Defined as the clean sweep — pole, win, and fastest lap in the same
    round. Data-driven via the `is_win` / `is_pole` flags on the lookup
    rows, so a league that scores a sprint format differently only edits
    `position_scores`.
    """
    if result.finish_position is None or result.grid_position is None:
        return False
    finish = scores.get(result.finish_position)
    grid = scores.get(result.grid_position)
    if finish is None or grid is None:
        return False
    return bool(finish.is_win and grid.is_pole and result.fastest_lap)


# ── Entry point ─────────────────────────────────────────────────────────


def build_observations(
    *,
    current: Sequence[RoundResult],
    history: Mapping[int, Sequence[RoundResult]],
    position_scores: Iterable[PositionScore],
    tuning: ResultsTuning,
) -> list[DriverObservations]:
    """
    Turn one round's raw results into per-driver factor observations.

    Args:
        current: the round being priced, one row per driver.
        history: every prior round keyed by driver_id, used for the
            form and consistency windows. The current round should be
            included so the newest result counts toward both.
        position_scores: the season's normalization curve.
        tuning: resolved window lengths and the incident scale.

    Returns one `DriverObservations` per row in `current`, in the same
    order, so the caller can zip it against its driver list.

    Determinism: no iteration over unordered collections, no clock, no
    randomness — the same inputs always produce the same output, which
    is what makes a dry-run preview trustworthy as a preview.
    """
    scores = {row.position: row for row in position_scores}
    # Points are normalized against the best award on the curve, so the
    # observation is "share of the maximum available" rather than a raw
    # championship figure whose scale depends on the league's points table.
    max_points = max((row.points for row in scores.values()), default=_ZERO)

    out: list[DriverObservations] = []
    for result in current:
        finish = (
            scores.get(result.finish_position)
            if result.finish_position is not None
            else None
        )
        grid = (
            scores.get(result.grid_position)
            if result.grid_position is not None
            else None
        )
        classified = not (result.dnf or result.dns)

        points_share = _ZERO
        if finish is not None and classified and max_points > _ZERO:
            points_share = _clamp_unit(finish.points / max_points)

        incidents = _ZERO
        if tuning.max_incident_points > _ZERO:
            incidents = _clamp_unit(
                result.incident_points / tuning.max_incident_points
            )

        driver_history = history.get(result.driver_id, [])

        factor_values = {
            FACTOR_RACE_FINISH: _race_score_for(result, scores),
            FACTOR_QUALI_FINISH: (
                grid.quali_score if grid is not None else _ZERO
            ),
            FACTOR_POINTS: points_share,
            FACTOR_WINS: _flag(
                finish is not None and classified and finish.is_win
            ),
            FACTOR_PODIUMS: _flag(
                finish is not None and classified and finish.is_podium
            ),
            FACTOR_POLES: _flag(grid is not None and grid.is_pole),
            FACTOR_FASTEST_LAPS: _flag(result.fastest_lap),
            FACTOR_DRIVER_OF_DAY: _flag(result.driver_of_day),
            FACTOR_DNF: _flag(result.dnf),
            FACTOR_INCIDENTS: incidents,
            FACTOR_FORM_TREND: compute_form_trend(
                driver_history, scores, tuning.form_window_rounds
            ),
            FACTOR_CONSISTENCY: compute_consistency(
                driver_history, scores, tuning.consistency_window_rounds
            ),
        }

        out.append(
            DriverObservations(
                driver_id=result.driver_id,
                factor_values=factor_values,
                exceptional=is_exceptional_weekend(result, scores),
            )
        )

    return out
