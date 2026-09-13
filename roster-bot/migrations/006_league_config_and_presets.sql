-- Phase 1 foundations: league config table + all the seeded lookup tables
-- the market/contracts feature will reference across later phases.
--
-- Per ADR-001 rule 2, domain entity types (contract types, offer states,
-- transaction kinds, board kinds) are rows in tables, not Python enums.
-- The F1 preset seeds standard rows; other leagues would seed different
-- values against the same schema.
--
-- driver_statuses lives in migration 003 because the drivers table has a
-- foreign key to it.
--
-- No BEGIN/COMMIT — the runner wraps this file in a transaction.

-- ── Global lookups (shared across seasons; codes are stable) ─────────────

CREATE TABLE IF NOT EXISTS contract_types (
    code        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS contract_states (
    code        TEXT    PRIMARY KEY,
    label       TEXT    NOT NULL,
    is_terminal BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS offer_states (
    code        TEXT    PRIMARY KEY,
    label       TEXT    NOT NULL,
    is_terminal BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS transaction_kinds (
    code  TEXT PRIMARY KEY,
    label TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS board_kinds (
    code  TEXT PRIMARY KEY,
    label TEXT NOT NULL
);

-- ── Season-scoped valuation factor weights ──────────────────────────────
--
-- The valuation engine iterates these rows; adding or reweighting a factor
-- is a data change, not a code change. Weights are dimensionless
-- multipliers; max_contribution caps a single factor's per-run swing.

CREATE TABLE IF NOT EXISTS valuation_factors (
    id               BIGSERIAL PRIMARY KEY,
    season_id        BIGINT        NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    code             TEXT          NOT NULL,
    label            TEXT          NOT NULL,
    weight           NUMERIC(12,4) NOT NULL,
    max_contribution NUMERIC(12,2),
    sort_order       INTEGER       NOT NULL DEFAULT 0,
    UNIQUE (season_id, code)
);

CREATE INDEX IF NOT EXISTS idx_valuation_factors_season
    ON valuation_factors (season_id);

-- ── League configuration ────────────────────────────────────────────────
--
-- A row with tier_id IS NULL is the season default. A row with an explicit
-- tier_id overrides the default for that tier. Resolution lives in
-- bot/market/config.py (added later).

CREATE TABLE IF NOT EXISTS league_config (
    id                      BIGSERIAL PRIMARY KEY,
    season_id               BIGINT        NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    tier_id                 BIGINT        REFERENCES tiers(id) ON DELETE CASCADE,
    salary_cap              NUMERIC(12,2) NOT NULL,
    min_salary              NUMERIC(12,2) NOT NULL,
    max_salary              NUMERIC(12,2),
    active_driver_slots     INTEGER       NOT NULL,
    weekly_move_cap         NUMERIC(12,2) NOT NULL,
    exceptional_move_cap    NUMERIC(12,2) NOT NULL,
    max_term_seasons        INTEGER       NOT NULL,
    max_incentive_pct       NUMERIC(6,3)  NOT NULL,
    offer_ttl_hours         INTEGER       NOT NULL,
    free_agency_open        BOOLEAN       NOT NULL DEFAULT FALSE,
    market_channel_id       BIGINT,
    transactions_channel_id BIGINT,
    approvals_channel_id    BIGINT,
    commissioner_role_id    BIGINT
);

-- Enforce "one season default row, one row per (season, tier)". Split into
-- two partial unique indexes because NULLs in a plain UNIQUE constraint are
-- treated as distinct in Postgres.
CREATE UNIQUE INDEX IF NOT EXISTS uq_league_config_season_default
    ON league_config (season_id)
    WHERE tier_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_league_config_season_tier
    ON league_config (season_id, tier_id)
    WHERE tier_id IS NOT NULL;
