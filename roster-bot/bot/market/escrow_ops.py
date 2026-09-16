"""
Salary escrow operations against an open connection.

Sits between the pure engine (`bot/market/escrow.py`) and the callers
that hold a `Connection`: `bot/contracts/service.py` at every point a
deal starts or ends, and the results-import path once per race. Nothing
here imports discord.

The three things that happen to escrow, and the invariants that keep
them honest:

* **Open** (`open_for_contract`) — a holding is created at zero when a
  contract becomes active. Zero, not the full salary: escrow charges as
  races are run, so a deal that never runs a race costs its team
  nothing. `uq_contract_escrow_one_held` makes a second live holding on
  the same contract row impossible at the database level.
* **Charge** (`charge_race`) — one race's share leaves the team's cash.
  Idempotent on `(contract_id, round_id)`, because a round re-imported
  after a stewards' decision must not advance the term twice or charge
  twice. A re-import returns `None` and changes nothing.
* **Settle** (`settle_holding`) — the holding returns, adjusted by the
  driver's P/L. The ledger rows and the audit record are written in the
  caller's transaction alongside the state change, so a settlement
  cannot half-exist.

Escrow follows `budget_config.escrow_enabled` and is deliberately
independent of `enforce_budget`: a league can take salary in cash
without policing headroom, or police headroom without taking cash.
Worth knowing when reading a budget that looks stricter or looser than
expected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import asyncpg

from bot import queries
from bot.market import escrow as escrow_engine
from bot.models import Contract

KIND_SALARY_ESCROW = "salary_escrow"
KIND_ESCROW_RETURN = "escrow_return"
KIND_ESCROW_PL = "escrow_pl"

# Declared as a transaction kind by migration 015 and, until now, never
# written by anything. A term ending is a contract event rather than a
# money event, which is why it belongs in `contract_ledger` and not in
# the budget ledger.
KIND_TERM_COMPLETED = "term_completed"

REASON_TERM_COMPLETE = "term_complete"
REASON_RELEASE = "release"
REASON_BUYOUT = "buyout"
REASON_VOID = "void"
REASON_TRADE = "trade"
REASON_SEASON_END = "season_end"

_ZERO = Decimal(0)


class EscrowError(Exception):
    """Raised for an escrow problem the caller should surface as-is."""


@dataclass(frozen=True)
class RaceCharge:
    """What one race's escrow charge did."""

    contract_id: int
    round_id: int
    team_id: int
    driver_id: int
    amount: Decimal
    total_held: Decimal
    races_served: int
    term_races: int
    term_complete: bool


@dataclass(frozen=True)
class SettlementResult:
    """A completed settlement: the arithmetic plus what was written."""

    settlement_id: int
    escrow_id: int
    contract_id: int
    team_id: int
    driver_id: int
    reason: str
    result: escrow_engine.Settlement


async def is_enabled(
    conn: asyncpg.Connection, *, season_id: int, tier_id: int | None
) -> bool:
    """
    Whether this season/tier takes salary in cash.

    A season with no `budget_config` row at all is treated as escrow-off,
    which is how every season behaved before migration 015.
    """
    cfg = await queries.fetch_budget_config(conn, season_id, tier_id)
    return bool(cfg is not None and cfg.escrow_enabled)


async def races_served(conn: asyncpg.Connection, contract: Contract) -> int:
    """
    How far through its term a contract is, counting the whole chain.

    A carried row starts partway through: `races_served_before` is what
    earlier rows served, and `contract_race_service` holds this row's
    own races. Summing beats re-walking the chain on every race night.
    """
    own = await queries.count_race_service(conn, contract.id)
    return contract.races_served_before + own


async def open_for_contract(
    conn: asyncpg.Connection,
    *,
    contract: Contract,
    tier_id: int | None = None,
    actor_id: int | None = None,
    races_served_at_open: int = 0,
) -> int | None:
    """
    Open a zero holding for a newly active contract row.

    Returns the holding's id, or None when escrow is off for the season
    or the contract already has a live holding. The already-open case is
    not an error: carry-over and trade paths can both reach a row that
    was opened moments earlier, and quietly reusing the existing holding
    is safer than raising and aborting a signing.

    A `term_races` of None means the caller built this Contract in memory
    without a term and never persisted it; there is no term to escrow
    against, so nothing is opened.
    """
    if contract.term_races is None:
        return None
    if not await is_enabled(
        conn, season_id=contract.season_id, tier_id=tier_id or contract.tier_id
    ):
        return None
    existing = await queries.fetch_held_escrow(conn, contract.id)
    if existing is not None:
        return existing["id"]
    escrow_id = await queries.insert_contract_escrow(
        conn,
        contract_id=contract.id,
        origin_contract_id=contract.origin_contract_id or contract.id,
        season_id=contract.season_id,
        team_id=contract.team_id,
        races_served_at_open=races_served_at_open,
    )
    await queries.append_ledger(
        conn,
        season_id=contract.season_id,
        tier_id=contract.tier_id,
        driver_id=contract.driver_id,
        team_id=contract.team_id,
        contract_id=contract.id,
        kind="escrow_opened",
        detail={
            "escrow_id": str(escrow_id),
            "term_races": str(contract.term_races),
            "races_served_before": str(contract.races_served_before),
        },
        actor_id=actor_id,
    )
    return escrow_id


