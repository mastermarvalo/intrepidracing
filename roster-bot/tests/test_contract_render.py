"""
Contract renderer invariants: layout, line-width, embed limits, empty
states. The functions take plain mappings/dataclasses so no DB is
needed to exercise them.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bot import limits
from bot.contracts import render as contract_render
from bot.contracts.rules import OfferInputs, validate_offer


def _cap_row(
    *,
    display_name: str = "Verstappen",
    contract_value: Decimal = Decimal("12.50"),
    market_value: Decimal | None = Decimal("20.75"),
    term_seasons: int = 3,
    contract_type: str = "standard",
) -> dict:
    return {
        "display_name": display_name,
        "contract_value": contract_value,
        "market_value": market_value,
        "term_seasons": term_seasons,
        "contract_type": contract_type,
        "signing_bonus": Decimal("0"),
        "signed_at": None,
    }


def _pl_row(
    *,
    display_name: str = "Hamilton",
    team_name: str = "Mercedes",
    contract_value: Decimal = Decimal("15.00"),
    market_value: Decimal | None = Decimal("20.00"),
) -> dict:
    return {
        "display_name": display_name,
        "team_name": team_name,
        "contract_value": contract_value,
        "market_value": market_value,
    }


# ── cap sheet ────────────────────────────────────────────────────────


def test_cap_sheet_empty_state():
    embed = contract_render.render_cap_sheet(
        team_name="Red Bull", color=None,
        contracts_with_market=[],
        payroll=Decimal("0"), salary_cap=Decimal("145.00"),
        active_slots_used=0, active_slots_max=2,
    )
    values = {f.name: f.value for f in embed.fields}
    assert "*No active contracts yet.*" in values["Roster"]


def test_cap_sheet_shows_cap_arithmetic():
    embed = contract_render.render_cap_sheet(
        team_name="Red Bull", color=None,
        contracts_with_market=[_cap_row()],
        payroll=Decimal("12.50"), salary_cap=Decimal("145.00"),
        active_slots_used=1, active_slots_max=2,
    )
    cap_field = next(f for f in embed.fields if f.name == "Salary cap")
    assert "$145.00M" in cap_field.value
    assert "$12.50M" in cap_field.value
    assert "$132.50M" in cap_field.value  # cap space
    assert "1/2" in cap_field.value


def test_cap_sheet_shows_pl_when_market_known():
    embed = contract_render.render_cap_sheet(
        team_name="Red Bull", color=None,
        contracts_with_market=[_cap_row(
            contract_value=Decimal("12.50"),
            market_value=Decimal("20.75"),
        )],
        payroll=Decimal("12.50"), salary_cap=Decimal("145.00"),
        active_slots_used=1, active_slots_max=2,
    )
    roster = next(f for f in embed.fields if f.name == "Roster")
    assert "P/L: +$8.25M" in roster.value


def test_cap_sheet_missing_market_shows_dash():
    embed = contract_render.render_cap_sheet(
        team_name="Red Bull", color=None,
        contracts_with_market=[_cap_row(market_value=None)],
        payroll=Decimal("12.50"), salary_cap=Decimal("145.00"),
        active_slots_used=1, active_slots_max=2,
    )
    roster = next(f for f in embed.fields if f.name == "Roster")
    assert "Market: —" in roster.value


def test_cap_sheet_line_width_bound():
    long_row = _cap_row(display_name="X" * 90)
    embed = contract_render.render_cap_sheet(
        team_name="Red Bull", color=None,
        contracts_with_market=[long_row] * 5,
        payroll=Decimal("62.50"), salary_cap=Decimal("145.00"),
        active_slots_used=5, active_slots_max=5,
    )
    for f in embed.fields:
        for line in (f.value or "").split("\n"):
            assert len(line) <= limits.RENDER_LINE_WIDTH


# ── PL table (surplus / underwater) ──────────────────────────────────


def test_pl_table_surplus_sorts_positive_first():
    embed = contract_render.render_pl_table(
        title="Surplus — T1",
        color=None,
        rows=[
            _pl_row(display_name="A", contract_value=Decimal("10"),
                    market_value=Decimal("20")),  # +10
            _pl_row(display_name="B", contract_value=Decimal("15"),
                    market_value=Decimal("18")),  # +3
            _pl_row(display_name="C", contract_value=Decimal("20"),
                    market_value=Decimal("15")),  # -5
        ],
        top_first=True,
        round_label="R1",
    )
    desc = embed.description or ""
    a_pos = desc.index("A ")
    b_pos = desc.index("B ")
    c_pos = desc.index("C ")
    assert a_pos < b_pos < c_pos


def test_pl_table_underwater_sorts_negative_first():
    embed = contract_render.render_pl_table(
        title="Underwater — T1",
        color=None,
        rows=[
            _pl_row(display_name="A", contract_value=Decimal("10"),
                    market_value=Decimal("20")),   # +10
            _pl_row(display_name="B", contract_value=Decimal("15"),
                    market_value=Decimal("5")),    # -10
        ],
        top_first=False,
        round_label=None,
    )
    desc = embed.description or ""
    assert desc.index("B ") < desc.index("A ")


def test_pl_table_skips_rows_without_market_value():
    embed = contract_render.render_pl_table(
        title="Surplus — T1", color=None,
        rows=[
            _pl_row(display_name="Missing", market_value=None),
            _pl_row(display_name="Present", market_value=Decimal("20"),
                    contract_value=Decimal("5")),
        ],
        top_first=True, round_label=None,
    )
    assert "Present" in (embed.description or "")
    assert "Missing" not in (embed.description or "")


def test_pl_table_empty_state():
    embed = contract_render.render_pl_table(
        title="Surplus — T1", color=None, rows=[],
        top_first=True, round_label=None,
    )
    assert "No active contracts" in (embed.description or "")


# ── review panel ─────────────────────────────────────────────────────


def _passing_inputs() -> OfferInputs:
    return OfferInputs(
        actor_id=1, actor_is_principal=True, actor_is_admin=False,
        driver_present_in_tier=True, driver_status="active",
        driver_has_active_contract=False,
        duplicate_open_offer_exists=False,
        salary=Decimal("5"), min_salary=Decimal("1"),
        max_salary=Decimal("50"), signing_bonus=Decimal("0"),
        incentives_amount=Decimal("0"),
        max_incentive_pct=Decimal("0.15"),
        team_payroll_before=Decimal("50"),
        salary_cap=Decimal("145"),
        active_slots_used=1, active_slots_max=2,
        has_linked_release=False,
        term_seasons=2, max_term_seasons=3,
        offer_kind="new", free_agency_open=True,
    )


def test_review_panel_shows_before_after_and_pass_status():
    v = validate_offer(_passing_inputs())
    embed = contract_render.render_review_panel(
        team_name="Red Bull",
        driver_name="Verstappen",
        tier_label="Tier 1",
        offer_kind="new",
        salary=Decimal("5.00"),
        term_seasons=2,
        contract_type="standard",
        signing_bonus=Decimal("0"),
        incentives=None,
        message=None,
        payroll_before=Decimal("50.00"),
        salary_cap=Decimal("145.00"),
        current_market_value=Decimal("7.00"),
        validation=v,
    )
    cap_field = next(f for f in embed.fields if f.name == "Cap impact")
    assert "Payroll after:  $55.00M" in cap_field.value
    assert "Cap space after:  $90.00M" in cap_field.value
    assert (embed.footer.text or "").startswith("✅")


def test_review_panel_shows_block_status_when_check_fails():
    inputs = _passing_inputs()
    from dataclasses import replace
    v = validate_offer(replace(inputs, term_seasons=999))
    embed = contract_render.render_review_panel(
        team_name="Red Bull",
        driver_name="Verstappen",
        tier_label="Tier 1",
        offer_kind="new",
        salary=Decimal("5.00"),
        term_seasons=999,
        contract_type="standard",
        signing_bonus=Decimal("0"),
        incentives=None,
        message=None,
        payroll_before=Decimal("50.00"),
        salary_cap=Decimal("145.00"),
        current_market_value=None,
        validation=v,
    )
    assert (embed.footer.text or "").startswith("⛔")
    checks_field = next(f for f in embed.fields if f.name == "Checks")
    assert "term_too_long" in checks_field.value


# ── driver-facing offer card ─────────────────────────────────────────


def test_driver_offer_card_shows_market_and_current_contract():
    embed = contract_render.render_driver_offer_card(
        team_name="Red Bull",
        tier_label="Tier 1",
        salary=Decimal("12.00"),
        term_seasons=2,
        contract_type="standard",
        signing_bonus=Decimal("0.50"),
        incentives=None,
        message="welcome",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        current_market_value=Decimal("15.00"),
        current_contract_value=Decimal("10.00"),
    )
    market_field = next(
        f for f in embed.fields
        if f.name == "Compared to your market value"
    )
    contract_field = next(
        f for f in embed.fields
        if f.name == "Compared to your current contract"
    )
    assert "P/L: +$3.00M" in market_field.value
    assert "+$2.00M" in contract_field.value


# ── signed contract post ─────────────────────────────────────────────


def test_signed_contract_post_includes_external_ref_and_approver():
    embed = contract_render.render_signed_contract_post(
        team_name="Red Bull", driver_name="Verstappen",
        tier_label="Tier 1",
        contract_value=Decimal("12.50"),
        signing_bonus=Decimal("0.50"),
        term_seasons=3,
        contract_type="standard",
        value_at_signing=Decimal("20.00"),
        external_ref="T2-S1-0042",
        approved_by_mention="<@999>",
    )
    footer = embed.footer.text or ""
    assert "T2-S1-0042" in footer
    assert "<@999>" in footer


# ── contract status ─────────────────────────────────────────────────


def test_contract_status_no_active_shows_none():
    embed = contract_render.render_contract_status(
        driver_name="Rookie",
        tier_label="Tier 3",
        active=None,
        history=[],
        open_offers=[],
    )
    active_field = next(f for f in embed.fields if f.name == "Active contract")
    assert "*None.*" in active_field.value
