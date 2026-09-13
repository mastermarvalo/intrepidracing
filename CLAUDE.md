# CLAUDE.md — Driver Market & Contract System

Orchestration brief for implementing the tiered driver market, salary cap, and
contract/negotiation system inside the existing `roster-bot` codebase.

Read this file in full before writing code. Then read
`docs/ADR-001-f1-with-generic-future.md` — it is binding, not advisory.

---

## 1. Repository map (current state)

Single Python package at `roster-bot/`, `discord.py` 2.x + asyncpg + Postgres 16,
deployed with Docker Compose. Python 3.11+, managed with `uv`, linted with `ruff`
(line-length 100, rules `E,F,I`), tested with `pytest` (`asyncio_mode = "auto"`).

```
roster-bot/
  bot/
    main.py          RosterBot(commands.Bot); setup_hook loads cogs + db.init(), tree.sync()
    db.py            asyncpg pool, connect() context manager (auto transaction),
                     _run_migrations() applies migrations/*.sql in sorted filename order
    models.py        dataclasses: Team, TeamSlot, GuildConfig, StatBoard
    queries.py       all SQL; functions take an open asyncpg.Connection
    flow.py          9-step interactive team create/edit wizard (in-memory state,
                     keyed by (guild_id, user_id)); modals + select views
    render.py        embed builders + Pillow image rendering (flair bars, avatar cards)
    events.py        on_member_update trigger + 15-minute poll safety net
    sheets.py        Google Sheets fetch/format
    cogs/roster.py   /roster * command group (~20 subcommands)
    cogs/sheets.py   /sheets * stat board group
  migrations/001_init.sql   single Postgres baseline
  tests/            conftest.py fakes (FakeMember/FakeRole/FakeAvatar), render + overflow tests
```

Existing tables: `teams`, `team_slots`, `guild_config`, `stat_boards`,
`transactions`, `schema_migrations`.

Key existing conventions to imitate exactly:

- **No members table.** Roster membership is derived live from Discord roles at
  render time. Do not change this.
- **Render is idempotent.** Builders edit a stored `message_id` in place; if the
  message is gone, the id is cleared and the entity is flagged as broken.
- **Permission model.** `_is_admin()` = Manage Server. Team-scoped actions also
  accept `teams.principal_role_id`. Reuse these helpers; do not invent a new one.
- **Queries layer is the only place SQL lives.** Cogs never embed SQL strings.
- **Ephemeral replies** for anything administrative or private.

---

## 2. What we are building

A **per-tier driver market** with weekly valuations, a **salary cap**, and a
**contract lifecycle** (offer → negotiation → acceptance → commissioner approval
→ ledger + public transaction post).

Core domain rules, non-negotiable:

1. **Markets are strictly tier-isolated.** A driver's value is computed only
   against the field of their own tier. No cross-tier normalization, ever. A
   cross-tier dashboard may *display* all three, but must never let values
   interact.
2. **Two distinct money numbers per driver:**
   - `market_value` — recomputed from performance each cycle.
   - `contract_value` — the salary agreed when the current team signed them; it
     is frozen at signing.
   - `P/L = market_value − contract_value`. Positive = under-market (team
     surplus). Negative = above-market.
3. **Cap compliance is measured on contract value, not market value.**
   Market value drives free-agent cost, trade value, extension demands, and
   buyout math only.
4. **Every money mutation is an append-only ledger row.** Nothing overwrites
   history. Value snapshots, offers, counteroffers, signings, releases, trades,
   and admin adjustments are all recorded with actor, timestamp, and reason.
5. **Movement caps.** Standard weekly change is bounded (default ±$0.75M);
   an exceptional-performance band allows a larger bound (default ±$1.25M).
   Both are configuration, not literals.
6. **Contract Value resets only on extension/renegotiation.** A trade transfers
   the existing deal untouched unless the trade explicitly includes a
   renegotiation. A release freezes that contract's P/L in the ledger.

---

## 3. ADR-001 compliance — how it binds this feature

Violating any of these is grounds for rejecting the change.

