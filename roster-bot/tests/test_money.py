"""
Money boundary: parse/format round-trips, rounding at the boundary,
sign-aware P/L formatting including negative zero.
"""

from decimal import Decimal

import pytest

from bot.market.money import format_money, format_pl, parse_money, round_money

# ── parse ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$20.75M", Decimal("20.75")),
        ("20.75M", Decimal("20.75")),
        ("20.75", Decimal("20.75")),
        ("$20.75", Decimal("20.75")),
        ("$0", Decimal("0.00")),
        ("$1M", Decimal("1.00")),
        ("+8.25M", Decimal("8.25")),
        ("-8.25M", Decimal("-8.25")),
        ("  $20.75M  ", Decimal("20.75")),
        ("145.00m", Decimal("145.00")),
    ],
)
def test_parse_accepts_common_shapes(text, expected):
    assert parse_money(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "not-money", "$", "M", "1.2.3"])
def test_parse_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_money(text)


def test_parse_rejects_non_string():
    with pytest.raises(TypeError):
        parse_money(20.75)  # type: ignore[arg-type]


def test_parse_result_is_always_rounded_to_two_decimals():
    # 20.7501 rounds up under ROUND_HALF_UP; 20.7549 rounds down.
    assert parse_money("20.7501") == Decimal("20.75")
    assert parse_money("20.7550") == Decimal("20.76")


# ── format ────────────────────────────────────────────────────────────


def test_format_money_canonical_shape():
    assert format_money(Decimal("20.75")) == "$20.75M"


def test_format_money_pads_to_two_decimals():
    assert format_money(Decimal("20")) == "$20.00M"
    assert format_money(Decimal("20.5")) == "$20.50M"


def test_format_money_rounds_at_the_boundary():
    # ROUND_HALF_UP: 20.755 → 20.76, 20.745 → 20.75.
    assert format_money(Decimal("20.755")) == "$20.76M"
    assert format_money(Decimal("20.745")) == "$20.75M"


def test_format_money_handles_zero_and_negative():
    assert format_money(Decimal("0")) == "$0.00M"
    assert format_money(Decimal("-3.5")) == "$-3.50M"  # not a P/L helper


# ── format_pl ─────────────────────────────────────────────────────────


def test_format_pl_positive_carries_plus():
    assert format_pl(Decimal("8.25")) == "+$8.25M"


def test_format_pl_negative_carries_minus_outside_dollar():
    # sign lives outside the $ symbol so it reads left to right as
    # "minus eight point twenty-five million" rather than "dollar minus".
    assert format_pl(Decimal("-8.25")) == "-$8.25M"


def test_format_pl_zero_is_unsigned():
    assert format_pl(Decimal("0")) == "$0.00M"


def test_format_pl_negative_zero_collapses_to_positive_zero():
    # Decimal preserves the sign of zero through arithmetic, so a
    # residual `-0.00` can arise after subtraction. It must not display
    # as `-$0.00M`.
    assert format_pl(Decimal("-0.00")) == "$0.00M"
    assert format_pl(Decimal("-0.001")) == "$0.00M"


# ── round-trip ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [Decimal("0"), Decimal("1.00"), Decimal("20.75"), Decimal("145.00"), Decimal("-8.25")],
)
def test_parse_and_format_are_round_trip(value):
    # Whatever format_money emits, parse_money must accept and recover
    # the same Decimal.
    assert parse_money(format_money(value)) == value


# ── boundary function ────────────────────────────────────────────────


def test_round_money_is_the_single_boundary():
    # ROUND_HALF_UP behaviour (not banker's rounding).
    assert round_money(Decimal("0.005")) == Decimal("0.01")
    assert round_money(Decimal("0.015")) == Decimal("0.02")
    assert round_money(Decimal("0.025")) == Decimal("0.03")
    assert round_money(Decimal("-0.005")) == Decimal("-0.01")


def test_round_money_never_produces_more_than_two_decimals():
    result = round_money(Decimal("1.2345678"))
    assert result == Decimal("1.23")
    # Decimal.as_tuple().exponent is -2 when quantized to 0.01.
    assert result.as_tuple().exponent == -2
