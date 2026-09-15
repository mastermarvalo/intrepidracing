# CLAUDE.md — Intrepid Racing League bot

Operating manual for the `roster-bot` codebase: a Discord bot running a
multi-tier F1 sim-racing league with rosters, a per-tier driver market, a
salary cap, and a full contract lifecycle.

**Read this file in full before writing code.** Then read
`docs/ADR-001-f1-with-generic-future.md` — it is binding, not advisory.

This file describes the system **as built**, not as planned. Where a rule
is marked BINDING, violating it is grounds for rejecting the change.

---

## 1. Status

Phases 1–6 are shipped. Git history:

```
fda8506  Make contract length bounds commissioner-editable
95a41fb  Interactive Setup, Approvals and Boards screens
01a4ee3  Add /league control panel and /help browser
1ea8d80  Document Google Sheets service-account setup
ab25adc  Phase 6: race-results ingestion and normalization
d5f4ece  Add docs/MARKET_GUIDE.md
```

- **454 tests passing**, 26 test modules.
- **74 slash commands** across 7 groups.
- **11 migrations** (005 is deliberately absent — see §5).
- `ruff` clean, magic-number guard clean.

> **Unverified:** at the time of writing, these commits were **local only
> and not pushed** to `github.com/mastermarvalo/intrepidracing`. Confirm
> with `git status -sb` before assuming the remote matches.

### Running the gate

Every change must pass all three:

```bash
cd roster-bot
uv run ruff check .
uv run python scripts/check_magic_numbers.py
TEST_DATABASE_URL="postgresql://..." uv run pytest -q
```

Tests that need Postgres skip cleanly when `TEST_DATABASE_URL` is unset,
so bare `uv run pytest` works but proves less. Always run with a database
before claiming a change is done.

In a sandbox without Docker or apt, an ephemeral server can be had via
the `pgserver` wheel:

```bash
uv python install 3.12
uv venv --python 3.12 /tmp/pg312
uv pip install --python /tmp/pg312/bin/python pgserver
mkdir -p /tmp/pgdata
/tmp/pg312/bin/python -c 'import pgserver; print(pgserver.get_server("/tmp/pgdata", cleanup_mode=None).get_uri())'
```

That prints a URI like `postgresql://postgres:@/postgres?host=/tmp/pgdata`.
Re-run the last line to restart it after the sandbox idles out.

---

## 2. Repository map

Single Python package at `roster-bot/`. `discord.py` 2.x + asyncpg +
Postgres 16, deployed with Docker Compose. Python 3.11+, managed with
`uv`, linted with `ruff` (line-length 100, rules `E,F,I`), tested with
`pytest` (`asyncio_mode = "auto"`).

```
roster-bot/
  bot/
    main.py            60   RosterBot(commands.Bot); setup_hook loads cogs, db.init(), tree.sync()
    db.py              75   asyncpg pool, connect() context manager (auto transaction),
                            _run_migrations() applies migrations/*.sql in sorted filename order
    models.py         235   dataclasses: Team, TeamSlot, GuildConfig, StatBoard, LeagueConfig, …
    queries.py       2360   ALL SQL. Functions take an open asyncpg.Connection
    limits.py          53   Discord protocol limits + render widths (deliberately outside
                            the magic-number guard — protocol facts, not league policy)
    roster_ops.py     106   sign_to_team / drop_from_team — the ONLY role-mutation path
    flow.py          1254   9-step interactive team create/edit wizard (pre-existing)
    render.py         471   roster embeds + Pillow image rendering
    events.py         233   on_member_update trigger, 15-min poll, board refresh loop
    sheets.py         245   Google Sheets fetch/format
    results_ingest.py 293   CSV/Sheets → parsed race results, Discord-free
    approvals.py      341   Discord-AWARE approval orchestration
    workflow.py       813   Discord-FREE shared layer behind the panel and the cogs
    panel_help.py     214   /help catalog, built from live tree.walk_commands()

    market/
      valuation.py    236   pure, data-driven, Decimal-only; applies movement caps
      money.py         96   Decimal parse/format, single rounding boundary
      render.py       377   market tables, movers, cap sheets, driver cards, dashboard
      boards.py       238   self-updating market board embeds
      results.py      346   race results → normalized valuation observations

    contracts/
      rules.py        425   validation predicates; pure functions over plain data
      service.py     1100   offer/contract state machine
      render.py       618   review panel, driver offer card, counteroffer, signed post

    presets/
      f1.py           326   seeds tiers, statuses, types, states, factors, points,
                            default league_config. Core modules must NOT import this

    ui/                     Interactive panel screens (discord.ui views/modals)
      base.py         125   OwnedView, AdminOwnedView, BackButton, truncate_field,
                            report_error, PANEL_TIMEOUT_SECONDS, MODAL_MAX_INPUTS
      setup_screen.py 681   guided setup checklist + season/tier/role/channel flows
      approvals_screen.py 359  approval queue browser with detail + reject modal
      boards_screen.py 325  board add/remove wizard
      config_modal.py 460   league config editors (money limits / contract rules)

    cogs/
      roster.py       757   /roster (16 commands)
      sheets.py       392   /sheets (4)
      market.py       385   /market (7)
      contracts.py   1082   /contract (9)
      trades.py       393   /trade (5)
      admin_market.py 1574  /market-admin (31)
      panel.py        738   /league + /help (2)

  migrations/               001–011, forward-only
  scripts/check_magic_numbers.py  125  the ADR-001 CI guard
  docs/results_template.csv       race-results import template
  tests/                          26 modules, 454 tests
docs/
  ADR-001-f1-with-generic-future.md   BINDING architecture decision
  MARKET_GUIDE.md                     league-member walkthrough
```

