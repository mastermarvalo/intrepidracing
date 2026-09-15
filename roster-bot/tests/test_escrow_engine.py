"""
The escrow arithmetic, tested as pure functions.

These cover the money rules a league owner was told to expect, the two
policy edges the engine settles on its own (pro-rated early P/L and the
"never lose more than the escrow" floor), and the boundaries where a
naive implementation would silently do the wrong thing: an unpublished
valuation, a rate of zero, a term shorter than the league minimum, and
rounding that must never invent or destroy money.
"""

from decimal import Decimal

import pytest

from bot.market import escrow

# ── per-race share ───────────────────────────────────────────────────


def test_a_season_rate_divides_across_the_season():
    """The headline example from the owner's runbook."""
    assert escrow.per_race_share(Decimal("24.00"), 24) == Decimal("1.00")


def test_share_rounds_to_cents():
    assert escrow.per_race_share(Decimal("23.75"), 24) == Decimal("0.99")


def test_a_single_race_season_charges_the_whole_rate():
    assert escrow.per_race_share(Decimal("24.00"), 1) == Decimal("24.00")


def test_zero_races_per_season_is_refused_not_divided_by():
    with pytest.raises(ValueError, match="at least 1"):
        escrow.per_race_share(Decimal("24.00"), 0)


def test_rounding_drift_is_never_more_than_a_cent_a_race():
    """
    Drift is acceptable only because a settlement returns what was
    actually charged. This pins the size of it so a future change to the
    rounding boundary cannot quietly widen the gap.
    """
    share = escrow.per_race_share(Decimal("23.75"), 24)
    charged = share * Decimal(24)
    assert abs(charged - Decimal("23.75")) <= Decimal("0.01") * Decimal(24)


# ── price floor ──────────────────────────────────────────────────────


def _floor(**overrides):
    kwargs = {
        "base_value": Decimal("20.00"),
        "term_races": 36,
        "min_term_races": 5,
        "length_premium_pct": Decimal("0.005"),
        "resign_premium_pct": Decimal("0.150"),
        "is_resign": False,
        "min_salary": Decimal("1.00"),
    }
    kwargs.update(overrides)
    return escrow.price_floor(**kwargs)


def test_rival_team_pays_only_the_length_premium():
    """20.00 × (1 + 0.005 × 31) = 23.10 — the runbook's worked example."""
    assert _floor() == Decimal("23.10")


def test_the_drivers_own_team_pays_the_resign_premium_on_top():
    """20.00 × 1.155 × 1.15 = 26.57 — keeping your own driver costs more."""
    assert _floor(is_resign=True) == Decimal("26.57")


def test_a_minimum_length_deal_pays_no_length_premium():
    assert _floor(term_races=5) == Decimal("20.00")


def test_a_term_below_the_league_minimum_never_discounts():
    """
    Term bounds are enforced elsewhere; the floor must not go *below* the
    market value just because it was handed a short term.
    """
    assert _floor(term_races=1) == Decimal("20.00")


def test_both_premiums_at_zero_reproduce_the_old_behaviour():
    floor = _floor(
        length_premium_pct=Decimal("0"),
        resign_premium_pct=Decimal("0"),
        is_resign=True,
    )
    assert floor == Decimal("20.00")


def test_the_league_salary_floor_still_applies_to_a_cheap_driver():
    assert _floor(base_value=Decimal("0.10"), term_races=5) == Decimal("1.00")


def test_an_unpriced_driver_falls_back_to_the_league_floor():
    """
    No published valuation means no market value to derive a floor from.
    It must not raise, and must not pretend the driver is worth zero.
    """
    assert _floor(base_value=None) == Decimal("1.00")


def test_the_resign_premium_compounds_on_the_length_premium():
    """Multiplicative, not additive — 1.155 × 1.15, never 1 + 0.155 + 0.15."""
    rival = _floor()
    own = _floor(is_resign=True)
    assert own > rival * Decimal("1.14")
    assert own < rival * Decimal("1.16")


# ── settlement ───────────────────────────────────────────────────────


def _settle(**overrides):
    kwargs = {
        "amount_held": Decimal("36.00"),
        "market_value": Decimal("27.50"),
        "contract_value": Decimal("24.00"),
        "races_served": 36,
        "term_races": 36,
    }
    kwargs.update(overrides)
    return escrow.settle(**kwargs)


