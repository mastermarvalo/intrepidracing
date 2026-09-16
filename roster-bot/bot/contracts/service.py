"""
Contract-offer state machine.

Every transition — submit, deliver, driver_accept, driver_decline,
driver_counter, team_counter, team_withdraw, expire_offer,
commissioner_approve, commissioner_reject, void_contract — routes
through here. Cogs never mutate `offer.state` or `contract.state`
directly (CLAUDE.md §8 invariant), so the ledger, the state guards,
and the derived side-effects (role assignment, transactions post,
board refresh) all stay in one place.

The service functions are async but do NOT open their own
connections: the caller supplies one so a whole flow (validate →
insert offer → append ledger → refresh board) can share a
transaction. `run_now` is passed in for `expire_offer` so the
expiry task and the lazy-check-on-read path agree on "now."

Ledger contract: every state change writes at least one row (see
CLAUDE.md §2 rule 4 — "every money mutation is an append-only ledger
row"). Even zero-amount events (declines, withdrawals) go in so
audit history is complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg

from bot import queries
from bot.market import budget_ops, escrow_ops

_ZERO = Decimal(0)


class TransitionError(Exception):
    """Raised when a state transition is not legal from the current state."""


@dataclass
class ApprovalResult:
    contract_id: int
    external_ref: str
    # Phase 9. `escrow_id` is None when escrow is off for the season,
    # which is every pre-migration-015 season and stays true for all of
    # them — this is not an error and callers must not treat it as one.
    escrow_id: int | None = None


# ── open-state helpers ────────────────────────────────────────────────


_TERMINAL_OFFER_STATES = {
    "approved", "rejected", "declined", "withdrawn", "expired",
    # `countered` is terminal for the parent — the child offer
    # carries the negotiation forward. See migration 008 index.
    "countered",
}


def _is_open(state: str) -> bool:
    return state not in _TERMINAL_OFFER_STATES


def _now() -> datetime:
    return datetime.now(UTC)


# ── offer creation ────────────────────────────────────────────────────


async def submit_offer(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int,
    driver_id: int,
    team_id: int,
    offered_by: int,
    offer_kind: str,
    salary: Decimal,
    term_seasons: int,
    contract_type: str,
    signing_bonus: Decimal,
    incentives: str | None,
    message: str | None,
    ttl_hours: int,
    validation: dict,
    parent_offer_id: int | None = None,
    initial_state: str = "pending_driver",
) -> int:
    """
    Insert a new offer row in `initial_state` and log an
    `offer_created` ledger entry. Returns the new offer id.
    """
    expires_at = _now() + timedelta(hours=ttl_hours)
    offer_id = await queries.insert_offer(
        conn,
        season_id=season_id,
        tier_id=tier_id,
        driver_id=driver_id,
        team_id=team_id,
        offered_by=offered_by,
        offer_kind=offer_kind,
        salary=salary,
        term_seasons=term_seasons,
        contract_type=contract_type,
        state=initial_state,
        signing_bonus=signing_bonus,
        incentives=incentives,
        message=message,
        parent_offer_id=parent_offer_id,
        expires_at=expires_at,
        validation=validation,
    )
    await queries.append_ledger(
        conn,
        season_id=season_id,
        tier_id=tier_id,
        driver_id=driver_id,
        team_id=team_id,
        offer_id=offer_id,
        kind="offer_created",
        amount=salary,
        detail={
            "offer_kind": offer_kind,
            "term_seasons": term_seasons,
            "contract_type": contract_type,
            "parent_offer_id": parent_offer_id,
            "initial_state": initial_state,
        },
        actor_id=offered_by,
    )
    return offer_id


async def record_thread(
    conn: asyncpg.Connection, offer_id: int, thread_id: int
) -> None:
    """Store the negotiation thread id once it's been created."""
    await queries.set_offer_thread_id(conn, offer_id, thread_id)


# ── driver-side transitions ───────────────────────────────────────────


async def driver_accept(
    conn: asyncpg.Connection, offer_id: int, *, actor_id: int
) -> None:
    offer = await _load_open_offer(conn, offer_id)
    _require_state(offer, {"pending_driver", "pending_team"})
    await queries.update_offer_state(
        conn, offer_id,
        new_state="pending_approval",
        resolved_by=actor_id, resolved_at=_now(),
    )
    await queries.append_ledger(
        conn,
        season_id=offer.season_id, tier_id=offer.tier_id,
        driver_id=offer.driver_id, team_id=offer.team_id,
        offer_id=offer_id,
        kind="offer_accepted",
        amount=offer.salary,
        detail={"prior_state": offer.state},
        actor_id=actor_id,
    )


async def driver_decline(
    conn: asyncpg.Connection, offer_id: int, *, actor_id: int, note: str | None = None
) -> None:
    offer = await _load_open_offer(conn, offer_id)
    _require_state(offer, {"pending_driver", "pending_team"})
    await queries.update_offer_state(
        conn, offer_id,
        new_state="declined",
        resolved_by=actor_id, resolved_at=_now(),
    )
    await queries.append_ledger(
        conn,
        season_id=offer.season_id, tier_id=offer.tier_id,
        driver_id=offer.driver_id, team_id=offer.team_id,
        offer_id=offer_id,
        kind="offer_declined",
        amount=offer.salary,
        detail={"prior_state": offer.state, "note": note},
        actor_id=actor_id,
    )


