"""
Shared driver-enrolment helpers.

`/market-admin driver add`, `sync`, and `sync-all` all need the same
underlying step: given a (season, tier, member_id, display_name), create
a `drivers` row if one doesn't already exist in that tier, and write a
`driver_registered` ledger entry so the audit trail records who enrolled
whom.

The helpers here take a `Connection` and plain data (member ids + display
names), never a discord.py Guild or Member — that keeps them testable
against the real `pg_conn_migrated` fixture without any discord fakes,
and lets the cog layer decide how members are sourced (a single mention,
a tier role's `.members`, or every tier role in the season).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import asyncpg

from bot import queries


@dataclass(frozen=True)
class DriverSeed:
    """Minimal payload the cog hands the helper for each candidate member."""

    member_id: int
    display_name: str


@dataclass(frozen=True)
class EnrolmentResult:
    seed: DriverSeed
    driver_id: int
    created: bool  # False when the driver row was already present


async def enrol_driver(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int,
    seed: DriverSeed,
    status: str,
    actor_id: int | None,
) -> EnrolmentResult:
    """
    Idempotent single-member enrolment. If the driver already exists in
    this tier, returns `created=False` and no ledger row is written.
    """
    existing = await queries.fetch_driver(conn, season_id, tier_id, seed.member_id)
    if existing is not None:
        return EnrolmentResult(seed=seed, driver_id=existing.id, created=False)

    driver_id = await queries.insert_driver(
        conn,
        season_id=season_id,
        tier_id=tier_id,
        member_id=seed.member_id,
        display_name=seed.display_name,
        status=status,
    )
    await queries.append_ledger(
        conn,
        season_id=season_id,
        tier_id=tier_id,
        driver_id=driver_id,
        kind="driver_registered",
        detail={
            "member_id": seed.member_id,
            "display_name": seed.display_name,
            "status": status,
        },
        actor_id=actor_id,
    )
    return EnrolmentResult(seed=seed, driver_id=driver_id, created=True)


async def sync_tier(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int,
    seeds: Iterable[DriverSeed],
    status: str,
    actor_id: int | None,
) -> list[EnrolmentResult]:
    """Enrol every seed missing in the tier; return one result per seed."""
    results: list[EnrolmentResult] = []
    for seed in seeds:
        results.append(
            await enrol_driver(
                conn,
                season_id=season_id,
                tier_id=tier_id,
                seed=seed,
                status=status,
                actor_id=actor_id,
            )
        )
    return results


async def unregistered_in_tier(
    conn: asyncpg.Connection,
    *,
    season_id: int,
    tier_id: int,
    seeds: Iterable[DriverSeed],
) -> list[DriverSeed]:
    """Which of the given seeds have no `drivers` row in this tier yet."""
    missing: list[DriverSeed] = []
    for seed in seeds:
        existing = await queries.fetch_driver(conn, season_id, tier_id, seed.member_id)
        if existing is None:
            missing.append(seed)
    return missing
