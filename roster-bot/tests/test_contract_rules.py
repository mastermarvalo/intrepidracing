"""
One test per row of the CLAUDE.md §7 validation matrix, both the pass
and the fail path. Every failure asserts a stable failure code so the
render layer + downstream tooling can key off codes without breaking
when messages get reworded.
"""

from decimal import Decimal

from bot.contracts import rules


def _baseline_ok() -> rules.OfferInputs:
    """An input set that passes every rule — tests flip one field at a time."""
    return rules.OfferInputs(
        actor_id=100,
        actor_is_principal=True,
        actor_is_admin=False,
        driver_present_in_tier=True,
        driver_status="active",
        driver_has_active_contract=False,
        duplicate_open_offer_exists=False,
        salary=Decimal("5.00"),
        min_salary=Decimal("1.00"),
        max_salary=Decimal("50.00"),
        signing_bonus=Decimal("0"),
        incentives_amount=Decimal("0"),
        max_incentive_pct=Decimal("0.15"),
        team_payroll_before=Decimal("50.00"),
        salary_cap=Decimal("145.00"),
        active_slots_used=1,
        active_slots_max=2,
        has_linked_release=False,
        term_seasons=2,
        max_term_seasons=3,
        term_races=24,
        min_term_races=1,
        max_term_races=48,
        offer_kind="new",
        free_agency_open=True,
    )


# ── one-test-per-rule ────────────────────────────────────────────────


def test_baseline_passes_everything():
    v = rules.validate_offer(_baseline_ok())
    assert v.ok
    assert v.blockers == []


def test_actor_not_authorised_blocks():
    i = _baseline_ok()
    i = _replace(i, actor_is_principal=False, actor_is_admin=False)
    result = rules.actor_is_authorised(i)
    assert not result.ok
    assert result.code == "actor_not_authorised"
    v = rules.validate_offer(i)
    assert not v.ok


def test_admin_alone_authorises():
    i = _replace(_baseline_ok(), actor_is_principal=False, actor_is_admin=True)
    assert rules.actor_is_authorised(i).ok


def test_driver_missing_in_tier_blocks():
    i = _replace(_baseline_ok(), driver_present_in_tier=False)
    result = rules.driver_exists_in_tier(i)
    assert result.code == "driver_missing_in_tier" and not result.ok


def test_status_inactive_warns_not_blocks():
    i = _replace(_baseline_ok(), driver_status="inactive")
    result = rules.driver_status_permits_signing(i)
    assert result.code == "status_inactive_warning"
    assert result.severity == "warn"
    # A warning alone does not block submission.
    assert rules.validate_offer(i).ok


def test_status_suspended_blocks():
    i = _replace(_baseline_ok(), driver_status="suspended")
    result = rules.driver_status_permits_signing(i)
    assert result.code == "status_suspended" and not result.ok
    assert result.severity == "block"


def test_status_unknown_blocks():
    i = _replace(_baseline_ok(), driver_status="mystery")
    assert rules.driver_status_permits_signing(i).code == "status_unknown"


def test_active_contract_blocks_new_signing():
    i = _replace(_baseline_ok(), driver_has_active_contract=True)
    result = rules.no_active_contract_conflict(i)
    assert result.code == "active_contract_conflict"


def test_active_contract_allowed_via_extension():
    i = _replace(_baseline_ok(), driver_has_active_contract=True,
                 offer_kind="extension")
    assert rules.no_active_contract_conflict(i).ok


def test_active_contract_allowed_via_trade_and_sign():
    i = _replace(_baseline_ok(), driver_has_active_contract=True,
                 offer_kind="trade_and_sign")
    assert rules.no_active_contract_conflict(i).ok


def test_salary_below_min_blocks():
    i = _replace(_baseline_ok(), salary=Decimal("0.50"),
                 min_salary=Decimal("1.00"))
    r = rules.salary_within_bounds(i)
    assert r.code == "salary_below_min" and not r.ok


