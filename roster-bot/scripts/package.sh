#!/usr/bin/env bash
# Package the bot for transfer to a cloud VM.
# Produces a single tarball with source + compose files + .env + a fresh
# logical pg_dump of the live database.
#
# Output: /tmp/roster-bot-<UTC-timestamp>.tar.gz  (override with OUT=...)
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)

STAMP=$(date -u +%Y%m%d-%H%M%SZ)
OUT="${OUT:-/tmp/roster-bot-${STAMP}.tar.gz}"
STAGING=$(mktemp -d -t roster-bot-pkg-XXXXXX)
trap 'rm -rf "$STAGING"' EXIT

DEST="${STAGING}/roster-bot"
mkdir -p "${DEST}/db"

# Source POSTGRES_USER / POSTGRES_DB from .env if present, else use compose defaults.
if [[ -f .env ]]; then
  set -a; . ./.env; set +a
fi
POSTGRES_USER="${POSTGRES_USER:-roster}"
POSTGRES_DB="${POSTGRES_DB:-roster}"

echo ">> verifying postgres compose service is running"
if ! sudo docker compose ps --status=running --services 2>/dev/null | grep -qx 'postgres'; then
  echo "ERROR: postgres compose service is not running." >&2
  echo "       Run 'make up' first so we can pg_dump the live database." >&2
  exit 1
fi

echo ">> pg_dump -> db/dump.sql"
sudo docker compose exec -T postgres \
  pg_dump --clean --if-exists --no-owner --no-privileges \
    -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  > "${DEST}/db/dump.sql"

DUMP_BYTES=$(wc -c < "${DEST}/db/dump.sql")
if (( DUMP_BYTES < 200 )); then
  echo "ERROR: dump looks empty (${DUMP_BYTES} bytes). Aborting." >&2
  exit 1
fi
echo "   dump size: ${DUMP_BYTES} bytes"

echo ">> copying project source"
# Mirrors .gitignore + .dockerignore, plus drops a few stale top-level files
# (the old SQLite db, an unused image, a leftover font zip, the SQLite-only
# migration helper, and the archived sqlite migrations).
rsync -a \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' --exclude='*.pyo' \
  --exclude='.pytest_cache/' \
  --exclude='.ruff_cache/' \
  --exclude='roster_bot.egg-info/' \
  --exclude='roster.db' \
  --exclude='IMG_4979.png' \
  --exclude='Formula1-Display-Bold-Bold.zip' \
  --exclude='scripts/migrate_sqlite_to_pg.py' \
  --exclude='migrations/sqlite/' \
  --exclude='db/' \
  ./ "${DEST}/"

echo ">> writing ${OUT}"
tar -C "${STAGING}" -czf "${OUT}" roster-bot

echo
echo "Tarball:  ${OUT}"
ls -lh "${OUT}"
echo
echo "Contents (top-level):"
tar -tzf "${OUT}" | awk -F/ '{print $1"/"$2}' | sort -u