---

## 3. Binding rules

### From ADR-001

| Rule | What it means here |
|---|---|
| **No magic numbers** | Salary cap, movement caps, min/max salary, contract term bounds, roster slots, incentive ceilings, valuation weights, points tables — all live in DB config rows seeded by a preset. Zero numeric business literals in `bot/market/`, `bot/contracts/`, `bot/valuation.py`. Only `0`, `1`, `-1` are allowed. Enforced by `scripts/check_magic_numbers.py`, which excludes `bot/presets/`. |
| **Domain entities as data, not enums** | `contract_types`, `offer_states`, `trade_states`, `valuation_factors`, `transaction_kinds`, `board_kinds`, `driver_statuses` are tables with `code`/`label`, not Python `Enum`s. Code looks entities up by `code`. |
| **F1 specifics in `presets/`** | F1 valuation weighting, points table, and tier naming live in `bot/presets/f1.py`, which only *writes rows* into generic tables. **Core modules must not import `bot/presets/`.** |
| **Season scoping from day 1** | Every market/contract table is scoped to `season_id`, never `guild_id`. |

### Inherited from the original roster bot

- **No members table.** Roster membership is derived live from Discord
  roles at render time. Do not change this. (`drivers` is a scoped
  exception — see §5.)
- **Queries layer is the only place SQL lives.** Cogs never embed SQL
  strings.
- **Render is idempotent.** Builders edit a stored `message_id` in place;
  if the message is gone, the id is cleared and the entity is flagged as
  broken.
- **Permission model.** `_is_admin()` = Manage Server. Team-scoped
  actions also accept `teams.principal_role_id`. Reuse these helpers; do
  not invent a new one.
- **Ephemeral replies** for anything administrative or private.

### Money and market invariants

1. **Markets are strictly tier-isolated.** A driver's value is computed
   only against the field of their own tier. **No cross-tier
   normalization, ever.** A cross-tier dashboard may *display* all three
   but must never let values interact.
2. **Two distinct money numbers per driver:**
   - `market_value` — recomputed from performance each cycle.
   - `contract_value` — the salary agreed when the current team signed
     them; frozen at signing.
   - `P/L = market_value − contract_value`. Positive = under-market
     (team surplus). Negative = above-market.
3. **Cap compliance is measured on contract value, never market value.**
   Market value drives free-agent cost, trade value, extension demands,
   and buyout math only.
4. **Every money mutation is an append-only `contract_ledger` row** with
   actor, timestamp, and reason. Nothing overwrites history.
5. **Movement caps.** Standard weekly change is bounded (default
   ±$0.75M); an exceptional-performance band allows a larger bound
   (default ±$1.25M). Both are configuration, not literals.
