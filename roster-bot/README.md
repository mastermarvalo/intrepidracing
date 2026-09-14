# Roster Bot

Discord bot that maintains live, self-updating team roster embeds. Admins create teams via a guided `/roster create` flow; the bot keeps roster messages in sync by watching role changes and polling every 15 minutes.

## Requirements

- Python 3.11+ (local dev only)
- Docker + Docker Compose (for deployment)
- A Discord bot token with **Server Members Intent** enabled in the developer portal

## Deployment (Docker)

```sh
cp .env.example .env
# fill in DISCORD_TOKEN
```

Then use the Makefile:

```sh
make up        # build image and start detached
make down      # stop and remove container
make restart   # rebuild image and redeploy (use after code changes)
make logs      # follow logs
make build     # build image only
```

The database is Postgres (service: `postgres`, image `postgres:16-alpine`); data
lives in the `postgres-data` named volume and persists across restarts/rebuilds.
Schema migrations apply automatically on bot startup from `migrations/`.

**Importing an existing SQLite roster.db** (one-time, e.g. after upgrading from
the SQLite-only version):

```sh
# 1. Stop the bot only (Postgres keeps running)
sudo docker compose stop roster-bot

# 2. Apply the schema (start the bot once so migrations run, then stop again,
#    OR psql -f migrations/001_init.sql)
sudo docker compose up -d roster-bot && sleep 5 && sudo docker compose stop roster-bot

# 3. Run the data migration (from inside a Python venv with asyncpg installed)
DATABASE_URL=postgresql://roster:roster@127.0.0.1:5432/roster \
  uv run python scripts/migrate_sqlite_to_pg.py /path/to/roster.db

# 4. Bring everything back up
make up
```

## Local development

```sh
cp .env.example .env
# fill in DISCORD_TOKEN

uv sync --extra dev      # create venv and install deps
uv run python -m bot     # start the bot
```

Run tests (no bot token needed):

```sh
uv run pytest
```

Lint:

```sh
uv run ruff check bot/
```

## Discord setup

1. Create a bot at <https://discord.com/developers/applications>
2. Under **Bot → Privileged Gateway Intents**, enable **Server Members Intent**
3. Invite with scopes `bot` + `applications.commands` and permissions:
   - Read Messages / View Channels
   - Send Messages
   - Embed Links
   - Manage Roles (for `/roster sign` and `/roster drop`)
   - Manage Messages (to delete roster messages on `/roster remove`)
4. Make sure the bot's role sits **above all team roles** in Server Settings → Roles, otherwise it won't be able to assign them

## Commands

### Admin commands (require Manage Server)

| Command | Description |
|---|---|
| `/roster create <name>` | Open the 7-step setup flow for a new team |
| `/roster edit <name>` | Re-open the setup flow, pre-filled with current values |
| `/roster list` | List all teams configured in this server |
| `/roster remove <name>` | Delete a team and its roster message |
| `/roster config` | Set the server-wide Free Agent role |

### Sign / drop (require Manage Server **or** the team's principal role)

| Command | Description |
|---|---|
| `/roster sign <name> <member>` | Give a member the team role; removes Free Agent role if set |
| `/roster drop <name> <member>` | Remove a member's team role; restores Free Agent role if set |

### Open to everyone

| Command | Description |
|---|---|
| `/roster view <name>` | Show a team's current roster embed |
| `/roster freeagents` | List members who have the Free Agent role and at least one Tier role |

## Market & Contracts

The market/contract system lives alongside `/roster` and is being built in
phases.

- **Phase 1** — schema, F1 preset, and commissioner setup (season, tier,
  config).
- **Phase 2** — valuation engine (`bot/market/valuation.py`), money
  boundary (`bot/market/money.py`), dry-run→publish flow.
- **Phase 3** — public `/market` surfaces + self-updating market boards
  that mirror the `stat_boards` refresh pattern (`bot/market/render.py`,
  `bot/market/boards.py`).
- **Phase 4** — contracts: offer → negotiate → approve cycle with cap
  enforcement, per-driver P/L, offer expiry, and an append-only ledger
  (`bot/contracts/{rules,service,render}.py`).
- **Phase 5** — trades (1-for-1 contract swaps with two-party
  approval), release (frozen P/L in ledger), buyout (dead-money row
  that counts against the effective cap), extension (updates the
  existing active contract per CLAUDE.md §2 rule 6), and
  promotion/relegation (moves driver + active contract across tiers).

**For a league-member walkthrough of the whole thing (setup,
offering, trading, releasing, buyouts, promotion/relegation), see
[`docs/MARKET_GUIDE.md`](../docs/MARKET_GUIDE.md).**

