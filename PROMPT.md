# Discord League Management Bot — Project Context

## What this is now

A Discord bot for managing F1 sim racing leagues end-to-end. Started as a roster
manager (`roster-bot`) for a friend's 400-member F1 25 league with a 4-tier
structure mirroring real F1 — constructors, drivers, principals, free agents.

The roster/transaction layer is built and working in production on a single
server. The next phase expands this into a complete league management
platform: races, standings, stewarding, driver profiles, schedule, and
F1-game-specific integrations.

## What this is becoming

A commercial Discord bot, sold to F1 leagues on a tiered pricing model. F1
focus is the marketing wedge and the v1 feature set. The codebase is built
with eventual generalization to other league formats (other sim racing titles,
non-racing competitive leagues) as a v2 expansion path — but **don't build
generic abstractions until they're needed**. Build F1-specific features with
clean seams so they can be generalized later, not generic-from-day-one.

See `docs/ADR-001-f1-with-generic-future.md` for the architectural
philosophy and the specific patterns to follow.

## Pricing model (informs feature gating)

- **Free tier** — basic roster management, 1–3 teams, plain embeds, role-based
  auto-update. Already-built features in `roster-bot/`.
- **Premium tier ($5–10/mo per server)** — visual flair (Pillow-rendered
  cards), Google Sheets integration, custom team colors, banner images,
  transaction logging, free agent board, principal role delegation, race
  results, standings, stewarding, schedule.
- **Enterprise tier ($30–75/mo)** — private hosting, custom bot branding,
  F1 game UDP telemetry integration, OCR results parsing.

Feature gating is enforced via a `guild_tier` table. Functions that produce
premium output check tier first and either render the premium version or fall
back to a basic version with an upgrade hint.

## League structure (the friend's league, our reference customer)

- 4 tiers, mirroring real F1 sub-leagues
- 10 constructor teams per tier (Red Bull, Ferrari, McLaren, etc.) — using
  real F1 team names and colors
- 2 drivers per constructor per tier
- 1 team principal per constructor (manages signings, drops, lineup)
- Free agents pool for substitutes and call-ups
- Sprint and standard race weekends, real F1 points system
- 12-point license system over rolling 12 months

This shape is the v1 target. Build for this exactly. Generalize later.

## Conventions

These are non-negotiable, carried over from the original prompt and reinforced
by experience:

- **Don't guess APIs.** discord.py 2.x signatures, F1 telemetry packet specs,
  Codemasters UDP format — verify before writing code. If unsure, say so.
- **Explain as you go.** When introducing a pattern (UDP socket listener,
  preset system, tier gating), a couple sentences on why it works that way.
- **Small commits, one logical change each.** Each phase below is multiple
  commits, not one mega-commit.
- **Type hints everywhere.** Existing code is well-typed; keep it that way.
- **Tests for rendering and pure logic.** Mock Discord objects. The render
  module already has good test coverage; new pure-logic code (points
  calculation, standings, license points) gets the same treatment.
- **No magic numbers in code.** Points tables, tier counts, penalty values,
  seat counts — all in DB or config. See the ADR for why.
- **Migrations are append-only.** Existing migrations 001–011 stay as-is.
  New schema goes in 012+.

## Stack (unchanged)

- Python 3.11+, discord.py 2.x
- aiosqlite (will move to Postgres for multi-tenant; not yet)
- Pillow for image generation
- aiohttp for outbound HTTP
- ruff for lint/format
- pytest + pytest-asyncio for tests
- Container deploy via Podman (Containerfile in repo)

## Repository layout (current and planned)

```
roster-bot/                 # current, working code
  bot/
    cogs/
      roster.py             # /roster command group (built)
      sheets.py             # /sheets command group (built)
      race.py               # NEW — /race command group
      steward.py            # NEW — /steward command group
      schedule.py           # NEW — /schedule command group
    flow.py                 # roster create/edit guided flow (built)
    render.py               # embed + Pillow image generation (built)
    queries.py              # DB access (built; extend for new tables)
    models.py               # dataclasses (built; extend for new entities)
    db.py                   # connection helper (built)
    events.py               # role-change events + 15-min poll (built)
    presets/                # NEW — F1 league preset data (constructors,
      f1_constructors.py    #   colors, points systems, penalty types)
      f1_points.py
      f1_penalties.py
    integrations/           # NEW — external integrations
      f1_telemetry.py       #   UDP listener for F1 25/26
      results_ocr.py        #   screenshot OCR fallback
  migrations/
    001_init.sql … 011_transaction_log.sql   # existing
    012_seasons.sql         # NEW — season scoping
    013_races.sql           # NEW — race events and results
    014_standings.sql       # NEW — points and standings
    015_stewarding.sql      # NEW — incidents, penalties, license points
    016_guild_tier.sql      # NEW — pricing tier per guild
  docs/
    ADR-001-f1-with-generic-future.md   # NEW
```

## Build phases

Each phase is a self-contained body of work. **Do them in order.** Don't start
phase N+1 before phase N is shipped, tested, and used in production by the
reference league.

1. **Foundation refactor** — guild_tier table + feature gating helpers, season
   scoping for existing teams, F1 preset module structure. No user-facing
   features change. This is the substrate the rest is built on.

2. **Race results + standings** — the highest-value addition. Manual results
   entry first (admin types finishing order), then standings calculation,
   then standings image generation. Sprint weekend support included from day 1.

3. **Stewarding system** — incident reports, steward decisions, penalties,
   license points with rolling 12-month window. F1-specific penalty types.

4. **Schedule + attendance** — season calendar, Discord events auto-creation,
   RSVP tracking, no-show alerts to principals.

5. **Driver profiles** — career stats card per driver, leveraging existing
   transaction_log plus new race results data.

6. **F1 game integration** — UDP telemetry listener for live results capture.
   This is the enterprise tier moat. Build last because it's the most complex
   and benefits from having the rest of the system stable.

Each phase has its own implementation prompt. Ask me for the next phase's
prompt when you've completed the current one.

## What you (Claude Code) should do when starting a session

1. Read this file.
2. Read `docs/ADR-001-f1-with-generic-future.md`.
3. Read the implementation prompt for the current phase.
4. Look at the existing code for the area you're touching — don't re-derive
   patterns that already exist in the codebase.
5. Ask clarifying questions before writing code. Especially about the F1
   game's actual rules and behaviors — I'll verify against the real spec.