6. **Contract value resets only on extension/renegotiation.** A trade
   transfers the existing deal untouched unless the trade explicitly
   includes a renegotiation. A release freezes that contract's P/L in the
   ledger.
7. **Money is `NUMERIC(12,2)` / `Decimal` end to end. Never floats.**
   Round half-up at a single documented boundary in `bot/market/money.py`.
   Tests must use `Decimal` equality, never float tolerance.
8. **Role assignment goes through `roster_ops.sign_to_team` /
   `drop_from_team`.** Never mutate team roles anywhere else.

---

## 4. Discord platform constraints

These have each caused a bug or a redesign. Treat them as hard facts.

- **A modal holds at most 5 text inputs**, and **cannot contain select
  menus.** This is why the offer flow and the config editor are both
  two-stage. See `bot/ui/base.py: MODAL_MAX_INPUTS`.
- **A select menu holds at most 25 options.** See `SELECT_MAX_OPTIONS`.
- **Embed field values cap at 1024 chars**, embeds at 6000 total. See
  `bot/limits.py` and `truncate_field`.
- **Discord wraps long lines badly on mobile.** Do not render wide
  multi-column tables. Use the two-line-per-driver layout:

  ```
  4. ZeezinDomar — Williams
     Market: $20.75M  |  Week: ▲ $1.25M
     Contract: $12.50M  |  P/L: +$8.25M
  ```

  Paginate at 10 drivers per message with page buttons. Every renderer
  needs a test asserting no line exceeds the configured width and no
  embed exceeds Discord's limits — extend `tests/test_overflow.py`'s
  approach rather than inventing a new one.
- **Component interactions arrive from anyone who can see the message**,
  even for ephemeral replies after a client resync. All panel views
  extend `OwnedView` (opener-locked) or `AdminOwnedView` (re-checks
  Manage Server on every click).

---

## 5. Schema as built

New migration files only — **never edit `001_init.sql`.**
`db._run_migrations` applies `migrations/*.sql` in sorted filename order
and records each in `schema_migrations`. Files must be idempotent-safe
and forward-only. **Do not put `BEGIN`/`COMMIT` inside a migration**; the
runner already wraps each file in a transaction.

| Migration | Tables |
|---|---|
| `001_init.sql` | `teams`, `team_slots`, `guild_config`, `stat_boards`, `transactions`, `schema_migrations` — **never edit** |
| `002_seasons_tiers.sql` | `seasons`, `tiers`; alters `teams` |
| `003_drivers_and_tier_membership.sql` | `driver_statuses`, `drivers` |
| `004_valuations.sql` | `valuation_runs`, `driver_valuations` |
| *005* | **absent by design** — numbering skipped, do not backfill it |
| `006_league_config_and_presets.sql` | `contract_types`, `contract_states`, `offer_states`, `transaction_kinds`, `board_kinds`, `valuation_factors`, `league_config` |
| `007_market_boards.sql` | `market_boards` |
| `008_contracts_and_offers.sql` | `contracts`, `contract_offers`, `contract_ledger` |
| `009_trades_and_dead_money.sql` | `trade_states`, `trades`, `trade_items`, `dead_money` |
| `010_race_results_and_normalization.sql` | `position_scores`, `results_config`, `race_rounds`, `race_results`; alters `valuation_runs` |
| `011_min_contract_term.sql` | adds `league_config.min_term_seasons` + CHECK constraints |

### The `drivers` exception

`drivers` is a deliberate, scoped exception to "no members table": it
stores **market and contract state**, not roster membership. Discord
roles remain the source of truth for who is on a team and in a tier;
`drivers` only anchors money and history to a member id.

A member may legitimately exist in more than one tier (e.g. a Tier 1
reserve who races Tier 2). Each is a separate `drivers` row with its own
independent market value, keyed `UNIQUE (season_id, tier_id, member_id)`.
**Never merge them.**

### DB-level enforcement

- Partial unique index: each driver has at most one `active` contract.
- At most one pending offer per `(team, driver)`.
- `league_config`: `min_term_seasons >= 1` and
  `min_term_seasons <= max_term_seasons`.

---

## 6. Purity boundaries

This is what makes the codebase testable and the magic-number guard
meaningful.