def test_a_full_term_returns_escrow_plus_pl():
    """The runbook example: 36.00 held, driver up 3.50, so 39.50 back."""
    result = _settle()
    assert result.pl == Decimal("3.50")
    assert result.pl_applied == Decimal("3.50")
    assert result.amount_returned == Decimal("39.50")
    assert result.is_full_term
    assert not result.clamped


def test_a_declining_driver_costs_the_team_the_difference():
    result = _settle(market_value=Decimal("21.00"))
    assert result.pl == Decimal("-3.00")
    assert result.amount_returned == Decimal("33.00")


def test_a_driver_exactly_on_his_number_returns_the_escrow():
    result = _settle(market_value=Decimal("24.00"))
    assert result.pl == Decimal("0.00")
    assert result.amount_returned == Decimal("36.00")


def test_an_unpriced_driver_returns_only_the_escrow_with_pl_unknown():
    """
    None, not zero: "no valuation was ever published" and "landed exactly
    on his contract number" are different facts and must stay different.
    """
    result = _settle(market_value=None)
    assert result.pl is None
    assert result.pl_applied is None
    assert result.amount_returned == Decimal("36.00")


def test_an_early_exit_pro_rates_the_pl_by_races_served():
    """
    Half a term served carries half the P/L. The full-term figure is
    still reported so a receipt can show both.
    """
    result = _settle(races_served=18, amount_held=Decimal("18.00"))
    assert result.pl == Decimal("3.50")
    assert result.pl_applied == Decimal("1.75")
    assert result.amount_returned == Decimal("19.75")
    assert not result.is_full_term


def test_an_exit_after_one_race_barely_moves_the_pl():
    """
    The reason pro-rating exists: without it this settlement would carry
    a full season's P/L for a single race of service.
    """
    result = _settle(races_served=1, amount_held=Decimal("1.00"))
    assert result.pl_applied == Decimal("0.10")
    assert result.amount_returned == Decimal("1.10")


def test_a_team_never_loses_more_than_it_escrowed():
    """
    A collapsed valuation early in a short term would otherwise produce a
    negative return — a settlement billing the team money it never set
    aside. The loss is capped at the escrow and the cap is recorded.
    """
    result = escrow.settle(
        amount_held=Decimal("1.00"),
        market_value=Decimal("0.00"),
        contract_value=Decimal("24.00"),
        races_served=1,
        term_races=12,
    )
    assert result.amount_returned == Decimal("0.00")
    assert result.clamped is True
    assert result.pl == Decimal("-24.00"), "the real P/L is still on the record"


def test_an_uncapped_loss_is_not_flagged_as_capped():
    result = _settle(market_value=Decimal("21.00"))
    assert result.clamped is False


def test_over_served_term_settles_as_a_full_term():
    """
    An admin shortening a term after races were run must not produce a
    pro-rated fraction above 1, which would over-pay the P/L.
    """
    result = _settle(races_served=40, term_races=36)
    assert result.pl_applied == result.pl
    assert result.amount_returned == Decimal("39.50")


def test_zero_races_served_returns_the_escrow_with_no_pl_applied():
    result = _settle(races_served=0, amount_held=Decimal("0.00"))
    assert result.pl_applied == Decimal("0.00")
    assert result.amount_returned == Decimal("0.00")


def test_a_zero_race_term_is_refused():
    with pytest.raises(ValueError, match="at least 1"):
        _settle(term_races=0)


def test_negative_races_served_is_refused():
    with pytest.raises(ValueError, match="cannot be negative"):
        _settle(races_served=-1)


# ── term progress ────────────────────────────────────────────────────


def test_races_remaining_counts_down():
    assert escrow.races_remaining(term_races=36, races_served=14) == 22


def test_races_remaining_never_goes_negative():
    assert escrow.races_remaining(term_races=36, races_served=40) == 0


def test_a_term_is_complete_on_its_final_race():
    assert escrow.is_term_complete(term_races=24, races_served=24)


def test_a_term_is_not_complete_one_race_short():
    assert not escrow.is_term_complete(term_races=24, races_served=23)


def test_an_over_served_term_is_complete():
    assert escrow.is_term_complete(term_races=24, races_served=25)


def test_carry_over_moves_the_remainder_not_a_fresh_term():
    """A 36-race deal 24 races in arrives owing 12, not another 36."""
    assert escrow.carried_term_races(term_races=36, races_served=24) == 12


def test_a_finished_term_carries_nothing():
    assert escrow.carried_term_races(term_races=24, races_served=24) == 0
