"""
Render-layer invariants — Discord embed limits, pagination boundaries,
and the two-line-per-driver layout width bound.

These tests fake asyncpg.Record with dicts because the render layer
only reads them via mapping access (`row["market_value"]`) — there's
no need to spin up Postgres just to exercise formatting.
"""

from decimal import Decimal

import pytest

from bot import limits
from bot.market import render


def _row(**kw):
    """Default an entire market-table row; override just what the test cares about."""
    base = {
        "driver_id": 1,
        "display_name": "Verstappen",
        "market_value": Decimal("20.75"),
        "previous_value": Decimal("19.50"),
        "delta": Decimal("1.25"),
        "rank_in_tier": 1,
        "capped": False,
    }
    base.update(kw)
    return base


def _many_rows(n: int):
    return [
        _row(
            driver_id=i,
            display_name=f"Driver{i:02d}",
            market_value=Decimal(f"{20 + i * Decimal('0.10')}"),
            delta=Decimal("0.10"),
            rank_in_tier=i,
        )
        for i in range(1, n + 1)
    ]


# ── pagination boundaries ────────────────────────────────────────────


def test_paginate_empty_returns_single_empty_page():
    page, page_num, total = render.paginate([], page=1)
    assert page == []
    assert page_num == 1
    assert total == 1


def test_paginate_exactly_one_page_worth():
    items = list(range(limits.MARKET_PAGE_SIZE))
    page, page_num, total = render.paginate(items, page=1)
    assert len(page) == limits.MARKET_PAGE_SIZE
    assert total == 1
    # Asking for page 2 clamps back to the only page.
    page, page_num, total = render.paginate(items, page=2)
    assert page_num == 1


def test_paginate_page_size_plus_one_makes_two_pages():
    items = list(range(limits.MARKET_PAGE_SIZE + 1))
    page1, num1, total = render.paginate(items, page=1)
    page2, num2, _ = render.paginate(items, page=2)
    assert total == 2
    assert num1 == 1 and num2 == 2
    assert len(page1) == limits.MARKET_PAGE_SIZE
    assert len(page2) == 1


def test_paginate_high_page_clamps():
    items = list(range(limits.MARKET_PAGE_SIZE * 2))
    _, num, total = render.paginate(items, page=99)
    assert total == 2
    assert num == 2


def test_paginate_low_page_clamps():
    items = list(range(3))
    _, num, _ = render.paginate(items, page=0)
    assert num == 1


# ── market view ──────────────────────────────────────────────────────


def test_market_page_two_line_layout_and_line_width_bound():
    embed = render.render_market_page(
        tier_label="Tier 1",
        round_label="Post-Abu Dhabi",
        accent_color=0xff1801,
        drivers=[_row()],
        page=1,
    )
    assert embed.description is not None
    lines = embed.description.split("\n")
    # Every line stays within the render width bound.
    for line in lines:
        assert len(line) <= limits.RENDER_LINE_WIDTH, f"line too wide: {line!r}"
    # Two-line-per-driver: first line ends with the name, second holds
    # the money.
    assert lines[0].startswith("1. Verstappen")
    assert "Market: $20.75M" in lines[1]
    assert "▲" in lines[1]  # positive delta arrow


def test_market_page_empty_tier_renders_helpful_body():
    embed = render.render_market_page(
        tier_label="Tier 1",
        round_label=None,
        accent_color=None,
        drivers=[],
        page=1,
    )
    assert embed.description is not None
    assert "No drivers" in embed.description
    # Footer should still show page 1/1.
    assert "Page 1/1" in (embed.footer.text or "")


def test_market_page_single_driver_renders_two_lines_only():
    embed = render.render_market_page(
        tier_label="Tier 1", round_label="R1", accent_color=None,
        drivers=[_row()], page=1,
    )
    lines = (embed.description or "").split("\n")
    assert len(lines) == 2


def test_market_page_capped_flag_surfaces_in_render():
    embed = render.render_market_page(
        tier_label="Tier 1", round_label="R1", accent_color=None,
        drivers=[_row(capped=True)], page=1,
    )
    assert "capped" in (embed.description or "")


def test_market_page_two_pages_produce_correct_footers_and_totals():
    rows = _many_rows(limits.MARKET_PAGE_SIZE + 5)
    e1 = render.render_market_page(
        tier_label="Tier 1", round_label="R1", accent_color=None,
        drivers=rows, page=1,
    )
    e2 = render.render_market_page(
        tier_label="Tier 1", round_label="R1", accent_color=None,
        drivers=rows, page=2,
    )
    assert "Page 1/2" in (e1.footer.text or "")
    assert "Page 2/2" in (e2.footer.text or "")
    # Page 1 has PAGE_SIZE drivers (× 2 lines each), page 2 has 5.
    assert (e1.description or "").count("\n") + 1 == limits.MARKET_PAGE_SIZE * 2
    assert (e2.description or "").count("\n") + 1 == 5 * 2


