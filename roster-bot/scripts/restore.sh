#!/usr/bin/env bash
# Run on the cloud VM after extracting the tarball, from the roster-bot/ root.
# Brings up Postgres, restores db/dump.sql into it, then starts the bot.
set -euo pipefail

cd "$(dirname "$0")/.."

DUMP="db/dump.sql"
[[ -f "$DUMP" ]] || { echo "ERROR: $DUMP not found (run scripts/package.sh on the source host first)" >&2; exit 1; }
[[ -f .env  ]] || { echo "ERROR: .env not found"  >&2; exit 1; }

# Source POSTGRES_USER / POSTGRES_DB from .env if present, else use compose defaults.
set -a; . ./.env; set +a
POSTGRES_USER="${POSTGRES_USER:-roster}"
POSTGRES_DB="${POSTGRES_DB:-roster}"

# Refuse to clobber an existing populated DB unless caller opts in.
if sudo docker compose ps --status=running --services 2>/dev/null | grep -qx 'postgres'; then
  TABLE_COUNT=$(sudo docker compose exec -T postgres \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
      "select count(*) from information_schema.tables where table_schema='public'" \
    2>/dev/null || echo 0)
  if (( TABLE_COUNT > 0 )) && [[ "${FORCE:-0}" != "1" ]]; then
    echo "ERROR: target database already has ${TABLE_COUNT} table(s)." >&2
    echo "       Re-run with FORCE=1 to overwrite (the dump uses DROP IF EXISTS)." >&2
    exit 1
  fi
fi

echo ">> bringing up postgres"
sudo docker compose up -d postgres

echo ">> waiting for postgres to accept connections"
for i in $(seq 1 60); do
  if sudo docker compose exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 1
  if (( i == 60 )); then
    echo "ERROR: postgres did not become ready within 60s" >&2
    exit 1
  fi
done

echo ">> restoring db/dump.sql"
sudo docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$DUMP"

echo ">> building + starting the bot"
sudo docker compose up -d --build roster-bot

echo
echo "Done. Tail logs:  make logs"
