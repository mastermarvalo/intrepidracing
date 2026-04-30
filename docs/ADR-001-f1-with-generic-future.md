# ADR-001: F1-First Architecture with Generic Future

## Status

Accepted. This is the architectural philosophy for all new features in the
league management bot.

## Context

The bot started as a generic team roster manager. It is being expanded into a
full league management platform with F1 sim racing as the v1 target market and
specifically the friend's 4-tier F1 25/26 league as the reference customer.

There is a real possibility we will later expand to other sim racing titles
(ACC, iRacing, rFactor 2) or non-racing competitive formats. The architectural
question: how do we ship F1 features fast without painting ourselves into a
corner?

## Decision

**Build F1-specific features as the first instance of more general systems,
but only build the general system when the F1 feature genuinely needs it.**

In practice, this means four concrete rules:

### Rule 1: No magic numbers in code

Anything that *could* differ between leagues lives in the database or config,
never as a Python literal in business logic.

**Bad:**
```python
POINTS = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]
def points_for_position(pos: int) -> int:
    return POINTS[pos - 1] if pos <= 10 else 0
```

**Good:**
```python
async def points_for_position(conn, season_id: int, pos: int) -> int:
    row = await conn.execute(
        "SELECT points FROM points_systems "
        "WHERE season_id = ? AND position = ?",
        (season_id, pos),
    ).fetchone()
    return row["points"] if row else 0
```

The F1 points system is *seeded* into `points_systems` when an F1 league is
created. Other leagues seed their own values. Same code path, different data.

Applies to: points tables, tier counts, seat counts, penalty severities,
license point thresholds, race classification rules.

### Rule 2: Domain entities as data, not enums

Penalty types, event types, achievement types, session types — these change
between leagues. They are rows in tables, not Python enums.

**Bad:**
```python
class PenaltyType(Enum):
    FIVE_SECOND = "5sec"
    TEN_SECOND = "10sec"
    DRIVE_THROUGH = "drive_through"
    STOP_GO = "stop_go"
```

**Good:**
```sql
CREATE TABLE penalty_types (
    id INTEGER PRIMARY KEY,
    league_id INTEGER NOT NULL,
    code TEXT NOT NULL,           -- "5sec"
    label TEXT NOT NULL,          -- "5-second time penalty"
    seconds_added INTEGER,        -- application logic
    license_points INTEGER,       -- application logic
    UNIQUE(league_id, code)
);
```

For F1, we seed the standard penalty types when a league is created. For other
leagues, they configure their own. The application code says "look up the
penalty by code" not "switch on the enum value."

### Rule 3: F1 specifics live in `presets/` and `integrations/`

Anything that is *only* meaningful for F1 — Codemasters UDP packet format,
constructor autofill data, the 12-point license system, hat-trick achievement
logic — lives in dedicated modules under `bot/presets/` or
`bot/integrations/`.

The core code in `bot/cogs/`, `bot/render.py`, `bot/queries.py` does **not
import from these modules**. Instead, presets *write data* into the same
generic tables the rest of the system reads from.

This means turning off F1 specificity later is just: don't run the F1 preset
seeder for non-F1 leagues. The core code never knew about F1 in the first
place.

### Rule 4: Season scoping from day 1

Everything race-related is scoped to a `season_id`, not a `guild_id`. A guild
can have multiple seasons (current season, next season, archive of past
seasons). A season belongs to a guild.

This is non-negotiable because retrofitting season scoping later means
migrating production data for every customer. Do it now while the only
production user is the friend's league.

```sql
CREATE TABLE seasons (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,           -- "F1 2026 Season"
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(guild_id, name)
);
```

Existing tables (`teams`, `team_slots`) keep their `guild_id` for backward
compatibility but new tables (`races`, `race_results`, `incidents`, etc.) are
season-scoped.

A migration adds an optional `season_id` to `teams` so teams can be
season-specific in the future without breaking current usage. Default behavior
when `season_id IS NULL` on a team: it belongs to the active season.

## What this looks like in practice

Here's how an F1 feature gets built under these rules:

**Feature: Sprint weekend support**

Wrong way (rule violation):
```python
class SessionType(Enum):
    QUALI = "quali"
    SPRINT_QUALI = "sprint_quali"
    SPRINT = "sprint"
    RACE = "race"

if session_type == SessionType.SPRINT:
    points = SPRINT_POINTS[position - 1]
elif session_type == SessionType.RACE:
    points = RACE_POINTS[position - 1]
```

Right way:
```sql
CREATE TABLE session_types (
    id INTEGER PRIMARY KEY,
    league_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    label TEXT NOT NULL,
    points_system_id INTEGER NOT NULL REFERENCES points_systems(id)
);
```

```python
async def points_for_finish(conn, session_type_id: int, position: int) -> int:
    # Look up the session type, then its points system, then the points
    # for that position. All data-driven.
    ...
```

The F1 preset seeds:
- `session_types`: "quali", "sprint_quali", "sprint", "race"
- `points_systems`: a "main race" system (25-18-15...), a "sprint" system (8-7-6...)
- Links each session type to its points system

A non-F1 league seeds different rows. The application code is unchanged.

## What we don't do

**We don't build a generic UI for configuring all this.** League admins are
not configuring points tables row by row in v1. The F1 preset is selected
during league setup and the values are seeded automatically. The configurability
is in the *schema*, not in the user-facing flow. That comes later if and when
non-F1 leagues become a serious market.

**We don't pre-build adapters for other games.** F1 25/26 telemetry is the only
integration we ship. ACC/iRacing/rFactor adapters can be added later in their
own integration modules without changing core code.

**We don't make the F1 specifics optional in v1.** F1 is the product. The
schema supports genericity; the *product* is F1-specific. We use F1 terminology
in the UI (constructors, drivers, pole position, fastest lap) because that's
what our customers want.

## Consequences

**Positive:**
- F1 features ship fast because they're built directly, not through layers of
  abstraction.
- Schema is general enough that non-F1 expansion is a "write new presets"
  problem, not a "rewrite the core" problem.
- Each new league type is a config exercise, not an engineering exercise.

**Negative:**
- Slightly more upfront cost than hardcoding F1 values everywhere — a few
  extra tables, a preset seeding step.
- Easy to slip into rule violations if not vigilant. Code review must
  specifically check for magic numbers and enum-instead-of-table patterns.

**Mitigations:**
- This ADR is read at the start of every implementation phase.
- A lint check (custom ruff rule or grep) flags numeric literals in business
  logic. Phase 1 includes setting this up.
