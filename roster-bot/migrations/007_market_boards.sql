-- Phase 3: self-updating market boards, mirroring the stat_boards
-- pattern.
--
-- A market_board is a Discord message the bot maintains in place: on
-- valuation publish (and via a periodic drift-recovery poll) the bot
-- re-renders the embed and edits the stored message_id. Deleting the
-- message clears message_id and the /market-admin board list surfaces
-- the row as broken so the commissioner can re-post.
--
-- `kind` FKs to board_kinds (seeded by the F1 preset). tier_id is
-- nullable so cross-tier boards (dashboard) can live in the same
-- table. `page` supports paginated boards that live as a specific
-- page of a paginated view — for a Phase-3 dashboard on page 0 there
-- is no interactivity to worry about, but the column is there so
-- Phase 3+ can wire pagination into a persistent board without a
-- migration.
--
-- No BEGIN/COMMIT — the runner wraps each file in a transaction.

CREATE TABLE IF NOT EXISTS market_boards (
    id              BIGSERIAL PRIMARY KEY,
    season_id       BIGINT      NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    tier_id         BIGINT      REFERENCES tiers(id) ON DELETE CASCADE,
    kind            TEXT        NOT NULL REFERENCES board_kinds(code),
    channel_id      BIGINT      NOT NULL,
    message_id      BIGINT,
    page            INTEGER     NOT NULL DEFAULT 0,
    forum_thread_id BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_market_boards_season_tier
    ON market_boards (season_id, tier_id);

CREATE INDEX IF NOT EXISTS idx_market_boards_channel
    ON market_boards (channel_id);
