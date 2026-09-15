# Upgrade notes

## Control panel fix — discord.py ≥ 2.6 (no migration)

Every button and select in the `/league` panel stored its owning view as
`self._parent`. discord.py 2.6 introduced its own `Item._parent` (the
item's container) and reads it in `Item._run_checks` before every
callback, so on a current library the first click on **Free agency**,
**Teams → adjust cap**, **Drivers → pick a driver**, and every other
panel control raised
`AttributeError: '<View>' object has no attribute '_run_checks'` and the
click was ignored. The attribute is now `_owner` throughout `bot/ui/` and
`bot/cogs/contracts.py`; `tests/test_ui_reserved_attrs.py` scans every
`discord.ui.*` subclass for reserved names and drives the three
crashing controls through discord.py's real check path.

## Migration 014 — contract carry-over (Phase 8)

Apply as usual; the runner picks up `014_contract_carryover.sql` on the
next bot start. Forward-only, safe to re-apply.

**What it adds**

- `contracts.season_index INTEGER NOT NULL DEFAULT 1` (CHECK `>= 1`) —
  which season of `term_seasons` the row represents.
- `contracts.carried_from_contract_id` / `contracts.origin_contract_id`
  — nullable self-references (`ON DELETE SET NULL`) linking each season's
  row to the previous one and to the original signing.
- Partial unique index `uq_contracts_carried_once` on
  `carried_from_contract_id`: a row can be continued at most once.
- Indexes on `origin_contract_id` and `(season_id, state)`.
- `contract_states` gains `carried` (terminal).
- `transaction_kinds` gains `contract_carried` and `contract_expired`.

**What it changes in existing data**

Every existing contract row gets `season_index = 1` and NULL links. No
row changes state; nothing else is written.

**Behavioural changes to be aware of**

- **Nothing happens until you run `/market-admin season carry-over`.**
  Contracts from past seasons remain `active` (and keep counting against
  live payroll, exactly as before) until a commissioner runs the command
  for that season. If the bot has already crossed a season boundary,
  run it once per past season, oldest first.
- `queries.update_contract_terms` (the extension path) now also sets
  `season_index = 1`. An extension is treated as a new term starting in
  the current season.
- `budget_ops.rollover` computes the source season's payroll with the
  new `queries.fetch_team_season_payroll` (rows that served that season
  — `active`, `carried`, or `expired` — plus dead money) instead of
  `fetch_team_effective_payroll` (active rows only). The number is
  identical when carry-over has not run and stays put when it has.

## Migration 013 — team budgets (Phase 7)

Apply as usual; the runner picks up `013_team_budgets.sql` on the next bot
start. Forward-only, safe to re-apply.

**What it adds**

- `budget_entry_kinds` — lookup of ledger kinds with a `direction`
  (`credit`, `debit`, `either`). Seeded with `opening_balance`,
  `rollover`, `prize_money`, `race_earnings`, `dnf_penalty`,
  `dns_penalty`, `incident_penalty`, `adjustment`.
- `team_budget_ledger` — append-only, one row per money movement, with
  `season_id`, `team_id`, `kind`, `amount NUMERIC(12,2)`, optional
  `race_result_id` / `round_id` / `from_season_id`, `is_correction`,
  `actor_id`. A trigger rejects a row whose sign disagrees with its kind
  unless `is_correction` is set. Partial unique indexes allow exactly one
  `opening_balance` and one `rollover` per team per season.
- `budget_config` — season default with optional per-tier override:
  `enforce_budget`, `rollover_enabled`, `opening_budget`,
  `earnings_per_point`, `dnf_penalty`, `dns_penalty`,
  `penalty_per_incident_pt`.

**What it changes in existing data**

Nothing. No existing table is altered and no rows are written.

**Behavioural changes to be aware of**

- **Existing seasons are not enforced until you opt in.** The F1 preset
  seeds a `budget_config` row for seasons created *after* this upgrade.
  A season that already exists has no row, so offers, trades, and imports
  behave exactly as before. To enable budgets on a running season, run
  `/market-admin budget config enforce: true ...` once.
- **Trade approval now checks the receiving team's cap and budget.**
  Before Phase 7, `commissioner_approve_trade` executed the transfer
  without checking payroll against the salary cap. It now refuses a trade
  that would put the receiving team over the spending cap (always) or
  over its budget (when enforced). This closes a pre-existing gap.
- **Offer validation gains a rule.** `budget_headroom_ok` produces the
  `budget_exceeded` code. When budgets are not enforced it passes with a
  note, so existing review panels are unchanged apart from one extra line.

**Preset rates are placeholders.** `earnings_per_point 0.05`,
`dnf_penalty 0.50`, `dns_penalty 1.00`, `penalty_per_incident_pt 0.25`
were chosen so that a full season of results moves a budget by single-
digit millions against a $145M cap. Tune them with
`/market-admin budget config` once you have seen a real round.

## Migration 010 — race results and normalization (Phase 6)

Apply as usual; the runner picks up `010_race_results_and_normalization.sql`
on the next bot start. Migrations are forward-only and this one is safe to
re-apply.

**What it adds**

- `position_scores` — the per-season normalization curve. One row per
  finishing position carrying `race_score`, `quali_score`, `points`, and
  the `is_win` / `is_podium` / `is_pole` flags.
- `results_config` — form and consistency window lengths plus the
  incident-point scale, season default with optional per-tier override.
- `race_rounds` / `race_results` — durable, re-importable raw facts.
- `valuation_runs.round_id` — nullable link from a run to the round it
  priced. Existing runs keep `NULL`.

**What it changes in existing data**

This migration **recalibrates the Phase 2 valuation factor weights**, and
that is a behavioural change, not just a schema one. It is deliberate:
`race_finish` shipped at `+1.0000` against a *raw* finishing position, and
because the engine computes `contribution = weight * raw_value`, a P20
scored twenty times a win. Separately, `points_scored` had weight `0.60`
against raw championship points while its `max_contribution` was `0.50`, so
a 25-point win and a 10-point P5 both clipped to the same value and the
factor carried no information.

The new weights assume observations arrive normalized to `[0, 1]`, which is
what `bot/market/results.py` now guarantees.

| Factor | Weight | Max contribution |
|---|---:|---:|
| `race_finish` | 0.4500 | 0.60 |
| `points_scored` | 0.2000 | 0.30 |
| `wins` | 0.1800 | 0.25 |
| `form_trend` | 0.1500 | 0.25 |
| `quali_finish` | 0.1200 | 0.20 |
| `podiums` | 0.1000 | 0.15 |
| `consistency` | 0.1000 | 0.15 |
| `poles` | 0.0800 | 0.12 |
| `driver_of_day` *(new)* | 0.0600 | 0.10 |
| `fastest_laps` | 0.0500 | 0.10 |
| `dnf` | −0.3500 | 0.40 |
| `incidents` | −0.2000 | 0.30 |

**Commissioner tuning is preserved.** The recalibration only rewrites a
weight that still holds its exact Phase 2 default. If you had already
retuned a factor by hand, your value is left alone — and you should
re-check it against the new `[0, 1]` observation scale, because a weight
chosen for raw inputs will no longer mean what you intended.

The migration also backfills `position_scores` and a default
`results_config` row for every existing season, and adds the new
`driver_of_day` factor to every season that already has factors seeded.
F1 25/26 is currently the only preset, so the backfilled curve matches
what a new season would get from `bot/presets/f1.py`.

**Published values are not touched.** No existing `driver_valuations`,
`contracts`, or `contract_ledger` rows change. The new weights apply from
the next valuation run onward.

**Suggested rollout**

1. Apply the migration.
2. `/market-admin results import` the most recent round.
3. `/market-admin results show` it and sanity-check the observations.
4. `/market-admin valuation run` — this is a **dry run**, nothing publishes.
5. Compare the preview against expectations, adjust weights via
   `valuation_factors` or reshape `position_scores` if the movement feels
   wrong, and re-run.
6. `/market-admin valuation publish` only once the preview looks right.

Because a run is bounded by `round_order`, steps 2–5 are repeatable with
no side effects on the live market.

---

# Upgrade & migration notes

## Phase 5 (Trades, releases, buyouts)

Ships migration `009_trades_and_dead_money.sql`.

**What changes for the live database:**

- New tables: `trade_states` (seeded inline with 9 rows), `trades`,
  `trade_items`, `dead_money`.
- All new tables reference existing Phase 1–4 tables; no columns
  change on any existing table.
- Partial unique index on `trades.expires_at` restricted to
  non-terminal states — same pattern the Phase 4 offer index uses.
- No data migration required.

**Rollback:** dropping the four new tables reverts Phase 5 without
touching Phase 1–4 state.

---

## Phase 4 (Contracts, offers, ledger)

Ships migration `008_contracts_and_offers.sql`. Applied automatically
after Phase 3 is recorded.

Numbering note: CLAUDE.md §4 called this file `005_*`, but migrations
apply in sorted filename order and the contracts tables reference
lookup tables that live in `006_*` (contract_types, contract_states,
offer_states, transaction_kinds). Keeping the numeric ordering
truthful — contracts come after their FK targets — is why this file
is `008_*` rather than `005_*`.

**What changes for the live database:**

- New tables: `contracts`, `contract_offers`, `contract_ledger`.
- All three reference existing tables (seasons/tiers/drivers/teams
  plus the Phase 1 lookup tables); nothing changes on existing
  tables.
- Two partial unique indexes enforce the core business invariants at
  DB level:
  - `uq_contracts_one_active_per_driver` — at most one row where
    `state = 'active'` per driver.
  - `uq_offers_one_open_per_team_driver` — at most one row in the
    open-state set per (team, driver). `countered` is intentionally
    NOT in the open set — the parent offer becomes `countered` and
    the child carries the negotiation forward, so parent+child can
    legitimately share a (team, driver).
- No data migration required.

**Rollback:** the tables are additive and self-contained. Dropping
them (plus the two partial indexes) reverts Phase 4 without touching
Phase 1–3 state.

---

## Phase 3 (Market surfaces)

Ships migration `007_market_boards.sql`. Applied automatically on
next bot start after Phase 2 is already recorded.

**What changes for the live database:**

- New table: `market_boards`. References existing `seasons`, `tiers`,
  and `board_kinds` (all present from Phase 1/2).
- No column changes on any existing table.
- No data migration required.

**Rollback:** dropping `market_boards` reverts Phase 3 cleanly. The
`/market` cog is a read-only surface — removing the cog while the
table exists is also safe.

---

## Phase 2 (Valuation engine)

Ships migration `004_valuations.sql`. On next bot start,
`db._run_migrations` applies it in sorted order after the Phase 1
migrations already recorded in `schema_migrations`.

**What changes for the live database:**

- New tables: `valuation_runs`, `driver_valuations`. Both are additive
  and reference existing Phase 1 tables (`seasons`, `tiers`,
  `drivers`).
- No column changes on any existing table.
- No data migration required.

**Rollback:** dropping the two new tables reverses Phase 2 cleanly.
Nothing in `bot/cogs/roster.py` reads them.

---

## Phase 1 (Market & Contracts foundations)

Phase 1 ships migrations `002_seasons_tiers.sql`,
`003_drivers_and_tier_membership.sql`, and
`006_league_config_and_presets.sql`. On next bot start,
`db._run_migrations` applies each one in order and records it in
`schema_migrations`.

**What changes for the live database:**

- New tables: `seasons`, `tiers`, `drivers`, `driver_statuses`,
  `contract_types`, `contract_states`, `offer_states`,
  `transaction_kinds`, `board_kinds`, `valuation_factors`,
  `league_config`.
- `teams` gains two nullable columns: `season_id` and `tier_id`. Both
  reference the new tables but stay `NULL` for every existing row.
  `NULL` means "belongs to the active season" — no data migration is
  required, and `/roster` behaves identically until a commissioner opts
  in to the new market system via `/market-admin season create --preset f1`.
- Numbers 004/005/007 are reserved for later phases and do not exist
  yet; the runner is fine with the gap (files apply in sorted order and
  each records independently).

**No manual data migration is required.** Ship the code, restart the
bot, done.

**Rollback plan:** the new tables are all additive and self-contained.
If Phase 1 must be reverted, dropping the new tables and the two new
`teams` columns is safe — but nothing in `bot/cogs/roster.py` reads them,
so there is no code-level dependency to unwind first.

---

# Cutover runbook: local → cloud VM

The bot can only run in **one** place at a time — Discord allows a single
gateway connection per bot token. The plan below holds the bot offline for
~1 minute while a final DB snapshot is shipped across.

## What gets transferred

- All source under `roster-bot/` (excluding caches, venv, `.git/`, the legacy
  SQLite DB and a few stale top-level files)
- `Containerfile`, `docker-compose.yml`, `Makefile`, `pyproject.toml`, `uv.lock`
- `migrations/001_init.sql` (the live schema, applied automatically on bot startup)
- `.env` (Discord token, Google API key)
- `db/dump.sql` — `pg_dump --clean --if-exists` of the live database

## One-time VM prep

1. Install Docker + compose plugin:
   ```sh
   sudo apt update
   sudo apt install -y docker.io docker-compose-v2
   ```
2. Open no inbound ports — the bot only makes outbound connections to Discord,
   and Postgres is bound to `127.0.0.1` by `docker-compose.yml`.

## Pre-stage the image (optional, makes the cutover faster)

Doing this hours before cutover means the image is already built on the VM, so
the real cutover only ships a tiny dump.

```sh
# On the laptop
./scripts/package.sh
scp /tmp/roster-bot-*.tar.gz user@vm:/tmp/

# On the VM
tar -C ~ -xzf /tmp/roster-bot-*.tar.gz
cd ~/roster-bot
sudo docker compose build           # warm the layer cache
```

Don't run `restore.sh` yet — the dump is stale; you'll do a fresh one at cutover.

## The actual cutover (start a stopwatch)

### 1. Stop the local bot only — keep Postgres up so we can dump it
```sh
sudo docker compose stop roster-bot
```
Bot is now offline in Discord.

### 2. Take the final dump and ship it
```sh
./scripts/package.sh
scp /tmp/roster-bot-*.tar.gz user@vm:/tmp/
```

### 3. On the VM: extract → restore → start
```sh
rm -rf ~/roster-bot
tar -C ~ -xzf /tmp/roster-bot-*.tar.gz
cd ~/roster-bot
./scripts/restore.sh        # uses FORCE=1 if a stale DB already exists
make logs                   # confirm "Logged in as ..."
```

### 4. Verify, then tear down the local stack
- In Discord: bot shows online, `/roster view <team>` returns the embed,
  `/roster list` shows the same teams as before.
- Once happy, on the laptop:
  ```sh
  sudo docker compose down
  ```

## Rollback

If the VM is misbehaving **and you haven't torn down the local stack yet**:
```sh
# On the VM
sudo docker compose stop roster-bot

# On the laptop
sudo docker compose start roster-bot
```
The local DB still has the pre-cutover state. Any writes made on the VM during
the verification window will not be in the local DB — that's why you verify
*before* `compose down` on the laptop.

## After teardown

The local Postgres volume (`postgres-data`) still holds the pre-cutover data —
keep it around for a few days as a belt-and-suspenders backup. To remove it
once you're confident:
```sh
sudo docker compose down -v
```

## Security notes

- The tarball contains your live Discord token in `.env`. Treat the file like
  a credential: `scp` over SSH only, and `shred -u` it from `/tmp/` on both
  hosts when you're done.
- If the VM is ever shared or compromised, rotate the token in the Discord
  Developer Portal and update `.env`.
