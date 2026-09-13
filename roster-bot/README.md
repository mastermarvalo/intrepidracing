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

## Market & Contracts (Phase 1)

The market/contract system lives alongside `/roster` and is being built in
phases. Phase 1 lays down the schema, the F1 preset, and the commissioner
setup surface — `/market` and `/contract` (Phase 3–4) are not shipped yet.

Every dollar amount is a `Decimal` end to end (no floats); every business
number the league can tune (salary cap, movement caps, contract term
limits, valuation weights) lives in DB config rows seeded by the F1
preset — never as a Python literal in market/contract code. See
`docs/ADR-001-f1-with-generic-future.md` for the rules and
`scripts/check_magic_numbers.py` for the CI guard that enforces them.

### `/market-admin` (require Manage Server)

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

### Quick start for a new league

```text
/market-admin season create name: "F1 2026 Season" preset: F1 25/26
/market-admin season activate name: "F1 2026 Season"
/market-admin tier edit code: t1 label: "Tier 1" rank_order: 1 role: @Tier-1
/market-admin tier edit code: t2 label: "Tier 2" rank_order: 2 role: @Tier-2
/market-admin tier edit code: t3 label: "Tier 3" rank_order: 3 role: @Tier-3
/market-admin config show
/market-admin config edit               # tune the numeric defaults
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
