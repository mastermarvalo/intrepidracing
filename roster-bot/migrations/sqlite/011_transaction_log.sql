CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    team_id     INTEGER NOT NULL,
    member_id   INTEGER NOT NULL,
    member_name TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_transactions_guild_team
    ON transactions (guild_id, team_id);