async def record_race_served(
    conn: asyncpg.Connection,
    *,
    contract: Contract,
    round_id: int,
    amount: Decimal,
) -> int | None:
    """
    Count one race against a contract's term. Nothing to do with money.

    Serving a race is a *league* fact: the driver turned up for a round
    the contract covers, and the term is one race shorter. That is true
    whether or not the league models team cash, so this is deliberately
    independent of `escrow_enabled`.

    Keeping it separate fixes a real bug. The service row used to be
    written only inside `charge_race`, which returns early when escrow is
    off — so an escrow-off league recorded no service at all,
    `races_served` never advanced, and a race-based term never completed.
    Contracts stayed active forever and had to be ended by hand.

    `amount` is what was escrowed for this race, and is zero when no
    money moved. It is informational: `races_served` counts rows, never
    sums amounts, so a zero here still advances the term.

    Returns the new row's id, or None if this (contract, round) was
    already counted — the guard that makes a re-imported round safe.
    """
    if contract.term_races is None:
        return None
    return await queries.record_race_service(
        conn,
        contract_id=contract.id,
        round_id=round_id,
        team_id=contract.team_id,
        amount=amount,
    )


async def charge_race(
    conn: asyncpg.Connection,
    *,
    contract: Contract,
    round_id: int,
    races_per_season: int,
    tier_id: int | None = None,
    actor_id: int | None = None,
    service_counted: bool = False,
) -> RaceCharge | None:
    """
    Debit one race's share of salary from the team into escrow.

    Returns None — changing nothing — when escrow is off, the contract
    predates race terms, there is no live holding, or this round was
    already counted for this contract. That last case is the important
    one: results get re-imported, and the term must not advance twice.

    The charge is recorded three times on purpose, because each answers a
    different question: `contract_race_service` says the race was served
    (and makes re-imports safe), `contract_escrow.amount_held` says what
    the team can get back, and `team_budget_ledger` says where the money
    went.

    `service_counted` tells this function the caller already wrote the
    service row, so it must not try again and must not read a conflict as
    "already served". `charge_round_for_tier` records service itself —
    unconditionally, because a term advances whether or not escrow is on
    — and then calls here for the money. A direct caller leaves the flag
    alone and gets the self-contained behaviour, including the re-import
    guard.
    """
    if contract.term_races is None:
        return None
    if not await is_enabled(
        conn, season_id=contract.season_id, tier_id=tier_id or contract.tier_id
    ):
        return None
    holding = await queries.fetch_held_escrow(conn, contract.id)
    if holding is None:
        return None

    share = escrow_engine.per_race_share(contract.contract_value, races_per_season)

    if not service_counted:
        counted = await record_race_served(
            conn, contract=contract, round_id=round_id, amount=share,
        )
        if counted is None:
            return None

    total_held = holding["amount_held"]
    if share > _ZERO:
        total_held = await queries.add_escrow_held(conn, holding["id"], share)
        await queries.insert_budget_entry(
            conn,
            season_id=contract.season_id,
            team_id=contract.team_id,
            kind=KIND_SALARY_ESCROW,
            amount=-share,
            round_id=round_id,
            contract_id=contract.id,
            note="Salary escrowed for one race",
            detail={
                "contract_value": str(contract.contract_value),
                "races_per_season": str(races_per_season),
                "share": str(share),
            },
            actor_id=actor_id,
        )

    served = await races_served(conn, contract)
    complete = escrow_engine.is_term_complete(
        term_races=contract.term_races, races_served=served
    )

    await queries.append_ledger(
        conn,
        season_id=contract.season_id,
        tier_id=contract.tier_id,
        driver_id=contract.driver_id,
        team_id=contract.team_id,
        contract_id=contract.id,
        kind="escrow_charged",
        amount=share,
        detail={
            "round_id": str(round_id),
            "races_served": str(served),
            "term_races": str(contract.term_races),
        },
        actor_id=actor_id,
    )

    return RaceCharge(
        contract_id=contract.id,
        round_id=round_id,
        team_id=contract.team_id,
        driver_id=contract.driver_id,
        amount=share,
        total_held=Decimal(total_held),
        races_served=served,
        term_races=contract.term_races,
        term_complete=complete,
    )