async def driver_counter(
    conn: asyncpg.Connection,
    parent_offer_id: int,
    *,
    actor_id: int,
    salary: Decimal,
    term_seasons: int,
    signing_bonus: Decimal,
    incentives: str | None,
    message: str | None,
    ttl_hours: int,
    validation: dict,
) -> int:
    """
    Driver counters the team's offer: the parent goes to `countered`
    (terminal from the parent's perspective) and a new child in
    `pending_team` carries the driver's terms back.
    """
    parent = await _load_open_offer(conn, parent_offer_id)
    _require_state(parent, {"pending_driver"})
    await queries.update_offer_state(
        conn, parent_offer_id,
        new_state="countered",
        resolved_by=actor_id, resolved_at=_now(),
    )
    child_id = await submit_offer(
        conn,
        season_id=parent.season_id,
        tier_id=parent.tier_id,
        driver_id=parent.driver_id,
        team_id=parent.team_id,
        offered_by=actor_id,
        offer_kind=parent.offer_kind,
        salary=salary,
        term_seasons=term_seasons,
        contract_type=parent.contract_type,
        signing_bonus=signing_bonus,
        incentives=incentives,
        message=message,
        ttl_hours=ttl_hours,
        validation=validation,
        parent_offer_id=parent_offer_id,
        initial_state="pending_team",
    )
    await queries.append_ledger(
        conn,
        season_id=parent.season_id, tier_id=parent.tier_id,
        driver_id=parent.driver_id, team_id=parent.team_id,
        offer_id=parent_offer_id,
        kind="offer_countered",
        amount=salary,
        detail={"child_offer_id": child_id, "by": "driver"},
        actor_id=actor_id,
    )
    return child_id


# ── team-side transitions ─────────────────────────────────────────────


async def team_counter(
    conn: asyncpg.Connection,
    parent_offer_id: int,
    *,
    actor_id: int,
    salary: Decimal,
    term_seasons: int,
    signing_bonus: Decimal,
    incentives: str | None,
    message: str | None,
    ttl_hours: int,
    validation: dict,
) -> int:
    """Team counters the driver's counter (state = pending_team)."""
    parent = await _load_open_offer(conn, parent_offer_id)
    _require_state(parent, {"pending_team"})
    await queries.update_offer_state(
        conn, parent_offer_id,
        new_state="countered",
        resolved_by=actor_id, resolved_at=_now(),
    )
    child_id = await submit_offer(
        conn,
        season_id=parent.season_id,
        tier_id=parent.tier_id,
        driver_id=parent.driver_id,
        team_id=parent.team_id,
        offered_by=actor_id,
        offer_kind=parent.offer_kind,
        salary=salary,
        term_seasons=term_seasons,
        contract_type=parent.contract_type,
        signing_bonus=signing_bonus,
        incentives=incentives,
        message=message,
        ttl_hours=ttl_hours,
        validation=validation,
        parent_offer_id=parent_offer_id,
        initial_state="pending_driver",
    )
    await queries.append_ledger(
        conn,
        season_id=parent.season_id, tier_id=parent.tier_id,
        driver_id=parent.driver_id, team_id=parent.team_id,
        offer_id=parent_offer_id,
        kind="offer_countered",
        amount=salary,
        detail={"child_offer_id": child_id, "by": "team"},
        actor_id=actor_id,
    )
    return child_id


async def team_withdraw(
    conn: asyncpg.Connection, offer_id: int, *, actor_id: int
) -> None:
    offer = await _load_open_offer(conn, offer_id)
    _require_state(offer, {"draft", "pending_driver", "pending_team"})
    await queries.update_offer_state(
        conn, offer_id,
        new_state="withdrawn",
        resolved_by=actor_id, resolved_at=_now(),
    )
    await queries.append_ledger(
        conn,
        season_id=offer.season_id, tier_id=offer.tier_id,
        driver_id=offer.driver_id, team_id=offer.team_id,
        offer_id=offer_id,
        kind="offer_withdrawn",
        amount=offer.salary,
        detail={"prior_state": offer.state},
        actor_id=actor_id,
    )


# ── expiry ────────────────────────────────────────────────────────────


async def expire_offer(
    conn: asyncpg.Connection, offer_id: int
) -> bool:
    """
    Move a still-open offer to `expired`. Idempotent — returns False
    if the offer was already terminal. Called both from the periodic
    expiry loop and lazily by transitions that read a live offer.
    """
    offer = await queries.fetch_offer_by_id(conn, offer_id)
    if offer is None or not _is_open(offer.state):
        return False
    await queries.update_offer_state(
        conn, offer_id,
        new_state="expired",
        resolved_by=None, resolved_at=_now(),
    )
    await queries.append_ledger(
        conn,
        season_id=offer.season_id, tier_id=offer.tier_id,
        driver_id=offer.driver_id, team_id=offer.team_id,
        offer_id=offer_id,
        kind="offer_expired",
        amount=offer.salary,
        detail={"prior_state": offer.state,
                "expires_at": offer.expires_at.isoformat()},
        actor_id=None,
    )
    return True


