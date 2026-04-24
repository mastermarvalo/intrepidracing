# Discord Roster Bot — Build Spec

## What this is
A Discord bot that maintains a live, self-updating team roster message in a channel. Admin runs one command to create a team, fills out a form, bot posts the roster. After that it maintains itself by watching Discord role changes.

Design comes from a mockup for an F1 sim racing league.

## Commands

All under `/roster`, all require Manage Server permission.

- `/roster create <name>` — opens setup form for a new team. Name is the identifier (e.g. `redbull`). Admin picks target channel as part of the flow; roster posts there on submit.
- `/roster edit <name>` — opens the same form, pre-filled with current values.
- `/roster list` — lists all teams configured in this server.
- `/roster remove <name>` — deletes team config and its roster message. Confirmation required.

That's it. Four commands.

## The setup form

One form, used by both create and edit. Admin fills in:

- **Team name** — display name ("Red Bull Racing")
- **Team role** — the primary role that marks someone as on this team (@Red Bull)
- **Tagline** — optional free text under team name ("6x WCC | 3x WDC | 1x ICC")
- **Logo URL** — optional
- **Staff slots** — list of (role, label, quantity) for staff positions
- **Driver slots** — list of (role, label, quantity) for driver tiers

### How the form actually works in Discord

Discord modals max at 5 text inputs and don't allow role selects inside them, so the form is a short guided flow, not a single screen. On `/roster create redbull`:

1. **Modal** — team name, tagline, logo URL (3 text inputs)
2. **Role select** — pick the team role
3. **Staff slots builder** — ephemeral message with "Add staff slot" button. Each click opens a modal for label + quantity, plus a role select. Admin adds as many as they want, clicks "Done".
4. **Driver slots builder** — same pattern.
5. **Channel select** — pick the channel to post the roster in.
6. **Confirm** — preview the roster, admin clicks "Post" or "Cancel".

On `/roster edit redbull`, same flow but every field is pre-filled and the admin can skip straight to confirm if nothing needs changing.

The whole flow uses ephemeral messages so it doesn't clutter the channel. Only the final roster message is public.

## Scope

**In:**
- Multiple teams per server
- Event-driven updates (role add/remove) + 15-minute safety poll
- Soft overflow: show everyone assigned to a slot's role, flag extras past configured quantity
- SQLite persistence, single file

**Out:**
- Web dashboard
- Stats/achievements
- Any non-admin user commands
- Cross-server teams

## Stack

- Python 3.11+, discord.py 2.x
- `aiosqlite` for DB
- `.env` for bot token, `roster.db` for everything else
- Local for dev, Podman container for n3rvnas deployment
- `ruff` for lint/format

## Data model

```
teams
  id           INTEGER PK
  guild_id     INTEGER
  key          TEXT         -- command identifier ("redbull")
  name         TEXT         -- display name ("Red Bull Racing")
  team_role_id INTEGER
  tagline      TEXT NULL
  logo_url     TEXT NULL
  channel_id   INTEGER
  message_id   INTEGER NULL
  created_at   TIMESTAMP
  UNIQUE(guild_id, key)

team_slots
  id           INTEGER PK
  team_id      INTEGER FK
  slot_role_id INTEGER
  label        TEXT
  quantity     INTEGER
  slot_type    TEXT         -- 'staff' or 'driver'
  sort_order   INTEGER
```

No members table — source of truth is Discord. Query `guild.members` filtered by role at render time.

## Update logic

Two triggers, same render path:

1. **`on_member_update`** — if roles changed, find teams in that guild whose `team_role_id` or any `slot_role_id` is in the old-or-new role set. Re-render those.
2. **Poll** — every 15 min, re-render every team. Catches bot-offline gaps and event drops.

Render is idempotent: read DB config → query members → build embed → edit the stored message. If the message was deleted, log a warning and null out `message_id`; next `/roster list` shows it as broken.

## Render algorithm

For a team:
1. Find all members with `team_role_id` — the team pool.
2. For each staff slot in sort_order: filter pool by `slot_role_id`, display under label. If count > quantity, show all with an overflow indicator.
3. Same for driver slots, under a "Drivers" header.
4. Empty slots show *Spot Open* in italics, repeated to fill quantity.
5. Build Discord embed: name as title, tagline as description, logo as thumbnail, staff + drivers as fields.

Members render as `<@userid>` mentions — Discord shows those as colored pills, matching the mockup.

## Project structure

```
roster-bot/
  bot/
    __init__.py
    main.py              # entry, load cogs, start poll loop
    db.py                # aiosqlite helper, migrations
    models.py            # Team, TeamSlot dataclasses
    render.py            # build_embed(team, guild) -> discord.Embed
    cogs/
      roster.py          # all four /roster commands
    flow.py              # the create/edit guided flow (modals, selects, builders)
    events.py            # on_member_update
    poll.py              # 15-min refresh loop
  migrations/
    001_init.sql
  tests/
    test_render.py       # render with mocked members
    test_overflow.py
  .env.example
  Containerfile
  pyproject.toml
  README.md
```

## Build order (one commit per step)

1. Scaffold, pyproject.toml, .env.example, main.py that connects and logs "ready"
2. DB layer: migrations, models, connection helper
3. Render module + tests (mocked Members and Roles, no Discord needed)
4. `/roster list` and `/roster remove` — the easy commands first
5. `/roster create` — the full guided flow. Do this in one focused session since the flow is the hard part.
6. `/roster edit` — reuses the flow with pre-filled values
7. `on_member_update` event handler
8. 15-min poll loop
9. Containerfile + README with Podman deploy notes

## Conventions I care about

- **Don't guess APIs.** If you're unsure about a discord.py 2.x signature or behavior, say so — we'll check the docs. I've been burned by AI hallucinating library calls.
- **Explain as you go.** I want to understand each piece, not just get working code. When you introduce a pattern (app_commands groups, cogs, View/Modal classes, aiosqlite context managers), a couple sentences on why it works that way.
- **Small commits.** Each build-order step is a separate commit with a clear message.
- **Tests for render.** It's the most bug-prone part. Mock `discord.Member` and `discord.Role` and unit-test the embed builder thoroughly.
- **Type hints everywhere.**

## Visual target

Roster embed should look roughly like the mockup:

> **🐂 Red Bull Racing**
> *6x WCC | 3x WDC | 1x ICC*
>
> **Staff**
> Team Principal
> @Davis
> Sporting Director
> *Spot Open*
>
> **Drivers**
> Tier 1 Drivers
> @Scorenzy
> @Zezin
> Tier 2 Drivers
> @Mersaki
> @Scen
> *(etc)*

Logo goes in the thumbnail slot.

## Environment

- `.env`: `DISCORD_TOKEN`, optional `LOG_LEVEL`
- DB file path configurable, defaults to `./roster.db`
- Required intents: `guilds`, `guild_members` (privileged — must enable in dev portal)
- `message_content` NOT needed

## Start here

Read this whole file. Ask me questions before writing code. Then start with step 1.
