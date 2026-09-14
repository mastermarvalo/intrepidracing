"""
Valuation engine invariants: determinism, movement-cap symmetry,
per-factor caps, tier isolation, Decimal in/out, ranking stability.

The engine is a pure function over plain data; these tests never touch
the database.
"""

from decimal import Decimal

import pytest

from bot.market.valuation import (
    DriverInput,
    FactorWeight,
    MovementCaps,
    breakdown_to_json,
    compute_run,
)

# ── shared fixtures ──────────────────────────────────────────────────


def _factors():
    return [
        FactorWeight(code="wins", weight=Decimal("1.0"), max_contribution=Decimal("0.60")),
        FactorWeight(code="dnf", weight=Decimal("-0.5"), max_contribution=Decimal("0.40")),
        FactorWeight(code="poles", weight=Decimal("0.4"), max_contribution=None),
    ]


def _standard_caps():
    return MovementCaps(weekly=Decimal("0.75"), exceptional=Decimal("1.25"))


def _driver(driver_id: int, prev: str, **factor_values):
    return DriverInput(
        driver_id=driver_id,
        display_name=f"Driver {driver_id}",
        previous_value=Decimal(prev),
        factor_values={k: Decimal(str(v)) for k, v in factor_values.items()},
    )


# ── determinism ──────────────────────────────────────────────────────


def test_same_inputs_always_produce_same_outputs():
    drivers = [
        _driver(1, "20.00", wins=1, poles=1),
        _driver(2, "15.00", dnf=1),
        _driver(3, "18.50", wins=0, poles=0),
    ]
    first = compute_run(_factors(), drivers, _standard_caps())
    second = compute_run(_factors(), drivers, _standard_caps())
    assert first == second


def test_ranking_ties_broken_deterministically_by_driver_id():
    # Two drivers with identical previous_value and no factor
    # observations should end up in driver_id order.
    drivers = [
        _driver(9, "10.00"),
        _driver(3, "10.00"),
        _driver(7, "10.00"),
    ]
    result = compute_run(_factors(), drivers, _standard_caps())
    assert [v.driver_id for v in result] == [3, 7, 9]
    assert [v.rank_in_tier for v in result] == [1, 2, 3]


# ── movement caps ────────────────────────────────────────────────────


def test_weekly_move_cap_clips_positive_delta():
    # A dominant winner with uncapped factors would move by ~2.0; the
    # weekly cap (0.75) must clip it.
    factors = [FactorWeight(code="wins", weight=Decimal("2.0"), max_contribution=None)]
    result = compute_run(
        factors, [_driver(1, "20.00", wins=1)], _standard_caps()
    )
    assert result[0].delta == Decimal("0.75")
    assert result[0].market_value == Decimal("20.75")
    assert result[0].capped is True
    assert result[0].cap_applied == Decimal("0.75")


def test_weekly_move_cap_clips_negative_delta():
    factors = [FactorWeight(code="dnf", weight=Decimal("-2.0"), max_contribution=None)]
    result = compute_run(
        factors, [_driver(1, "20.00", dnf=1)], _standard_caps()
    )
    assert result[0].delta == Decimal("-0.75")
    assert result[0].market_value == Decimal("19.25")
    assert result[0].capped is True


def test_exceptional_flag_widens_the_cap_for_that_driver_only():
    factors = [FactorWeight(code="wins", weight=Decimal("2.0"), max_contribution=None)]
    caps = _standard_caps()
    normal = DriverInput(
        driver_id=1, display_name="A",
        previous_value=Decimal("20.00"),
        factor_values={"wins": Decimal("1")},
    )
    exceptional = DriverInput(
        driver_id=2, display_name="B",
        previous_value=Decimal("20.00"),
        factor_values={"wins": Decimal("1")},
        exceptional=True,
    )
    result = compute_run(factors, [normal, exceptional], caps)
    by_id = {v.driver_id: v for v in result}
    assert by_id[1].cap_applied == Decimal("0.75")
    assert by_id[1].delta == Decimal("0.75")  # weekly-capped
    assert by_id[2].cap_applied == Decimal("1.25")
    assert by_id[2].delta == Decimal("1.25")  # exceptional-capped
    assert by_id[1].capped and by_id[2].capped


def test_delta_within_cap_leaves_capped_false():
    factors = [FactorWeight(code="wins", weight=Decimal("0.3"), max_contribution=None)]
    result = compute_run(
        factors, [_driver(1, "20.00", wins=1)], _standard_caps()
    )
    assert result[0].delta == Decimal("0.30")
    assert result[0].capped is False


# ── per-factor caps ──────────────────────────────────────────────────


