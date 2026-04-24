CREATE TABLE IF NOT EXISTS teams (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    key          TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    team_role_id INTEGER NOT NULL,
    tagline      TEXT,
    logo_url     TEXT,
    channel_id   INTEGER NOT NULL,
    message_id   INTEGER,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(guild_id, key)
);

CREATE TABLE IF NOT EXISTS team_slots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id      INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    slot_role_id INTEGER NOT NULL,
    label        TEXT    NOT NULL,
    quantity     INTEGER NOT NULL,
    slot_type    TEXT    NOT NULL CHECK(slot_type IN ('staff', 'driver')),
    sort_order   INTEGER NOT NULL
);