def test_salary_above_max_blocks():
    i = _replace(_baseline_ok(), salary=Decimal("60.00"),
                 max_salary=Decimal("50.00"))
    r = rules.salary_within_bounds(i)
    assert r.code == "salary_above_max"


def test_max_salary_none_is_uncapped():
    i = _replace(_baseline_ok(), salary=Decimal("1000.00"), max_salary=None,
                 team_payroll_before=Decimal("0"), salary_cap=Decimal("2000"))
    assert rules.salary_within_bounds(i).ok


def test_cap_exceeded_blocks_and_shows_arithmetic():
    i = _replace(_baseline_ok(),
                 team_payroll_before=Decimal("144.00"),
                 salary=Decimal("2.00"), signing_bonus=Decimal("0"))
    r = rules.cap_headroom_ok(i)
    assert r.code == "cap_exceeded"
    assert r.detail["committed_after"] == "146.00"
    assert r.detail["over_cap"] == "1.00"


def test_cap_signing_bonus_counts_against_cap():
    i = _replace(_baseline_ok(),
                 team_payroll_before=Decimal("144.00"),
                 salary=Decimal("0.50"), signing_bonus=Decimal("1.00"))
    r = rules.cap_headroom_ok(i)
    assert r.code == "cap_exceeded"


# ── budget vs cap: two different walls ─────────────────────────────


def test_budget_not_enforced_passes_when_none():
    r = rules.budget_headroom_ok(_replace(_baseline_ok(), team_budget=None))
    assert r.ok
    assert r.detail["enforced"] == "false"


def test_budget_exceeded_blocks_even_when_under_cap():
    # Payroll after = 52, cap 145 (fine), budget 51 (not fine).
    i = _replace(_baseline_ok(),
                 team_payroll_before=Decimal("50.00"),
                 salary=Decimal("2.00"), team_budget=Decimal("51.00"))
    assert rules.cap_headroom_ok(i).ok
    r = rules.budget_headroom_ok(i)
    assert r.code == "budget_exceeded"
    assert r.detail["committed_after"] == "52.00"
    assert r.detail["short_by"] == "1.00"
    assert not rules.validate_offer(i).ok


def test_rich_team_is_still_blocked_by_cap():
    # Budget 200 > cap 145: money is not the problem, the ceiling is.
    i = _replace(_baseline_ok(),
                 team_payroll_before=Decimal("144.00"),
                 salary=Decimal("2.00"), team_budget=Decimal("200.00"))
    assert rules.budget_headroom_ok(i).ok
    assert rules.cap_headroom_ok(i).code == "cap_exceeded"
    assert not rules.validate_offer(i).ok


def test_budget_signing_bonus_counts_against_budget():
    i = _replace(_baseline_ok(),
                 team_payroll_before=Decimal("50.00"),
                 salary=Decimal("0.50"), signing_bonus=Decimal("1.00"),
                 team_budget=Decimal("51.00"))
    assert rules.budget_headroom_ok(i).code == "budget_exceeded"


def test_negative_budget_blocks_any_signing():
    # Penalties pushed the team underwater; nothing fits until it earns back.
    i = _replace(_baseline_ok(),
                 team_payroll_before=Decimal("0"),
                 salary=Decimal("1.00"), team_budget=Decimal("-3.00"))
    assert rules.budget_headroom_ok(i).code == "budget_exceeded"


def test_no_seat_available_blocks():
    i = _replace(_baseline_ok(),
                 active_slots_used=2, active_slots_max=2,
                 has_linked_release=False, offer_kind="new")
    r = rules.seat_available(i)
    assert r.code == "no_seat_available"


def test_seat_free_via_linked_release():
    i = _replace(_baseline_ok(),
                 active_slots_used=2, active_slots_max=2,
                 has_linked_release=True, offer_kind="new")
    assert rules.seat_available(i).ok


def test_extension_needs_no_seat():
    i = _replace(_baseline_ok(),
                 active_slots_used=2, active_slots_max=2,
                 offer_kind="extension", driver_has_active_contract=True)
    assert rules.seat_available(i).ok


