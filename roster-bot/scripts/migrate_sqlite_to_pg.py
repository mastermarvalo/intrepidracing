"""
One-shot data migration: copy every roster-bot row from a SQLite snapshot
into Postgres. Preserves primary keys so existing message_id references
(stored elsewhere as foreign concepts) and new transactions inserted before
old ones still line up.

Usage:
    DATABASE_URL=postgresql://user:pass@host/db \
    python scripts/migrate_sqlite_to_pg.py /path/to/roster.db [--force]

Pre-conditions:
- The Postgres database has the schema applied (run the bot once with the new
  code to apply migrations, OR psql -f migrations/001_init.sql).
- Postgres tables are empty. Pass --force to overwrite (TRUNCATE first).

The script:
1. Verifies PG is empty (or --force).
2. Copies in dependency order: guild_config → teams → team_slots → stat_boards
   → transactions.
3. Resets each BIGSERIAL sequence to MAX(id)+1 so the next INSERT picks up
   where SQLite left off.
4. Prints row counts as a sanity check.

`dark_mode` is converted from SQLite INTEGER (0/1) to PG BOOLEAN.
`created_at` columns are passed through as-is — asyncpg will parse the SQLite
ISO-8601 strings into TIMESTAMPTZ.
"""

import argparse
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timezone

import asyncpg

TABLES = ["guild_config", "teams", "team_slots", "stat_boards", "transactions"]
SEQUENCES = {
    "teams": "teams_id_seq",
    "team_slots": "team_slots_id_seq",
    "stat_boards": "stat_boards_id_seq",
    "transactions": "transactions_id_seq",
}


def _to_dt(v):
    """SQLite stores TIMESTAMP/DATETIME/TEXT as strings. Coerce to aware UTC datetime."""
    if v is None or isinstance(v, datetime):
        return v.replace(tzinfo=timezone.utc) if isinstance(v, datetime) and v.tzinfo is None else v
    s = str(v)
    # SQLite default produces "YYYY-MM-DD HH:MM:SS"; fromisoformat handles that.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        # Fall back: best-effort, treat as UTC
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite_path")
    parser.add_argument("--force", action="store_true",
                        help="TRUNCATE existing PG rows before copying")
    args = parser.parse_args()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(args.sqlite_path):
        print(f"sqlite file not found: {args.sqlite_path}", file=sys.stderr)
        sys.exit(1)

    src = sqlite3.connect(args.sqlite_path)
    src.row_factory = sqlite3.Row

    pg = await asyncpg.connect(dsn)
    try:
        # Schema must already exist
        existing = await pg.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "AND tablename = ANY($1::text[])",
            TABLES,
        )
        if {r["tablename"] for r in existing} != set(TABLES):
            print("Postgres schema not initialised. Apply migrations first "
                  "(start the bot once, or psql -f migrations/001_init.sql).",
                  file=sys.stderr)
            sys.exit(2)

        # Empty check (or --force truncate)
        for t in TABLES:
            n = await pg.fetchval(f"SELECT COUNT(*) FROM {t}")
            if n > 0 and not args.force:
                print(f"Postgres table {t} has {n} rows. Refusing without --force.",
                      file=sys.stderr)
                sys.exit(3)

        if args.force:
            print("Truncating PG tables…")
            await pg.execute(
                "TRUNCATE guild_config, transactions, stat_boards, team_slots, teams "
                "RESTART IDENTITY CASCADE"
            )

        async with pg.transaction():
            # ── guild_config ─────────────────────────────────────────────────
            rows = src.execute("SELECT * FROM guild_config").fetchall()
            print(f"guild_config: {len(rows)} rows")
            for r in rows:
                await pg.execute(
                    """
                    INSERT INTO guild_config
                        (guild_id, free_agent_role_id, fa_channel_id,
                         fa_message_id, transactions_channel_id)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    r["guild_id"], r["free_agent_role_id"], r["fa_channel_id"],
                    r["fa_message_id"], r["transactions_channel_id"],
                )

            # ── teams ────────────────────────────────────────────────────────
            rows = src.execute("SELECT * FROM teams").fetchall()
            print(f"teams: {len(rows)} rows")
            for r in rows:
                await pg.execute(
                    """
                    INSERT INTO teams
                        (id, guild_id, key, name, team_role_id, tagline, logo_url,
                         banner_url, channel_id, message_id, principal_role_id,
                         color, info_label, info_body, dark_mode, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                            $13, $14, $15, $16)
                    """,
                    r["id"], r["guild_id"], r["key"], r["name"], r["team_role_id"],
                    r["tagline"], r["logo_url"], r["banner_url"], r["channel_id"],
                    r["message_id"], r["principal_role_id"], r["color"],
                    r["info_label"], r["info_body"], bool(r["dark_mode"]),
                    _to_dt(r["created_at"]),
                )

            # ── team_slots ───────────────────────────────────────────────────
            rows = src.execute("SELECT * FROM team_slots").fetchall()
            print(f"team_slots: {len(rows)} rows")
            for r in rows:
                await pg.execute(
                    """
                    INSERT INTO team_slots
                        (id, team_id, slot_role_id, label, quantity, slot_type, sort_order)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    r["id"], r["team_id"], r["slot_role_id"], r["label"],
                    r["quantity"], r["slot_type"], r["sort_order"],
                )

            # ── stat_boards ──────────────────────────────────────────────────
            rows = src.execute("SELECT * FROM stat_boards").fetchall()
            print(f"stat_boards: {len(rows)} rows")
            for r in rows:
                # forum_thread_id was added in migration 012; missing on older rows
                forum_thread_id = (
                    r["forum_thread_id"] if "forum_thread_id" in r.keys() else None
                )
                await pg.execute(
                    """
                    INSERT INTO stat_boards
                        (id, guild_id, title, sheet_id, sheet_range, channel_id,
                         message_id, forum_thread_id, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    r["id"], r["guild_id"], r["title"], r["sheet_id"],
                    r["sheet_range"], r["channel_id"], r["message_id"],
                    forum_thread_id, _to_dt(r["created_at"]),
                )

            # ── transactions ─────────────────────────────────────────────────
            rows = src.execute("SELECT * FROM transactions").fetchall()
            print(f"transactions: {len(rows)} rows")
            for r in rows:
                await pg.execute(
                    """
                    INSERT INTO transactions
                        (id, guild_id, team_id, member_id, member_name, action, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    r["id"], r["guild_id"], r["team_id"], r["member_id"],
                    r["member_name"], r["action"], _to_dt(r["created_at"]),
                )

            # ── reset sequences past max(id) ─────────────────────────────────
            for table, seq in SEQUENCES.items():
                next_val = await pg.fetchval(
                    f"SELECT setval('{seq}', "
                    f"(SELECT COALESCE(MAX(id), 0) FROM {table}) + 1, false)"
                )
                print(f"sequence {seq} -> {next_val}")

        # ── post-migration sanity ────────────────────────────────────────────
        print("\nPost-migration row counts:")
        for t in TABLES:
            n = await pg.fetchval(f"SELECT COUNT(*) FROM {t}")
            print(f"  {t:14} {n}")

    finally:
        await pg.close()
        src.close()


if __name__ == "__main__":
    asyncio.run(main())
