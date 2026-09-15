"""
Contract carry-over across seasons (Phase 8).

A contract with `term_seasons = N` is N consecutive seasons of the same
money. The schema stores one `contracts` row per season served, linked
by `carried_from_contract_id` / `origin_contract_id` and numbered by
`season_index` (see migration 014). This module walks every active row
in a finished season and, for each one, either:

  * **carries** it — inserts the next season's row with contract_value,
    term, type, and max_incentives copied verbatim, then moves the old
    row to `carried`; or
  * **expires** it — the term is complete, the old row moves to
    `expired`, and the driver is a free agent in the new season; or
  * **skips** it — something a commissioner must look at (the target
    season has no tier with that code, or the driver already signed a
    fresh deal in the target season). Skipped rows stay `active` so a
    re-run reports them again rather than silently forgetting them.

Rules honoured here:

  * A carry-over is not a renegotiation: money is copied, never
    recomputed (CLAUDE.md §3 invariant 6). signing_bonus is a one-time
    payment already recorded on the origin row, so the continuation row
    carries 0.
  * Carried payroll is an existing obligation, so it is **not** gated by
    the cap or the budget. Teams that land over the cap are reported in
    the outcome for the commissioner to resolve; nothing is silently
    dropped.
  * Every state change writes a ledger row: `contract_carried` in both
    seasons (out of the old, into the new) and `contract_expired` in the
    old season.
  * Idempotent: a second run over the same pair of seasons finds no
    active rows to process and writes nothing. The partial unique index
    on carried_from_contract_id makes a double-carry impossible even
    under a race.

No Discord here. Role changes for expired drivers are the cog's job,
driven by `CarryOutcome.expired_members`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import asyncpg

from bot import queries
from bot.market import driver_ops, escrow_ops
from bot.models import Contract

STATE_ACTIVE = "active"
STATE_CARRIED = "carried"
STATE_EXPIRED = "expired"

KIND_CARRIED = "contract_carried"
KIND_EXPIRED = "contract_expired"

OUTCOME_CARRIED = "carried"
OUTCOME_EXPIRED = "expired"
OUTCOME_SKIPPED = "skipped"


class CarryOverError(Exception):
    """Raised for whole-run problems (bad season pair). Per-row problems
    become skipped lines, never exceptions."""


@dataclass(frozen=True)
class CarryLine:
    contract_id: int
    team_id: int
    member_id: int
    display_name: str
    tier_code: str
    contract_value: Decimal
    term_seasons: int
    season_index_from: int
    outcome: str
    new_contract_id: int | None = None
    new_tier_code: str | None = None
    driver_created: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class OverCap:
    team_id: int
    payroll: Decimal
    salary_cap: Decimal

    @property
    def over_by(self) -> Decimal:
        return self.payroll - self.salary_cap


@dataclass
class CarryOutcome:
    from_season_id: int
    to_season_id: int
    lines: list[CarryLine] = field(default_factory=list)
    over_cap: list[OverCap] = field(default_factory=list)

    @property
    def carried(self) -> int:
        return sum(1 for line in self.lines if line.outcome == OUTCOME_CARRIED)

    @property
    def expired(self) -> int:
        return sum(1 for line in self.lines if line.outcome == OUTCOME_EXPIRED)

    @property
    def skipped(self) -> int:
        return sum(1 for line in self.lines if line.outcome == OUTCOME_SKIPPED)

    @property
    def drivers_created(self) -> int:
        return sum(1 for line in self.lines if line.driver_created)

    @property
    def expired_members(self) -> list[tuple[int, int]]:
        """(member_id, team_id) for every driver whose deal ended — the
        cog uses this to drop team roles through roster_ops."""
        return [
            (line.member_id, line.team_id)
            for line in self.lines
            if line.outcome == OUTCOME_EXPIRED
        ]


def continues_next_season(contract: Contract) -> bool:
    """Pure: does this row have at least one more season after its own?"""
    return contract.season_index < contract.term_seasons


async def carry_over(
    conn: asyncpg.Connection,
    *,
    from_season_id: int,
    to_season_id: int,
    actor_id: int,
) -> CarryOutcome:
    if from_season_id == to_season_id:
        raise CarryOverError("Source and target seasons must differ.")
    from_season = await queries.fetch_season_by_id(conn, from_season_id)
    to_season = await queries.fetch_season_by_id(conn, to_season_id)
    if from_season is None or to_season is None:
        raise CarryOverError("Both seasons must exist.")
    if from_season.guild_id != to_season.guild_id:
        raise CarryOverError("Seasons belong to different servers.")

    outcome = CarryOutcome(from_season_id=from_season_id, to_season_id=to_season_id)
    tier_code_cache: dict[int, str] = {}
    touched_teams: set[int] = set()

    for contract in await queries.fetch_active_contracts_for_season(conn, from_season_id):
        driver = await queries.fetch_driver_by_id(conn, contract.driver_id)
        if driver is None:
            # FK is ON DELETE CASCADE, so this is unreachable in practice;
            # kept so a corrupt row is reported rather than crashing the run.
            continue
        if contract.tier_id not in tier_code_cache:
            tier = await queries.fetch_tier_by_id(conn, contract.tier_id)
            tier_code_cache[contract.tier_id] = tier.code if tier else "?"
        tier_code = tier_code_cache[contract.tier_id]
        base = dict(
            contract_id=contract.id, team_id=contract.team_id,
            member_id=driver.member_id, display_name=driver.display_name,
            tier_code=tier_code, contract_value=contract.contract_value,
            term_seasons=contract.term_seasons,
            season_index_from=contract.season_index,
        )

        if not continues_next_season(contract):
            await _expire(conn, contract, actor_id=actor_id, to_season_id=to_season_id)
            outcome.lines.append(CarryLine(outcome=OUTCOME_EXPIRED, **base))
            continue

        line = await _carry(
            conn, contract, driver_member_id=driver.member_id,
            driver_display_name=driver.display_name, tier_code=tier_code,
            to_season_id=to_season_id, actor_id=actor_id, base=base,
        )
        outcome.lines.append(line)
        if line.outcome == OUTCOME_CARRIED:
            touched_teams.add(contract.team_id)

    outcome.over_cap = await _teams_over_cap(conn, to_season_id, touched_teams)
    return outcome


async def _expire(
    conn: asyncpg.Connection, contract: Contract, *, actor_id: int, to_season_id: int
) -> None:
    closed = await queries.close_contract_at_season_end(
        conn, contract.id, new_state=STATE_EXPIRED
    )
    if not closed:
        return
    await queries.append_ledger(
        conn,
        season_id=contract.season_id, tier_id=contract.tier_id,
        driver_id=contract.driver_id, team_id=contract.team_id,
        contract_id=contract.id,
        kind=KIND_EXPIRED,
        amount=contract.contract_value,
        detail={
            "term_seasons": contract.term_seasons,
            "season_index": contract.season_index,
            "next_season_id": to_season_id,
            "origin_contract_id": contract.origin_contract_id or contract.id,
        },
        actor_id=actor_id,
    )


async def _carry(
    conn: asyncpg.Connection,
    contract: Contract,
    *,
    driver_member_id: int,
    driver_display_name: str,
    tier_code: str,
    to_season_id: int,
    actor_id: int,
    base: dict,
) -> CarryLine:
    # Where does the driver live in the target season? Prefer a row the
    # commissioner already created (they may have been re-tiered); fall
    # back to the same tier code; create the driver row if needed.
    target_driver = await queries.fetch_driver_by_member(
        conn, to_season_id, driver_member_id
    )
    driver_created = False
    if target_driver is not None:
        target_tier = await queries.fetch_tier_by_id(conn, target_driver.tier_id)
        target_driver_id = target_driver.id
    else:
        target_tier = await queries.fetch_tier(conn, to_season_id, tier_code)
        if target_tier is None:
            return CarryLine(
                outcome=OUTCOME_SKIPPED,
                reason=f"target season has no tier `{tier_code}`",
                **base,
            )
        enrolled = await driver_ops.enrol_driver(
            conn,
            season_id=to_season_id, tier_id=target_tier.id,
            seed=driver_ops.DriverSeed(
                member_id=driver_member_id, display_name=driver_display_name
            ),
            status=STATE_ACTIVE,
            actor_id=actor_id,
        )
        target_driver_id = enrolled.driver_id
        driver_created = enrolled.created
    if target_tier is None:  # pragma: no cover - defensive, tier FK guarantees it
        return CarryLine(outcome=OUTCOME_SKIPPED, reason="tier row missing", **base)

    if await queries.fetch_active_contract_for_driver(conn, target_driver_id) is not None:
        return CarryLine(
            outcome=OUTCOME_SKIPPED, driver_created=driver_created,
            reason="driver already has an active contract in the target season",
            **base,
        )

    origin_id = contract.origin_contract_id or contract.id

    # Race-based terms carry the SERVICE, not a fresh term. The new row
    # keeps the original `term_races` and inherits everything the chain
    # has already run as `races_served_before`, so a 36-race deal signed
    # in a 24-race season arrives owing 12 races rather than another 36.
    #
    # Deliberately not `escrow.carried_term_races` (shortening the term
    # instead): `settle` pro-rates an early exit by
    # races_served / term_races, and a shortened term would make the new
    # season's remainder look like a whole deal, over-crediting anyone
    # who released the driver early.
    served_so_far = await escrow_ops.races_served(conn, contract)

    new_id = await queries.insert_contract(
        conn,
        season_id=to_season_id, tier_id=target_tier.id,
        driver_id=target_driver_id, team_id=contract.team_id,
        contract_value=contract.contract_value,
        signing_bonus=Decimal("0"),
        max_incentives=contract.max_incentives,
        term_seasons=contract.term_seasons,
        term_races=contract.term_races,
        races_served_before=served_so_far,
        contract_type=contract.contract_type,
        state=STATE_ACTIVE,
        value_at_signing=contract.value_at_signing,
        approved_by=actor_id,
        external_ref=contract.external_ref,
        season_index=contract.season_index + 1,
        carried_from_contract_id=contract.id,
        origin_contract_id=origin_id,
        signed_at=contract.signed_at,
    )
    closed = await queries.close_contract_at_season_end(
        conn, contract.id, new_state=STATE_CARRIED
    )
    if not closed:  # pragma: no cover - fetch was 'active' moments ago
        raise CarryOverError(f"contract {contract.id} changed state mid-run")

    # Carry the escrow across the row boundary without moving money. The
    # term is unfinished, so it must not settle here, and the balance
    # must not be stranded on the closed row.
    held = await queries.fetch_held_escrow(conn, contract.id)
    if held is not None:
        await queries.repoint_contract_escrow(
            conn, held["id"], contract_id=new_id
        )

    detail = {
        "origin_contract_id": origin_id,
        "from_contract_id": contract.id,
        "to_contract_id": new_id,
        "from_season_id": contract.season_id,
        "to_season_id": to_season_id,
        "season_index": contract.season_index + 1,
        "term_seasons": contract.term_seasons,
        "term_races": contract.term_races,
        "races_served_before": served_so_far,
        "escrow_carried": held is not None,
        "tier_changed": target_tier.code != tier_code,
    }
    await queries.append_ledger(
        conn,
        season_id=contract.season_id, tier_id=contract.tier_id,
        driver_id=contract.driver_id, team_id=contract.team_id,
        contract_id=contract.id, kind=KIND_CARRIED,
        amount=contract.contract_value,
        detail={**detail, "direction": "out"}, actor_id=actor_id,
    )
    await queries.append_ledger(
        conn,
        season_id=to_season_id, tier_id=target_tier.id,
        driver_id=target_driver_id, team_id=contract.team_id,
        contract_id=new_id, kind=KIND_CARRIED,
        amount=contract.contract_value,
        detail={**detail, "direction": "in"}, actor_id=actor_id,
    )
    return CarryLine(
        outcome=OUTCOME_CARRIED, new_contract_id=new_id,
        new_tier_code=target_tier.code, driver_created=driver_created,
        **base,
    )


async def _teams_over_cap(
    conn: asyncpg.Connection, season_id: int, team_ids: set[int]
) -> list[OverCap]:
    if not team_ids:
        return []
    league = await queries.fetch_league_config_row(conn, season_id, None)
    if league is None:
        return []
    flagged: list[OverCap] = []
    for team_id in sorted(team_ids):
        payroll = await queries.fetch_team_effective_payroll(conn, team_id, season_id)
        if payroll > league.salary_cap:
            flagged.append(
                OverCap(team_id=team_id, payroll=payroll, salary_cap=league.salary_cap)
            )
    return flagged