async def expire_all_past_ttl(conn: asyncpg.Connection) -> int:
    """
    Scan every open offer past its TTL and expire it. Returns the
    number of offers expired — the caller (poll loop / lazy check)
    logs it.
    """
    expired = await queries.fetch_expired_open_offers(conn, _now())
    count = 0
    for offer in expired:
        if await expire_offer(conn, offer.id):
            count += 1
    return count


# ── commissioner transitions ──────────────────────────────────────────


async def commissioner_approve(
    conn: asyncpg.Connection,
    offer_id: int,
    *,
    actor_id: int,
    value_at_signing: Decimal | None,
) -> ApprovalResult:
    """
    Convert an accepted offer into an active contract. Dispatches on
    offer_kind:
      * `new` and `trade_and_sign` → create a fresh contracts row.
      * `extension` → find the existing active contract for
        (driver, team) and update its terms in place, per CLAUDE.md
        §2 rule 6 ("Contract Value resets only on
        extension/renegotiation"). If no active contract exists to
        extend, raises TransitionError — extension is not a signing
        path.
    """
    offer = await _load_open_offer(conn, offer_id)
    _require_state(offer, {"pending_approval"})

    if offer.offer_kind == "extension":
        return await _approve_extension(
            conn, offer, actor_id=actor_id, value_at_signing=value_at_signing,
        )
    return await _approve_new_signing(
        conn, offer, actor_id=actor_id, value_at_signing=value_at_signing,
    )


async def _approve_new_signing(
    conn, offer, *, actor_id: int, value_at_signing: Decimal | None,
) -> ApprovalResult:
    # Snapshot contract terms from the offer. Terms are immutable
    # once accepted — see CLAUDE.md §8 invariant.
    external_ref = _external_ref(offer)
    contract_id = await queries.insert_contract(
        conn,
        season_id=offer.season_id,
        tier_id=offer.tier_id,
        driver_id=offer.driver_id,
        team_id=offer.team_id,
        contract_value=offer.salary,
        signing_bonus=offer.signing_bonus,
        max_incentives=_parse_incentives_amount(offer.incentives),
        term_seasons=offer.term_seasons,
        contract_type=offer.contract_type,
        state="active",
        value_at_signing=value_at_signing,
        approved_by=actor_id,
        external_ref=external_ref,
    )
    await queries.update_offer_state(
        conn, offer.id,
        new_state="approved",
        resolved_by=actor_id, resolved_at=_now(),
    )
    await queries.append_ledger(
        conn,
        season_id=offer.season_id, tier_id=offer.tier_id,
        driver_id=offer.driver_id, team_id=offer.team_id,
        offer_id=offer.id, contract_id=contract_id,
        kind="offer_approved",
        amount=offer.salary,
        detail={"external_ref": external_ref, "path": "new_signing"},
        actor_id=actor_id,
    )
    await queries.append_ledger(
        conn,
        season_id=offer.season_id, tier_id=offer.tier_id,
        driver_id=offer.driver_id, team_id=offer.team_id,
        offer_id=offer.id, contract_id=contract_id,
        kind="contract_signed",
        amount=offer.salary,
        detail={
            "contract_type": offer.contract_type,
            "term_seasons": offer.term_seasons,
            "signing_bonus": str(offer.signing_bonus),
            "value_at_signing": (
                str(value_at_signing) if value_at_signing is not None else None
            ),
            "external_ref": external_ref,
        },
        actor_id=actor_id,
    )
    # Open the escrow holding. Zero money moves here: salary is taken
    # race by race as results land, not in a lump at signing, so a
    # contract signed between rounds costs the team nothing until the
    # next race is imported. Returns None when escrow is off.
    contract = await queries.fetch_contract_by_id(conn, contract_id)
    escrow_id = None
    if contract is not None:
        escrow_id = await escrow_ops.open_for_contract(
            conn, contract=contract, tier_id=offer.tier_id, actor_id=actor_id,
        )
    return ApprovalResult(
        contract_id=contract_id, external_ref=external_ref, escrow_id=escrow_id,
    )