def test_term_too_short_blocks():
    i = _replace(_baseline_ok(), term_seasons=0)
    assert rules.term_within_bounds(i).code == "term_too_short"


def test_term_too_long_blocks():
    i = _replace(_baseline_ok(), term_seasons=10, max_term_seasons=3)
    assert rules.term_within_bounds(i).code == "term_too_long"


def test_incentives_over_cap_blocks():
    # 15% of $5M = $0.75M — so $1.00M exceeds.
    i = _replace(_baseline_ok(),
                 salary=Decimal("5.00"),
                 incentives_amount=Decimal("1.00"),
                 max_incentive_pct=Decimal("0.15"))
    assert rules.incentives_within_cap(i).code == "incentives_over_cap"


def test_incentives_negative_blocks():
    i = _replace(_baseline_ok(), incentives_amount=Decimal("-1.00"))
    assert rules.incentives_within_cap(i).code == "incentives_negative"


def test_free_agency_closed_blocks_new_signing():
    i = _replace(_baseline_ok(), free_agency_open=False, offer_kind="new")
    assert rules.free_agency_window_ok(i).code == "free_agency_closed"


def test_free_agency_not_gated_for_extension():
    i = _replace(_baseline_ok(), free_agency_open=False, offer_kind="extension",
                 driver_has_active_contract=True)
    assert rules.free_agency_window_ok(i).ok


def test_duplicate_pending_blocks():
    i = _replace(_baseline_ok(), duplicate_open_offer_exists=True)
    assert rules.no_duplicate_pending(i).code == "duplicate_pending_offer"


# ── aggregator ──────────────────────────────────────────────────────


def test_ok_flag_reflects_only_blockers_not_warnings():
    # Inactive is a warning; nothing else fails.
    i = _replace(_baseline_ok(), driver_status="inactive")
    v = rules.validate_offer(i)
    assert v.ok
    assert v.warnings and v.warnings[0].code == "status_inactive_warning"


def test_validation_to_json_is_serialisable():
    import json
    v = rules.validate_offer(_baseline_ok())
    js = v.to_json()
    dumped = json.dumps(js)  # must not raise
    assert "ok" in js
    assert "results" in js
    assert isinstance(dumped, str)


def test_price_floor_is_inert_until_premiums_are_configured():
    """
    Both premiums default to zero, which is how this league priced its
    first seven seasons. At zero the floor can only ever be the league
    salary minimum, so an offer at market value must pass — otherwise
    enabling Phase 9 would silently reprice every contract in the league.
    """
    at_market = _replace(
        _baseline_ok(),
        salary=Decimal("20.00"), driver_base_value=Decimal("20.00"),
        term_races=36, min_term_races=5,
    )
    v = rules.validate_offer(at_market)
    assert v.ok, [b.code for b in v.blockers]


def test_resigning_your_own_driver_costs_more_than_signing_a_rivals():
    """The anti-cycling rule: the same deal must cost the incumbent more."""
    common = dict(
        driver_base_value=Decimal("20.00"), term_races=36, min_term_races=5,
        length_premium_pct=Decimal("0.005"),
        resign_premium_pct=Decimal("0.150"),
    )
    rival = _replace(_baseline_ok(), salary=Decimal("23.10"),
                     is_resign=False, **common)
    incumbent = _replace(_baseline_ok(), salary=Decimal("23.10"),
                         is_resign=True, **common)

    assert rules.validate_offer(rival).ok
    blocked = rules.validate_offer(incumbent)
    assert not blocked.ok
    codes = [b.code for b in blocked.blockers]
    assert "salary_below_price_floor" in codes
    floor_note = next(
        r for r in blocked.results if r.code == "salary_below_price_floor"
    )
    assert floor_note.detail["price_floor"] == "26.57"
    assert "re-signing premium" in floor_note.message