| Rule | What it means here |
|---|---|
| No magic numbers | Salary cap, movement caps, min/max salary, contract term limits, roster slot counts, incentive ceilings, valuation weights, points tables — all live in DB config rows seeded by a preset. Zero numeric business literals in `bot/`. |
| Domain entities as data, not enums | `contract_types`, `offer_states`, `valuation_factors`, `transaction_kinds` are tables with `code`/`label`, not Python `Enum`s. Code looks entities up by `code`. |
| F1 specifics in `presets/` | The F1-25/26 valuation weighting, points table, and tier naming go in `bot/presets/f1.py`, which only *writes rows* into generic tables. Core modules must not import from `bot/presets/`. |
| Season scoping from day 1 | Every new market/contract table is scoped to `season_id`, never `guild_id`. Add a `seasons` table and a `tiers` table now. |

Add a CI guard in Phase 1: a `ruff` config addition or a small
`scripts/check_magic_numbers.py` that fails when a numeric literal other than
`0`/`1`/`-1` appears in `bot/market/`, `bot/contracts/`, or `bot/valuation.py`.

---

## 4. Schema plan

New migration files only — **never edit `001_init.sql`**. `db._run_migrations`
applies `migrations/*.sql` in sorted filename order and records each in
`schema_migrations`, so files must be idempotent-safe and forward-only. Do not
put `BEGIN`/`COMMIT` inside a migration; the runner already wraps each file in a
transaction.

```
migrations/002_seasons_tiers.sql
migrations/003_drivers_and_tier_membership.sql
migrations/004_market_valuations.sql
migrations/005_contracts_and_offers.sql
migrations/006_league_config_and_presets.sql
migrations/007_market_boards.sql
```

### 002 — seasons + tiers

```sql
CREATE TABLE seasons (
    id         BIGSERIAL PRIMARY KEY,
    guild_id   BIGINT      NOT NULL,
    name       TEXT        NOT NULL,
    is_active  BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (guild_id, name)
);

CREATE TABLE tiers (
    id           BIGSERIAL PRIMARY KEY,
    season_id    BIGINT  NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    code         TEXT    NOT NULL,          -- 't1'
    label        TEXT    NOT NULL,          -- 'Tier 1'
    tier_role_id BIGINT,                    -- Discord role that marks tier membership
    rank_order   INTEGER NOT NULL,
    accent_color INTEGER,                   -- for embeds; per-tier branding
    UNIQUE (season_id, code)
);
```

Also add `ALTER TABLE teams ADD COLUMN season_id BIGINT REFERENCES seasons(id)`
and `ADD COLUMN tier_id BIGINT REFERENCES tiers(id)`, both nullable.
`season_id IS NULL` means "belongs to the active season" — preserve current
behaviour for every existing row.

### 003 — drivers

This feature does require persisted driver rows, which is a deliberate, scoped
exception to "no members table": we are storing **market and contract state**,
not roster membership. Discord roles remain the source of truth for who is on a
team and in a tier; `drivers` only anchors money and history to a member id.

```sql
CREATE TABLE drivers (
    id           BIGSERIAL PRIMARY KEY,
    season_id    BIGINT      NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    tier_id      BIGINT      NOT NULL REFERENCES tiers(id)   ON DELETE CASCADE,
    member_id    BIGINT      NOT NULL,      -- Discord snowflake
    display_name TEXT        NOT NULL,      -- cached for historical readability
    status       TEXT        NOT NULL,       -- FK to driver_statuses.code
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (season_id, tier_id, member_id)
);
```

A member may legitimately exist in more than one tier across the league (e.g. a
Tier 1 reserve who races Tier 2). Each is a separate `drivers` row with its own
independent market value. Never merge them.

`driver_statuses` is a seeded table (`active`, `reserve`, `free_agent`,
`restricted_fa`, `inactive`, `suspended`), not an enum.

### 004 — valuations