async def _approve_extension(
    conn, offer, *, actor_id: int, value_at_signing: Decimal | None,
) -> ApprovalResult:
    existing = await queries.fetch_active_contract_for_driver(
        conn, offer.driver_id
    )
    if existing is None or existing.team_id != offer.team_id:
        raise TransitionError(
            f"Cannot extend offer {offer.id}: driver has no active "
            "contract with this team. Use a new-signing offer instead."
        )
    old_value = existing.contract_value
    old_term = existing.term_seasons
    await queries.update_contract_terms(
        conn, existing.id,
        contract_value=offer.salary,
        term_seasons=offer.term_seasons,
        signing_bonus=offer.signing_bonus,
    )
    await queries.update_offer_state(
        conn, offer.id,
        new_state="approved",
        resolved_by=actor_id, resolved_at=_now(),
    )
    await queries.append_ledger(
        conn,
        season_id=offer.season_id, tier_id=offer.tier_id,
        driver_id=offer.driver_id, team_id=offer.team_id,
        offer_id=offer.id, contract_id=existing.id,
        kind="offer_approved",
        amount=offer.salary,
        detail={"external_ref": existing.external_ref, "path": "extension"},
        actor_id=actor_id,
    )
    await queries.append_ledger(
        conn,
        season_id=offer.season_id, tier_id=offer.tier_id,
        driver_id=offer.driver_id, team_id=offer.team_id,
        offer_id=offer.id, contract_id=existing.id,
        kind="contract_extended",
        amount=offer.salary,
        detail={
            "old_contract_value": str(old_value),
            "old_term_seasons": old_term,
            "new_contract_value": str(offer.salary),
            "new_term_seasons": offer.term_seasons,
            "value_at_signing": (
                str(value_at_signing) if value_at_signing is not None else None
            ),
        },
        actor_id=actor_id,
    )
    return ApprovalResult(
        contract_id=existing.id,
        external_ref=existing.external_ref or _external_ref(offer),
    )


async def commissioner_reject(
    conn: asyncpg.Connection,
    offer_id: int,
    *,
    actor_id: int,
    note: str | None = None,
) -> None:
    offer = await _load_open_offer(conn, offer_id)
    _require_state(offer, {"pending_approval"})
    await queries.update_offer_state(
        conn, offer_id,
        new_state="rejected",
        resolved_by=actor_id, resolved_at=_now(),
    )
    await queries.append_ledger(
        conn,
        season_id=offer.season_id, tier_id=offer.tier_id,
        driver_id=offer.driver_id, team_id=offer.team_id,
        offer_id=offer_id,
        kind="offer_rejected",
        amount=offer.salary,
        detail={"note": note},
        actor_id=actor_id,
    )


async def void_contract(
    conn: asyncpg.Connection,
    contract_id: int,
    *,
    actor_id: int,
    note: str | None = None,
) -> escrow_ops.SettlementResult | None:
    """
    Void a contract and return its escrow in full.

    Returns the settlement, or None when the contract held no escrow —
    which is the case for every season signed before escrow was enabled.

    A void is an administrative erasure: the contract is being treated as
    something that should never have existed. So no P/L is applied, and
    `market_value=None` is passed deliberately rather than omitted — the
    team gets back exactly what it put in, no profit and no loss, however
    the driver's value has moved since.
    """
    contract = await queries.fetch_contract_by_id(conn, contract_id)
    if contract is None:
        raise TransitionError(f"No contract with id {contract_id}")
    if contract.state != "active":
        raise TransitionError(
            f"Contract {contract_id} is `{contract.state}`, not `active`."
        )
    # Same guard as `release_contract`. Voiding a contract a pending
    # trade depends on leaves that trade unresolvable: approval fails
    # with "no longer active" and no button in the queue can clear it.
    in_trade = await queries.fetch_trade_involves_contract(conn, contract_id)
    if in_trade is not None:
        raise TransitionError(
            f"Contract {contract_id} is part of open trade {in_trade.id}. "
            "Withdraw or resolve the trade first."
        )
    await queries.void_contract(conn, contract_id, actor_id)
    await queries.append_ledger(
        conn,
        season_id=contract.season_id, tier_id=contract.tier_id,
        driver_id=contract.driver_id, team_id=contract.team_id,
        contract_id=contract_id,
        kind="contract_voided",
        amount=contract.contract_value,
        detail={"note": note},
        actor_id=actor_id,
    )
    return await escrow_ops.settle_holding(
        conn,
        contract=contract,
        reason=escrow_ops.REASON_VOID,
        actor_id=actor_id,
        note=note,
        market_value=None,
    )


# ── Phase 5: release / buyout / trades / promotion ──────────────────


async def release_contract(
    conn: asyncpg.Connection,
    contract_id: int,
    *,
    actor_id: int,
    market_value_at_release: Decimal | None,
    note: str | None = None,
) -> escrow_ops.SettlementResult | None:
    """
    End an active contract. `market_value_at_release` is captured in
    the ledger detail so the driver's frozen P/L (market − contract)
    is preserved even after future market moves.

    Also settles the contract's escrow, returning the settlement or None
    when there was none. An early release pro-rates the P/L by races
    served, so letting a driver go after one race of a long deal does not
    hand the team — or cost it — a whole term's worth of movement.
    """
    contract = await queries.fetch_contract_by_id(conn, contract_id)
    if contract is None:
        raise TransitionError(f"No contract with id {contract_id}")
    if contract.state != "active":
        raise TransitionError(
            f"Contract {contract_id} is `{contract.state}`, not `active`."
        )
    # Refuse if the contract is caught up in an open trade — that
    # trade would silently break if the underlying contract vanished.
    in_trade = await queries.fetch_trade_involves_contract(conn, contract_id)
    if in_trade is not None:
        raise TransitionError(
            f"Contract {contract_id} is part of open trade {in_trade.id}. "
            "Withdraw or resolve the trade first."
        )
    await queries.terminate_contract(conn, contract_id, state="terminated")
    pl_at_release = (
        (market_value_at_release - contract.contract_value)
        if market_value_at_release is not None else None
    )
    await queries.append_ledger(
        conn,
        season_id=contract.season_id, tier_id=contract.tier_id,
        driver_id=contract.driver_id, team_id=contract.team_id,
        contract_id=contract_id,
        kind="release",
        amount=contract.contract_value,
        detail={
            "note": note,
            "market_value_at_release": (
                str(market_value_at_release)
                if market_value_at_release is not None else None
            ),
            "contract_value": str(contract.contract_value),
            "pl_at_release": (
                str(pl_at_release) if pl_at_release is not None else None
            ),
        },
        actor_id=actor_id,
    )
    # Reuse the value the caller already resolved rather than re-reading
    # it, so the settlement and `pl_at_release` above can never disagree.
    return await escrow_ops.settle_holding(
        conn,
        contract=contract,
        reason=escrow_ops.REASON_RELEASE,
        actor_id=actor_id,
        note=note,
        market_value=market_value_at_release,
    )


