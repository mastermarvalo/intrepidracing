-- Phase 2: valuation runs + per-driver valuations.
--
-- A valuation_run is one execution of bot/market/valuation.py for a
-- (season, tier). It is created as unpublished (dry-run) first and
-- flipped to published only after the commissioner has reviewed the
-- diff, so every published value has been eyeballed before it becomes
-- the market-visible number. Dry runs stay in the table for audit
-- (never delete history — see CLAUDE.md §2 rule 4).
--
-- driver_valuations captures every driver's snapshot for that run,
-- including the per-factor breakdown JSON so any published value is
-- fully explainable months later. The `capped` flag records that a raw
-- value was clipped by the weekly (or exceptional) movement cap; the
-- render side surfaces that to the commissioner as a signal to review.
--
-- Money is NUMERIC(12,2) throughout; the Python side is Decimal. Never
-- floats.
--
-- No BEGIN/COMMIT — the runner wraps each file in a transaction.

CREATE TABLE IF NOT EXISTS valuation_runs (
    id          BIGSERIAL PRIMARY KEY,
    season_id   BIGINT      NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    tier_id     BIGINT      NOT NULL REFERENCES tiers(id)   ON DELETE CASCADE,
    round_label TEXT        NOT NULL,
    created_by  BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published   BOOLEAN     NOT NULL DEFAULT FALSE,
    published_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_valuation_runs_season_tier
    ON valuation_runs (season_id, tier_id);

CREATE INDEX IF NOT EXISTS idx_valuation_runs_published
    ON valuation_runs (tier_id, published, created_at DESC);

CREATE TABLE IF NOT EXISTS driver_valuations (
    id             BIGSERIAL PRIMARY KEY,
    run_id         BIGINT        NOT NULL REFERENCES valuation_runs(id) ON DELETE CASCADE,
    driver_id      BIGINT        NOT NULL REFERENCES drivers(id)        ON DELETE CASCADE,
    market_value   NUMERIC(12,2) NOT NULL,
    previous_value NUMERIC(12,2),
    delta          NUMERIC(12,2) NOT NULL,
    rank_in_tier   INTEGER       NOT NULL,
    capped         BOOLEAN       NOT NULL DEFAULT FALSE,
    breakdown      JSONB         NOT NULL,
    UNIQUE (run_id, driver_id)
);

CREATE INDEX IF NOT EXISTS idx_driver_valuations_driver
    ON driver_valuations (driver_id);
