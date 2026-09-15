"""
G8: a mistyped round label silently produced a baseline valuation.

`workflow.run_valuation` reports `priced_round=False` when no imported
round matched the label it was given. The panel warned about this; the
typed `/market-admin valuation run` command did not — it rendered the
baseline values in exactly the same layout as a real pricing run, so a
single typo produced an authoritative-looking run that could then be
published over a live market.
"""

from decimal import Decimal

from bot.cogs import admin_market


class _Outcome:
    def __init__(self, rank: int, name: str) -> None:
        self.rank_in_tier = rank
        self.display_name = name
        self.market_value = Decimal("20.00")
        self.delta = Decimal("0")
        self.capped = False
        self.previous_value = Decimal("20.00")


OUTCOMES = [_Outcome(1, "ZeezinDomar"), _Outcome(2, "DuelExploration")]


def test_a_baseline_run_is_flagged():
    rendered = admin_market._render_valuation_preview(
        run_id=7,
        tier_code="t1",
        round_label="R14 Abu Dabhi",  # deliberate typo
        published=False,
        outcomes=OUTCOMES,
        priced_round=False,
    )
    assert "baseline" in rendered.lower()
    # It must say what to do about it, not merely that it happened.
    assert "round label" in rendered.lower()


def test_a_real_pricing_run_is_not_flagged():
    """The warning must not cry wolf on every normal race night."""
    rendered = admin_market._render_valuation_preview(
        run_id=7,
        tier_code="t1",
        round_label="R14 Abu Dhabi",
        published=False,
        outcomes=OUTCOMES,
        priced_round=True,
    )
    assert "baseline" not in rendered.lower()


def test_the_flag_defaults_to_not_warning():
    """
    Additive change: existing callers that pass no `priced_round` must
    behave exactly as before rather than warning on every run.
    """
    rendered = admin_market._render_valuation_preview(
        run_id=7,
        tier_code="t1",
        round_label="R14 Abu Dhabi",
        published=False,
        outcomes=OUTCOMES,
    )
    assert "baseline" not in rendered.lower()


def test_the_warning_keeps_the_driver_rows():
    """
    Warning is added to the header, so the values an admin came to see
    must still be rendered underneath it.
    """
    rendered = admin_market._render_valuation_preview(
        run_id=7,
        tier_code="t1",
        round_label="typo",
        published=False,
        outcomes=OUTCOMES,
        priced_round=False,
    )
    assert "ZeezinDomar" in rendered
    assert "DuelExploration" in rendered


def test_the_typed_command_passes_the_flag_through():
    """
    The renderer supporting the warning is useless if the command never
    tells it. Asserted against the source because the command needs a
    live Discord interaction to invoke.
    """
    import inspect

    source = inspect.getsource(admin_market.AdminMarketCog)
    assert "priced_round=result.priced_round" in source