async def settle_holding(
    conn: asyncpg.Connection,
    *,
    contract: Contract,
    reason: str,
    actor_id: int | None = None,
    note: str | None = None,
    market_value: Decimal | None = None,
) -> SettlementResult | None:
    """
    Close a contract's holding and pay it back, adjusted for P/L.

    Returns None when there is no live holding — a contract signed before
    escrow was switched on, or one already settled. Callers use that to
    decide whether to mention money in their receipt at all, rather than
    reporting a $0.00 settlement that never happened.

    `market_value` may be passed by a caller that already resolved the
    driver's value (an approval flow that captured a baseline, for
    instance); otherwise the latest published valuation is read here.
    None means the driver was never priced, and the engine then returns
    the escrow with the P/L recorded as unknown rather than as zero.

    Two ledger rows are written rather than one net row: the escrow
    coming back and the P/L are different facts, and a team reading its
    budget history is entitled to see which was which. Their amounts sum
    to exactly what the engine said to return, including when a loss was
    capped at the escrow.
    """
    holding = await queries.fetch_held_escrow(conn, contract.id)
    if holding is None:
        return None

    if market_value is None:
        market_value = await queries.fetch_latest_published_valuation(
            conn, contract.driver_id
        )

    served = await races_served(conn, contract)
    term = contract.term_races or served or 1

    # Settle over THIS holding's service window, not the contract's whole
    # life. They are the same thing for a signing, where the window opens
    # at zero. They differ after a trade: a team that picked up a contract
    # with 30 of 36 races already run served 6, and pro-rating it over 36
    # would pay it almost a full term's P/L for six races of exposure.
    opened_at_race = holding["races_served_at_open"]
    window_served = max(0, served - opened_at_race)
    window_term = max(1, term - opened_at_race)

    result = escrow_engine.settle(
        amount_held=Decimal(holding["amount_held"]),
        market_value=market_value,
        contract_value=contract.contract_value,
        races_served=window_served,
        term_races=window_term,
    )

    team_id = holding["team_id"]
    season_id = holding["season_id"]

    if result.amount_held > _ZERO:
        await queries.insert_budget_entry(
            conn,
            season_id=season_id,
            team_id=team_id,
            kind=KIND_ESCROW_RETURN,
            amount=result.amount_held,
            contract_id=contract.id,
            note=f"Escrow returned ({reason})",
            detail={
                "reason": reason,
                "races_served": str(served),
                "term_races": str(term),
            },
            actor_id=actor_id,
        )

    pl_amount = result.amount_returned - result.amount_held
    if pl_amount != _ZERO:
        await queries.insert_budget_entry(
            conn,
            season_id=season_id,
            team_id=team_id,
            kind=KIND_ESCROW_PL,
            amount=pl_amount,
            contract_id=contract.id,
            note=f"Contract P/L settled ({reason})",
            detail={
                "reason": reason,
                "market_value": str(result.market_value),
                "contract_value": str(result.contract_value),
                "pl_full_term": str(result.pl),
                "pl_applied": str(result.pl_applied),
                "clamped": str(result.clamped),
            },
            actor_id=actor_id,
        )

    await queries.mark_escrow_settled(conn, holding["id"])

    settlement_id = await queries.insert_escrow_settlement(
        conn,
        escrow_id=holding["id"],
        contract_id=contract.id,
        origin_contract_id=holding["origin_contract_id"],
        season_id=season_id,
        team_id=team_id,
        driver_id=contract.driver_id,
        reason=reason,
        amount_returned=result.amount_returned,
        market_value=result.market_value,
        contract_value=result.contract_value,
        pl=result.pl_applied,
        races_served=served,
        term_races=term,
        note=note,
        actor_id=actor_id,
    )

    await queries.append_ledger(
        conn,
        season_id=season_id,
        tier_id=contract.tier_id,
        driver_id=contract.driver_id,
        team_id=team_id,
        contract_id=contract.id,
        kind="escrow_settled",
        amount=result.amount_returned,
        detail={
            "reason": reason,
            "amount_held": str(result.amount_held),
            "amount_returned": str(result.amount_returned),
            "pl_applied": str(result.pl_applied),
            "clamped": str(result.clamped),
        },
        actor_id=actor_id,
    )

    return SettlementResult(
        settlement_id=settlement_id,
        escrow_id=holding["id"],
        contract_id=contract.id,
        team_id=team_id,
        driver_id=contract.driver_id,
        reason=reason,
        result=result,
    )


