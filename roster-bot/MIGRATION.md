# Upgrade & migration notes

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