def test_an_unpriced_driver_is_still_signable_but_says_so():
    """
    A driver enrolled mid-season has no valuation, so no floor can be
    derived. Blocking would make him unsignable; passing silently would
    imply a check that never happened.
    """
    unpriced = _replace(
        _baseline_ok(), salary=Decimal("5.00"), driver_base_value=None,
        term_races=36, min_term_races=5,
        resign_premium_pct=Decimal("0.150"),
    )
    v = rules.validate_offer(unpriced)
    assert v.ok
    note = next(r for r in v.results if r.code == "price_floor_unpriced")
    assert "not checked against a market value" in note.message


def test_every_documented_rule_code_is_reachable():
    # This test locks the codes rule_codes() advertises. Adding a new
    # rule requires either extending rule_codes() OR explicitly
    # documenting the omission, which prevents silent drift.
    documented = set(rules.rule_codes())
    # Trigger each failure once and collect the codes we saw.
    seen = set()
    scenarios = [
        _replace(_baseline_ok(), actor_is_principal=False),
        _replace(_baseline_ok(), driver_present_in_tier=False),
        _replace(_baseline_ok(), driver_status="inactive"),
        _replace(_baseline_ok(), driver_status="suspended"),
        _replace(_baseline_ok(), driver_status="mystery"),
        _replace(_baseline_ok(), driver_has_active_contract=True),
        _replace(_baseline_ok(), salary=Decimal("0.50"), min_salary=Decimal("1")),
        _replace(_baseline_ok(), salary=Decimal("60"), max_salary=Decimal("50")),
        _replace(_baseline_ok(),
                 team_payroll_before=Decimal("144"), salary=Decimal("2")),
        _replace(_baseline_ok(),
                 team_payroll_before=Decimal("50"), salary=Decimal("2"),
                 team_budget=Decimal("51")),
        _replace(_baseline_ok(),
                 active_slots_used=2, active_slots_max=2),
        _replace(_baseline_ok(), term_seasons=0),
        _replace(_baseline_ok(), term_seasons=1, min_term_seasons=2),
        _replace(_baseline_ok(), term_seasons=999, max_term_seasons=3),
        _replace(_baseline_ok(), incentives_amount=Decimal("-1")),
        _replace(_baseline_ok(),
                 salary=Decimal("5"), incentives_amount=Decimal("1"),
                 max_incentive_pct=Decimal("0.15")),
        _replace(_baseline_ok(), free_agency_open=False),
        _replace(_baseline_ok(), duplicate_open_offer_exists=True),
        _replace(_baseline_ok(), term_races=0),
        _replace(_baseline_ok(), term_races=1, min_term_races=5),
        _replace(_baseline_ok(), term_races=999, max_term_races=48),
        # Price floor: a $20 driver on a 36-race deal with a 5-race
        # minimum and both premiums live cannot be had for $5.
        _replace(_baseline_ok(),
                 salary=Decimal("5.00"), driver_base_value=Decimal("20.00"),
                 term_races=36, min_term_races=5,
                 length_premium_pct=Decimal("0.005"),
                 resign_premium_pct=Decimal("0.150"), is_resign=True),
    ]
    for scenario in scenarios:
        v = rules.validate_offer(scenario)
        for r in v.results:
            if not r.ok:
                seen.add(r.code)
    missing = documented - seen
    assert not missing, f"rule codes documented but not reached: {missing}"


# ── helpers ──────────────────────────────────────────────────────────


def _replace(inputs: rules.OfferInputs, **kw) -> rules.OfferInputs:
    """dataclass replace, avoiding an extra import at every call site."""
    from dataclasses import replace
    return replace(inputs, **kw)


# ── G18: cap adjustments move the ceiling ────────────────────────────
#
# `cap_adjustment` ledger rows had writers and no reader. A commissioner
# could dock a team's cap, see the row recorded, and watch the team keep
# spending to the full league ceiling — while two screens promised
# enforcement was "coming in Phase 5". There is no Phase 5.


