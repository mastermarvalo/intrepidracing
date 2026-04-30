-- Postgres baseline schema for roster-bot.
--
-- Captures the final state of the SQLite migrations 001..012 (now archived
-- under migrations/sqlite/). Discord snowflake IDs use BIGINT (64-bit);
-- internal surrogate keys use BIGSERIAL.

CREATE TABLE teams (
    id                BIGSERIAL PRIMARY KEY,
    guild_id          BIGINT      NOT NULL,
    key               TEXT        NOT NULL,
    name              TEXT        NOT NULL,
    team_role_id      BIGINT      NOT NULL,
    tagline           TEXT,
    logo_url          TEXT,
    banner_url        TEXT,
    channel_id        BIGINT      NOT NULL,
    message_id        BIGINT,
    principal_role_id BIGINT,
    color             INTEGER,
    info_label        TEXT,
    info_body         TEXT,
    dark_mode         BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (guild_id, key)
);

CREATE TABLE team_slots (
    id           BIGSERIAL PRIMARY KEY,
    team_id      BIGINT  NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    slot_role_id BIGINT  NOT NULL,
    label        TEXT    NOT NULL,
    quantity     INTEGER NOT NULL,
    slot_type    TEXT    NOT NULL CHECK (slot_type IN ('staff', 'driver')),
    sort_order   INTEGER NOT NULL
);

CREATE TABLE guild_config (
    guild_id                BIGINT PRIMARY KEY,
    free_agent_role_id      BIGINT,
    fa_channel_id           BIGINT,
    fa_message_id           BIGINT,
    transactions_channel_id BIGINT
);

CREATE TABLE stat_boards (
    id              BIGSERIAL PRIMARY KEY,
    guild_id        BIGINT      NOT NULL,
    title           TEXT        NOT NULL,
    sheet_id        TEXT        NOT NULL,
    sheet_range     TEXT        NOT NULL DEFAULT 'Sheet1',
    channel_id      BIGINT      NOT NULL,
    message_id      BIGINT,
    forum_thread_id BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE transactions (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT      NOT NULL,
    team_id     BIGINT      NOT NULL,
    member_id   BIGINT      NOT NULL,
    member_name TEXT        NOT NULL,
    action      TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_transactions_guild_team
    ON transactions (guild_id, team_id);
