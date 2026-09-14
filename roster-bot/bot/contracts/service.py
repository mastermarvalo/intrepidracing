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

_ZERO = Decimal(0)


class TransitionError(Exception):
    """Raised when a state transition is not legal from the current state."""


@dataclass
class ApprovalResult:
    contract_id: int
    external_ref: str


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
    Convert an accepted offer into an ACTIVE contract row and mark
    the offer approved. `value_at_signing` should be the driver's
    latest published market value (or None) — the caller fetches it.
    """
    offer = await _load_open_offer(conn, offer_id)
    _require_state(offer, {"pending_approval"})

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
        conn, offer_id,
        new_state="approved",
        resolved_by=actor_id, resolved_at=_now(),
    )
    await queries.append_ledger(
        conn,
        season_id=offer.season_id, tier_id=offer.tier_id,
        driver_id=offer.driver_id, team_id=offer.team_id,
        offer_id=offer_id, contract_id=contract_id,
        kind="offer_approved",
        amount=offer.salary,
        detail={"external_ref": external_ref},
        actor_id=actor_id,
    )
    await queries.append_ledger(
        conn,
        season_id=offer.season_id, tier_id=offer.tier_id,
        driver_id=offer.driver_id, team_id=offer.team_id,
        offer_id=offer_id, contract_id=contract_id,
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
    return ApprovalResult(contract_id=contract_id, external_ref=external_ref)


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
) -> None:
    contract = await queries.fetch_contract_by_id(conn, contract_id)
    if contract is None:
        raise TransitionError(f"No contract with id {contract_id}")
    if contract.state != "active":
        raise TransitionError(
            f"Contract {contract_id} is `{contract.state}`, not `active`."
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
