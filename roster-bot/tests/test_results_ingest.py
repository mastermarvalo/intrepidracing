"""
Sheet parsing for race results.

The governing rule under test: tolerant on input, strict on output. A
league should be able to label a column "Pos" or "Finishing Position",
but anything ambiguous must surface as a numbered error rather than be
coerced into a zero that quietly costs a driver money.
"""

from decimal import Decimal

from bot.results_ingest import map_columns, parse_results, resolve_drivers

HEADER = ["Driver", "Pos", "Grid", "DNF", "FL", "DOTD", "Incidents", "Notes"]


def sheet(*rows):
    return [HEADER, *[list(r) for r in rows]]


# ── Header mapping ──────────────────────────────────────────────────────


def test_header_aliases_are_matched_case_and_spacing_insensitively():
    mapping, errors = map_columns(
        ["  DRIVER ", "Finishing Position", "Qualifying", "Retired", "Fastest Lap"]
    )
    assert not errors
    assert mapping["driver"] == 0
    assert mapping["finish"] == 1
    assert mapping["grid"] == 2
    assert mapping["dnf"] == 3
    assert mapping["fastest_lap"] == 4


def test_missing_driver_column_is_an_error_not_a_guess():
    _, errors = map_columns(["Pos", "Grid"])
    assert any("driver column" in e for e in errors)


def test_missing_position_column_is_an_error():
    _, errors = map_columns(["Driver", "Grid"])
    assert any("finishing-position column" in e for e in errors)


def test_optional_columns_may_simply_be_absent():
    """A league that does not track incidents just omits the column."""
    outcome = parse_results([["Driver", "Pos"], ["Alice", "1"]])
    assert not outcome.errors
    assert outcome.rows[0].incident_points == Decimal(0)
    assert outcome.rows[0].driver_of_day is False


# ── Position parsing ────────────────────────────────────────────────────


def test_plain_numbers_and_p_prefixes_both_parse():
    outcome = parse_results(
        sheet(
            ["Alice", "1", "", "", "", "", "", ""],
            ["Bob", "P2", "", "", "", "", "", ""],
            ["Cara", "3rd", "", "", "", "", "", ""],
        )
    )
    assert not outcome.errors
    assert [r.finish_position for r in outcome.rows] == [1, 2, 3]


def test_textual_retirement_tokens_become_dnf():
    outcome = parse_results(
        sheet(
            ["Alice", "DNF", "", "", "", "", "", ""],
            ["Bob", "Ret", "", "", "", "", "", ""],
            ["Cara", "DSQ", "", "", "", "", "", ""],
        )
    )
    assert not outcome.errors
    assert all(r.dnf and r.finish_position is None for r in outcome.rows)


def test_a_blank_position_is_treated_as_a_no_show():
    outcome = parse_results(sheet(["Alice", "", "", "", "", "", "", ""]))
    assert not outcome.errors
    assert outcome.rows[0].dns is True
    assert outcome.rows[0].dnf is False


def test_an_explicit_dnf_column_overrides_a_recorded_position():
    """
    Sheets often keep a driver's position and mark the retirement
    separately. The retirement must win, or a driver who broke down
    banks a full finish.
    """
    outcome = parse_results(sheet(["Alice", "1", "1", "Y", "", "", "", ""]))
    assert outcome.rows[0].dnf is True
    assert outcome.rows[0].finish_position is None
    # Grid is unaffected — qualifying still happened.
    assert outcome.rows[0].grid_position == 1


def test_unreadable_position_is_reported_with_its_row_number():
    outcome = parse_results(sheet(["Alice", "second-ish", "", "", "", "", "", ""]))
    assert not outcome.rows
    assert any("Row 2" in e and "Alice" in e for e in outcome.errors)


def test_a_zero_or_negative_position_is_rejected():
    outcome = parse_results(sheet(["Alice", "0", "", "", "", "", "", ""]))
    assert any("positive" in e for e in outcome.errors)


# ── Booleans ────────────────────────────────────────────────────────────


def test_common_truthy_spellings_are_all_accepted():
    outcome = parse_results(
        sheet(
            ["Alice", "1", "", "", "Y", "", "", ""],
            ["Bob", "2", "", "", "yes", "", "", ""],
            ["Cara", "3", "", "", "TRUE", "", "", ""],
            ["Dan", "4", "", "", "x", "", "", ""],
            ["Erin", "5", "", "", "1", "", "", ""],
        )
    )
    assert not outcome.errors
    assert all(r.fastest_lap for r in outcome.rows)


def test_an_empty_or_unrecognised_flag_cell_means_no():
    outcome = parse_results(
        sheet(
            ["Alice", "1", "", "", "", "", "", ""],
            ["Bob", "2", "", "", "maybe", "", "", ""],
        )
    )
    assert not any(r.fastest_lap for r in outcome.rows)