```sql
CREATE TABLE valuation_runs (
    id          BIGSERIAL PRIMARY KEY,
    season_id   BIGINT      NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    tier_id     BIGINT      NOT NULL REFERENCES tiers(id)   ON DELETE CASCADE,
    round_label TEXT        NOT NULL,        -- 'Post-Abu Dhabi'
    created_by  BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published   BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE TABLE driver_valuations (
    id             BIGSERIAL PRIMARY KEY,
    run_id         BIGINT        NOT NULL REFERENCES valuation_runs(id) ON DELETE CASCADE,
    driver_id      BIGINT        NOT NULL REFERENCES drivers(id)        ON DELETE CASCADE,
    market_value   NUMERIC(12,2) NOT NULL,
    previous_value NUMERIC(12,2),
    delta          NUMERIC(12,2) NOT NULL,
    rank_in_tier   INTEGER       NOT NULL,
    capped         BOOLEAN       NOT NULL DEFAULT FALSE,
    breakdown      JSONB         NOT NULL,   -- per-factor contributions, auditable
    UNIQUE (run_id, driver_id)
);
```

`valuation_factors` is a config table: `code`, `label`, `weight`,
`max_contribution`, `season_id`. The valuation engine iterates rows; adding a
factor is a data change, not a code change.

Use `NUMERIC(12,2)` for all money. **Never floats.** Convert to `Decimal` in
Python; round half-up at a single, documented boundary.

### 005 — contracts and offers

```sql
CREATE TABLE contracts (
    id              BIGSERIAL PRIMARY KEY,
    season_id       BIGINT        NOT NULL REFERENCES seasons(id),
    tier_id         BIGINT        NOT NULL REFERENCES tiers(id),
    driver_id       BIGINT        NOT NULL REFERENCES drivers(id),
    team_id         BIGINT        NOT NULL REFERENCES teams(id),
    contract_value  NUMERIC(12,2) NOT NULL,    -- frozen at signing; P/L baseline
    signing_bonus   NUMERIC(12,2) NOT NULL DEFAULT 0,
    max_incentives  NUMERIC(12,2) NOT NULL DEFAULT 0,
    term_seasons    INTEGER       NOT NULL,
    contract_type   TEXT          NOT NULL,    -- FK contract_types.code
    state           TEXT          NOT NULL,    -- FK contract_states.code
    value_at_signing NUMERIC(12,2),            -- market value on signing date
    signed_at       TIMESTAMPTZ,
    expires_after   INTEGER,                   -- season index
    voided_at       TIMESTAMPTZ,
    approved_by     BIGINT,
    external_ref    TEXT                       -- human transaction id, e.g. T2-S7-0142
);

CREATE TABLE contract_offers (
    id             BIGSERIAL PRIMARY KEY,
    season_id      BIGINT        NOT NULL REFERENCES seasons(id),
    tier_id        BIGINT        NOT NULL REFERENCES tiers(id),
    driver_id      BIGINT        NOT NULL REFERENCES drivers(id),
    team_id        BIGINT        NOT NULL REFERENCES teams(id),
    offered_by     BIGINT        NOT NULL,      -- TP member id
    offer_kind     TEXT          NOT NULL,      -- new / extension / trade_and_sign
    salary         NUMERIC(12,2) NOT NULL,
    term_seasons   INTEGER       NOT NULL,
    contract_type  TEXT          NOT NULL,
    signing_bonus  NUMERIC(12,2) NOT NULL DEFAULT 0,
    incentives     TEXT,
    message        TEXT,
    state          TEXT          NOT NULL,      -- FK offer_states.code
    parent_offer_id BIGINT REFERENCES contract_offers(id),  -- counteroffer chain
    expires_at     TIMESTAMPTZ   NOT NULL,
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    resolved_at    TIMESTAMPTZ,
    resolved_by    BIGINT,
    thread_id      BIGINT,                      -- private negotiation thread
    validation     JSONB         NOT NULL       -- snapshot of checks at submit time
);

CREATE TABLE contract_ledger (
    id          BIGSERIAL PRIMARY KEY,
    season_id   BIGINT      NOT NULL REFERENCES seasons(id),
    tier_id     BIGINT      NOT NULL REFERENCES tiers(id),
    driver_id   BIGINT      REFERENCES drivers(id),
    team_id     BIGINT      REFERENCES teams(id),
    contract_id BIGINT      REFERENCES contracts(id),
    offer_id    BIGINT      REFERENCES contract_offers(id),
    kind        TEXT        NOT NULL,           -- FK transaction_kinds.code
    amount      NUMERIC(12,2),
    detail      JSONB       NOT NULL,
    actor_id    BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Enforce at DB level: a partial unique index giving each driver at most one
`active` contract, and one at most one `pending` offer per (team, driver).

### 006 — league config

```sql
CREATE TABLE league_config (
    id                     BIGSERIAL PRIMARY KEY,
    season_id              BIGINT        NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    tier_id                BIGINT        REFERENCES tiers(id) ON DELETE CASCADE, -- NULL = season default
    salary_cap             NUMERIC(12,2) NOT NULL,
    min_salary             NUMERIC(12,2) NOT NULL,
    max_salary             NUMERIC(12,2),
    active_driver_slots    INTEGER       NOT NULL,
    weekly_move_cap        NUMERIC(12,2) NOT NULL,
    exceptional_move_cap   NUMERIC(12,2) NOT NULL,
    max_term_seasons       INTEGER       NOT NULL,
    max_incentive_pct      NUMERIC(6,3)  NOT NULL,
    offer_ttl_hours        INTEGER       NOT NULL,
    free_agency_open       BOOLEAN       NOT NULL DEFAULT FALSE,
    market_channel_id      BIGINT,
    transactions_channel_id BIGINT,
    approvals_channel_id   BIGINT,
    commissioner_role_id   BIGINT,
    UNIQUE (season_id, tier_id)
);
```

Tier-level rows override the season default row. Resolution helper lives in
`bot/market/config.py`.

### 007 — market boards

Self-updating market embeds, mirroring the `stat_boards` pattern exactly:
`market_boards(id, season_id, tier_id, channel_id, message_id, page, kind)`
where `kind` ∈ seeded `board_kinds` (`market`, `movers`, `cap`, `surplus`,
`underwater`, `dashboard`).

---

## 5. Module plan

```
bot/
  market/
    __init__.py
    config.py       resolve_config(season, tier) with tier→season fallback
    valuation.py    compute_run(): pure, data-driven, Decimal-only; returns
                    per-driver value + breakdown; applies movement caps
    money.py        Decimal parsing/formatting ($20.75M ⇄ Decimal), sign-aware
                    P/L formatting, single rounding boundary
    render.py       market tables, movers, cap sheets, driver cards, dashboard
  contracts/
    __init__.py
    rules.py        validation predicates, each returning (ok, code, message)
    service.py      state machine: submit → validate → deliver → accept /
                    decline / counter → approve → commit
    render.py       offer review panel, driver-facing offer card, counteroffer,
                    signed-contract post
  presets/
    __init__.py
    f1.py           seeds tiers, driver_statuses, contract_types, offer_states,
                    transaction_kinds, valuation_factors, points table,
                    default league_config
  cogs/
    market.py       /market group
    contracts.py    /contract group
    admin_market.py /market-admin group (commissioner)