@dataclass(frozen=True)
class RoundEscrowOutcome:
    """
    What one race night did to escrow across a whole tier.

    `charges` and `settlements` are parallel to what a receipt needs to
    show: money taken into escrow for races served, and money paid back
    for terms that ended. `skipped` counts contracts nothing happened to
    — already counted for this round, or no term to serve — so a receipt
    can distinguish "nothing to do" from "nothing happened".

    `advanced` counts terms that moved one race closer to ending. With
    escrow on it tracks `charges`; with escrow off it is the only sign
    the import did anything, because no money moves and `charges` stays
    empty while terms still run down and still complete.
    """

    charges: list[RaceCharge] = field(default_factory=list)
    settlements: list[SettlementResult] = field(default_factory=list)
    completed_contract_ids: list[int] = field(default_factory=list)
    skipped: int = 0
    advanced: int = 0

    @property
    def total_charged(self) -> Decimal:
        return sum((c.amount for c in self.charges), _ZERO)

    @property
    def total_returned(self) -> Decimal:
        return sum(
            (s.result.amount_returned for s in self.settlements), _ZERO
        )


async def charge_round_for_tier(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int,
    round_id: int,
    races_per_season: int,
    actor_id: int | None = None,
) -> RoundEscrowOutcome:
    """
    Take one race's salary from every team in the tier, then settle the
    contracts whose term just ended.

    This is where salary actually leaves a team's cash. Called from the
    results import in the SAME transaction as the results themselves, so
    a round's facts and its money land together or not at all.

    Settlement happens in the same pass rather than on a later sweep: the
    race that completes a term is the moment the money is owed back, and
    deferring it would leave a team unable to spend cash it had earned
    until some other event happened to run.

    Serving the race and paying for it are two separate steps here, in
    that order, and the order matters. A term advances because a round
    was imported, not because money moved: an escrow-off league still
    runs its contracts down and still ends them. Recording service first
    — unconditionally — is what makes that true. Charging is then an
    extra that happens only when escrow is on and the contract has a
    live holding.

    That also fixes the narrower case of a contract signed while escrow
    was off and raced after it was switched on, or the reverse: it has no
    holding, so no money moves, but its term still completes on schedule
    instead of never.

    Safe to re-run. The service row is unique per (contract, round), so a
    re-imported sheet advances no term and takes no second payment.
    """
    charges: list[RaceCharge] = []
    settlements: list[SettlementResult] = []
    completed: list[int] = []
    skipped = 0
    advanced = 0
    escrow_on = await is_enabled(conn, season_id=season_id, tier_id=tier_id)
    contracts = await queries.fetch_active_contracts_for_tier(
        conn, season_id, tier_id
    )
    for contract in contracts:
        if contract.term_races is None:
            skipped += 1
            continue

        # Is there money to move for this contract? Escrow can be on for
        # the season while an individual contract has no holding, and
        # then the term must still run.
        holding = None
        if escrow_on:
            holding = await queries.fetch_held_escrow(conn, contract.id)
        share = (
            escrow_engine.per_race_share(contract.contract_value, races_per_season)
            if holding is not None
            else _ZERO
        )

        # Step one: count the race. None means this round was already
        # counted for this contract, so the whole contract is a no-op.
        if await record_race_served(
            conn, contract=contract, round_id=round_id, amount=share,
        ) is None:
            skipped += 1
            continue
        advanced += 1

        # Step two: take the money, if there is any to take.
        if holding is not None:
            charge = await charge_race(
                conn,
                contract=contract,
                round_id=round_id,
                races_per_season=races_per_season,
                tier_id=tier_id,
                actor_id=actor_id,
                service_counted=True,
            )
            if charge is not None:
                charges.append(charge)

        # Step three: end the term if it is done, escrow or no escrow.
        served = await races_served(conn, contract)
        if not escrow_engine.is_term_complete(
            term_races=contract.term_races, races_served=served
        ):
            continue
        # The term ran its full length, so settle at the unpro-rated P/L
        # and close the contract. `complete_contract` returning False
        # means another path got there first; the holding is then already
        # gone and `settle_holding` would return None anyway.
        if not await queries.complete_contract(conn, contract.id):
            continue
        completed.append(contract.id)
        # Recorded even when no money settles, so that a contract ending
        # is never invisible. With escrow off this is the only trace in
        # the ledger that the term ran out.
        await queries.append_ledger(
            conn,
            season_id=contract.season_id,
            tier_id=contract.tier_id,
            driver_id=contract.driver_id,
            team_id=contract.team_id,
            contract_id=contract.id,
            kind=KIND_TERM_COMPLETED,
            detail={
                "races_served": str(served),
                "term_races": str(contract.term_races),
                "round_id": str(round_id),
            },
            actor_id=actor_id,
        )
        settlement = await settle_holding(
            conn,
            contract=contract,
            reason=REASON_TERM_COMPLETE,
            actor_id=actor_id,
            note=f"Term complete after {served} race(s)",
        )
        if settlement is not None:
            settlements.append(settlement)
    return RoundEscrowOutcome(
        charges=charges,
        settlements=settlements,
        completed_contract_ids=completed,
        skipped=skipped,
        advanced=advanced,
    )
