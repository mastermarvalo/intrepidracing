-- Phase 1 foundations: seasons + tiers, and optional season/tier attachment
-- for existing teams.
--
-- Per ADR-001 rule 4, all new race/market/contract data is scoped to
-- season_id from day 1. Existing team rows keep working with
-- season_id/tier_id IS NULL, which the resolution helper treats as
-- "belongs to the active season."
--
-- The db._run_migrations runner wraps this file in a transaction; do not
-- add BEGIN/COMMIT here. Forward-only.

CREATE TABLE IF NOT EXISTS seasons (
    id         BIGSERIAL PRIMARY KEY,
    guild_id   BIGINT      NOT NULL,
    name       TEXT        NOT NULL,
    is_active  BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (guild_id, name)
);

-- At most one active season per guild. Enforced by a partial unique index so
-- inactive seasons can freely coexist.
CREATE UNIQUE INDEX IF NOT EXISTS uq_seasons_one_active_per_guild
    ON seasons (guild_id)
    WHERE is_active;

CREATE TABLE IF NOT EXISTS tiers (
    id           BIGSERIAL PRIMARY KEY,
    season_id    BIGINT  NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    code         TEXT    NOT NULL,
    label        TEXT    NOT NULL,
    tier_role_id BIGINT,
    rank_order   INTEGER NOT NULL,
    accent_color INTEGER,
    UNIQUE (season_id, code)
);

CREATE INDEX IF NOT EXISTS idx_tiers_season ON tiers (season_id);

-- Optional attachment for existing team rows. NULL means "active season" for
-- backwards-compatible reads; explicit values kick in as leagues create
-- multiple concurrent seasons.
ALTER TABLE teams ADD COLUMN IF NOT EXISTS season_id BIGINT REFERENCES seasons(id);
ALTER TABLE teams ADD COLUMN IF NOT EXISTS tier_id   BIGINT REFERENCES tiers(id);
