"""
F1 25/26 preset — data seeded when a commissioner runs
`/market-admin season create --preset f1`.

This module is *data*, not logic. It only writes rows into the generic
tables defined by migrations 002/003/006 (and later 004/005/007). Per
ADR-001 rule 3, nothing under bot/market/ or bot/contracts/ may import
from this module — the core code reads the same rows regardless of which
preset seeded them, so switching presets is a data change, not a code
change. This is also why every F1 numeric literal (weekly cap size, tier
count, salary cap, weights) lives here and only here; the magic-number
guard in scripts/check_magic_numbers.py scans the core modules and
excludes bot/presets/.

Every write is idempotent so re-running the preset on an existing season
is safe — useful for iterating on default values during setup.
"""

from decimal import Decimal

import asyncpg

from bot import queries

# ── Static seed data ──────────────────────────────────────────────────────

_DRIVER_STATUSES: list[tuple[str, str]] = [
    ("active", "Active"),
    ("reserve", "Reserve"),
    ("free_agent", "Free Agent"),
    ("restricted_fa", "Restricted FA"),
    ("inactive", "Inactive"),
    ("suspended", "Suspended"),
]

_CONTRACT_TYPES: list[tuple[str, str, str]] = [
    ("standard", "Standard", "Standard multi-season deal."),
    ("rookie", "Rookie", "Entry-level deal for a first-time driver."),
    ("reserve", "Reserve", "Reserve driver with limited race commitments."),
    ("franchise", "Franchise Tag", "Max-value single-season lock."),
]

_CONTRACT_STATES: list[tuple[str, str, bool]] = [
    ("active", "Active", False),
    ("expired", "Expired", True),
    ("voided", "Voided", True),
    ("terminated", "Terminated", True),
]

_OFFER_STATES: list[tuple[str, str, bool]] = [
    ("draft", "Draft", False),
    ("pending_driver", "Pending — Driver Review", False),
    ("pending_team", "Pending — Team Review", False),
    ("countered", "Countered", False),
    ("accepted", "Accepted", False),
    ("pending_approval", "Pending — Commissioner", False),
    ("approved", "Approved", True),
    ("rejected", "Rejected", True),
    ("declined", "Declined", True),
    ("withdrawn", "Withdrawn", True),
    ("expired", "Expired", True),
]

_TRANSACTION_KINDS: list[tuple[str, str]] = [
    ("valuation_snapshot", "Valuation snapshot"),
    ("offer_created", "Offer created"),
    ("offer_accepted", "Offer accepted"),
    ("offer_declined", "Offer declined"),
    ("offer_countered", "Counteroffer sent"),
    ("offer_withdrawn", "Offer withdrawn"),
    ("offer_expired", "Offer expired"),
    ("offer_approved", "Offer approved"),
    ("offer_rejected", "Offer rejected"),
    ("contract_signed", "Contract signed"),
    ("contract_extended", "Contract extended"),
    ("contract_voided", "Contract voided"),
    ("release", "Release"),
    ("buyout", "Buyout"),
    ("trade", "Trade"),
    ("cap_adjustment", "Cap adjustment"),
    ("status_change", "Driver status change"),
]

_BOARD_KINDS: list[tuple[str, str]] = [
    ("market", "Market"),
    ("movers", "Movers"),
    ("cap", "Cap Sheet"),
    ("surplus", "Surplus"),
    ("underwater", "Underwater"),
    ("dashboard", "Cross-Tier Dashboard"),
]

_TIERS: list[tuple[str, str, int]] = [
    ("t1", "Tier 1", 1),
    ("t2", "Tier 2", 2),
    ("t3", "Tier 3", 3),
]

# Per-factor F1 25/26 valuation weights. Weights are dimensionless
# multipliers applied by the (Phase 2) valuation engine; the engine caps
# each factor's per-run contribution at max_contribution (in $M). These
# values are starting points a commissioner tunes as the season plays.
_VALUATION_FACTORS: list[tuple[str, str, Decimal, Decimal | None]] = [
    ("race_finish", "Race finish position", Decimal("1.0000"), Decimal("0.60")),
    ("quali_finish", "Qualifying position", Decimal("0.4000"), Decimal("0.30")),
    ("points_scored", "Points scored", Decimal("0.6000"), Decimal("0.50")),
    ("wins", "Race wins", Decimal("1.2000"), Decimal("0.75")),
    ("podiums", "Podium finishes", Decimal("0.5000"), Decimal("0.40")),
    ("poles", "Pole positions", Decimal("0.3500"), Decimal("0.30")),
    ("fastest_laps", "Fastest laps", Decimal("0.2000"), Decimal("0.20")),
    ("dnf", "DNFs / retirements", Decimal("-0.5000"), Decimal("0.40")),
    ("incidents", "Incidents & penalties", Decimal("-0.3000"), Decimal("0.30")),
    ("form_trend", "Recent form trend", Decimal("0.5000"), Decimal("0.35")),
    ("consistency", "Finish consistency", Decimal("0.3000"), Decimal("0.25")),
]

