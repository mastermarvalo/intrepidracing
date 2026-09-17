"""
The race term an offer is validated against must be the race term it
will be stored with.

Two bugs lived here, and both were invisible to the existing suite
because every other test builds `rules.OfferInputs` by hand with
`term_races` already set. Only the cog omitted it.

1. `bot/cogs/contracts.py` passed `term_seasons` but never `term_races`,
   so both race-bounds rules saw the dataclass default of 1. On a league
   seeded from the F1 preset (`min_term_races` 5) that blocked EVERY
   contract offer with "Term of 1 race(s) is below the league minimum of
   5" — a figure the TP never typed, in a unit the form never showed.

2. The preset then advertised `max_term_seasons` 3 while capping
   `max_term_races` at 48, so the longest term the league has always
   allowed was refused as too long.

The bot had never been deployed, so neither had ever been hit live.
"""

from decimal import Decimal

import pytest

from bot.cogs.contracts import (
    _FALLBACK_RACES,
    _offer_term_races,
    _seasons_for_races,
    parse_term,
)
from bot.contracts import rules
from bot.presets.f1 import _DEFAULT_LEAGUE_CONFIG as F1


class _Cfg:
    """Only the attribute `_offer_term_races` reads."""

    def __init__(self, races_per_season):
        self.races_per_season = races_per_season


def _inputs(*, term_seasons, cfg, **over):
    """An otherwise-valid TP offer, built the way the cog builds it."""
    fields = dict(
        actor_id=1,
        actor_is_principal=True,
        actor_is_admin=False,
        driver_present_in_tier=True,
        driver_status="free_agent",
        driver_has_active_contract=False,
        duplicate_open_offer_exists=False,
        salary=Decimal("12.50"),
        min_salary=Decimal("0.50"),
        signing_bonus=Decimal("0"),
        incentives_amount=Decimal("0"),
        max_incentive_pct=Decimal("0.25"),
        max_salary=Decimal("60.00"),
        team_payroll_before=Decimal("0"),
        salary_cap=Decimal("145.00"),
        active_slots_used=0,
        active_slots_max=2,
        term_seasons=term_seasons,
        min_term_seasons=F1["min_term_seasons"],
        max_term_seasons=F1["max_term_seasons"],
        term_races=_offer_term_races(cfg, term_seasons),
        min_term_races=F1["min_term_races"],
        max_term_races=F1["max_term_races"],
        offer_kind="new",
        free_agency_open=True,
    )
    fields.update(over)
    return rules.OfferInputs(**fields)


def _blockers(validation):
    return [r.code for r in validation.results if not r.ok]


# ── the conversion ───────────────────────────────────────────────────


def test_a_season_term_converts_at_the_league_calendar():
    assert _offer_term_races(_Cfg(24), 1) == 24
    assert _offer_term_races(_Cfg(24), 2) == 48
    assert _offer_term_races(_Cfg(12), 2) == 24


def test_a_missing_calendar_falls_back_the_same_way_queries_does():
    # queries.derive_term_races COALESCEs to 24. If these disagreed, an
    # offer could validate against one term and be stored with another.
    assert _offer_term_races(_Cfg(None), 2) == 2 * _FALLBACK_RACES
    assert _offer_term_races(object(), 2) == 2 * _FALLBACK_RACES


def test_the_conversion_never_returns_less_than_one_race():
    # contracts.term_races carries a CHECK; a zero term must not reach it.
    assert _offer_term_races(_Cfg(24), 0) == 1


# ── bug 1: the default-of-1 blocker ──────────────────────────────────


async def test_a_normal_offer_is_not_blocked_by_the_race_minimum():
    validation = rules.validate_offer(
        _inputs(term_seasons=2, cfg=_Cfg(24))
    )
    assert "term_races_below_minimum" not in _blockers(validation)
    assert validation.ok, _blockers(validation)


async def test_omitting_the_race_term_is_what_used_to_block_everything():
    # Reproduces the exact pre-fix call: term_seasons set, term_races not.
    # Kept as a guard so anyone dropping the argument sees this fail.
    stale = rules.OfferInputs(
        actor_id=1,
        actor_is_principal=True,
        actor_is_admin=False,
        driver_present_in_tier=True,
        driver_status="free_agent",
        driver_has_active_contract=False,
        duplicate_open_offer_exists=False,
        salary=Decimal("12.50"),
        min_salary=Decimal("0.50"),
        signing_bonus=Decimal("0"),
        incentives_amount=Decimal("0"),
        max_incentive_pct=Decimal("0.25"),
        max_salary=Decimal("60.00"),
        salary_cap=Decimal("145.00"),
        active_slots_max=2,
        term_seasons=2,
        min_term_seasons=F1["min_term_seasons"],
        max_term_seasons=F1["max_term_seasons"],
        min_term_races=F1["min_term_races"],
        max_term_races=F1["max_term_races"],
    )
    assert stale.term_races == 1
    assert "term_races_below_minimum" in _blockers(
        rules.validate_offer(stale)
    )


# ── bug 2: preset self-consistency ───────────────────────────────────


def test_the_f1_preset_race_ceiling_matches_its_season_ceiling():
    assert (
        F1["max_term_races"]
        == F1["max_term_seasons"] * F1["races_per_season"]
    )