```

`bot/market/valuation.py` and `bot/contracts/rules.py` must be **pure functions
over plain data** — no `discord` import, no DB access. That is what makes them
testable and what keeps the magic-number guard meaningful.

### Formatting constraint (learned the hard way)

Discord wraps long lines badly on mobile. **Do not render wide multi-column
tables.** Use the two-line-per-driver layout:

```
4. ZeezinDomar — Williams
   Market: $20.75M  |  Week: ▲ $1.25M
   Contract: $12.50M  |  P/L: +$8.25M
```

Paginate at 10 drivers per message with page buttons. Cap sheets and driver
cards use stacked label/value lines. All renderers must have a test asserting no
output line exceeds the configured width and no embed field exceeds Discord's
1024-char / 6000-char total limits — extend the existing `tests/test_overflow.py`
approach rather than inventing a new one.

---

## 6. Command surface

Team Principal / driver commands (authority via `teams.principal_role_id` or
Manage Server, same helpers as `cogs/roster.py`):

```
/market view tier:<t> [page]        paginated tier market
/market movers tier:<t>             risers / fallers
/market driver name:<driver>        driver card: value, contract, P/L, trend
/market team name:<team>            cap sheet: payroll, cap space, per-driver P/L
/market surplus tier:<t>            best contracts by P/L
/market underwater tier:<t>         worst contracts by P/L
/market dashboard                   cross-tier summary (display only)