def test_max_contribution_clips_individual_factor():
    # A weight of 5 on wins=1 would be 5.00, but max_contribution=0.60
    # clips it to 0.60. The clipped flag on that breakdown row is True.
    factors = [FactorWeight(code="wins", weight=Decimal("5.0"), max_contribution=Decimal("0.60"))]
    result = compute_run(
        factors, [_driver(1, "20.00", wins=1)], _standard_caps()
    )
    row = result[0].breakdown[0]
    assert row.contribution == Decimal("0.60")
    assert row.clipped is True


def test_missing_factor_defaults_to_zero_without_penalty():
    factors = _factors()
    # Driver 1 has NO factor_values at all.
    result = compute_run(
        factors, [_driver(1, "20.00")], _standard_caps()
    )
    assert result[0].delta == Decimal("0.00")
    assert all(row.contribution == Decimal("0") for row in result[0].breakdown)


# ── tier isolation ───────────────────────────────────────────────────


def test_adding_a_driver_does_not_change_other_drivers_values():
    # Because the engine has no notion of tier and does no
    # cross-driver normalisation, adding a driver to the input can only
    # affect that driver's own row (and the ranks — but those are
    # nominal, not the value itself).
    drivers = [_driver(1, "20.00", wins=1), _driver(2, "15.00")]
    baseline = compute_run(_factors(), drivers, _standard_caps())
    baseline_values = {v.driver_id: v.market_value for v in baseline}

    with_new = compute_run(
        _factors(),
        drivers + [_driver(3, "25.00", wins=1, poles=1)],
        _standard_caps(),
    )
    for v in with_new:
        if v.driver_id in baseline_values:
            assert v.market_value == baseline_values[v.driver_id]


# ── Decimal in, Decimal out ──────────────────────────────────────────


def test_all_output_amounts_are_decimals_quantized_to_two_places():
    result = compute_run(
        _factors(),
        [_driver(1, "20.00", wins=1, poles=1)],
        _standard_caps(),
    )
    v = result[0]
    for amount in (v.previous_value, v.market_value, v.delta, v.cap_applied):
        assert isinstance(amount, Decimal)
    # market_value and delta go through the money boundary → 2 dp.
    assert v.market_value.as_tuple().exponent == -2
    assert v.delta.as_tuple().exponent == -2


def test_market_value_equals_previous_plus_delta_exactly():
    # After the internal recompute the persisted row is self-consistent.
    result = compute_run(
        _factors(),
        [_driver(1, "20.00", wins=1, poles=1)],
        _standard_caps(),
    )
    v = result[0]
    assert v.market_value == v.previous_value + v.delta


# ── breakdown serialisation ──────────────────────────────────────────


def test_breakdown_to_json_uses_strings_for_decimals():
    # `poles` has no max_contribution in _factors(), so the row will
    # not be individually clipped — good target for asserting the
    # not-clipped branch alongside the string serialisation.
    result = compute_run(
        _factors(),
        [_driver(1, "20.00", poles=1)],
        _standard_caps(),
    )
    js = breakdown_to_json(result[0].breakdown)
    row = next(r for r in js if r["code"] == "poles")
    assert isinstance(row["contribution"], str)
    assert isinstance(row["weight"], str)
    assert isinstance(row["raw_value"], str)
    assert row["max_contribution"] is None
    assert row["clipped"] is False


# ── ranking ──────────────────────────────────────────────────────────


def test_rank_in_tier_is_one_based_and_descends_by_value():
    drivers = [
        _driver(1, "10.00"),
        _driver(2, "30.00"),
        _driver(3, "20.00"),
    ]
    result = compute_run(_factors(), drivers, _standard_caps())
    assert [v.driver_id for v in result] == [2, 3, 1]
    assert [v.rank_in_tier for v in result] == [1, 2, 3]


# ── smoke: empty inputs ──────────────────────────────────────────────


def test_no_drivers_returns_empty_list():
    assert compute_run(_factors(), [], _standard_caps()) == []


@pytest.mark.parametrize("cap_field", ["weekly", "exceptional"])
def test_caps_symmetric_positive_and_negative(cap_field):
    caps = _standard_caps()
    cap = getattr(caps, cap_field)
    factors = [FactorWeight(code="x", weight=Decimal("10"), max_contribution=None)]
    exceptional = cap_field == "exceptional"

    up = DriverInput(
        driver_id=1, display_name="up",
        previous_value=Decimal("20.00"),
        factor_values={"x": Decimal("1")},
        exceptional=exceptional,
    )
    down = DriverInput(
        driver_id=2, display_name="down",
        previous_value=Decimal("20.00"),
        factor_values={"x": Decimal("-1")},
        exceptional=exceptional,
    )
    result = compute_run(factors, [up, down], caps)
    by_id = {v.driver_id: v for v in result}
    assert by_id[1].delta == cap
    assert by_id[2].delta == -cap