def test_the_f1_preset_race_floor_is_reachable():
    # A floor above one full season would block the shortest term the
    # season bounds permit.
    assert F1["min_term_races"] <= (
        F1["min_term_seasons"] * F1["races_per_season"]
    )


@pytest.mark.parametrize("seasons", [1, 2, 3])
async def test_every_term_the_season_bounds_allow_is_actually_offerable(
    seasons,
):
    validation = rules.validate_offer(
        _inputs(term_seasons=seasons, cfg=_Cfg(F1["races_per_season"]))
    )
    assert validation.ok, (
        f"{seasons}-season term rejected: {_blockers(validation)}"
    )


async def test_a_term_past_the_season_ceiling_is_still_refused():
    # The fix must not have loosened the ceiling, only aligned it.
    validation = rules.validate_offer(
        _inputs(
            term_seasons=F1["max_term_seasons"] + 1,
            cfg=_Cfg(F1["races_per_season"]),
        )
    )
    assert not validation.ok
    codes = _blockers(validation)
    assert "term_too_long" in codes
    assert "term_races_too_long" in codes


async def test_the_race_rule_is_never_looser_than_the_season_rule():
    """
    Races are the authoritative term; seasons are derived as the span a
    term touches. So anything the race rule ACCEPTS the season rule must
    also accept, or a TP would be told a term is legal in the unit they
    typed and illegal in a unit they never saw.

    The converse is allowed and intended: a 1-race term satisfies the
    season bounds (it touches one season) while the race minimum
    rightly refuses it.
    """
    cfg = _Cfg(F1["races_per_season"])
    for races in range(1, F1["max_term_races"] + F1["races_per_season"]):
        results = {
            r.code: r.ok
            for r in rules.validate_offer(
                _inputs(
                    term_seasons=_seasons_for_races(cfg, races),
                    cfg=cfg,
                    term_races=races,
                )
            ).results
        }
        if results.get("term_races_within_bounds"):
            assert results.get("term_within_bounds"), (
                f"{races} races passes the race rule but fails the "
                "season rule"
            )


# ── offering a term in races (migration 018) ─────────────────────────


def test_a_race_term_is_read_as_races():
    cfg = _Cfg(24)
    assert parse_term(cfg, "10") == (10, 1)
    assert parse_term(cfg, "10 races") == (10, 1)
    assert parse_term(cfg, "10r") == (10, 1)


def test_a_season_term_is_still_accepted_as_a_convenience():
    cfg = _Cfg(24)
    assert parse_term(cfg, "2s") == (48, 2)
    assert parse_term(cfg, "2 seasons") == (48, 2)
    assert parse_term(cfg, "2 season") == (48, 2)


def test_races_is_not_mistaken_for_a_season_suffix():
    # "races" ends in "s". Stripping season suffixes first turned
    # "5 races" into five SEASONS -- a 120-race deal from a TP who
    # asked for five races.
    cfg = _Cfg(24)
    assert parse_term(cfg, "5 races") == (5, 1)
    assert parse_term(cfg, "1 race") == (1, 1)


def test_the_term_entry_is_case_and_space_insensitive():
    cfg = _Cfg(24)
    assert parse_term(cfg, "  3S  ") == (72, 3)
    assert parse_term(cfg, "2 Seasons") == (48, 2)
    assert parse_term(cfg, " 12 ") == (12, 1)


@pytest.mark.parametrize(
    "raw", ["", "   ", "abc", "0", "-3", "two", "s", "races"]
)
def test_an_unreadable_term_is_refused_with_a_message_for_the_tp(raw):
    with pytest.raises(ValueError) as caught:
        parse_term(_Cfg(24), raw)
    assert str(caught.value)


def test_the_derived_season_span_rounds_up():
    # A 30-race deal runs into a second season. Reporting one season
    # would quote it against a single season's salary.
    cfg = _Cfg(24)
    assert _seasons_for_races(cfg, 24) == 1
    assert _seasons_for_races(cfg, 25) == 2
    assert _seasons_for_races(cfg, 30) == 2
    assert _seasons_for_races(cfg, 48) == 2
    assert _seasons_for_races(cfg, 49) == 3


def test_a_short_race_deal_is_offerable_and_legal():
    # The whole point: the F1 preset allows a 5-race minimum, and before
    # migration 018 the shortest offerable term was a full season.
    cfg = _Cfg(F1["races_per_season"])
    races, seasons = parse_term(cfg, str(F1["min_term_races"]))
    assert races == F1["min_term_races"]
    validation = rules.validate_offer(
        _inputs(term_seasons=seasons, cfg=cfg, term_races=races)
    )
    assert validation.ok, _blockers(validation)


def test_a_term_below_the_race_minimum_is_still_refused():
    cfg = _Cfg(F1["races_per_season"])
    races = F1["min_term_races"] - 1
    validation = rules.validate_offer(
        _inputs(
            term_seasons=_seasons_for_races(cfg, races),
            cfg=cfg,
            term_races=races,
        )
    )
    assert "term_races_below_minimum" in _blockers(validation)


def test_a_term_past_the_race_ceiling_is_refused():
    cfg = _Cfg(F1["races_per_season"])
    races = F1["max_term_races"] + 1
    validation = rules.validate_offer(
        _inputs(
            term_seasons=_seasons_for_races(cfg, races),
            cfg=cfg,
            term_races=races,
        )
    )
    assert "term_races_too_long" in _blockers(validation)
