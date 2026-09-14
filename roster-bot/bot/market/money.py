"""
The single money boundary.

Every dollar amount that enters or leaves the market/contract code goes
through this module. Rules — non-negotiable per CLAUDE.md §2:

* Decimal everywhere. Never float. `parse_money` returns Decimal;
  `format_money` accepts Decimal.
* One documented rounding boundary: `round_money` (banker's-rounding-off,
  ROUND_HALF_UP quantized to two decimals). All other code operates on
  Decimals of arbitrary precision and rounds only when it hits a
  display or persistence step that goes through here.
* All amounts are in $M ("millions of dollars"), because that is how the
  Discord surface reads and how the F1 league talks. `parse_money`
  accepts a leading `$` and a trailing `M`/`m` but stores the numeric
  value verbatim — `$20.75M` becomes `Decimal("20.75")`, not
  `Decimal("20_750_000")`. Multiplying by a million at the edges buys
  nothing and invites rounding drift.

The ADR-001 magic-number guard scans this module (it lives under
`bot/market/`), so only 0, 1, and -1 may appear as numeric literals.
Constants like the quantization step live as string arguments to
`Decimal(...)`.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Two-decimal quantum. `Decimal("0.01")` is a *string* literal, so the
# magic-number guard is satisfied. Changing precision would be a
# schema-level decision (NUMERIC(12, 2) in the DB) — do not tweak here
# without also revisiting the migrations.
_QUANTUM = Decimal("0.01")

# Zero as a Decimal — used often enough to name it once so the sign
# comparisons below read cleanly. `Decimal(0)` is allowed because 0
# is on the ALLOWED_LITERALS list in scripts/check_magic_numbers.py.
_ZERO = Decimal(0)


def round_money(value: Decimal) -> Decimal:
    """
    Quantize to two decimals with ROUND_HALF_UP. THE rounding boundary.

    Round-half-up matches how spectators read numbers ($1.375M → $1.38M),
    and matches the SQL NUMERIC(12,2) storage — there is no lossy
    conversion between what the engine computes and what the DB stores.
    """
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def parse_money(text: str) -> Decimal:
    """
    Parse `$20.75M` / `20.75M` / `20.75` / `+8.25M` / `-8.25M` into a
    Decimal amount in $M. Whitespace, an optional leading `$`, and an
    optional trailing `M` / `m` are stripped; anything else raises
    ValueError with the original input for a legible error message.
    """
    if not isinstance(text, str):
        raise TypeError(f"parse_money expected str, got {type(text).__name__}")
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty money string")
    body = stripped.lstrip("$").rstrip("Mm").strip()
    if not body:
        raise ValueError(f"could not parse money: {text!r}")
    try:
        return round_money(Decimal(body))
    except InvalidOperation as exc:
        raise ValueError(f"could not parse money: {text!r}") from exc


def format_money(value: Decimal) -> str:
    """
    Canonical display: `$20.75M`, always two decimals, no thousands
    separator (values in $M are single-digit millions most of the time
    and comma-grouping the wrong side of the decimal produces false-
    positive confusion with international separator conventions).
    """
    return f"${round_money(value)}M"


def format_pl(value: Decimal) -> str:
    """
    Sign-aware P/L: `+$8.25M`, `-$8.25M`, `$0.00M`. Zero is unsigned so
    a driver on their contract number reads as "on the money" rather
    than "up zero." Negative-zero (Decimal preserves the sign of zero
    through arithmetic) collapses to positive zero on display.
    """
    v = round_money(value)
    if v > _ZERO:
        return f"+${v}M"
    if v < _ZERO:
        return f"-${-v}M"
    return f"${abs(v)}M"
