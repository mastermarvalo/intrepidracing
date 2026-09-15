"""
Results normalization: raw race facts to valuation observations.

These are the tests that guard the bug the module was written to kill —
finishing position fed to a positively weighted factor, which made a
last place worth more than a win. Every assertion here is exact Decimal
arithmetic; nothing is approximate, because these observations multiply
into real money.
"""

from decimal import Decimal

import pytest

from bot.market import results as R

# A small hand-built curve. Deliberately non-linear so tests can tell
# "P1 scored higher" apart from "positions were merely inverted".
CURVE = [
    R.PositionScore(1, Decimal("1.00"), Decimal("1.00"), Decimal(25), True, True, True),
    R.PositionScore(2, Decimal("0.80"), Decimal("0.90"), Decimal(18), False, True, False),
    R.PositionScore(3, Decimal("0.70"), Decimal("0.80"), Decimal(15), False, True, False),
    R.PositionScore(4, Decimal("0.60"), Decimal("0.70"), Decimal(12), False, False, False),
    R.PositionScore(5, Decimal("0.50"), Decimal("0.60"), Decimal(10), False, False, False),
    R.PositionScore(10, Decimal("0.20"), Decimal("0.30"), Decimal(1), False, False, False),
    R.PositionScore(20, Decimal("0.00"), Decimal("0.00"), Decimal(0), False, False, False),
]
SCORES = {row.position: row for row in CURVE}

TUNING = R.ResultsTuning(
    form_window_rounds=3,
    consistency_window_rounds=3,
    max_incident_points=Decimal("6.00"),
)


def result(**kw) -> R.RoundResult:
    base = {"round_order": 1, "driver_id": 1}
    base.update(kw)
    return R.RoundResult(**base)


def observe(current, history=None):
    return R.build_observations(
        current=current,
        history=history or {},
        position_scores=CURVE,
        tuning=TUNING,
    )


# ── The inversion bug ───────────────────────────────────────────────────


def test_winning_scores_higher_than_finishing_last():
    """
    The regression test for the original bug. race_finish carries a
    positive weight, so if raw positions ever reach the engine again a
    P20 outscores a P1 and the whole market inverts.
    """
    obs = observe([result(driver_id=1, finish_position=1),
                   result(driver_id=2, finish_position=20)])
    assert obs[0].factor_values[R.FACTOR_RACE_FINISH] == Decimal("1.00")
    assert obs[1].factor_values[R.FACTOR_RACE_FINISH] == Decimal("0.00")
    assert (
        obs[0].factor_values[R.FACTOR_RACE_FINISH]
        > obs[1].factor_values[R.FACTOR_RACE_FINISH]
    )


def test_every_observation_is_within_the_unit_interval():
    """
    The engine clips at max_contribution, which only means anything if
    observations arrive on a shared [0, 1] scale. form_trend is signed.
    """
    obs = observe(
        [
            result(
                driver_id=1,
                finish_position=1,
                grid_position=1,
                fastest_lap=True,
                driver_of_day=True,
                incident_points=Decimal("99"),
            )
        ]
    )
    for code, value in obs[0].factor_values.items():
        if code == R.FACTOR_FORM_TREND:
            assert Decimal(-1) <= value <= Decimal(1), code
        else:
            assert Decimal(0) <= value <= Decimal(1), code


def test_points_are_a_share_of_the_maximum_not_a_raw_total():
    """
    A 25-point win and a 1-point P10 must be distinguishable. Fed raw,
    both clipped to the same per-factor cap and the factor carried no
    information at all.
    """
    obs = observe([result(driver_id=1, finish_position=1),
                   result(driver_id=2, finish_position=10)])
    assert obs[0].factor_values[R.FACTOR_POINTS] == Decimal(1)
    assert obs[1].factor_values[R.FACTOR_POINTS] == Decimal(1) / Decimal(25)
    assert obs[0].factor_values[R.FACTOR_POINTS] != obs[1].factor_values[R.FACTOR_POINTS]


# ── Flags come from data, not thresholds ────────────────────────────────


def test_win_podium_and_pole_flags_follow_the_curve_rows():
    obs = observe([result(finish_position=3, grid_position=1)])
    values = obs[0].factor_values
    assert values[R.FACTOR_WINS] == Decimal(0)
    assert values[R.FACTOR_PODIUMS] == Decimal(1)
    assert values[R.FACTOR_POLES] == Decimal(1)


def test_quali_observation_uses_the_grid_column_not_the_finish():
    obs = observe([result(finish_position=20, grid_position=2)])
    assert obs[0].factor_values[R.FACTOR_QUALI_FINISH] == Decimal("0.90")
    assert obs[0].factor_values[R.FACTOR_RACE_FINISH] == Decimal("0.00")


def test_missing_grid_gives_a_neutral_quali_observation():
    """A league that does not record grids must not be penalised for it."""
    obs = observe([result(finish_position=1)])
    assert obs[0].factor_values[R.FACTOR_QUALI_FINISH] == Decimal(0)
    assert obs[0].factor_values[R.FACTOR_POLES] == Decimal(0)


