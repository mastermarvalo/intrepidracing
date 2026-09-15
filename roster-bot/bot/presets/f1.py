"""
F1 25/26 preset — data seeded when a commissioner runs
`/market-admin season create` with the F1 preset choice.

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
    ("carried", "Carried to next season", True),
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
    ("contract_carried", "Contract carried over"),
    ("contract_expired", "Contract expired"),
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

# Per-factor F1 25/26 valuation weights, in $M.
#
# Observations reaching the engine are normalized to [0,1] by
# bot/market/results.py (form_trend is signed, [-1,1]), so a weight is
# literally "the dollar move a perfect observation of this factor is
# worth" and max_contribution is a defensive backstop rather than a
# binding constraint.
#
# Calibrated so a clean sweep (pole + win + fastest lap + full points,
# with good form) sums past the exceptional movement cap, a podium lands
# in the middle of the standard cap, a quiet midfield finish barely
# moves, and a DNF costs roughly half the standard cap. Starting points
# a commissioner tunes as the season plays.
_VALUATION_FACTORS: list[tuple[str, str, Decimal, Decimal | None]] = [
    ("race_finish", "Race finish position", Decimal("0.4500"), Decimal("0.60")),
    ("quali_finish", "Qualifying position", Decimal("0.1200"), Decimal("0.20")),
    ("points_scored", "Points scored", Decimal("0.2000"), Decimal("0.30")),
    ("wins", "Race wins", Decimal("0.1800"), Decimal("0.25")),
    ("podiums", "Podium finishes", Decimal("0.1000"), Decimal("0.15")),
    ("poles", "Pole positions", Decimal("0.0800"), Decimal("0.12")),
    ("fastest_laps", "Fastest laps", Decimal("0.0500"), Decimal("0.10")),
    ("driver_of_day", "Driver of the Day", Decimal("0.0600"), Decimal("0.10")),
    ("dnf", "DNFs / retirements", Decimal("-0.3500"), Decimal("0.40")),
    ("incidents", "Incidents & penalties", Decimal("-0.2000"), Decimal("0.30")),
    ("form_trend", "Recent form trend", Decimal("0.1500"), Decimal("0.25")),
    ("consistency", "Finish consistency", Decimal("0.1000"), Decimal("0.15")),
]

# Position -> normalized score curve, points award, and the win/podium/
# pole flags. This is the table bot/market/results.py reads instead of
# hard-coding "a podium is the top three".
#
# race_score is deliberately non-linear: the gap between P1 and P2 is
# wider than between P9 and P10, because winning a race says more about
# a driver than gaining one midfield place. quali_score is flatter —
# grid position matters, but less than what you do with it.
#
# (position, race_score, quali_score, points, is_win, is_podium, is_pole)
_POSITION_SCORES: list[tuple[int, Decimal, Decimal, Decimal, bool, bool, bool]] = [
    (1, Decimal("1.0000"), Decimal("1.0000"), Decimal("25"), True, True, True),
    (2, Decimal("0.8600"), Decimal("0.9000"), Decimal("18"), False, True, False),
    (3, Decimal("0.7600"), Decimal("0.8200"), Decimal("15"), False, True, False),
    (4, Decimal("0.6800"), Decimal("0.7500"), Decimal("12"), False, False, False),
    (5, Decimal("0.6100"), Decimal("0.6900"), Decimal("10"), False, False, False),
    (6, Decimal("0.5500"), Decimal("0.6300"), Decimal("8"), False, False, False),
    (7, Decimal("0.4900"), Decimal("0.5800"), Decimal("6"), False, False, False),
    (8, Decimal("0.4400"), Decimal("0.5300"), Decimal("4"), False, False, False),
    (9, Decimal("0.3900"), Decimal("0.4800"), Decimal("2"), False, False, False),
    (10, Decimal("0.3500"), Decimal("0.4400"), Decimal("1"), False, False, False),
    (11, Decimal("0.3100"), Decimal("0.4000"), Decimal("0"), False, False, False),
    (12, Decimal("0.2700"), Decimal("0.3600"), Decimal("0"), False, False, False),
    (13, Decimal("0.2400"), Decimal("0.3200"), Decimal("0"), False, False, False),
    (14, Decimal("0.2100"), Decimal("0.2800"), Decimal("0"), False, False, False),
    (15, Decimal("0.1800"), Decimal("0.2400"), Decimal("0"), False, False, False),
    (16, Decimal("0.1500"), Decimal("0.2000"), Decimal("0"), False, False, False),
    (17, Decimal("0.1200"), Decimal("0.1600"), Decimal("0"), False, False, False),
    (18, Decimal("0.0900"), Decimal("0.1200"), Decimal("0"), False, False, False),
    (19, Decimal("0.0600"), Decimal("0.0800"), Decimal("0"), False, False, False),
    (20, Decimal("0.0300"), Decimal("0.0400"), Decimal("0"), False, False, False),
    (21, Decimal("0.0100"), Decimal("0.0200"), Decimal("0"), False, False, False),
    (22, Decimal("0.0000"), Decimal("0.0000"), Decimal("0"), False, False, False),
]

# Windows are in rounds. A five-round form window is long enough to
# smooth out one bad weekend without being so long that a driver who has
# genuinely turned a corner stays underpriced for half a season.
_DEFAULT_RESULTS_CONFIG: dict[str, object] = {
    "form_window_rounds": 5,
    "consistency_window_rounds": 5,
    "max_incident_points": Decimal("6.00"),
}

# Team budget defaults, in $M. These are STARTING POINTS for a league to
# tune via /market-admin budget config, not a claim about the right
# numbers for any given grid.
#
#   opening_budget = salary_cap so that, on the day this ships, a team
#   with no prize money and no penalties can commit exactly what it
#   could before — the budget only starts to bite once results move it.
#
#   earnings_per_point: a win (25 pts) earns $1.25M; a team scoring
#   ~40 pts a round across a 12-round season banks ~$24M — meaningful
#   against a $145M cap without making the cap irrelevant.
#
#   dns_penalty > dnf_penalty on purpose: not showing up costs the league
#   a grid slot and the other teams a race, so it is priced above a
#   retirement that at least started.
#
#   penalty_per_incident_pt × max_incident_points (6.00) caps a single
#   weekend's stewarding at $1.50M.
_DEFAULT_BUDGET_CONFIG: dict[str, object] = {
    "enforce_budget": True,
    "rollover_enabled": True,
    "opening_budget": Decimal("145.00"),
    "earnings_per_point": Decimal("0.0500"),
    "dnf_penalty": Decimal("0.50"),
    "dns_penalty": Decimal("1.00"),
    "penalty_per_incident_pt": Decimal("0.2500"),
}

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
    # One season is the shortest contract the league recognises; three is
    # the longest a TP may offer. Both are commissioner-editable.
    "min_term_seasons": 1,
    "max_term_seasons": 3,
    "max_incentive_pct": Decimal("0.150"),
    "offer_ttl_hours": 48,
    # ── race-denominated terms (Phase 9)
    # A modern F1 calendar; the season bounds above are kept and this is
    # what converts them. Commissioner-editable, because a league that
    # runs a 12-race split season needs its own number and every
    # per-race salary charge divides by this.
    "races_per_season": 24,
    # Five races is the shortest deal the league recognises and two full
    # seasons the longest. Deliberately NOT 1 and 72: a one-race contract
    # is a free look at a driver with no commitment, which is what the
    # premiums exist to discourage. Both are commissioner-editable.
    "min_term_races": 5,
    "max_term_races": 48,
    # ── contract premiums (Phase 9) — UNTUNED PLACEHOLDERS
    # Zero means no price floor above the driver's market value, which is
    # exactly how the first seven seasons of this league priced contracts.
    # Shipping at zero keeps deploy-day behaviour identical and leaves the
    # commissioner to dial them in from the config panel against real
    # contract history, rather than having a number invented here silently
    # reprice every offer. See PHASE9_PLAN.md — these need tuning.
    "resign_premium_pct": Decimal("0"),
    "length_premium_pct": Decimal("0"),
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
      - season-scoped position_scores (the normalization curve)
      - season-default results_config row (tier_id = NULL)
      - season-default budget_config row (tier_id = NULL)
      - season-default league_config row (tier_id = NULL)

    The caller is responsible for creating the season row first and for
    the surrounding transaction — this function only writes.
    """
    await _seed_global_lookups(conn)
    await _seed_tiers(conn, season_id)
    await _seed_valuation_factors(conn, season_id)
    await _seed_position_scores(conn, season_id)
    await _seed_default_results_config(conn, season_id)
    await _seed_default_budget_config(conn, season_id)
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