| Layer | May import | Must NOT import |
|---|---|---|
| `bot/market/valuation.py`, `bot/market/money.py`, `bot/contracts/rules.py` | stdlib, `Decimal` | `discord`, DB, `bot/presets/` |
| `bot/results_ingest.py`, `bot/workflow.py` | DB, queries | `discord` |
| `bot/contracts/service.py` | DB, queries, rules | `discord` where avoidable |
| `bot/approvals.py`, `bot/ui/*`, `bot/cogs/*` | everything | raw SQL strings |

`bot/workflow.py` is the Discord-free shared layer. Both the slash
commands and the interactive panel screens call into it, which is what
keeps the two surfaces behaviourally identical. When adding a feature,
put the logic in `workflow.py` and let both surfaces delegate — do not
implement it twice.

---

## 7. Command surface — 74 commands

**Nothing here may be removed or renamed.** The panel (§8) is an additive
layer on top; every command remains available for power users.

### `/league` and `/help` (2)

```
/league                             the control panel — the intended entry point
/help                               browsable catalog of every command, built
                                    from the live command tree
```

### `/market` (7)

```
/market view tier:<t> [page]        paginated tier market
/market movers tier:<t>             risers / fallers
/market driver name:<driver>        value, contract, P/L, trend
/market team name:<team>            cap sheet: payroll, space, per-driver P/L
/market surplus tier:<t>            best contracts by P/L
/market underwater tier:<t>         worst contracts by P/L
/market dashboard                   cross-tier summary (DISPLAY ONLY)
```

### `/contract` (9)

```
/contract offer                     two-stage: selects → modal → review → submit
/contract offers                    my team's outstanding offers
/contract withdraw id:<n>
/contract counter id:<n>            driver-facing counteroffer modal
/contract accept id:<n>
/contract decline id:<n>
/contract status driver:<d>
/contract release driver:<d>
/contract buyout driver:<d>
```

### `/trade` (5)

```
/trade propose | accept | decline | withdraw | status
```

### `/market-admin` (31, commissioner)

```
season create | activate | list
tier add | edit | list
config edit | show | channel | role | free-agency
valuation run | preview | publish | list
results import | list | show
board add | remove | refresh | list
approve | reject | approve-trade | reject-trade
void | set-status | adjust-cap | promote | relegate
```

### `/roster` (16) and `/sheets` (4)

Pre-existing. **Behaviour must stay byte-for-byte unchanged.**

```
roster: create edit view list refresh relink config sign drop
        bulksign bulkdrop remove freeagents graphic history dm
sheets: add list refresh remove
```

---

## 8. The control panel

`/league` is the intended entry point for admins. It exists because 74
slash commands is an unusable discovery surface. The panel wraps them in
a guided flow; it **adds** a layer and removes nothing.

```
/league
├── ⚙️  Setup      → guided checklist (season → tiers → rules → roles → channels → boards)
├── 🏁 Race Night  → import results → preview valuation → publish   (needs tiers)
├── 📋 Approvals   → pending offer + trade queue with detail view    (needs season)
└── 📊 Boards      → add / remove / refresh market boards            (needs tiers)
```

Buttons for steps that cannot work yet are greyed out — you cannot add a
tier before a season exists. Roles and channels use Discord's own
pickers, so ids are never typed by hand.

Implementation rules:

- Screens live in `bot/ui/*_screen.py`; each exposes `build_*_embed()`
  (pure, testable) and `open_*()` (the entry point).
- All logic goes through `bot/workflow.py`. A screen that reaches past it
  into queries directly is a bug.
- Views are opener-locked (`OwnedView`) and admin actions re-check
  Manage Server per click (`AdminOwnedView`) — an ephemeral message is
  not an authorisation boundary.
- Navigation is a tree with `BackButton`; every screen must be able to
  return home. `PANEL_TIMEOUT_SECONDS = 600`.
- `bot/panel_help.py` builds `/help` from `tree.walk_commands()` at
  runtime. It must never hardcode a command list — a new command appears
  in help automatically, and a stale entry is impossible.

---

## 9. Configuration surface

`league_config` is scoped per `(season, tier)`; a tier row is intended to
override the season-default row (`tier_id IS NULL`). The F1 preset writes
**one league-wide row** with `tier_id = NULL` that every tier inherits
until an override is created.