Every dollar amount is a `Decimal` end to end (no floats); every business
number the league can tune (salary cap, movement caps, contract term
limits, valuation weights) lives in DB config rows seeded by the F1
preset — never as a Python literal in market/contract code. See
`docs/ADR-001-f1-with-generic-future.md` for the rules and
`scripts/check_magic_numbers.py` for the CI guard that enforces them.

Valuations are **tier-isolated by construction**: the engine takes one
tier's inputs at a time and has no notion of tier structure, so a Tier-2
driver mathematically cannot influence a Tier-1 value.

### `/market-admin` (require Manage Server)

**Setup (Phase 1)**

| Command | Description |
|---|---|
| `/market-admin season create <name> [preset]` | Create a season; `preset: F1 25/26` seeds tiers, lookups, valuation factors, and default league config |
| `/market-admin season activate <name>` | Make a season the active one for this guild |
| `/market-admin season list` | List all seasons |
| `/market-admin tier add <code> <label> <rank> [role] [color]` | Add a tier to the active season |
| `/market-admin tier edit <code> ...` | Edit an existing tier (labels, roles, colors) |
| `/market-admin tier list` | List tiers for the active season |
| `/market-admin config show [tier]` | Show league config (season default or tier override) |
| `/market-admin config edit [tier]` | Modal to edit the five most-tuned numeric values |
| `/market-admin config channel <kind> <#channel> [tier]` | Set market / transactions / approvals channel |
| `/market-admin config role <@role> [tier]` | Set the commissioner role |
| `/market-admin config free-agency <open\|closed> [tier]` | Open or close the free-agency window |

**Valuations (Phase 2)**

| Command | Description |
|---|---|
| `/market-admin valuation run <tier> <round_label>` | Create an *unpublished* dry-run for a tier; shows the preview |
| `/market-admin valuation preview <run_id>` | Re-show a run's preview |
| `/market-admin valuation publish <run_id>` | Flip a dry-run to published — makes it the live market value |
| `/market-admin valuation list [tier]` | 20 most recent runs |

Phase 2 runs use empty factor observations (all zeros) — the plumbing
and audit trail are in place; ingest of real per-race performance data
lands with the /market surfaces in a future phase.

**Boards (Phase 3)**

| Command | Description |
|---|---|
| `/market-admin board add <kind> <#channel> [tier]` | Post a self-updating board (`market`, `movers`, `dashboard`) |
| `/market-admin board remove <board_id>` | Delete a board (also deletes its Discord message) |
| `/market-admin board refresh [board_id]` | Re-render a specific board or all boards |
| `/market-admin board list` | List boards in the active season with health status |

Every board auto-refreshes after `/market-admin valuation publish` for
its tier (plus every cross-tier dashboard) and again every 15 minutes
via the safety poll — same pattern the roster embeds use. Board kinds
in Phase 3 + 4: `market`, `movers`, `dashboard`, `surplus`,
`underwater`.

### `/market` (open to everyone, ephemeral replies)

| Command | Description |
|---|---|
| `/market view <tier> [page]` | Paginated tier market with Prev/Next buttons |
| `/market movers <tier>` | Top risers and fallers from the last published run |
| `/market driver <@member>` | Driver card: current value, week's movement, trend of last N runs |
| `/market team <name>` | Cap sheet: payroll, cap space, per-driver P/L |
| `/market surplus <tier>` | Drivers with the biggest positive P/L (market − contract) |
| `/market underwater <tier>` | Drivers with the biggest negative P/L |
| `/market dashboard` | Cross-tier top-of-tier summary (display only) |

### `/contract` (open to everyone; TP / driver / commissioner scopes)

| Command | Who | Description |
|---|---|---|
| `/contract offer <team> <tier> <driver> <kind> <ttl>` | TP for team | Two-stage flow (slash args → modal → review → submit); ephemeral review shows cap arithmetic + validation checks |
| `/contract offers` | TP | My team's open offers |
| `/contract withdraw <offer_id>` | TP | Cancel an offer I own |
| `/contract accept <offer_id>` | Driver | Move to commissioner queue |
| `/contract decline <offer_id> [note]` | Driver | End negotiation |
| `/contract counter <offer_id>` | Driver | Open a counter modal |
| `/contract status <@driver>` | Anyone | Active contract + open offers + history |

### `/market-admin` contract commands (Manage Server)