async def buyout_contract(
    conn: asyncpg.Connection,
    contract_id: int,
    *,
    actor_id: int,
    buyout_amount: Decimal,
    market_value_at_release: Decimal | None,
    note: str | None = None,
) -> escrow_ops.SettlementResult | None:
    """
    Buy out a contract: release it AND leave a dead-money row on the
    team's books for the season. Cap enforcement counts dead_money
    via `queries.fetch_team_effective_payroll`.

    The escrow settles too, and the two are independent: the escrow comes
    back (adjusted for P/L over the races actually served) while the
    buyout fee stays charged as dead money. A team buying out a deal
    therefore recovers its unserved salary but still pays to get out.
    """
    contract = await queries.fetch_contract_by_id(conn, contract_id)
    if contract is None:
        raise TransitionError(f"No contract with id {contract_id}")
    if contract.state != "active":
        raise TransitionError(
            f"Contract {contract_id} is `{contract.state}`, not `active`."
        )
    if buyout_amount < _ZERO:
        raise TransitionError("Buyout amount cannot be negative.")
    in_trade = await queries.fetch_trade_involves_contract(conn, contract_id)
    if in_trade is not None:
        raise TransitionError(
            f"Contract {contract_id} is part of open trade {in_trade.id}. "
            "Withdraw or resolve the trade first."
        )

    await queries.terminate_contract(conn, contract_id, state="terminated")
    dead_id = await queries.insert_dead_money(
        conn,
        season_id=contract.season_id,
        tier_id=contract.tier_id,
        team_id=contract.team_id,
        amount=buyout_amount,
        source_contract_id=contract_id,
        note=note,
        actor_id=actor_id,
    )
    await queries.append_ledger(
        conn,
        season_id=contract.season_id, tier_id=contract.tier_id,
        driver_id=contract.driver_id, team_id=contract.team_id,
        contract_id=contract_id,
        kind="buyout",
        amount=buyout_amount,
        detail={
            "note": note,
            "buyout_amount": str(buyout_amount),
            "contract_value": str(contract.contract_value),
            "dead_money_id": dead_id,
            "market_value_at_release": (
                str(market_value_at_release)
                if market_value_at_release is not None else None
            ),
        },
        actor_id=actor_id,
    )
    return await escrow_ops.settle_holding(
        conn,
        contract=contract,
        reason=escrow_ops.REASON_BUYOUT,
        actor_id=actor_id,
        note=note,
        market_value=market_value_at_release,
    )


async def propose_trade(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    proposing_team_id: int,
    other_team_id: int,
    proposed_by: int,
    items: list[tuple[int, int]],   # (from_team_id, contract_id) pairs
    message: str | None,
    ttl_hours: int,
) -> int:
    """
    Create a trade in `pending_other` state. `items` is the list of
    contracts moving — at least one from each side to be a valid
    swap. Phase 5 MVP does 1-for-1, but the schema accepts any
    number of items.
    """
    if proposing_team_id == other_team_id:
        raise TransitionError("A team cannot trade with itself.")
    if not items:
        raise TransitionError("A trade must move at least one contract.")

    valid_teams = {proposing_team_id, other_team_id}
    for from_team_id, contract_id in items:
        if from_team_id not in valid_teams:
            raise TransitionError(
                f"Trade item contract {contract_id}: from_team_id "
                f"{from_team_id} is not one of the two trading teams."
            )
        contract = await queries.fetch_contract_by_id(conn, contract_id)
        if contract is None:
            raise TransitionError(f"No contract with id {contract_id}.")
        if contract.state != "active":
            raise TransitionError(
                f"Contract {contract_id} is `{contract.state}`, not `active`."
            )
        if contract.team_id != from_team_id:
            raise TransitionError(
                f"Contract {contract_id} is owned by team {contract.team_id}, "
                f"not {from_team_id}."
            )

    expires_at = _now() + timedelta(hours=ttl_hours)
    trade_id = await queries.insert_trade(
        conn,
        season_id=season_id,
        proposing_team_id=proposing_team_id,
        other_team_id=other_team_id,
        proposed_by=proposed_by,
        state="pending_other",
        message=message,
        expires_at=expires_at,
    )
    for from_team_id, contract_id in items:
        await queries.insert_trade_item(
            conn, trade_id, from_team_id=from_team_id, contract_id=contract_id,
        )
    # Ledger entry per trading team so the audit rows show up on
    # both cap sheets.
    for team_id in (proposing_team_id, other_team_id):
        await queries.append_ledger(
            conn,
            season_id=season_id,
            tier_id=(await queries.fetch_contract_by_id(conn, items[0][1])).tier_id,
            team_id=team_id,
            kind="trade",
            detail={
                "trade_id": trade_id,
                "event": "proposed",
                "proposing_team_id": proposing_team_id,
                "other_team_id": other_team_id,
                "items": [
                    {"from_team_id": ft, "contract_id": cid}
                    for ft, cid in items
                ],
            },
            actor_id=proposed_by,
        )
    return trade_id