> **Known gap — read before relying on tier overrides.** ADR-001 called
> for a single resolver at `bot/market/config.py`. **It was never built.**
> `queries.fetch_league_config_row` fetches an exact `(season, tier)` row
> and does *not* fall back. The fallback is currently open-coded at each
> call site:
>
> | Site | Behaviour |
> |---|---|
> | `workflow.py:214` (valuation) | tier row, then season default |
> | `cogs/contracts.py:668, 803, 908` | tier row, then season default |
> | `cogs/trades.py:308, 351` | **season default only — a tier override is ignored** |
> | `cogs/market.py:176`, `cogs/admin_market.py:381, 442` | explicit scope, no fallback |
>
> So a per-tier salary cap would be honoured when validating an offer but
> **not** when validating a trade. Until a single resolver exists, treat
> per-tier overrides as only partially wired, and add the fallback to any
> new call site explicitly.

### Fields and F1 preset defaults

| Field | Default | Editable via |
|---|---|---|
| `salary_cap` | **145.00** | Money limits |
| `min_salary` | 1.00 | Money limits |
| `max_salary` | `NULL` (none) | Money limits |
| `weekly_move_cap` | 0.75 | Money limits |
| `exceptional_move_cap` | 1.25 | Money limits |
| `min_term_seasons` | 1 | Contract rules |
| `max_term_seasons` | 3 | Contract rules |
| `active_driver_slots` | 2 | Contract rules |
| `max_incentive_pct` | 0.150 | Contract rules |
| `offer_ttl_hours` | 48 | Contract rules |
| `free_agency_open` | false | `/market-admin config free-agency` |
| `market_channel_id` | — | `config channel` / Setup → Channels |
| `transactions_channel_id` | — | as above |
| `approvals_channel_id` | — | as above |
| `commissioner_role_id` | — | `config role` / Setup → Commissioner |

### The two-modal split

Ten numeric tunables do not fit Discord's five-input modal, so
`/market-admin config edit` and the Setup panel's **Cap & rules** button
open a chooser with two modals: **Money limits** and **Contract rules**.

`bot/ui/config_modal.py` is the single definition of both. A second copy
in a cog would drift the moment someone added a field.

Rules for this module:

- `_save()` rewrites every column, carrying over the half the modal did
  not edit. Last-writer-wins across concurrent section edits is accepted;
  config edits are rare enough that locking costs more than it saves.
- **Unsatisfiable ranges are rejected at edit time, not offer time.** A
  minimum above the maximum makes every offer illegal while looking like
  the Team Principal's mistake. `validate_terms()` and `_validate_money()`
  own these checks; the DB carries matching CHECK constraints so no other
  write path can persist one.
- `max_incentive_pct` is **stored as a fraction** (0.150) and **shown as
  a percentage** (15.0%), matching `/market-admin config show`. The
  `_PCT_SCALE` conversion lives in `bot/ui/`, which is outside the
  magic-number guard — do not move it into `bot/contracts/`.
- `test_every_numeric_config_field_is_editable_somewhere` asserts the two
  modals cover all ten fields. Adding a config column without adding an
  input will fail that test. That is intentional.

---

## 10. Validation matrix

`bot/contracts/rules.py` implements each row as a named predicate
returning a stable machine code plus a human message. The full result set
is stored in `contract_offers.validation` at submit time so disputes are
auditable. `rules.rule_codes()` enumerates every code, and
`tests/test_contract_rules.py` asserts every documented code is actually
reachable — **adding a rule requires adding a scenario there.**

| Check | Failure code | Behaviour |
|---|---|---|
| Actor is TP (or admin) for the offering team in that tier | `actor_not_authorised` | reject |
| Driver row exists in the selected tier | `driver_missing_in_tier` | reject |
| Driver status permits signing | `status_inactive_warning` / `status_suspended` / `status_unknown` | warn or reject |
| Driver already under an active contract | `active_contract_conflict` | allow only via trade/buyout; flag as interest |
| Salary ≥ `min_salary` | `salary_below_min` | reject |
| Salary ≤ `max_salary` (if set) | `salary_above_max` | reject |
| Payroll + salary + bonus ≤ `salary_cap` | `cap_exceeded` | reject, show the arithmetic |
| Team has a free `active_driver_slots` seat | `no_seat_available` | reject or require a linked release |
| Term ≥ 1 season | `term_too_short` | reject — absolute floor, a nonsense value |
| Term ≥ `min_term_seasons` | `term_below_minimum` | reject — league policy |
| Term ≤ `max_term_seasons` | `term_too_long` | reject |
| Incentives ≤ `max_incentive_pct` × salary | `incentives_negative` / `incentives_over_cap` | reject |
| Free agency window open (FA offers) | `free_agency_closed` | reject or queue |
| Duplicate pending offer from this team to this driver | `duplicate_pending_offer` | require edit/withdraw |