# ── Incident points ─────────────────────────────────────────────────────


def test_incident_points_parse_as_decimal():
    outcome = parse_results(sheet(["Alice", "1", "", "", "", "", "2.5", ""]))
    assert outcome.rows[0].incident_points == Decimal("2.5")
    assert isinstance(outcome.rows[0].incident_points, Decimal)


def test_negative_incident_points_are_rejected():
    outcome = parse_results(sheet(["Alice", "1", "", "", "", "", "-3", ""]))
    assert not outcome.rows
    assert any("negative" in e for e in outcome.errors)


def test_unreadable_incident_points_are_rejected():
    outcome = parse_results(sheet(["Alice", "1", "", "", "", "", "two", ""]))
    assert any("incident points" in e for e in outcome.errors)


# ── Structural validation ───────────────────────────────────────────────


def test_duplicate_driver_rows_are_rejected():
    outcome = parse_results(
        sheet(
            ["Alice", "1", "", "", "", "", "", ""],
            ["alice", "4", "", "", "", "", "", ""],
        )
    )
    assert any("duplicate" in e.lower() for e in outcome.errors)
    assert len(outcome.rows) == 1


def test_two_drivers_cannot_share_a_finishing_position():
    outcome = parse_results(
        sheet(
            ["Alice", "1", "", "", "", "", "", ""],
            ["Bob", "1", "", "", "", "", "", ""],
        )
    )
    assert any("Duplicate finishing position P1" in e for e in outcome.errors)


def test_two_drivers_cannot_share_a_grid_slot():
    outcome = parse_results(
        sheet(
            ["Alice", "1", "3", "", "", "", "", ""],
            ["Bob", "2", "3", "", "", "", "", ""],
        )
    )
    assert any("Duplicate grid position P3" in e for e in outcome.errors)


def test_several_retirements_do_not_collide():
    """Retirements have no position, so they must not trip the uniqueness check."""
    outcome = parse_results(
        sheet(
            ["Alice", "DNF", "1", "", "", "", "", ""],
            ["Bob", "DNF", "2", "", "", "", "", ""],
        )
    )
    assert not outcome.errors


def test_blank_spacer_rows_are_skipped_silently():
    outcome = parse_results(
        sheet(
            ["Alice", "1", "", "", "", "", "", ""],
            ["", "", "", "", "", "", "", ""],
            ["Bob", "2", "", "", "", "", "", ""],
        )
    )
    assert not outcome.errors
    assert [r.driver_name for r in outcome.rows] == ["Alice", "Bob"]


def test_short_rows_do_not_raise():
    """Sheets API omits trailing empty cells entirely."""
    outcome = parse_results(sheet(["Alice", "1"]))
    assert not outcome.errors
    assert outcome.rows[0].finish_position == 1


def test_row_numbers_in_errors_match_the_spreadsheet():
    """A commissioner has to be able to find the bad cell."""
    outcome = parse_results(
        sheet(
            ["Alice", "1", "", "", "", "", "", ""],
            ["Bob", "nonsense", "", "", "", "", "", ""],
        )
    )
    # Header is row 1, so Bob is row 3.
    assert any("Row 3" in e for e in outcome.errors)


def test_an_empty_range_is_an_error():
    assert parse_results([]).errors


def test_a_header_with_no_data_is_an_error():
    assert parse_results([HEADER]).errors


# ── Driver resolution ───────────────────────────────────────────────────


def test_drivers_resolve_by_display_name_case_insensitively():
    outcome = parse_results(sheet(["ZeezinDomar", "1", "", "", "", "", "", ""]))
    resolved, errors = resolve_drivers(outcome.rows, {"zeezindomar": 42})
    assert not errors
    assert resolved[0]["driver_id"] == 42
    assert resolved[0]["finish_position"] == 1


def test_an_unknown_driver_name_is_reported_not_skipped():
    """
    A typo'd name would otherwise mean that driver silently receives no
    market movement for the round — much harder to notice later than a
    failed import now.
    """
    outcome = parse_results(sheet(["Typo Name", "1", "", "", "", "", "", ""]))
    resolved, errors = resolve_drivers(outcome.rows, {"realname": 1})
    assert not resolved
    assert any("Typo Name" in e for e in errors)


def test_resolution_carries_every_fact_through():
    outcome = parse_results(
        sheet(["Alice", "2", "1", "", "Y", "Y", "1.5", "late penalty"])
    )
    resolved, errors = resolve_drivers(outcome.rows, {"alice": 7})
    assert not errors
    row = resolved[0]
    assert row == {
        "driver_id": 7,
        "finish_position": 2,
        "grid_position": 1,
        "dnf": False,
        "dns": False,
        "fastest_lap": True,
        "driver_of_day": True,
        "incident_points": Decimal("1.5"),
        "note": "late penalty",
    }