def test_cap_headroom_is_unchanged_when_no_adjustment_exists():
    """The common case must behave exactly as it did before G18."""
    inputs = _replace(_baseline_ok(), 
        team_payroll_before=Decimal("140.00"), salary=Decimal("5.00")
    )
    result = rules.cap_headroom_ok(inputs)
    assert result.ok is True
    assert result.detail["effective_cap"] == "145.00"
    # No adjustment means no confusing arithmetic in the message.
    assert "effective cap" not in result.message


def test_a_granted_adjustment_allows_a_signing_the_cap_would_block():
    over = _replace(_baseline_ok(), 
        team_payroll_before=Decimal("144.00"), salary=Decimal("5.00")
    )
    assert rules.cap_headroom_ok(over).ok is False

    granted = _replace(_baseline_ok(), 
        team_payroll_before=Decimal("144.00"),
        salary=Decimal("5.00"),
        cap_adjustment=Decimal("10.00"),
    )
    result = rules.cap_headroom_ok(granted)
    assert result.ok is True
    assert result.detail["effective_cap"] == "155.00"
    assert "granted" in result.message


def test_a_docked_adjustment_blocks_a_signing_the_cap_would_allow():
    """The half that was actually dangerous: a penalty that did nothing."""
    fits = _replace(_baseline_ok(), 
        team_payroll_before=Decimal("140.00"), salary=Decimal("4.00")
    )
    assert rules.cap_headroom_ok(fits).ok is True

    docked = _replace(_baseline_ok(), 
        team_payroll_before=Decimal("140.00"),
        salary=Decimal("4.00"),
        cap_adjustment=Decimal("-10.00"),
    )
    result = rules.cap_headroom_ok(docked)
    assert result.ok is False
    assert result.code == "cap_exceeded"
    assert result.detail["effective_cap"] == "135.00"
    assert result.detail["over_cap"] == "9.00"


def test_a_docked_cap_says_it_was_docked_and_by_how_much():
    """
    "You are 9.00 over the 145.00 cap" would be arithmetic the TP cannot
    reproduce. The message has to name the adjustment.
    """
    result = rules.cap_headroom_ok(
        _replace(_baseline_ok(), 
            team_payroll_before=Decimal("140.00"),
            salary=Decimal("4.00"),
            cap_adjustment=Decimal("-10.00"),
        )
    )
    assert "135.00" in result.message
    assert "145.00" in result.message
    assert "10.00" in result.message
    assert "docked" in result.message


def test_the_signing_bonus_counts_against_the_adjusted_cap_too():
    result = rules.cap_headroom_ok(
        _replace(_baseline_ok(), 
            team_payroll_before=Decimal("130.00"),
            salary=Decimal("4.00"),
            signing_bonus=Decimal("2.00"),
            cap_adjustment=Decimal("-10.00"),
        )
    )
    assert result.ok is False
    assert result.detail["committed_after"] == "136.00"


def test_the_cap_detail_always_reports_base_adjustment_and_effective():
    """
    Whatever renders a validation result can show the breakdown without
    recomputing it, and the ledger keeps all three.
    """
    for adjustment in (Decimal("0"), Decimal("10.00"), Decimal("-10.00")):
        detail = rules.cap_headroom_ok(
            _replace(_baseline_ok(), cap_adjustment=adjustment)
        ).detail
        assert detail["salary_cap"] == "145.00"
        assert detail["cap_adjustment"] == str(adjustment)
        assert detail["effective_cap"] == str(Decimal("145.00") + adjustment)


def test_full_offer_validation_blocks_on_a_docked_cap():
    """The rule is actually wired into validate_offer, not just callable."""
    validation = rules.validate_offer(
        _replace(_baseline_ok(), 
            team_payroll_before=Decimal("140.00"),
            salary=Decimal("4.00"),
            cap_adjustment=Decimal("-10.00"),
        )
    )
    assert validation.ok is False
    assert any(r.code == "cap_exceeded" for r in validation.results)
