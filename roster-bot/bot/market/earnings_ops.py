"""
Driver career earnings against an open connection.

The driver side of a salary. `escrow_ops` moves the TEAM's cash — out at
each race, back at settlement. This module records what the DRIVER was
paid over the same races, and does nothing else: no team is debited
here, no balance moves, no cap is consulted. Team money behaves exactly
as it did before this module existed.

A driver's total is a number to display and compete over. It buys
nothing today. `amount` is signed anyway, so giving it purchasing power
later is a debit row against this same ledger rather than a schema
change.

── Deliberately independent of escrow ──────────────────────────────────

`escrow_ops.charge_race` returns early when `budget_config.escrow_enabled`
is off, because there is no cash to move. Earnings do NOT follow that
switch. Neither does term service, for the same reason — see
`escrow_ops.record_race_served`. A driver under a $20M contract earned that salary whether or not
the league models team cash, so this credits on every imported round for
every active contract, in both escrow modes. That independence is the
whole reason this is a separate pass rather than a few lines inside
`charge_race`.

A consequence worth knowing: because earnings run independently, a
league with escrow OFF still builds a full career leaderboard.

── Paid per round, not per finish ──────────────────────────────────────

A salary is owed for being under contract, not for finishing, so this
pays every active contract in the tier once per imported round —
including a driver who did not show up. That mirrors escrow, which
charges the team for the same race regardless of the result, and keeps
the two sides reconcilable: with escrow on, what a tier's teams were
charged for a round equals what its drivers earned for it.

If a league would rather withhold pay for a no-show, that is a rule on
top of this, not a change to it: the DNS is already in `race_results`,
and the deduction would be an `adjustment` row.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import asyncpg

from bot import queries
from bot.market import escrow as escrow_engine
from bot.models import Contract

KIND_RACE_SALARY = "race_salary"
KIND_CARRY_IN = "carry_in"
KIND_ADJUSTMENT = "adjustment"

_ZERO = Decimal(0)


class EarningsError(Exception):
    """Raised for an earnings problem the caller should surface as-is."""


@dataclass(frozen=True)
class EarningCredit:
    """One driver paid for one race."""

    member_id: int
    driver_id: int
    contract_id: int
    team_id: int
    amount: Decimal


@dataclass(frozen=True)
class RoundEarningsOutcome:
    """What one round paid out to drivers."""

    round_id: int
    credits: tuple[EarningCredit, ...]
    total_paid: Decimal
    # Contracts whose salary for this round was already on the ledger.
    # Non-zero on a re-import, and expected there rather than a problem.
    already_paid: int
    # Contracts whose per-race share rounded to nothing. A salary below
    # one cent per race is legal and pays zero; the ledger rejects a
    # zero row, so these are counted and skipped rather than written.
    too_small_to_pay: int

    @property
    def paid_count(self) -> int:
        return len(self.credits)


async def credit_race(
    conn: asyncpg.Connection,
    *,
    guild_id: int,
    contract: Contract,
    member_id: int,
    round_id: int,
    races_per_season: int,
    actor_id: int | None = None,
) -> EarningCredit | None:
    """
    Credit one race's salary to one driver's career total.

    Returns None — changing nothing — when the contract has no race term,
    the share rounds to zero, or this (contract, round) was already paid.
    That last case is what makes a re-import safe.

    The share is `escrow.per_race_share`, the same function the team side
    charges with, so the two halves of a salary cannot drift apart.
    """
    if contract.term_races is None:
        return None
    share = escrow_engine.per_race_share(contract.contract_value, races_per_season)
    if share <= _ZERO:
        return None
    row_id = await queries.insert_driver_earning(
        conn,
        guild_id=guild_id,
        member_id=member_id,
        kind=KIND_RACE_SALARY,
        amount=share,
        season_id=contract.season_id,
        tier_id=contract.tier_id,
        driver_id=contract.driver_id,
        contract_id=contract.id,
        round_id=round_id,
        note=None,
        actor_id=actor_id,
    )
    if row_id is None:
        return None
    return EarningCredit(
        member_id=member_id,
        driver_id=contract.driver_id,
        contract_id=contract.id,
        team_id=contract.team_id,
        amount=share,
    )


async def credit_round_for_tier(
    conn: asyncpg.Connection,
    *,
    guild_id: int,
    season_id: int,
    tier_id: int,
    round_id: int,
    races_per_season: int,
    actor_id: int | None = None,
) -> RoundEarningsOutcome:
    """
    Pay one race's salary to every driver under contract in the tier.

    Called from the results import in the SAME transaction as the results
    themselves, so a round's facts and the earnings it created land
    together or not at all.

    Safe to re-run: a round already paid against a contract writes
    nothing and is counted in `already_paid`.
    """
    contracts = await queries.fetch_active_contracts_for_tier(
        conn, season_id, tier_id
    )
    members = await queries.fetch_member_ids_for_drivers(
        conn, [c.driver_id for c in contracts]
    )

    credits: list[EarningCredit] = []
    total = _ZERO
    already = 0
    too_small = 0

    for contract in contracts:
        member_id = members.get(contract.driver_id)
        if member_id is None:
            # The contract's driver row is gone. Nothing to credit a
            # career total to, and inventing a member id would attach
            # this money to whoever holds that id later.
            continue
        if contract.term_races is None:
            continue
        share = escrow_engine.per_race_share(
            contract.contract_value, races_per_season
        )
        if share <= _ZERO:
            too_small += 1
            continue
        credit = await credit_race(
            conn,
            guild_id=guild_id,
            contract=contract,
            member_id=member_id,
            round_id=round_id,
            races_per_season=races_per_season,
            actor_id=actor_id,
        )
        if credit is None:
            already += 1
            continue
        credits.append(credit)
        total += credit.amount

    return RoundEarningsOutcome(
        round_id=round_id,
        credits=tuple(credits),
        total_paid=total,
        already_paid=already,
        too_small_to_pay=too_small,
    )


async def adjust_career_total(
    conn: asyncpg.Connection,
    *,
    guild_id: int,
    member_id: int,
    amount: Decimal,
    note: str,
    kind: str = KIND_ADJUSTMENT,
    season_id: int | None = None,
    actor_id: int | None = None,
) -> Decimal:
    """
    Add to or subtract from a driver's career total by hand, and return
    the new total.

    Two uses. `carry_in` seeds a league that raced before earnings were
    tracked, so season 8's leaderboard does not open at zero for drivers
    with seven seasons behind them. `adjustment` corrects a mistake or
    records pay the bot could not know about.

    A note is required for both. This ledger is the only record of where
    a career total came from, and an unexplained adjustment is
    indistinguishable from a bug by the time anyone asks about it.
    """
    if amount == _ZERO:
        raise EarningsError("Amount must be non-zero.")
    if not note.strip():
        raise EarningsError("Earnings adjustments require a note for the audit trail.")
    if kind not in (KIND_CARRY_IN, KIND_ADJUSTMENT):
        raise EarningsError(
            f"{kind!r} is written by race imports, not by hand. "
            f"Use {KIND_ADJUSTMENT!r} or {KIND_CARRY_IN!r}."
        )
    await queries.insert_driver_earning(
        conn,
        guild_id=guild_id,
        member_id=member_id,
        kind=kind,
        amount=amount,
        season_id=season_id,
        note=note.strip(),
        actor_id=actor_id,
    )
    return await queries.fetch_career_earnings(conn, member_id, guild_id)
