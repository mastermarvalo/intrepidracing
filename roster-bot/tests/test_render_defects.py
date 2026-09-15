"""
Defects in the existing renderers, found while building the desk screens.

D1 — `/market driver` raised `KeyError: 'rank_in_tier'`. The renderer
reads the rank off the newest valuation row, but the query feeding it
never selected that column, so the command crashed for every driver
that had a published valuation — i.e. every driver in a live league.

D2 — the same card advertised "contracts land in Phase 4". They landed.

D7 — `render_cap_sheet` computed `available = balance − effective_payroll`
unconditionally. Under escrow, salary genuinely leaves the balance one
race at a time, so subtracting payroll again charged every team twice
and understated the headroom the signing validator would actually allow.
"""

from decimal import Decimal

from bot.contracts import render as contract_render
from bot.market import render as market_render
from bot.market.budget import available_to_spend

CAP = Decimal("145.00")


def _latest(rank=3):
    row = {
        "market_value": Decimal("20.75"),
        "previous_value": Decimal("19.00"),
        "delta": Decimal("1.75"),
        "capped": False,
        "round_label": "R14 Abu Dhabi",
    }
    if rank is not None:
        row["rank_in_tier"] = rank
    return row


# ── D1 / D2 ──────────────────────────────────────────────────────────


def test_the_driver_card_renders_the_rank():
    embed = market_render.render_driver_card(
        display_name="ZeezinDomar",
        tier_label="Tier 1",
        accent_color=None,
        latest=_latest(rank=3),
        history=[_latest(rank=3)],
    )
    rank_field = [f for f in embed.fields if f.name == "Rank in tier"]
    assert rank_field and rank_field[0].value == "3"


def test_the_driver_card_survives_a_row_without_a_rank():
    """
    Degrading to a dash beats raising: a caller passing a row from a
    different query should not take down the whole command.
    """
    embed = market_render.render_driver_card(
        display_name="ZeezinDomar",
        tier_label="Tier 1",
        accent_color=None,
        latest=_latest(rank=None),
        history=[],
    )
    rank_field = [f for f in embed.fields if f.name == "Rank in tier"]
    assert rank_field and rank_field[0].value == "—"


def test_the_driver_card_no_longer_claims_contracts_are_unbuilt():
    embed = market_render.render_driver_card(
        display_name="ZeezinDomar",
        tier_label="Tier 1",
        accent_color=None,
        latest=_latest(),
        history=[],
    )
    contract = [f for f in embed.fields if f.name == "Contract"][0]
    assert "Phase 4" not in contract.value
    # It should say where the information actually lives.
    assert "/contract status" in contract.value


def test_the_valuation_history_query_selects_the_rank():
    """
    The renderer can only show the rank if the query provides it. This
    pins the column D1 was missing, in the SQL rather than the render.
    """
    import inspect
    import re

    from bot import queries

    source = inspect.getsource(queries.fetch_driver_valuation_history)
    # Strip SQL comments first: an explanatory comment mentioning the
    # column would otherwise satisfy this assertion while the SELECT
    # list stayed broken. (It did, on the first draft of this test.)
    sql = re.sub(r"--[^\n]*", "", source)
    select = sql[sql.index("SELECT"):sql.index("FROM")]
    assert "rank_in_tier" in select, select


# ── D7 ───────────────────────────────────────────────────────────────


def _cap_sheet(*, escrow: bool):
    return contract_render.render_cap_sheet(
        team_name="McLaren",
        color=None,
        contracts_with_market=[],
        payroll=Decimal("34.00"),
        salary_cap=CAP,
        active_slots_used=2,
        active_slots_max=2,
        budget_balance=Decimal("100.00"),
        escrow_enabled=escrow,
    )


def _available_line(embed) -> str:
    field = [f for f in embed.fields if f.name == "Team budget"][0]
    line = [
        ln for ln in field.value.splitlines()
        if "Available to spend" in ln
    ]
    assert line, field.value
    return line[0]


def test_without_escrow_payroll_is_still_subtracted():
    """The pre-escrow behaviour must be unchanged."""
    line = _available_line(_cap_sheet(escrow=False))
    # 100.00 balance − 34.00 payroll
    assert "66.00" in line


def test_under_escrow_payroll_is_not_subtracted_twice():
    """
    Salary already left the balance race by race, so the balance is
    the answer. Subtracting payroll again understated headroom by the
    full payroll.
    """
    line = _available_line(_cap_sheet(escrow=True))
    assert "100.00" in line
    assert "66.00" not in line


def test_the_cap_sheet_matches_the_signing_validator():
    """
    The display and the rule that gates signings must agree, in both
    modes. A team shown less headroom than the validator allows will
    not attempt signings it is entitled to make.
    """
    balance, payroll = Decimal("100.00"), Decimal("34.00")
    for escrow in (False, True):
        expected = available_to_spend(
            balance, payroll, escrow_enabled=escrow
        )
        assert f"{expected:.2f}" in _available_line(_cap_sheet(escrow=escrow))


def test_escrow_mode_is_labelled_on_the_cap_sheet():
    """
    A number that means "cash on hand" and a number that means
    "headroom after commitments" must not look identical.
    """
    field = [
        f for f in _cap_sheet(escrow=True).fields if f.name == "Team budget"
    ][0]
    assert "Escrow is on" in field.value
    field_off = [
        f for f in _cap_sheet(escrow=False).fields if f.name == "Team budget"
    ][0]
    assert "Escrow is on" not in field_off.value


def test_escrow_defaults_to_off_for_existing_callers():
    """Additive: a caller that passes no flag behaves exactly as before."""
    embed = contract_render.render_cap_sheet(
        team_name="McLaren",
        color=None,
        contracts_with_market=[],
        payroll=Decimal("34.00"),
        salary_cap=CAP,
        active_slots_used=2,
        active_slots_max=2,
        budget_balance=Decimal("100.00"),
    )
    assert "66.00" in _available_line(embed)


def test_the_offer_deadline_column_is_not_nullable():
    """
    Recorded because it was reported as a crash (a NULL `expires_at`
    reaching `int(expires_at.timestamp())`) and it is not one:
    `contract_offers.expires_at` is NOT NULL in 008 and no migration
    ever relaxes it. Kept as a test so that if someone does relax it,
    this fails loudly and the renderer gets the guard it would then
    need.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "migrations"
    init = (root / "008_contracts_and_offers.sql").read_text()
    assert "expires_at      TIMESTAMPTZ NOT NULL" in init
    for sql in root.glob("*.sql"):
        text = sql.read_text().lower()
        assert "alter table contract_offers" not in text, sql.name