async def accept_trade(
    conn: asyncpg.Connection, trade_id: int, *, actor_id: int
) -> None:
    trade = await _load_open_trade(conn, trade_id)
    _require_trade_state(trade, {"pending_other"})
    await queries.update_trade_state(
        conn, trade_id,
        new_state="pending_approval",
        resolved_by=actor_id, resolved_at=_now(),
    )
    await queries.append_ledger(
        conn,
        season_id=trade.season_id,
        tier_id=(await _first_item_tier(conn, trade_id)),
        team_id=trade.other_team_id,
        kind="trade",
        detail={"trade_id": trade_id, "event": "accepted"},
        actor_id=actor_id,
    )


async def decline_trade(
    conn: asyncpg.Connection, trade_id: int, *, actor_id: int, note: str | None = None
) -> None:
    trade = await _load_open_trade(conn, trade_id)
    _require_trade_state(trade, {"pending_other"})
    await queries.update_trade_state(
        conn, trade_id,
        new_state="declined",
        resolved_by=actor_id, resolved_at=_now(),
    )
    await queries.append_ledger(
        conn,
        season_id=trade.season_id,
        tier_id=(await _first_item_tier(conn, trade_id)),
        team_id=trade.other_team_id,
        kind="trade",
        detail={"trade_id": trade_id, "event": "declined", "note": note},
        actor_id=actor_id,
    )


async def withdraw_trade(
    conn: asyncpg.Connection, trade_id: int, *, actor_id: int
) -> None:
    trade = await _load_open_trade(conn, trade_id)
    _require_trade_state(trade, {"draft", "pending_other", "accepted"})
    await queries.update_trade_state(
        conn, trade_id,
        new_state="withdrawn",
        resolved_by=actor_id, resolved_at=_now(),
    )
    await queries.append_ledger(
        conn,
        season_id=trade.season_id,
        tier_id=(await _first_item_tier(conn, trade_id)),
        team_id=trade.proposing_team_id,
        kind="trade",
        detail={"trade_id": trade_id, "event": "withdrawn"},
        actor_id=actor_id,
    )


async def expire_trade(conn: asyncpg.Connection, trade_id: int) -> bool:
    trade = await queries.fetch_trade_by_id(conn, trade_id)
    if trade is None or trade.state not in queries.OPEN_TRADE_STATES:
        return False
    await queries.update_trade_state(
        conn, trade_id,
        new_state="expired",
        resolved_by=None, resolved_at=_now(),
    )
    await queries.append_ledger(
        conn,
        season_id=trade.season_id,
        tier_id=(await _first_item_tier(conn, trade_id)),
        team_id=trade.proposing_team_id,
        kind="trade",
        detail={
            "trade_id": trade_id, "event": "expired",
            "expires_at": trade.expires_at.isoformat(),
        },
        actor_id=None,
    )
    return True


async def expire_all_past_ttl_trades(conn: asyncpg.Connection) -> int:
    expired = await queries.fetch_expired_open_trades(conn, _now())
    count = 0
    for trade in expired:
        if await expire_trade(conn, trade.id):
            count += 1
    return count


