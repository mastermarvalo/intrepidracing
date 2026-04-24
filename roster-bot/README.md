# Roster Bot

Discord bot that maintains a live, self-updating team roster embed. Admins create teams via a guided `/roster create` flow; the bot keeps the roster message in sync by watching role changes and polling every 15 minutes.

## Requirements

- Python 3.11+
- A Discord bot token with **Server Members Intent** enabled in the developer portal
- Podman (for n3rvnas deployment) or any OCI-compatible runtime

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
   - Manage Messages (to delete roster messages on `/roster remove`)

## Podman deploy (n3rvnas)

Build the image:

```sh
podman build -t roster-bot -f Containerfile .
```

Create a persistent volume for the DB:

```sh
podman volume create roster-bot-data
```

Run:

```sh
podman run -d \
  --name roster-bot \
  --restart=always \
  -e DISCORD_TOKEN=your-token-here \
  -v roster-bot-data:/data:Z \
  roster-bot
```

View logs:

```sh
podman logs -f roster-bot
```

To update, rebuild the image and restart the container:

```sh
podman build -t roster-bot -f Containerfile .
podman stop roster-bot && podman rm roster-bot
# re-run the `podman run` command above
```

The DB file is in the `roster-bot-data` volume and persists across restarts.

## Commands

All commands require **Manage Server** permission.

| Command | Description |
|---|---|
| `/roster create <name>` | Open the setup flow for a new team |
| `/roster edit <name>` | Re-open the setup flow, pre-filled with current values |
| `/roster list` | List all teams configured in this server |
| `/roster remove <name>` | Delete a team and its roster message |

## Configuration

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Bot token |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `DB_PATH` | `./roster.db` | Path to SQLite database file |

## Architecture notes

- **No members table** — source of truth is Discord. Member lists are queried live from `guild.members` at render time (requires `members` intent and member caching).
- **Render is idempotent** — `build_embed` + edit stored message. If the message was deleted, `message_id` is cleared; `/roster list` shows the team as broken.
- **Two update triggers**: `on_member_update` (event-driven) + 15-min poll (safety net).
- **In-flight create/edit state** lives in memory, keyed by `(guild_id, user_id)`. A bot restart abandons any in-progress flows.