`term_too_short` and `term_below_minimum` are deliberately **distinct
codes**: a zero-season contract is a nonsense value, a one-season
contract under a two-season floor is a policy violation, and the Team
Principal needs to know which one they hit.

The review panel must display, before submission: the driver's current
market value, current contract and P/L, the offering team's payroll
before and after, cap space remaining, and every warning. **A TP should
never be able to submit an invalid offer by accident.**

---

## 11. Lifecycles

### Offers

```
DRAFT → PENDING_DRIVER → (DECLINED | WITHDRAWN | EXPIRED)
                       → COUNTERED → PENDING_TEAM → …
                       → ACCEPTED → PENDING_APPROVAL → APPROVED → contract ACTIVE
                                                     → REJECTED
```

`queries.OPEN_OFFER_STATES = ("draft", "pending_driver", "pending_team",
"accepted", "pending_approval")`. The approval queue reads
`state = 'pending_approval'`.

### Trades

`queries.OPEN_TRADE_STATES = ("draft", "pending_other", "accepted",
"pending_approval")`.

### Invariants

- Terms are **immutable once accepted.** A driver can only accept,
  decline, or counter — never edit money inside the acceptance flow.
- On `APPROVED`: create the `contracts` row, set `contract_value`,
  snapshot `value_at_signing`, recompute team payroll, write ledger rows,
  assign the Discord team role **via `roster_ops.sign_to_team`**, and
  post to the transactions channel.
- Expiry is enforced by a `discord.ext.tasks` loop **and** lazily on
  read, so a missed tick can never surface a stale offer as actionable.
- **All state transitions go through `bot/contracts/service.py`.** No cog
  mutates offer or contract state directly.

### Background loops

| Loop | Interval | Location |
|---|---|---|
| Roster poll + `market_boards.refresh_all_boards` | 15 min | `bot/events.py` `poll_loop` (`@tasks.loop` at line 89) |
| Sheets poll | 10 min | `bot/cogs/sheets.py:317` |

There are exactly **two** `@tasks.loop` declarations in the codebase.
Board refresh is not its own loop — it is called inside the 15-minute
roster poll, which is the safety net for missed `on_member_update`
events.

---

## 12. Race results and valuation

Results drive valuations. The pipeline is:

```
CSV / Google Sheet → results_ingest.parse_results → race_results rows
                   → market/results.py normalization (position_scores lookup)
                   → valuation.compute_run (per-factor, Decimal, movement-capped)
                   → preview → publish → boards refresh
```

- The position → score curve, points table, and win/podium/pole
  thresholds live in `position_scores` **as data**, seeded by
  `bot/presets/f1.py`. Code asks the row; it never computes
  `position <= 3`.
- `valuation_factors` is a config table (`code`, `label`, `weight`,
  `max_contribution`, `season_id`). Adding a factor is a data change, not
  a code change.
- Valuation is **deterministic**: same input → same output. Tier
  isolation is tested — adding a Tier 2 driver cannot change any Tier 1
  value.
- Always **preview before publish**.

### Import template

`roster-bot/docs/results_template.csv` holds the required headers. A live
Google Sheets version exists with `README`, `Round Template`, an example
round, and a `Column Reference` tab; the example tab is verified to parse
through the real `results_ingest.parse_results` with zero errors.

Google Sheets import uses a **service account**; the sheet must be shared
with the service-account email. Setup is documented in
`roster-bot/README.md`. Note the deployment gotcha already fixed there:
the credentials path must be reachable *inside the container*, not just
on the host.

### Ordering gotcha