# ── embed size caps ──────────────────────────────────────────────────


def test_no_embed_field_exceeds_1024_chars():
    # A tier with a huge number of drivers still renders without
    # breaking Discord's field-value cap. render_market_page uses the
    # description (not fields), but the movers view uses fields — so
    # bombard movers with long-named drivers.
    rows = [
        _row(display_name="A" * 90, driver_id=i, delta=Decimal("0.10"))
        for i in range(1, limits.MOVERS_PER_DIRECTION * 3 + 1)
    ]
    embed = render.render_movers(
        tier_label="Tier 1",
        round_label=None,
        accent_color=None,
        risers=rows[:limits.MOVERS_PER_DIRECTION],
        fallers=rows[:limits.MOVERS_PER_DIRECTION],
    )
    for field in embed.fields:
        assert len(field.value or "") <= limits.EMBED_FIELD_VALUE_MAX


def test_total_embed_char_count_within_6000():
    # Very long tier + long-named drivers should still fit or truncate
    # cleanly. render._enforce_total is the guard.
    long_rows = [
        _row(display_name="X" * 90, driver_id=i, rank_in_tier=i,
             delta=Decimal("0.10"))
        for i in range(1, 50)
    ]
    embed = render.render_market_page(
        tier_label="Tier 1", round_label="R1", accent_color=None,
        drivers=long_rows, page=1,
    )
    assert render._embed_char_count(embed) <= limits.EMBED_TOTAL_MAX


# ── movers ───────────────────────────────────────────────────────────


def test_movers_shows_none_hint_when_direction_is_empty():
    embed = render.render_movers(
        tier_label="Tier 1", round_label="R1", accent_color=None,
        risers=[_row(delta=Decimal("0.10"))],
        fallers=[],
    )
    values_by_name = {f.name: f.value for f in embed.fields}
    assert "▲ Risers" in values_by_name
    assert "▼ Fallers" in values_by_name
    assert "*none*" in values_by_name["▼ Fallers"]


def test_movers_empty_both_sides_message():
    embed = render.render_movers(
        tier_label="Tier 1", round_label=None, accent_color=None,
        risers=[], fallers=[],
    )
    assert "No movement" in (embed.description or "")


# ── driver card ──────────────────────────────────────────────────────


def test_driver_card_points_at_where_the_contract_lives():
    """
    Was `..._shows_contract_placeholder_pre_phase4`, asserting the card
    said "contracts land in Phase 4". Contracts landed, so that text was
    stale and the test was pinning it in place (D2). The card still does
    not render contract terms — it is not given them — but it now names
    the commands that do.
    """
    embed = render.render_driver_card(
        display_name="Verstappen",
        tier_label="Tier 1",
        accent_color=None,
        latest=_row(),
        history=[],
    )
    contract_field = next(f for f in embed.fields if f.name == "Contract")
    assert "Phase 4" not in contract_field.value
    assert "/contract status" in contract_field.value


def test_driver_card_no_valuations_still_renders():
    embed = render.render_driver_card(
        display_name="Rookie",
        tier_label="Tier 3",
        accent_color=None,
        latest=None,
        history=[],
    )
    market_field = next(f for f in embed.fields if f.name == "Market")
    assert "No published valuation" in market_field.value


# ── dashboard ────────────────────────────────────────────────────────


def test_dashboard_empty_state():
    embed = render.render_dashboard(season_name="F1 2026", tier_rows=[])
    assert "No published valuations" in (embed.description or "")


def test_dashboard_groups_by_tier_in_rank_order():
    rows = [
        # Deliberately out-of-order to prove the render sorts.
        {**_row(driver_id=101, display_name="T2-One", rank_in_tier=1),
         "tier_code": "t2", "tier_label": "Tier 2",
         "tier_rank_order": 2, "accent_color": None},
        {**_row(driver_id=201, display_name="T1-One", rank_in_tier=1),
         "tier_code": "t1", "tier_label": "Tier 1",
         "tier_rank_order": 1, "accent_color": None},
        {**_row(driver_id=202, display_name="T1-Two", rank_in_tier=2),
         "tier_code": "t1", "tier_label": "Tier 1",
         "tier_rank_order": 1, "accent_color": None},
    ]
    embed = render.render_dashboard(season_name="F1 2026", tier_rows=rows)
    assert [f.name for f in embed.fields] == ["Tier 1", "Tier 2"]
    # Tier 1 field has both drivers.
    assert "T1-One" in embed.fields[0].value
    assert "T1-Two" in embed.fields[0].value


# ── delta arrow helper ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1.00"), "▲"),
        (Decimal("-1.00"), "▼"),
        (Decimal("0"), "•"),
        (None, "•"),
    ],
)
def test_delta_arrow(value, expected):
    assert render._delta_arrow(value) == expected