# F1 25/26 defaults, in $M. Commissioners override via
# /market-admin config set. All amounts are Decimal to keep the money
# path float-free from day 1.
_DEFAULT_LEAGUE_CONFIG: dict[str, object] = {
    "salary_cap": Decimal("145.00"),
    "min_salary": Decimal("1.00"),
    "max_salary": None,
    "active_driver_slots": 2,
    "weekly_move_cap": Decimal("0.75"),
    "exceptional_move_cap": Decimal("1.25"),
    "max_term_seasons": 3,
    "max_incentive_pct": Decimal("0.150"),
    "offer_ttl_hours": 48,
}


# ── Seeder ────────────────────────────────────────────────────────────────


async def seed_season(conn: asyncpg.Connection, season_id: int) -> None:
    """
    Seed all F1 preset data for a season. Idempotent: safe to re-run.

    Writes:
      - three tiers (t1/t2/t3, roles unset — commissioner assigns via
        /market-admin tier edit)
      - global lookup rows (driver_statuses, contract_types,
        contract_states, offer_states, transaction_kinds, board_kinds)
      - season-scoped valuation_factors
      - season-default league_config row (tier_id = NULL)

    The caller is responsible for creating the season row first and for
    the surrounding transaction — this function only writes.
    """
    await _seed_global_lookups(conn)
    await _seed_tiers(conn, season_id)
    await _seed_valuation_factors(conn, season_id)
    await _seed_default_config(conn, season_id)


async def _seed_global_lookups(conn: asyncpg.Connection) -> None:
    await conn.executemany(
        "INSERT INTO driver_statuses (code, label) VALUES ($1, $2) "
        "ON CONFLICT (code) DO NOTHING",
        _DRIVER_STATUSES,
    )
    await conn.executemany(
        "INSERT INTO contract_types (code, label, description) VALUES ($1, $2, $3) "
        "ON CONFLICT (code) DO NOTHING",
        _CONTRACT_TYPES,
    )
    await conn.executemany(
        "INSERT INTO contract_states (code, label, is_terminal) VALUES ($1, $2, $3) "
        "ON CONFLICT (code) DO NOTHING",
        _CONTRACT_STATES,
    )
    await conn.executemany(
        "INSERT INTO offer_states (code, label, is_terminal) VALUES ($1, $2, $3) "
        "ON CONFLICT (code) DO NOTHING",
        _OFFER_STATES,
    )
    await conn.executemany(
        "INSERT INTO transaction_kinds (code, label) VALUES ($1, $2) "
        "ON CONFLICT (code) DO NOTHING",
        _TRANSACTION_KINDS,
    )
    await conn.executemany(
        "INSERT INTO board_kinds (code, label) VALUES ($1, $2) "
        "ON CONFLICT (code) DO NOTHING",
        _BOARD_KINDS,
    )


async def _seed_tiers(conn: asyncpg.Connection, season_id: int) -> None:
    for code, label, rank_order in _TIERS:
        existing = await queries.fetch_tier(conn, season_id, code)
        if existing is None:
            await queries.insert_tier(
                conn, season_id, code=code, label=label, rank_order=rank_order
            )


async def _seed_valuation_factors(conn: asyncpg.Connection, season_id: int) -> None:
    rows = [
        (season_id, code, label, weight, cap, idx)
        for idx, (code, label, weight, cap) in enumerate(_VALUATION_FACTORS)
    ]
    await conn.executemany(
        """
        INSERT INTO valuation_factors
            (season_id, code, label, weight, max_contribution, sort_order)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (season_id, code) DO NOTHING
        """,
        rows,
    )


async def _seed_default_config(conn: asyncpg.Connection, season_id: int) -> None:
    if await queries.fetch_league_config_row(conn, season_id, None) is not None:
        return
    await queries.upsert_league_config(
        conn,
        season_id=season_id,
        tier_id=None,
        salary_cap=_DEFAULT_LEAGUE_CONFIG["salary_cap"],  # type: ignore[arg-type]
        min_salary=_DEFAULT_LEAGUE_CONFIG["min_salary"],  # type: ignore[arg-type]
        max_salary=_DEFAULT_LEAGUE_CONFIG["max_salary"],  # type: ignore[arg-type]
        active_driver_slots=_DEFAULT_LEAGUE_CONFIG["active_driver_slots"],  # type: ignore[arg-type]
        weekly_move_cap=_DEFAULT_LEAGUE_CONFIG["weekly_move_cap"],  # type: ignore[arg-type]
        exceptional_move_cap=_DEFAULT_LEAGUE_CONFIG["exceptional_move_cap"],  # type: ignore[arg-type]
        max_term_seasons=_DEFAULT_LEAGUE_CONFIG["max_term_seasons"],  # type: ignore[arg-type]
        max_incentive_pct=_DEFAULT_LEAGUE_CONFIG["max_incentive_pct"],  # type: ignore[arg-type]
        offer_ttl_hours=_DEFAULT_LEAGUE_CONFIG["offer_ttl_hours"],  # type: ignore[arg-type]
    )