async def commissioner_approve_trade(
    conn: asyncpg.Connection, trade_id: int, *, actor_id: int
) -> None:
    """
    Execute a trade: for each item, move the contract's team_id to
    the opposite team. contract_value is untouched (CLAUDE.md §2
    rule 6). Contract's tier_id follows its new team's tier only if
    the new team's driver's tier differs — Phase 5 MVP holds trades
    within a single tier, so tier_id is not rewritten here; use
    /market-admin promote/relegate for cross-tier moves before
    proposing a trade.
    """
    trade = await _load_open_trade(conn, trade_id)
    _require_trade_state(trade, {"pending_approval"})
    items = await queries.fetch_trade_items(conn, trade_id)

    await _require_trade_affordable(conn, trade, items)

    approved_ref = f"TR-S{trade.season_id}-{trade.id:04d}"
    for item in items:
        new_team_id = (
            trade.other_team_id if item.from_team_id == trade.proposing_team_id
            else trade.proposing_team_id
        )
        contract = await queries.fetch_contract_by_id(conn, item.contract_id)
        if contract is None or contract.state != "active":
            raise TransitionError(
                f"Contract {item.contract_id} is no longer active — trade "
                "cannot be approved."
            )
        # Hand the escrow over with the contract. The selling team is
        # settled for the races it actually served — it gets its money
        # back adjusted for how the driver moved on its watch — and the
        # buying team opens a fresh holding that starts accruing from the
        # next imported race. Settling BEFORE the transfer so the holding
        # is closed against the team that owned it.
        served_at_transfer = await escrow_ops.races_served(conn, contract)
        await escrow_ops.settle_holding(
            conn,
            contract=contract,
            reason=escrow_ops.REASON_TRADE,
            actor_id=actor_id,
            note=f"Traded to team {new_team_id} (trade {trade_id})",
        )
        await queries.transfer_contract(
            conn, item.contract_id, new_team_id=new_team_id,
        )
        moved = await queries.fetch_contract_by_id(conn, item.contract_id)
        if moved is not None:
            await escrow_ops.open_for_contract(
                conn,
                contract=moved,
                tier_id=contract.tier_id,
                actor_id=actor_id,
                races_served_at_open=served_at_transfer,
            )
        await queries.append_ledger(
            conn,
            season_id=trade.season_id, tier_id=contract.tier_id,
            driver_id=contract.driver_id,
            team_id=new_team_id,
            contract_id=item.contract_id,
            kind="trade",
            amount=contract.contract_value,
            detail={
                "trade_id": trade_id,
                "event": "contract_transferred",
                "from_team_id": item.from_team_id,
                "to_team_id": new_team_id,
                "contract_value": str(contract.contract_value),
                "approved_ref": approved_ref,
            },
            actor_id=actor_id,
        )

    await queries.update_trade_state(
        conn, trade_id,
        new_state="approved",
        resolved_by=actor_id, resolved_at=_now(),
        approved_ref=approved_ref,
    )


async def commissioner_reject_trade(
    conn: asyncpg.Connection, trade_id: int, *,
    actor_id: int, note: str | None = None,
) -> None:
    trade = await _load_open_trade(conn, trade_id)
    _require_trade_state(trade, {"pending_approval"})
    await queries.update_trade_state(
        conn, trade_id,
        new_state="rejected",
        resolved_by=actor_id, resolved_at=_now(),
    )
    await queries.append_ledger(
        conn,
        season_id=trade.season_id,
        tier_id=(await _first_item_tier(conn, trade_id)),
        team_id=trade.proposing_team_id,
        kind="trade",
        detail={"trade_id": trade_id, "event": "rejected", "note": note},
        actor_id=actor_id,
    )


async def move_driver_between_tiers(
    conn: asyncpg.Connection,
    driver_id: int,
    *,
    new_tier_id: int,
    actor_id: int,
    note: str | None = None,
) -> None:
    """
    Promotion/relegation. Moves the driver to `new_tier_id` and moves
    the driver's active contract (if any) to the same tier. Writes a
    ledger entry with old→new tier ids.
    """
    driver = await queries.fetch_driver_by_id(conn, driver_id)
    if driver is None:
        raise TransitionError(f"No driver with id {driver_id}")
    old_tier_id = driver.tier_id
    if old_tier_id == new_tier_id:
        raise TransitionError(
            f"Driver {driver_id} is already in tier {new_tier_id}."
        )

    # One member can hold a driver row in more than one tier at once —
    # a T3 regular who also keeps a T2 reserve seat. `drivers` is
    # UNIQUE (season_id, tier_id, member_id), so moving one of those
    # rows onto a tier the member already occupies is a collision, not
    # a move. Catch it here: `set_driver_tier` below is a bare UPDATE,
    # and the UniqueViolationError it raises reaches the commissioner
    # as a generic database notice with no way to act on it.
    clash = await queries.fetch_driver(
        conn, driver.season_id, new_tier_id, driver.member_id,
    )
    if clash is not None:
        tier = await queries.fetch_tier_by_id(conn, new_tier_id)
        where = f"`{tier.label}`" if tier is not None else f"tier {new_tier_id}"
        raise TransitionError(
            f"{driver.display_name} already has an entry in {where} "
            f"(driver id {clash.id}), so there is nothing to move them "
            "into. A driver may hold a seat in several tiers, but not "
            f"two in the same one. Either move driver {clash.id} "
            "instead, or remove it first."
        )

    await queries.set_driver_tier(conn, driver_id, new_tier_id)
    active = await queries.fetch_active_contract_for_driver(conn, driver_id)
    if active is not None:
        await queries.transfer_contract(
            conn, active.id,
            new_team_id=active.team_id, new_tier_id=new_tier_id,
        )

    await queries.append_ledger(
        conn,
        season_id=driver.season_id,
        tier_id=new_tier_id,
        driver_id=driver_id,
        team_id=active.team_id if active is not None else None,
        contract_id=active.id if active is not None else None,
        kind="status_change",
        detail={
            "event": "tier_change",
            "from_tier_id": old_tier_id,
            "to_tier_id": new_tier_id,
            "note": note,
            "moved_contract_id": active.id if active is not None else None,
        },
        actor_id=actor_id,
    )