/contract offer                     select menus (tier, driver, type, TTL) then
                                    modal (salary, term, bonus, incentives, note)
/contract offers                    my team's outstanding offers
/contract withdraw id:<n>
/contract counter id:<n>            driver-facing counteroffer modal
/contract accept id:<n>
/contract decline id:<n>
/contract status driver:<d>
```

Commissioner commands:

```
/market-admin season create|activate|list
/market-admin tier add|edit|list
/market-admin config set             tier or season scope
/market-admin valuation run tier:<t> round:<label>   → dry-run preview first
/market-admin valuation publish run:<id>
/market-admin board add|remove|refresh
/market-admin approve offer:<id> | reject offer:<id>
/market-admin void contract:<id> | set-status driver:<d> | adjust-cap team:<t>
/market-admin free-agency open|close tier:<t>
```

**Discord modal limitation:** modals cannot contain select menus and allow at
most five text inputs. So the offer flow is: slash command → ephemeral select
view (tier → driver → offer kind → contract type → expiry) → modal with the
text fields (salary, term, signing bonus, incentives, message) → ephemeral
review panel → submit. Build it as an explicit two-stage flow; do not attempt to
cram selects into the modal.

Follow `bot/flow.py`'s existing pattern for staged interactive state (in-memory,
keyed by `(guild_id, user_id)`, discarded on restart) — but for offers, persist
to `contract_offers` the moment the review panel is confirmed, so a bot restart
cannot lose a submitted offer.

---

## 7. Validation matrix

`bot/contracts/rules.py` implements each of these as a named predicate. Every
rejection returns a stable machine code plus a human message, and the full
result set is stored in `contract_offers.validation` at submit time so disputes
are auditable.

| Check | Behaviour on failure |
|---|---|
| Actor is TP (or admin) for the offering team **in that tier** | reject |
| Driver row exists in the selected tier | reject |
| Driver status permits signing | reject |
| Driver already under an active contract | allow only via trade/buyout flow; flag as interest |
| Salary ≥ `min_salary` | reject |
| Salary ≤ `max_salary` (if set) | reject |
| Contract payroll + salary + bonus ≤ `salary_cap` | reject, show the arithmetic |
| Team has a free `active_driver_slots` seat | reject or require a linked release |
| `term_seasons` within `1..max_term_seasons` | reject |
| Incentives ≤ `max_incentive_pct` × salary | reject |
| Free agency window open (for FA offers) | reject or queue |
| Duplicate pending offer from this team to this driver | require edit/withdraw |
| Driver suspended/inactive | warn + require commissioner approval |

The review panel must display, before submission: driver's current market value,
current contract and P/L, the offering team's payroll before and after, cap
space remaining, and every warning. A TP should never be able to submit an
invalid offer by accident.

---

## 8. Lifecycle

```
DRAFT → PENDING_DRIVER → (DECLINED | WITHDRAWN | EXPIRED)
                       → COUNTERED → PENDING_TEAM → …
                       → ACCEPTED → PENDING_APPROVAL → APPROVED → contract ACTIVE
                                                     → REJECTED
