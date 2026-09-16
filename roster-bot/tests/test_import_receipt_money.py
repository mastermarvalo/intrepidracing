"""
The import receipt must report money. Both surfaces, same lines.

G9: the panel receipt reported only the result count, so an admin who
imported through Race Night never saw that budgets moved — and once
escrow shipped, never saw that salary had been debited from every team.
"""

from decimal import Decimal

from bot.market import escrow as escrow_engine
from bot.market import escrow_ops
from bot.ui import receipts


class _FakeBudget:
    entries_written = 12
    total_credited = Decimal("4.50")
    total_debited = Decimal("1.25")
    corrections_written = 0


class _FakeOutcome:
    budget = None
    budget_unattributed: list[str] = []
    escrow = None
    # Phase 10. Mirrors the real `ImportOutcome` field so this stub
    # keeps standing in for it; the driver-earnings line is covered in
    # `test_driver_earnings.py`.
    earnings = None


def _charge(amount: str) -> escrow_ops.RaceCharge:
    return escrow_ops.RaceCharge(
        contract_id=1, round_id=1, team_id=1, driver_id=1,
        amount=Decimal(amount), total_held=Decimal(amount),
        races_served=1, term_races=10, term_complete=False,
    )


def _settlement(
    *, held: str, pl: str | None, returned: str, clamped: bool = False
) -> escrow_ops.SettlementResult:
    return escrow_ops.SettlementResult(
        settlement_id=1, escrow_id=1, contract_id=1, team_id=1, driver_id=1,
        reason=escrow_ops.REASON_TERM_COMPLETE,
        result=escrow_engine.Settlement(
            amount_held=Decimal(held),
            market_value=None if pl is None else Decimal("27.50"),
            contract_value=Decimal("24.00"),
            races_served=10, term_races=10,
            pl=None if pl is None else Decimal(pl),
            pl_applied=None if pl is None else Decimal(pl),
            amount_returned=Decimal(returned),
            clamped=clamped,
        ),
    )


def test_a_quiet_import_reports_no_money_at_all():
    """A league with budgets and escrow off should read no money lines."""
    assert receipts.render_money_outcome(_FakeOutcome()) == []


def test_budget_lines_appear_when_budgets_are_enforced():
    outcome = _FakeOutcome()
    outcome.budget = _FakeBudget()
    text = " ".join(receipts.render_money_outcome(outcome))
    assert "12 entr(ies)" in text
    assert "4.50" in text
    assert "1.25" in text


def test_drivers_with_no_contract_are_named_not_just_counted():
    """An admin needs to know WHO was skipped to go fix the roster."""
    outcome = _FakeOutcome()
    outcome.budget = _FakeBudget()
    outcome.budget_unattributed = ["Alice", "Bob"]
    text = " ".join(receipts.render_money_outcome(outcome))
    assert "Alice" in text and "Bob" in text


def test_a_long_unattributed_list_is_truncated_not_dumped():
    """Discord rejects an oversized field, which would lose the receipt."""
    outcome = _FakeOutcome()
    outcome.budget = _FakeBudget()
    outcome.budget_unattributed = [f"D{i}" for i in range(40)]
    text = " ".join(receipts.render_money_outcome(outcome))
    assert "\u2026" in text
    assert "D39" not in text


def test_the_salary_debit_is_reported():
    """
    The headline of Phase 9: importing results takes money. If this line
    is missing the debit is invisible until someone opens Money.
    """
    outcome = _FakeOutcome()
    outcome.escrow = escrow_ops.RoundEscrowOutcome(
        charges=[_charge("1.00"), _charge("0.50")]
    )
    text = " ".join(receipts.render_money_outcome(outcome))
    assert "1.50" in text
    assert "2 contract(s)" in text


def test_a_completed_term_reports_the_payback_and_the_profit():
    outcome = _FakeOutcome()
    outcome.escrow = escrow_ops.RoundEscrowOutcome(
        charges=[_charge("1.00")],
        settlements=[_settlement(held="36.00", pl="3.50", returned="39.50")],
    )
    text = " ".join(receipts.render_money_outcome(outcome))
    assert "36.00" in text
    assert "3.50" in text
    assert "39.50" in text
    assert "profit" in text


def test_a_loss_is_described_as_a_loss_not_a_negative_profit():
    outcome = _FakeOutcome()
    outcome.escrow = escrow_ops.RoundEscrowOutcome(
        settlements=[_settlement(held="10.00", pl="-2.00", returned="8.00")],
    )
    text = " ".join(receipts.render_money_outcome(outcome))
    assert "loss" in text
    assert "2.00" in text


def test_a_clamped_loss_explains_itself():
    """
    A team that sees 0.00 returned must be told why, or it looks like the
    bot lost the money.
    """
    outcome = _FakeOutcome()
    outcome.escrow = escrow_ops.RoundEscrowOutcome(
        settlements=[
            _settlement(held="1.00", pl="-2.00", returned="0.00", clamped=True)
        ],
    )
    text = " ".join(receipts.render_money_outcome(outcome))
    assert "capped" in text
    assert "never more" in text


def test_an_unpriced_driver_settles_without_inventing_a_profit():
    outcome = _FakeOutcome()
    outcome.escrow = escrow_ops.RoundEscrowOutcome(
        settlements=[_settlement(held="5.00", pl=None, returned="5.00")],
    )
    text = " ".join(receipts.render_money_outcome(outcome))
    assert "no published value" in text
    assert "profit or loss" in text


def test_an_escrow_round_that_did_nothing_stays_silent():
    """
    A re-imported round charges nothing. Reporting "0 contracts" would
    imply something went wrong.
    """
    outcome = _FakeOutcome()
    outcome.escrow = escrow_ops.RoundEscrowOutcome(skipped=3)
    assert receipts.render_money_outcome(outcome) == []


def test_budget_lines_come_before_escrow_lines():
    """Earnings then charges: the order the money actually moved."""
    outcome = _FakeOutcome()
    outcome.budget = _FakeBudget()
    outcome.escrow = escrow_ops.RoundEscrowOutcome(charges=[_charge("1.00")])
    lines = receipts.render_money_outcome(outcome)
    assert "Budgets" in lines[0]
    assert "Salary" in lines[1]