# ── Retirements ─────────────────────────────────────────────────────────


def test_dnf_scores_zero_race_and_points_and_raises_the_dnf_factor():
    obs = observe([result(finish_position=None, grid_position=1, dnf=True)])
    values = obs[0].factor_values
    assert values[R.FACTOR_RACE_FINISH] == Decimal(0)
    assert values[R.FACTOR_POINTS] == Decimal(0)
    assert values[R.FACTOR_DNF] == Decimal(1)
    # Qualifying still happened, so the pole still counts.
    assert values[R.FACTOR_POLES] == Decimal(1)


def test_a_dnf_from_a_winning_position_still_scores_no_points():
    """
    Guards against a sheet that records both a finishing position and a
    retirement — the retirement must win, or a driver who broke down on
    the last lap banks a full win.
    """
    obs = observe([result(finish_position=1, dnf=True)])
    values = obs[0].factor_values
    assert values[R.FACTOR_WINS] == Decimal(0)
    assert values[R.FACTOR_PODIUMS] == Decimal(0)
    assert values[R.FACTOR_POINTS] == Decimal(0)
    assert values[R.FACTOR_RACE_FINISH] == Decimal(0)


def test_dns_is_not_counted_as_a_retirement():
    """A no-show is an absence, not a mechanical failure."""
    obs = observe([result(finish_position=None, dns=True)])
    assert obs[0].factor_values[R.FACTOR_DNF] == Decimal(0)
    assert obs[0].factor_values[R.FACTOR_RACE_FINISH] == Decimal(0)


def test_position_beyond_the_curve_scores_zero_rather_than_raising():
    """An oversized field must not take down a whole valuation run."""
    obs = observe([result(finish_position=99)])
    assert obs[0].factor_values[R.FACTOR_RACE_FINISH] == Decimal(0)


# ── Incidents ───────────────────────────────────────────────────────────


def test_incidents_are_scaled_against_the_configured_maximum():
    obs = observe([result(finish_position=5, incident_points=Decimal(3))])
    assert obs[0].factor_values[R.FACTOR_INCIDENTS] == Decimal("0.5")


def test_incidents_above_the_maximum_clamp_to_one():
    obs = observe([result(finish_position=5, incident_points=Decimal(50))])
    assert obs[0].factor_values[R.FACTOR_INCIDENTS] == Decimal(1)


def test_zero_incident_scale_is_not_a_division_error():
    """A league that disables incident tracking sets the scale to zero."""
    obs = R.build_observations(
        current=[result(finish_position=5, incident_points=Decimal(3))],
        history={},
        position_scores=CURVE,
        tuning=R.ResultsTuning(3, 3, Decimal(0)),
    )
    assert obs[0].factor_values[R.FACTOR_INCIDENTS] == Decimal(0)


# ── Form trend ──────────────────────────────────────────────────────────


def test_form_trend_is_neutral_without_a_prior_baseline():
    """A driver's first rounds must read as neutral, not as a collapse."""
    history = [result(round_order=1, finish_position=1)]
    assert R.compute_form_trend(history, SCORES, 3) == Decimal(0)


def test_form_trend_is_positive_when_recent_rounds_are_stronger():
    history = [
        result(round_order=1, finish_position=20),
        result(round_order=2, finish_position=20),
        result(round_order=3, finish_position=1),
        result(round_order=4, finish_position=1),
        result(round_order=5, finish_position=1),
    ]
    # Recent 3 mean 1.00, prior 2 mean 0.00.
    assert R.compute_form_trend(history, SCORES, 3) == Decimal(1)


def test_form_trend_is_negative_when_recent_rounds_are_weaker():
    history = [
        result(round_order=1, finish_position=1),
        result(round_order=2, finish_position=1),
        result(round_order=3, finish_position=20),
        result(round_order=4, finish_position=20),
        result(round_order=5, finish_position=20),
    ]
    assert R.compute_form_trend(history, SCORES, 3) == Decimal(-1)


def test_form_trend_ignores_the_order_rows_arrive_in():
    """Sheet row order must never change a valuation."""
    rounds = [
        result(round_order=1, finish_position=20),
        result(round_order=2, finish_position=20),
        result(round_order=3, finish_position=1),
        result(round_order=4, finish_position=1),
        result(round_order=5, finish_position=1),
    ]
    assert R.compute_form_trend(rounds, SCORES, 3) == R.compute_form_trend(
        list(reversed(rounds)), SCORES, 3
    )


def test_a_zero_form_window_disables_the_factor():
    history = [result(round_order=i, finish_position=1) for i in range(1, 6)]
    assert R.compute_form_trend(history, SCORES, 0) == Decimal(0)


# ── Consistency ─────────────────────────────────────────────────────────


def test_identical_results_are_perfectly_consistent():
    history = [result(round_order=i, finish_position=5) for i in range(1, 4)]
    assert R.compute_consistency(history, SCORES, 3) == Decimal(1)