```

Invariants:

- Terms are **immutable once accepted**. A driver can only accept, decline, or
  counter — never edit money inside the acceptance flow.
- On `APPROVED`: create the `contracts` row, set `contract_value`, snapshot
  `value_at_signing`, recompute team payroll, write ledger rows, assign the
  Discord team role by reusing the existing `/roster sign` code path (do not
  duplicate role logic), and post to the transactions channel.
- Expiry is enforced by a `discord.ext.tasks` loop alongside the existing
  15-minute poll in `events.py`; also check expiry lazily on read so a missed
  tick can never surface a stale offer as actionable.
- All state transitions go through `bot/contracts/service.py`. No cog mutates
  offer or contract state directly.

---

## 9. Phased delivery

Each phase is a separate commit with migrations, tests, and README updates.
Do not begin a phase until the previous one has green `ruff` + `pytest`.

**Phase 1 — foundations.** Migrations 002/003/006, `seasons`/`tiers`/`drivers`/
`league_config` models and queries, `bot/presets/f1.py` seeder, the magic-number
guard script, `/market-admin season|tier|config`. Backfill: existing `teams`
rows get the active season. Deliverable: an F1 season with three tiers can be
created and configured, and nothing about existing `/roster` behaviour changes.

**Phase 2 — valuation engine.** Migration 004, `bot/market/valuation.py`,
`bot/market/money.py`, `valuation_factors` seeding, dry-run preview then
publish. Deliverable: `/market-admin valuation run` produces a reproducible,
per-factor-auditable set of values with movement caps applied, tier-isolated.

**Phase 3 — market surfaces.** Migration 007, `bot/market/render.py`, `/market
view|movers|driver|team|surplus|underwater|dashboard`, self-updating market
boards on the `stat_boards` pattern. Deliverable: readable, paginated,
mobile-safe market posts.

**Phase 4 — contracts.** Migration 005, `rules.py`, `service.py`,
`/contract offer` two-stage flow, review panel, driver-facing card,
counteroffer, commissioner approval, signed-contract post, ledger. Deliverable:
a full offer→approval cycle with cap enforcement and an audit trail.

**Phase 5 — trades, releases, and rollover.** Trade flow that transfers a
contract without resetting `contract_value`, buyout costing, release with frozen
P/L, extension that resets the baseline from an effective date, and
promotion/relegation recalibration between tiers. Deliverable: documented,
tested rules for every way a contract can end or move.

---

## 10. Testing requirements

Extend `tests/`, reusing `conftest.py`'s `FakeMember`/`FakeRole` fakes and
`make_team`/`make_slot` factories.

Required coverage:

- `test_valuation.py` — determinism (same input → same output), movement caps
  respected in both directions, tier isolation (adding a Tier 2 driver cannot
  change any Tier 1 value), factor weights sum correctly, `Decimal` in and out.
- `test_money.py` — parse/format round-trips, rounding at the boundary, negative
  and zero P/L formatting.
- `test_contract_rules.py` — one test per row of the validation matrix, both the
  pass and the fail path, asserting the stable failure code.
- `test_contract_lifecycle.py` — every legal transition succeeds and every
  illegal one raises; accepted terms cannot be mutated; expiry.
- `test_market_render.py` — line-width bound, embed limits, pagination
  boundaries, empty-tier and single-driver cases.
- `test_migrations.py` — migrations apply cleanly onto a fresh DB and onto a
  `001_init.sql`-only DB with existing `teams` rows (the real upgrade path).

Money must never be compared with float tolerance. Use `Decimal` equality.

---

## 11. Definition of done

- `uv run ruff check bot/` and `uv run pytest` pass.
- No numeric business literal in `bot/market/`, `bot/contracts/`, or
  `bot/valuation.py` — the guard script proves it.
- Core modules do not import `bot/presets/`.
- All new tables are `season_id`-scoped; no new `guild_id`-scoped race data.
- Money is `NUMERIC`/`Decimal` end to end; no floats anywhere in the path.
- Cap compliance uses contract value; market value never affects compliance.
- Cross-tier views are display-only and provably cannot influence valuations.
- Every money mutation has a `contract_ledger` row with actor and reason.
- Existing `/roster` and `/sheets` behaviour is byte-for-byte unchanged; the
  upgrade requires no manual data migration beyond running the bot.
- `README.md` gains a Market & Contracts section with the full command table,
  and `MIGRATION.md` gains upgrade notes.

---

## 12. Working agreement

- Ask before changing anything in `001_init.sql`, the permission helpers, the
  "no members table" principle, or the `db.connect()` transaction contract.
- Prefer extending an existing pattern over introducing a new one. If you think
  a new pattern is warranted, say why in the commit message.
- When a rule in this file conflicts with something you find in the code, stop
  and flag it rather than silently picking one.
- Keep commits phase-sized and independently reviewable. Include the migration,
  the code, and the tests in the same commit.