async def _seed_position_scores(conn: asyncpg.Connection, season_id: int) -> None:
    rows = [
        (season_id, position, race, quali, points, is_win, is_podium, is_pole)
        for position, race, quali, points, is_win, is_podium, is_pole in _POSITION_SCORES
    ]
    await conn.executemany(
        """
        INSERT INTO position_scores
            (season_id, position, race_score, quali_score, points,
             is_win, is_podium, is_pole)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (season_id, position) DO NOTHING
        """,
        rows,
    )


async def _seed_default_results_config(conn: asyncpg.Connection, season_id: int) -> None:
    await conn.execute(
        """
        INSERT INTO results_config
            (season_id, tier_id, form_window_rounds,
             consistency_window_rounds, max_incident_points)
        VALUES ($1, NULL, $2, $3, $4)
        ON CONFLICT DO NOTHING
        """,
        season_id,
        _DEFAULT_RESULTS_CONFIG["form_window_rounds"],
        _DEFAULT_RESULTS_CONFIG["consistency_window_rounds"],
        _DEFAULT_RESULTS_CONFIG["max_incident_points"],
    )


async def _seed_default_budget_config(conn: asyncpg.Connection, season_id: int) -> None:
    await conn.execute(
        """
        INSERT INTO budget_config
            (season_id, tier_id, enforce_budget, rollover_enabled,
             opening_budget, earnings_per_point, dnf_penalty, dns_penalty,
             penalty_per_incident_pt)
        VALUES ($1, NULL, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT DO NOTHING
        """,
        season_id,
        _DEFAULT_BUDGET_CONFIG["enforce_budget"],
        _DEFAULT_BUDGET_CONFIG["rollover_enabled"],
        _DEFAULT_BUDGET_CONFIG["opening_budget"],
        _DEFAULT_BUDGET_CONFIG["earnings_per_point"],
        _DEFAULT_BUDGET_CONFIG["dnf_penalty"],
        _DEFAULT_BUDGET_CONFIG["dns_penalty"],
        _DEFAULT_BUDGET_CONFIG["penalty_per_incident_pt"],
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
        min_term_seasons=_DEFAULT_LEAGUE_CONFIG["min_term_seasons"],  # type: ignore[arg-type]
        max_term_seasons=_DEFAULT_LEAGUE_CONFIG["max_term_seasons"],  # type: ignore[arg-type]
        max_incentive_pct=_DEFAULT_LEAGUE_CONFIG["max_incentive_pct"],  # type: ignore[arg-type]
        offer_ttl_hours=_DEFAULT_LEAGUE_CONFIG["offer_ttl_hours"],  # type: ignore[arg-type]
        races_per_season=_DEFAULT_LEAGUE_CONFIG["races_per_season"],  # type: ignore[arg-type]
        min_term_races=_DEFAULT_LEAGUE_CONFIG["min_term_races"],  # type: ignore[arg-type]
        max_term_races=_DEFAULT_LEAGUE_CONFIG["max_term_races"],  # type: ignore[arg-type]
        resign_premium_pct=_DEFAULT_LEAGUE_CONFIG["resign_premium_pct"],  # type: ignore[arg-type]
        length_premium_pct=_DEFAULT_LEAGUE_CONFIG["length_premium_pct"],  # type: ignore[arg-type]
    )