| Command | Description |
|---|---|
| `/market-admin approve <offer_id>` | Convert an accepted offer into an active contract (extension offers update the existing contract in place; new-signing offers insert a new row). Creates ledger + assigns team role via the shared `/roster sign` path, posts to transactions channel |
| `/market-admin reject <offer_id> [note]` | Reject an offer in `pending_approval` |
| `/market-admin void <contract_id> [note]` | End an active contract |
| `/market-admin set-status <@driver> <status>` | Change a driver's status (writes a `status_change` ledger entry) |
| `/market-admin adjust-cap <team> <delta_m> <note>` | Log a cap adjustment in the ledger (audit trail only — enforcement is deferred) |
| `/market-admin approve-trade <trade_id>` | Execute an accepted trade — transfers each item's contract to the receiving team, drops/re-adds Discord roles via the shared helper, writes ledger rows for both sides |
| `/market-admin reject-trade <trade_id> [note]` | Reject an accepted trade |
| `/market-admin promote <@driver> <new_tier> [note]` | Move a driver + their active contract to a higher tier |
| `/market-admin relegate <@driver> <new_tier> [note]` | Move a driver + their active contract to a lower tier |

### `/contract` release / buyout (Phase 5)

| Command | Description |
|---|---|
| `/contract release <contract_id> <note>` | End an active contract; ledger captures P/L (market − contract) at release. Blocked if the contract is caught up in an open trade |
| `/contract buyout <contract_id> <buyout_m> <note>` | Release + record a `dead_money` row for the season. Dead money counts against the team's effective cap on the next `/market team` (and in the cap headroom check on new offers) |

### `/trade` (Phase 5)

Team-to-team contract swaps. Phase 5 ships 1-for-1 (one contract from
each side); the schema is multi-item ready.

| Command | Who | Description |
|---|---|---|
| `/trade propose <my_team> <other_team> <my_contract_id> <their_contract_id> <ttl>` | Proposing TP | Creates the trade in `pending_other` state, posts to the approvals channel as a private thread with both TPs added |
| `/trade accept <trade_id>` | Other-team TP | Move to `pending_approval` |
| `/trade decline <trade_id> [note]` | Other-team TP | End the negotiation |
| `/trade withdraw <trade_id>` | Proposing TP | Cancel the trade |
| `/trade status <trade_id>` | Anyone | Full trade embed: items on each side + cap impact for both teams |

### Quick start for a new league

```text
/market-admin season create name: "F1 2026 Season" preset: F1 25/26
/market-admin season activate name: "F1 2026 Season"
/market-admin tier edit code: t1 label: "Tier 1" rank_order: 1 role: @Tier-1
/market-admin tier edit code: t2 label: "Tier 2" rank_order: 2 role: @Tier-2
/market-admin tier edit code: t3 label: "Tier 3" rank_order: 3 role: @Tier-3
/market-admin config show
/market-admin config edit               # tune the numeric defaults

# First valuation snapshot per tier (baselines every driver at min_salary)
/market-admin valuation run tier: t1 round_label: "Pre-season baseline"
/market-admin valuation publish run_id: <printed above>

# Live market boards in a public channel
/market-admin board add kind: Market table channel: #market tier: t1
/market-admin board add kind: Movers channel: #market tier: t1
/market-admin board add kind: Cross-tier dashboard channel: #market
```

## Team setup flow

`/roster create` walks through 7 steps:

1. **Name / tagline / logo URL** — display info for the embed header
2. **Team role** — the Discord role that marks someone as on this team
3. **Principal role** *(optional)* — a role whose members can sign/drop players for this team without needing Manage Server
4. **Staff slots** — add as many labelled staff slots as needed (each has a label, quantity, and role)
5. **Driver slots** — same as staff
6. **Channel** — the text channel where the roster embed is posted
7. **Confirm** — preview the embed and post it

`/roster edit <name>` reopens the same flow pre-filled with existing values.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Bot token |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `DATABASE_URL` | `postgresql://roster:roster@postgres:5432/roster` (set by compose) | Postgres connection string |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `roster` | Postgres credentials (compose only) |

## Architecture notes

- **No members table** — source of truth is Discord. Member lists are queried live from `guild.members` at render time (requires Members Intent and member caching).
- **Render is idempotent** — `build_embed` edits the stored message in place. If the message was deleted, `message_id` is cleared and `/roster list` flags the team as broken.
- **Two update triggers**: `on_member_update` (event-driven, immediate) + 15-min poll (safety net for missed events).
- **In-flight flow state** lives in memory, keyed by `(guild_id, user_id)`. A bot restart abandons any in-progress create/edit flows.
- **Postgres 16** via asyncpg with a connection pool; migrations run automatically on startup from the `migrations/` directory. Historical SQLite migration files (001–012) are archived under `migrations/sqlite/` for reference; the live PG schema is the single baseline `001_init.sql`.
