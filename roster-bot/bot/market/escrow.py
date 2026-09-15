"""
Salary escrow: the arithmetic, with no database and no Discord.

The model, in one paragraph. A contract commits a **per-season** salary
against the cap, exactly as before. What changed is that the money is
now actually taken: each imported race debits that race's share of the
salary from the team's cash into an escrow holding. When the term ends
the holding comes back, adjusted by the driver's profit and loss — the
same P/L the surplus and underwater boards already show, market value
minus contract value. A driver who appreciates pays his team back more
than it escrowed; one who declines costs it the difference.

Every function here is pure: Decimal in, Decimal out. Persistence lives
in `escrow_ops.py`, which is the only place that writes a ledger row.

Two policy edges the arithmetic has to settle, both documented at their
functions because neither is obvious and both are tunable:

* **Early exits pro-rate the P/L** (`settle`). A release after one race
  of a 36-race deal cannot fairly carry the full-season P/L — that would
  charge a team a whole season's decline for a race it barely ran, and
  can exceed everything it escrowed. P/L is therefore scaled by the
  fraction of the term actually served. At term completion the fraction
  is exactly 1, so the headline case is unaffected.
* **A team can lose its escrow and never more than its escrow**
  (`settle`). Without a floor, a driver whose value collapses early in a
  short term produces a *negative* return — a settlement that bills the
  team further money it never set aside. The return is clamped at zero
  and the clamp is recorded, so the audit trail shows that a worse loss
  was capped rather than silently reshaped.

The ADR-001 magic-number guard scans this module, so 0, 1 and -1 are the
only numeric literals; anything else arrives as config from the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bot.market.money import round_money

_ZERO = Decimal(0)
_ONE = Decimal(1)


def per_race_share(contract_value: Decimal, races_per_season: int) -> Decimal:
    """
    What one race of a per-season salary costs.

    `contract_value` is the season rate — the number the cap counts and
    the number a Team Principal negotiated. Races only decide how long a
    deal runs and how the cash comes out of the balance.

    Rounding to cents here means the sum of a season's charges can differ
    from the season rate by a cent or two. That drift never costs anyone
    money: a settlement returns the sum of what was *actually* charged
    (`amount_held`), not a recomputed nominal figure.
    """
    if races_per_season < 1:
        raise ValueError(
            f"races_per_season must be at least 1, got {races_per_season}"
        )
    return round_money(contract_value / Decimal(races_per_season))


def price_floor(
    *,
    base_value: Decimal | None,
    term_races: int,
    min_term_races: int,
    length_premium_pct: Decimal,
    resign_premium_pct: Decimal,
    is_resign: bool,
    min_salary: Decimal,
) -> Decimal:
    """
    The least a team may offer for this driver, this long, on this path.

    Two premiums stack on the driver's own market value:

    * **Length** is charged per race *above the league minimum*, so a
      deal at exactly the minimum length pays none. Measuring from the
      minimum rather than from one race is what keeps the rate sane: the
      premium compounds per race, so it is expressed as a small per-race
      figure (0.005 being +0.5% a race) and a league that sets a 5-race
      floor does not silently charge four races of premium on its
      shortest legal contract.
    * **Re-signing** is charged when the offering team is already the
      driver's team. This is the anti-cycling lever: keeping your own
      driver costs more than his open-market value, so churning short
      contracts to reset terms is expensive rather than free.

    With both rates at zero — the default — this returns `min_salary` or
    the driver's value, whichever is greater, which is the behaviour the
    league had before premiums existed.

    `base_value` is None when no valuation has ever been published for
    the driver. There is then no market value to derive a floor from, so
    the league's flat salary floor is all that can be enforced; the
    caller is expected to say so rather than imply the offer was priced.
    """
    if base_value is None:
        return round_money(min_salary)

    length_steps = max(0, term_races - min_term_races)
    multiplier = _ONE + length_premium_pct * Decimal(length_steps)
    if is_resign:
        multiplier *= _ONE + resign_premium_pct

    return max(round_money(base_value * multiplier), round_money(min_salary))


@dataclass(frozen=True)
class Settlement:
    """
    What a settlement pays back, and the reasoning behind the number.

    `pl` is the full-term P/L for the record; `pl_applied` is what was
    actually paid, which differs only on an early exit. `clamped` is True
    when the loss would have exceeded the escrow and was capped.
    """

    amount_held: Decimal
    market_value: Decimal | None
    contract_value: Decimal
    races_served: int
    term_races: int
    pl: Decimal | None
    pl_applied: Decimal | None
    amount_returned: Decimal
    clamped: bool

    @property
    def is_full_term(self) -> bool:
        return self.races_served >= self.term_races


def settle(
    *,
    amount_held: Decimal,
    market_value: Decimal | None,
    contract_value: Decimal,
    races_served: int,
    term_races: int,
) -> Settlement:
    """
    Close out a holding: escrow back, plus or minus the driver's P/L.

    At term completion this is simply `amount_held + (market - contract)`.

    An early exit — release, buyout, void, or a trade moving the contract
    on — pro-rates the P/L by the share of the term served. The full-term
    figure is kept on the result so a receipt can show both, but paying
    the unscaled number for a deal that ran two races would make an early
    release a lottery on the driver's latest valuation rather than a
    settlement of what actually happened.

    `market_value` is None when the driver has no published valuation. The
    P/L is then genuinely unknown, so only the escrow comes back and both
    P/L fields stay None — not zero, which would claim the driver landed
    exactly on his contract number.
    """
    if term_races < 1:
        raise ValueError(f"term_races must be at least 1, got {term_races}")
    if races_served < 0:
        raise ValueError(f"races_served cannot be negative, got {races_served}")

    held = round_money(amount_held)

    if market_value is None:
        return Settlement(
            amount_held=held,
            market_value=None,
            contract_value=round_money(contract_value),
            races_served=races_served,
            term_races=term_races,
            pl=None,
            pl_applied=None,
            amount_returned=held,
            clamped=False,
        )

    pl = round_money(market_value - contract_value)

    served = min(races_served, term_races)
    if served >= term_races:
        pl_applied = pl
    else:
        pl_applied = round_money(pl * Decimal(served) / Decimal(term_races))

    returned = held + pl_applied
    clamped = returned < _ZERO
    if clamped:
        returned = _ZERO

    return Settlement(
        amount_held=held,
        market_value=round_money(market_value),
        contract_value=round_money(contract_value),
        races_served=races_served,
        term_races=term_races,
        pl=pl,
        pl_applied=pl_applied,
        amount_returned=round_money(returned),
        clamped=clamped,
    )


def races_remaining(*, term_races: int, races_served: int) -> int:
    """Races still owed on a term; never negative."""
    return max(0, term_races - races_served)


def is_term_complete(*, term_races: int, races_served: int) -> bool:
    """
    Whether the term is done and the contract should settle.

    Uses `>=` rather than `==` so a contract that somehow over-served —
    a term shortened by an admin after races were already run — still
    completes instead of running forever.
    """
    return races_served >= term_races


def carried_term_races(*, term_races: int, races_served: int) -> int:
    """
    The term a carried-over contract row should have in the new season.

    Carry-over moves the *remainder* of a term rather than adding a whole
    season, which is the whole point of measuring terms in races: a
    36-race deal signed in a 24-race season arrives in the next season
    owing 12 races, not another 36.
    """
    return races_remaining(term_races=term_races, races_served=races_served)