def test_alternating_results_are_less_consistent_than_steady_ones():
    swinging = [
        result(round_order=1, finish_position=1),
        result(round_order=2, finish_position=20),
        result(round_order=3, finish_position=1),
    ]
    steady = [
        result(round_order=1, finish_position=4),
        result(round_order=2, finish_position=5),
        result(round_order=3, finish_position=4),
    ]
    assert R.compute_consistency(swinging, SCORES, 3) < R.compute_consistency(
        steady, SCORES, 3
    )


def test_a_single_round_is_not_treated_as_perfect_consistency():
    """One data point has nothing to vary; a free 1.0 would be a payout."""
    assert R.compute_consistency(
        [result(round_order=1, finish_position=1)], SCORES, 3
    ) == Decimal(0)


def test_consistency_stays_exact_decimal():
    """
    MAD rather than standard deviation, specifically so no float ever
    enters a path that ends in a money value.
    """
    history = [
        result(round_order=1, finish_position=1),
        result(round_order=2, finish_position=20),
    ]
    value = R.compute_consistency(history, SCORES, 3)
    assert isinstance(value, Decimal)
    assert value == Decimal("0.5")


def test_consistency_only_looks_at_its_window():
    history = [
        result(round_order=1, finish_position=20),
        result(round_order=2, finish_position=5),
        result(round_order=3, finish_position=5),
        result(round_order=4, finish_position=5),
    ]
    # Window of 3 excludes the P20, leaving three identical results.
    assert R.compute_consistency(history, SCORES, 3) == Decimal(1)


# ── Exceptional weekend ─────────────────────────────────────────────────


def test_a_clean_sweep_is_an_exceptional_weekend():
    assert R.is_exceptional_weekend(
        result(finish_position=1, grid_position=1, fastest_lap=True), SCORES
    )


@pytest.mark.parametrize(
    "kw",
    [
        {"finish_position": 1, "grid_position": 2, "fastest_lap": True},
        {"finish_position": 2, "grid_position": 1, "fastest_lap": True},
        {"finish_position": 1, "grid_position": 1, "fastest_lap": False},
        {"finish_position": None, "grid_position": 1, "fastest_lap": True},
    ],
)
def test_a_partial_sweep_is_not_exceptional(kw):
    """
    The wider exceptional_move_cap is meant to be rare. Anything short of
    pole, win, and fastest lap uses the normal weekly cap.
    """
    assert not R.is_exceptional_weekend(result(**kw), SCORES)


def test_exceptional_flag_is_carried_onto_the_observation():
    obs = observe(
        [result(driver_id=1, finish_position=1, grid_position=1, fastest_lap=True),
         result(driver_id=2, finish_position=2, grid_position=2)]
    )
    assert obs[0].exceptional is True
    assert obs[1].exceptional is False


# ── build_observations contract ─────────────────────────────────────────


def test_output_order_matches_input_order():
    """The cog zips these against its driver list, so order is load-bearing."""
    current = [result(driver_id=i, finish_position=i) for i in (5, 1, 3, 2)]
    obs = observe(current)
    assert [o.driver_id for o in obs] == [5, 1, 3, 2]


def test_every_factor_code_is_populated_for_every_driver():
    """
    A missing key means the engine silently scores that factor zero —
    the exact class of failure that made the market inert.
    """
    expected = {
        R.FACTOR_RACE_FINISH, R.FACTOR_QUALI_FINISH, R.FACTOR_POINTS,
        R.FACTOR_WINS, R.FACTOR_PODIUMS, R.FACTOR_POLES,
        R.FACTOR_FASTEST_LAPS, R.FACTOR_DRIVER_OF_DAY, R.FACTOR_DNF,
        R.FACTOR_INCIDENTS, R.FACTOR_FORM_TREND, R.FACTOR_CONSISTENCY,
    }
    obs = observe([result(finish_position=1), result(driver_id=2, dnf=True)])
    for o in obs:
        assert set(o.factor_values) == expected


def test_observations_are_deterministic():
    """A dry-run preview is only a preview if it predicts the publish."""
    current = [result(driver_id=1, finish_position=1, grid_position=2)]
    history = {1: [result(round_order=i, finish_position=i) for i in range(1, 5)]}
    first = observe(current, history)
    second = observe(current, history)
    assert first[0].factor_values == second[0].factor_values
    assert first[0].exceptional == second[0].exceptional


def test_an_empty_round_produces_no_observations():
    assert observe([]) == []


def test_history_is_per_driver():
    """One driver's form must never leak into another's."""
    current = [result(driver_id=1, finish_position=5),
               result(driver_id=2, finish_position=5)]
    history = {
        1: [result(round_order=i, driver_id=1, finish_position=20) for i in range(1, 5)]
        + [result(round_order=5, driver_id=1, finish_position=5)],
        2: [result(round_order=5, driver_id=2, finish_position=5)],
    }
    obs = observe(current, history)
    by_id = {o.driver_id: o for o in obs}
    # Driver 1 has a climbing trend; driver 2 has no baseline at all.
    assert by_id[1].factor_values[R.FACTOR_FORM_TREND] > Decimal(0)
    assert by_id[2].factor_values[R.FACTOR_FORM_TREND] == Decimal(0)
