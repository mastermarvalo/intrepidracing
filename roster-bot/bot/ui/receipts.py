"""
Shared render helpers for the results-import receipt.

Two different surfaces import race results — the `/market-admin results
import` command and the Race Night panel — and both owe the admin the
same answer: what did this import do to the money? The budget renderer
used to live privately inside `bot/cogs/admin_market.py`, so the panel
path silently dropped every budget line and, once escrow shipped, would
have dropped the salary charges too. An admin importing through the
panel would see "12 results written" and no hint that a team had just
been debited.

Living in `bot/ui/` rather than in either cog keeps the two receipts
identical by construction instead of by discipline, and avoids one cog
importing another.

Formatting only. Nothing here reads the database or mutates state.
"""

from __future__ import annotations

from bot import workflow
from bot.market.money import format_money

# How many driver names to name before summarising. Long enough to be
# actionable, short enough not to blow Discord's field limit.
MAX_LISTED_NAMES = 8


def render_budget_outcome(outcome: workflow.ImportOutcome) -> list[str]:
    """
    Budget lines for the receipt; empty when budgets are not enforced.

    Empty rather than "budgets are off" because a league that never
    turned budgets on should not read a line about them on every import.
    """
    b = outcome.budget
    if b is None:
        return []
    lines = [
        f"\U0001f4b0 Budgets: {b.entries_written} entr(ies) written \u2014 "
        f"+{format_money(b.total_credited)} earned, "
        f"\u2212{format_money(b.total_debited)} in penalties"
        + (
            f", {b.corrections_written} correction(s)"
            if b.corrections_written
            else ""
        )
        + "."
    ]
    if outcome.budget_unattributed:
        names = outcome.budget_unattributed
        lines.append(
            f"\u26a0 {len(names)} driver(s) had no active contract, so no "
            "team was charged or credited: "
            + ", ".join(names[:MAX_LISTED_NAMES])
            + ("\u2026" if len(names) > MAX_LISTED_NAMES else "")
            + "."
        )
    return lines


def render_escrow_outcome(outcome: workflow.ImportOutcome) -> list[str]:
    """
    Salary-escrow lines for the receipt; empty when escrow is off.

    This is the one place an admin sees that importing results just took
    money out of every team's cash. Without it the debit is invisible
    until someone opens the Money screen and wonders what happened.

    Settlements are reported separately from charges because they are the
    rarer and more consequential event: a term ending pays a team back
    its escrow plus or minus the driver's profit, and an admin should not
    have to infer that from a net figure.
    """
    e = outcome.escrow
    if e is None or (not e.charges and not e.settlements):
        return []

    lines: list[str] = []
    if e.charges:
        lines.append(
            f"\U0001f4b8 Salary: \u2212{format_money(e.total_charged)} held "
            f"in escrow across {len(e.charges)} contract(s) for this race."
        )
    for settled in e.settlements:
        result = settled.result
        pl = result.pl_applied
        if pl is None:
            pl_text = "no published value, so no profit or loss was applied"
        elif pl > 0:
            pl_text = f"+{format_money(pl)} profit"
        elif pl < 0:
            pl_text = f"\u2212{format_money(abs(pl))} loss"
        else:
            pl_text = "break-even"
        line = (
            f"\U0001f4c4 Contract complete: "
            f"{format_money(result.amount_held)} escrow returned, "
            f"{pl_text} \u2014 "
            f"+{format_money(result.amount_returned)} back to the team."
        )
        if result.clamped:
            line += (
                " The loss was larger than the escrow, so it was capped: a "
                "team can lose what it held and never more."
            )
        lines.append(line)
    return lines


def render_money_outcome(outcome: workflow.ImportOutcome) -> list[str]:
    """Every money line this import produced, in reading order."""
    return render_budget_outcome(outcome) + render_escrow_outcome(outcome)
