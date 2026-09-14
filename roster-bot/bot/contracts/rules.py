"""
Contract-offer validation rules — pure predicates over plain data.

Every rule in CLAUDE.md §7 is implemented as a named function that
takes the assembled `OfferInputs` and returns a `RuleResult`. The
aggregator `validate_offer` runs them all in a stable order and
returns an `OfferValidation` bundle: a boolean OK-to-submit, the list
of blocking failures, the list of non-blocking warnings, and a
JSON-safe snapshot of the check results (persisted into
`contract_offers.validation` at submit time so disputes are auditable
months later).

Contract:
  * Pure — no DB, no discord, no ambient time (`now` is passed in for
    tests). All literals live in `bot.limits` or come from
    `OfferInputs`; the ADR-001 guard scans this module.
  * Stable failure codes — the render layer looks them up to shape
    the "why not?" list; renaming a code is a schema-ish change.
  * Warnings are advisory: they surface a `⚠` in the review panel
    but do not block submission. Blocking failures do.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Sequence

_ZERO = Decimal(0)
_ONE = Decimal(1)


# ── inputs / outputs ────────────────────────────────────────────────


@dataclass(frozen=True)
class OfferInputs:
    # ── actor
    actor_id: int
    actor_is_principal: bool
    actor_is_admin: bool

    # ── target
    driver_present_in_tier: bool
    driver_status: str
    driver_has_active_contract: bool
    duplicate_open_offer_exists: bool

    # ── money
    salary: Decimal
    min_salary: Decimal
    signing_bonus: Decimal
    incentives_amount: Decimal
    max_incentive_pct: Decimal
    max_salary: Decimal | None = None

    # ── cap
    team_payroll_before: Decimal = _ZERO
    salary_cap: Decimal = _ZERO

    # ── slots
    active_slots_used: int = 0
    active_slots_max: int = 0
    has_linked_release: bool = False

    # ── term
    term_seasons: int = 1
    max_term_seasons: int = 1

    # ── window / mechanics
    offer_kind: str = "new"
    free_agency_open: bool = True


@dataclass(frozen=True)
class RuleResult:
    code: str
    ok: bool
    severity: str  # "block" | "warn" | "info"
    message: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OfferValidation:
    ok: bool
    results: tuple[RuleResult, ...]

    @property
    def blockers(self) -> list[RuleResult]:
        return [r for r in self.results if not r.ok and r.severity == "block"]

    @property
    def warnings(self) -> list[RuleResult]:
        return [r for r in self.results if not r.ok and r.severity == "warn"]

    def to_json(self) -> dict:
        return {"ok": self.ok, "results": [asdict(r) for r in self.results]}


# ── individual predicates ──────────────────────────────────────────


def actor_is_authorised(inputs: OfferInputs) -> RuleResult:
    if inputs.actor_is_admin or inputs.actor_is_principal:
        return _pass("actor_authorised", "Actor is authorised for this team.")
    return _block(
        "actor_not_authorised",
        "You are not authorised to make offers for this team. Team principals "
        "and server admins only.",
    )


def driver_exists_in_tier(inputs: OfferInputs) -> RuleResult:
    if inputs.driver_present_in_tier:
        return _pass("driver_in_tier", "Driver row exists in the selected tier.")
    return _block(
        "driver_missing_in_tier",
        "That driver is not registered in the selected tier. Add them "
        "first, then re-open this flow.",
    )


def driver_status_permits_signing(inputs: OfferInputs) -> RuleResult:
    signable = {"active", "reserve", "free_agent", "restricted_fa"}
    warn = {"inactive"}
    hard_block = {"suspended"}
    if inputs.driver_status in signable:
        return _pass(
            "status_permits_signing",
            f"Driver status `{inputs.driver_status}` permits signing.",
        )
    if inputs.driver_status in warn:
        return _warn(
            "status_inactive_warning",
            f"Driver is `{inputs.driver_status}` — a commissioner will need "
            "to approve any offer.",
        )
    if inputs.driver_status in hard_block:
        return _block(
            "status_suspended",
            "Driver is suspended and cannot be signed until reinstated.",
        )
    return _block(
        "status_unknown",
        f"Unknown driver status `{inputs.driver_status}`; refusing to sign.",
    )


def no_active_contract_conflict(inputs: OfferInputs) -> RuleResult:
    if not inputs.driver_has_active_contract:
        return _pass(
            "no_active_contract",
            "Driver has no active contract — a straight signing is fine.",
        )
    if inputs.offer_kind in ("trade_and_sign", "extension"):
        return _pass(
            "active_contract_via_trade_or_extension",
            "Driver has an active contract — allowed via "
            f"`{inputs.offer_kind}` flow.",
        )
    return _block(
        "active_contract_conflict",
        "Driver already has an active contract. Use a trade-and-sign or "
        "extension flow, or wait for the current deal to end.",
    )


def salary_within_bounds(inputs: OfferInputs) -> RuleResult:
    if inputs.salary < inputs.min_salary:
        return _block(
            "salary_below_min",
            f"Salary {inputs.salary} is below league minimum "
            f"{inputs.min_salary}.",
            detail={"salary": str(inputs.salary),
                    "min_salary": str(inputs.min_salary)},
        )
    if inputs.max_salary is not None and inputs.salary > inputs.max_salary:
        return _block(
            "salary_above_max",
            f"Salary {inputs.salary} is above league maximum "
            f"{inputs.max_salary}.",
            detail={"salary": str(inputs.salary),
                    "max_salary": str(inputs.max_salary)},
        )
    return _pass("salary_within_bounds", "Salary within min/max bounds.")


def cap_headroom_ok(inputs: OfferInputs) -> RuleResult:
    committed = inputs.team_payroll_before + inputs.salary + inputs.signing_bonus
    if committed <= inputs.salary_cap:
        return _pass(
            "cap_headroom_ok",
            f"Payroll after signing ({committed}) fits under the cap "
            f"({inputs.salary_cap}).",
            detail={
                "payroll_before": str(inputs.team_payroll_before),
                "salary": str(inputs.salary),
                "signing_bonus": str(inputs.signing_bonus),
                "committed_after": str(committed),
                "salary_cap": str(inputs.salary_cap),
            },
        )
    over = committed - inputs.salary_cap
    return _block(
        "cap_exceeded",
        f"Signing would put payroll at {committed}, {over} over the "
        f"{inputs.salary_cap} cap.",
        detail={
            "payroll_before": str(inputs.team_payroll_before),
            "salary": str(inputs.salary),
            "signing_bonus": str(inputs.signing_bonus),
            "committed_after": str(committed),
            "salary_cap": str(inputs.salary_cap),
            "over_cap": str(over),
        },
    )


def seat_available(inputs: OfferInputs) -> RuleResult:
    if inputs.offer_kind == "extension":
        return _pass(
            "seat_available_extension",
            "Extension of an existing seat — no new seat required.",
        )
    seats_free = inputs.active_slots_max - inputs.active_slots_used
    if seats_free > 0:
        return _pass(
            "seat_available",
            f"Team has {seats_free} active driver seat(s) free.",
            detail={"used": inputs.active_slots_used,
                    "max": inputs.active_slots_max},
        )
    if inputs.has_linked_release:
        return _pass(
            "seat_available_via_release",
            "Team is full but a linked release will free a seat before signing.",
        )
    return _block(
        "no_seat_available",
        f"Team is at {inputs.active_slots_used}/{inputs.active_slots_max} "
        "active driver seats. Release a driver or use trade-and-sign.",
        detail={"used": inputs.active_slots_used,
                "max": inputs.active_slots_max},
    )


def term_within_bounds(inputs: OfferInputs) -> RuleResult:
    if inputs.term_seasons < 1:
        return _block(
            "term_too_short",
            f"Term must be at least 1 season (got {inputs.term_seasons}).",
        )
    if inputs.term_seasons > inputs.max_term_seasons:
        return _block(
            "term_too_long",
            f"Term of {inputs.term_seasons} seasons exceeds league max of "
            f"{inputs.max_term_seasons}.",
        )
    return _pass("term_within_bounds", "Term within league bounds.")


def incentives_within_cap(inputs: OfferInputs) -> RuleResult:
    if inputs.incentives_amount < _ZERO:
        return _block(
            "incentives_negative",
            "Incentives amount cannot be negative.",
        )
    max_allowed = inputs.salary * inputs.max_incentive_pct
    if inputs.incentives_amount > max_allowed:
        return _block(
            "incentives_over_cap",
            f"Incentives ({inputs.incentives_amount}) exceed "
            f"{inputs.max_incentive_pct * Decimal('100')}% of salary "
            f"(max {max_allowed}).",
            detail={
                "incentives": str(inputs.incentives_amount),
                "max_allowed": str(max_allowed),
                "max_pct": str(inputs.max_incentive_pct),
            },
        )
    return _pass("incentives_within_cap", "Incentives within league cap.")


def free_agency_window_ok(inputs: OfferInputs) -> RuleResult:
    if inputs.offer_kind != "new":
        return _pass(
            "free_agency_not_applicable",
            f"`{inputs.offer_kind}` offers are not gated by the FA window.",
        )
    if inputs.free_agency_open:
        return _pass("free_agency_open", "Free agency window is open.")
    return _block(
        "free_agency_closed",
        "Free agency is closed. Wait for the commissioner to open the "
        "window before submitting a new-signing offer.",
    )


def no_duplicate_pending(inputs: OfferInputs) -> RuleResult:
    if not inputs.duplicate_open_offer_exists:
        return _pass(
            "no_duplicate_pending",
            "No other live offer from this team to this driver.",
        )
    return _block(
        "duplicate_pending_offer",
        "You already have a live offer with this driver. Withdraw it (or "
        "wait for it to resolve) before opening a new one.",
    )


# ── aggregator ─────────────────────────────────────────────────────


_RULES = (
    actor_is_authorised,
    driver_exists_in_tier,
    driver_status_permits_signing,
    no_active_contract_conflict,
    salary_within_bounds,
    cap_headroom_ok,
    seat_available,
    term_within_bounds,
    incentives_within_cap,
    free_agency_window_ok,
    no_duplicate_pending,
)


def validate_offer(inputs: OfferInputs) -> OfferValidation:
    """Run every rule; ok=True only when zero blockers remain."""
    results: list[RuleResult] = [rule(inputs) for rule in _RULES]
    ok = not any(
        r for r in results if not r.ok and r.severity == "block"
    )
    return OfferValidation(ok=ok, results=tuple(results))


def rule_codes() -> Sequence[str]:
    """
    Enumerate every failure code a rule can emit — the tests parametrise
    on this so a new rule can't slip in without either being added to
    the test matrix or explicitly opted out.
    """
    return (
        "actor_not_authorised",
        "driver_missing_in_tier",
        "status_inactive_warning",
        "status_suspended",
        "status_unknown",
        "active_contract_conflict",
        "salary_below_min",
        "salary_above_max",
        "cap_exceeded",
        "no_seat_available",
        "term_too_short",
        "term_too_long",
        "incentives_negative",
        "incentives_over_cap",
        "free_agency_closed",
        "duplicate_pending_offer",
    )


# ── helpers ────────────────────────────────────────────────────────


def _pass(code: str, message: str, *, detail: dict | None = None) -> RuleResult:
    return RuleResult(code=code, ok=True, severity="info", message=message,
                      detail=detail or {})


def _block(code: str, message: str, *, detail: dict | None = None) -> RuleResult:
    return RuleResult(code=code, ok=False, severity="block", message=message,
                      detail=detail or {})


def _warn(code: str, message: str, *, detail: dict | None = None) -> RuleResult:
    return RuleResult(code=code, ok=False, severity="warn", message=message,
                      detail=detail or {})


# Explicit re-export so the guard doesn't flag `_ONE` as dead — it's
# kept for symmetry with `_ZERO` and used by callers that want an
# obviously-named "1 season" constant.
__all__ = [
    "OfferInputs",
    "OfferValidation",
    "RuleResult",
    "actor_is_authorised",
    "driver_exists_in_tier",
    "driver_status_permits_signing",
    "no_active_contract_conflict",
    "salary_within_bounds",
    "cap_headroom_ok",
    "seat_available",
    "term_within_bounds",
    "incentives_within_cap",
    "free_agency_window_ok",
    "no_duplicate_pending",
    "validate_offer",
    "rule_codes",
    "_ONE",
]