# ── trade helpers ───────────────────────────────────────────────────


_TERMINAL_TRADE_STATES = {"approved", "rejected", "declined", "withdrawn", "expired"}


async def _require_trade_affordable(conn, trade, items) -> None:
    """
    Both teams' post-trade payroll must clear the spending cap AND
    their own budget. The cap is the league ceiling; the budget is the
    team's money — a trade that a rich team can afford is still blocked
    at the cap, and a trade under the cap is still blocked if the team
    cannot pay for it. Mirrors `cap_headroom_ok` / `budget_headroom_ok`
    for offers; the review embed shows the same arithmetic beforehand.
    """
    change: dict[int, Decimal] = {
        trade.proposing_team_id: Decimal(0),
        trade.other_team_id: Decimal(0),
    }
    for item in items:
        contract = await queries.fetch_contract_by_id(conn, item.contract_id)
        if contract is None:
            continue
        to_team = (
            trade.other_team_id if item.from_team_id == trade.proposing_team_id
            else trade.proposing_team_id
        )
        change[item.from_team_id] -= contract.contract_value
        change[to_team] += contract.contract_value

    cfg = await queries.fetch_league_config_row(conn, trade.season_id, None)
    for team_id, delta in change.items():
        if delta <= 0:
            continue  # shedding payroll never needs headroom
        payroll_after = (
            await queries.fetch_team_effective_payroll(conn, team_id, trade.season_id)
            + delta
        )
        if cfg is not None and payroll_after > cfg.salary_cap:
            raise TransitionError(
                f"Trade would put team {team_id} at {payroll_after}, over the "
                f"{cfg.salary_cap} spending cap."
            )
        snap = await budget_ops.snapshot(
            conn, season_id=trade.season_id, tier_id=None, team_id=team_id,
        )
        if snap is not None and payroll_after > snap.balance:
            raise TransitionError(
                f"Trade would put team {team_id} at {payroll_after}, more than "
                f"its {snap.balance} budget."
            )


async def _load_open_trade(conn, trade_id: int):
    trade = await queries.fetch_trade_by_id(conn, trade_id)
    if trade is None:
        raise TransitionError(f"No trade with id {trade_id}")
    if trade.state not in _TERMINAL_TRADE_STATES and trade.expires_at <= _now():
        await expire_trade(conn, trade_id)
        raise TransitionError(
            f"Trade {trade_id} has expired (past TTL)."
        )
    return trade


def _require_trade_state(trade, allowed: set[str]) -> None:
    if trade.state not in allowed:
        raise TransitionError(
            f"Trade {trade.id} is in state `{trade.state}`; "
            f"allowed here: {sorted(allowed)}."
        )


async def _first_item_tier(conn, trade_id: int) -> int:
    """
    Trades don't carry their own tier_id — pick one from any item so
    the ledger row has something sensible for tier-scoped views.
    """
    row = await conn.fetchrow(
        """
        SELECT c.tier_id
        FROM trade_items ti
        JOIN contracts c ON c.id = ti.contract_id
        WHERE ti.trade_id = $1
        LIMIT 1
        """,
        trade_id,
    )
    if row is None:
        # Trade with no items (shouldn't happen but keep the ledger
        # write from crashing) — 0 is not a valid tier_id anywhere,
        # but the FK will reject and the caller will see the error.
        return 0
    return row["tier_id"]


# ── internals ─────────────────────────────────────────────────────────


async def _load_open_offer(conn, offer_id: int):
    offer = await queries.fetch_offer_by_id(conn, offer_id)
    if offer is None:
        raise TransitionError(f"No offer with id {offer_id}")
    # Lazy expiry: if a poll missed this offer, expire it before we
    # let a caller act on stale state.
    if _is_open(offer.state) and offer.expires_at <= _now():
        await expire_offer(conn, offer_id)
        raise TransitionError(
            f"Offer {offer_id} has expired (past TTL). "
            f"Submit a new one if the terms still stand."
        )
    return offer


def _require_state(offer, allowed: set[str]) -> None:
    if offer.state not in allowed:
        raise TransitionError(
            f"Offer {offer.id} is in state `{offer.state}`; "
            f"allowed here: {sorted(allowed)}."
        )


def _external_ref(offer) -> str:
    """
    Human-readable transaction id — Tier code isn't in the offer row
    directly, so we use the tier_id as the middle segment. Format
    matches the CLAUDE.md example shape (`T2-S7-0142`).
    """
    return f"T{offer.tier_id}-S{offer.season_id}-{offer.id:04d}"


def _parse_incentives_amount(incentives: str | None) -> Decimal:
    """
    Offers store incentives as a free-text string ("performance
    bonuses, DOTD"). Contracts want a Decimal for the max_incentives
    column, so we try to pull the first parseable amount out. Rich
    parsing is a Phase 5+ concern; MVP: any string means zero and
    the amount is set explicitly at approval time if needed.
    """
    if not incentives:
        return _ZERO
    for token in incentives.replace(",", " ").split():
        try:
            return Decimal(token.lstrip("$").rstrip("Mm"))
        except (ValueError, ArithmeticError):
            continue
    return _ZERO
