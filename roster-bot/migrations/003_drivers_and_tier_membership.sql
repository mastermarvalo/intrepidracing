-- Phase 1 foundations: drivers table + driver_statuses lookup.
--
-- Deliberate, scoped exception to the "no members table" principle: this
-- stores market and contract state anchored to a Discord member id, not
-- roster membership. Discord roles remain the source of truth for who is
-- on a team and in a tier. A member can legitimately have one drivers row
-- per (season, tier) — Tier 1 reserve who also races Tier 2 is two
-- independent driver rows with independent market values. Never merge.
--
-- No BEGIN/COMMIT — the runner wraps this file in a transaction.

CREATE TABLE IF NOT EXISTS driver_statuses (
    code  TEXT PRIMARY KEY,
    label TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drivers (
    id           BIGSERIAL PRIMARY KEY,
    season_id    BIGINT      NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    tier_id      BIGINT      NOT NULL REFERENCES tiers(id)   ON DELETE CASCADE,
    member_id    BIGINT      NOT NULL,
    display_name TEXT        NOT NULL,
    status       TEXT        NOT NULL REFERENCES driver_statuses(code),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (season_id, tier_id, member_id)
);

CREATE INDEX IF NOT EXISTS idx_drivers_season_tier ON drivers (season_id, tier_id);
CREATE INDEX IF NOT EXISTS idx_drivers_member      ON drivers (member_id);
