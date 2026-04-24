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

The database lives in a named Docker volume (`roster-data`) and persists across restarts and rebuilds.

**First-time setup only** — if you have an existing `roster.db` to migrate:

```sh
sudo docker compose create
sudo docker compose cp roster.db roster-bot:/data/roster.db
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
| `DB_PATH` | `./roster.db` | Path to SQLite database file |

## Architecture notes

- **No members table** — source of truth is Discord. Member lists are queried live from `guild.members` at render time (requires Members Intent and member caching).
- **Render is idempotent** — `build_embed` edits the stored message in place. If the message was deleted, `message_id` is cleared and `/roster list` flags the team as broken.
- **Two update triggers**: `on_member_update` (event-driven, immediate) + 15-min poll (safety net for missed events).
- **In-flight flow state** lives in memory, keyed by `(guild_id, user_id)`. A bot restart abandons any in-progress create/edit flows.
- **SQLite** via aiosqlite; migrations run automatically on startup from the `migrations/` directory.