`queries.list_race_rounds` orders **ascending**. The latest round is
`rounds[-1]`, not `rounds[0]`.

---

## 13. Testing requirements

Extend `tests/`, reusing `conftest.py`'s `FakeMember` / `FakeRole` /
`FakeAvatar` fakes and `make_team` / `make_slot` factories. DB fixtures
are `pg_conn` and `pg_conn_migrated` (each test isolated in a temporary
schema).

Required coverage per area:

- **`test_valuation.py`** — determinism, movement caps in both
  directions, tier isolation, factor weights, `Decimal` in and out.
- **`test_money.py`** — parse/format round-trips, rounding boundary,
  negative and zero P/L formatting.
- **`test_contract_rules.py`** — one test per validation-matrix row, both
  paths, asserting the stable failure code, plus the code-drift guard.
- **`test_contract_lifecycle.py`** — every legal transition succeeds,
  every illegal one raises; accepted terms cannot be mutated; expiry.
- **`test_market_render.py` / `test_overflow.py`** — line-width bounds,
  embed limits, pagination boundaries, empty-tier and single-driver cases.
- **`test_migrations.py`** — migrations apply cleanly onto a fresh DB
  **and** onto a `001_init.sql`-only DB with existing `teams` rows (the
  real upgrade path).
- **`test_panel_*.py` / `test_workflow_*.py`** — screen composition,
  navigation integrity, and the Discord-free workflow layer.

### Test gotchas learned the hard way

- `RuleResult`'s field is **`ok`**, not `passed`. `validate_offer`
  returns `OfferValidation(ok=…, results=…)` with a `.blockers` property.
- `contract_type` must be `"standard"` in fixtures.
- Drivers need a unique `member_id` per `(season, tier)`.
- The board message setter is
  `queries.set_market_board_message_id(conn, board_id, message_id)`.
- `Tier` has **`.label`**, not `.name`.
- `uv run ruff check --fix` will silently delete a re-export import it
  considers unused. If a test imports a symbol through a module that only
  re-exports it, point the test at the canonical home instead.

---

## 14. Definition of done

- `uv run ruff check .` and `uv run pytest` pass (with a real database).
- `scripts/check_magic_numbers.py` passes.
- Core modules do not import `bot/presets/`.
- All new tables are `season_id`-scoped.
- Money is `NUMERIC`/`Decimal` end to end; no floats anywhere in the path.
- Cap compliance uses contract value; market value never affects it.
- Cross-tier views are display-only and provably cannot influence
  valuations.
- Every money mutation has a `contract_ledger` row with actor and reason.
- Role changes go through `roster_ops`.
- Existing `/roster` and `/sheets` behaviour is unchanged; upgrading
  requires no manual data migration beyond running the bot.
- No command removed or renamed.
- `README.md` updated, including the command table and any new panel
  screen.

---

## 15. Working agreement

- **Ask before changing** anything in `001_init.sql`, the permission
  helpers, the "no members table" principle, or the `db.connect()`
  transaction contract.
- **Ask before pushing.** Commit locally; let the maintainer decide when
  the remote moves.
- Prefer extending an existing pattern over introducing a new one. If a
  new pattern is warranted, say why in the commit message.
- When a rule in this file conflicts with something in the code, **stop
  and flag it** rather than silently picking one.
- Keep commits reviewable: migration, code, and tests in the same commit.
- Flag anything that needs independent verification rather than
  presenting it as established fact.
- Report numbers actually produced by a command run in this session. Do
  not restate a previously claimed test count without re-running it.

---

## 16. Known open items

- **Commits are local.** Verify with `git status -sb` before assuming the
  GitHub remote is current.
- **No single `league_config` resolver** (see §9). This is the most
  significant outstanding architectural gap.
- **Phase 5 scope partially open.** Trades, releases, buyouts, and
  promotion/relegation exist (`009`, `test_trades.py`,
  `test_release_and_buyout.py`, `test_extension_and_promotion.py`).
  Season rollover is not built.
- **`MIGRATION.md` upgrade notes** were specified in the original brief
  and do not exist.
- **`bot/flow.py` (1254 lines)** predates the `bot/ui/` layer and uses
  its own in-memory staged-state pattern. New interactive work should
  follow `bot/ui/` instead. Consolidating the two is unscheduled.
