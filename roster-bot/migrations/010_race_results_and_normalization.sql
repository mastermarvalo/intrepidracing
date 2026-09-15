-- Phase 6: race results ingestion + the normalization lookup that turns
-- raw finishing facts into the [0,1] factor observations the valuation
-- engine expects.
--
-- Why a lookup table rather than arithmetic in Python: ADR-001 forbids
-- numeric business literals in bot/market/. The position -> score curve,
-- the points table, and the win/podium/pole thresholds are all league
-- tunables, so they live here as data and are seeded by bot/presets/f1.py.
--
-- Forward-only, no BEGIN/COMMIT (db._run_migrations wraps each file).

-- ── Normalization lookup ────────────────────────────────────────────────
-- One row per classified finishing position. `race_score` and
-- `quali_score` are the normalized [0,1] observations fed to the
-- race_finish / quali_finish factors; `points` is the championship points
-- award, normalized against the season maximum at read time.
--
-- is_win / is_podium / is_pole keep the "what counts as a podium" decision
-- in data. Code asks the row, never `position <= 3`.
CREATE TABLE IF NOT EXISTS position_scores (
    id          BIGSERIAL     PRIMARY KEY,
    season_id   BIGINT        NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    position    INTEGER       NOT NULL,
    race_score  NUMERIC(6,4)  NOT NULL,
    quali_score NUMERIC(6,4)  NOT NULL,
    points      NUMERIC(8,2)  NOT NULL DEFAULT 0,
    is_win      BOOLEAN       NOT NULL DEFAULT FALSE,
    is_podium   BOOLEAN       NOT NULL DEFAULT FALSE,
    is_pole     BOOLEAN       NOT NULL DEFAULT FALSE,
    UNIQUE (season_id, position)
);

CREATE INDEX IF NOT EXISTS idx_position_scores_season
    ON position_scores (season_id, position);

-- ── Results tuning knobs ────────────────────────────────────────────────
-- Kept out of league_config deliberately: league_config's upsert path is
-- shared by the contract/cap code, and widening it would touch the money
-- path for a feature that has nothing to do with money. Same
-- season-default + optional tier-override resolution pattern.
CREATE TABLE IF NOT EXISTS results_config (
    id                        BIGSERIAL    PRIMARY KEY,
    season_id                 BIGINT       NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    tier_id                   BIGINT       REFERENCES tiers(id) ON DELETE CASCADE,
    form_window_rounds        INTEGER      NOT NULL,
    consistency_window_rounds INTEGER      NOT NULL,
    max_incident_points       NUMERIC(6,2) NOT NULL,
    UNIQUE (season_id, tier_id)
);

-- A season-level default row must be uniquely identifiable even though
-- NULL never equals NULL in a UNIQUE constraint.
CREATE UNIQUE INDEX IF NOT EXISTS idx_results_config_season_default
    ON results_config (season_id)
    WHERE tier_id IS NULL;

-- ── Rounds ──────────────────────────────────────────────────────────────
-- A round is per (season, tier): tiers race their own calendars and a
-- valuation cycle covers exactly one tier's round. round_order drives the
-- form/consistency windows, so it must be monotonic within a tier.
CREATE TABLE IF NOT EXISTS race_rounds (
    id          BIGSERIAL   PRIMARY KEY,
    season_id   BIGINT      NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    tier_id     BIGINT      NOT NULL REFERENCES tiers(id) ON DELETE CASCADE,
    round_label TEXT        NOT NULL,
    round_order INTEGER     NOT NULL,
    held_on     DATE,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    imported_by BIGINT,
    source      TEXT,
    UNIQUE (season_id, tier_id, round_label),
    UNIQUE (season_id, tier_id, round_order)
);

-- ── Results ─────────────────────────────────────────────────────────────
-- Raw facts only. No money, no scores — everything here is something that
-- physically happened in the race, so a re-import after a stewards'
-- decision is a plain overwrite of facts rather than a money mutation.
--
-- finish_position is NULL for a retirement (dnf = TRUE) or a no-show.
CREATE TABLE IF NOT EXISTS race_results (
    id              BIGSERIAL    PRIMARY KEY,
    round_id        BIGINT       NOT NULL REFERENCES race_rounds(id) ON DELETE CASCADE,
    driver_id       BIGINT       NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    finish_position INTEGER,
    grid_position   INTEGER,
    dnf             BOOLEAN      NOT NULL DEFAULT FALSE,
    dns             BOOLEAN      NOT NULL DEFAULT FALSE,
    fastest_lap     BOOLEAN      NOT NULL DEFAULT FALSE,
    driver_of_day   BOOLEAN      NOT NULL DEFAULT FALSE,
    incident_points NUMERIC(6,2) NOT NULL DEFAULT 0,
    note            TEXT,
    UNIQUE (round_id, driver_id),
    CONSTRAINT race_results_finish_positive
        CHECK (finish_position IS NULL OR finish_position > 0),
    CONSTRAINT race_results_grid_positive
        CHECK (grid_position IS NULL OR grid_position > 0),
    CONSTRAINT race_results_incidents_nonnegative
        CHECK (incident_points >= 0),
    -- A classified finish and a retirement are mutually exclusive.
    CONSTRAINT race_results_dnf_has_no_finish
        CHECK (NOT (dnf AND finish_position IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_race_results_round ON race_results (round_id);
CREATE INDEX IF NOT EXISTS idx_race_results_driver ON race_results (driver_id);

-- ── Link a valuation run to the round it priced ─────────────────────────
-- Nullable: Phase 2/3 baseline runs legitimately have no round behind
-- them, and existing rows must keep applying cleanly.
ALTER TABLE valuation_runs
    ADD COLUMN IF NOT EXISTS round_id BIGINT REFERENCES race_rounds(id) ON DELETE SET NULL;

-- ── Recalibrate factor weights for normalized observations ──────────────
-- The Phase 2 preset weights assumed raw observations (points = 25, a
-- finishing position = 1..20). With a positive weight on race_finish that
-- scored P20 above P1, and a 25-point win clipping to max_contribution
-- alongside a 10-point P5, the factors had no resolution.
--
-- Observations are now normalized to [0,1], so weight == the contribution
-- at a perfect observation, and max_contribution goes back to being a
-- defensive backstop rather than a binding constraint.
--
-- Only rows still holding the original Phase 2 defaults are touched: if a
-- commissioner has already tuned a weight, their value is preserved.
UPDATE valuation_factors SET weight = 0.4500, max_contribution = 0.60
    WHERE code = 'race_finish'   AND weight = 1.0000;
UPDATE valuation_factors SET weight = 0.1200, max_contribution = 0.20
    WHERE code = 'quali_finish'  AND weight = 0.4000;
UPDATE valuation_factors SET weight = 0.2000, max_contribution = 0.30
    WHERE code = 'points_scored' AND weight = 0.6000;
UPDATE valuation_factors SET weight = 0.1800, max_contribution = 0.25
    WHERE code = 'wins'          AND weight = 1.2000;
UPDATE valuation_factors SET weight = 0.1000, max_contribution = 0.15
    WHERE code = 'podiums'       AND weight = 0.5000;
UPDATE valuation_factors SET weight = 0.0800, max_contribution = 0.12
    WHERE code = 'poles'         AND weight = 0.3500;
UPDATE valuation_factors SET weight = 0.0500, max_contribution = 0.10
    WHERE code = 'fastest_laps'  AND weight = 0.2000;
UPDATE valuation_factors SET weight = -0.3500, max_contribution = 0.40
    WHERE code = 'dnf'           AND weight = -0.5000;
UPDATE valuation_factors SET weight = -0.2000, max_contribution = 0.30
    WHERE code = 'incidents'     AND weight = -0.3000;
UPDATE valuation_factors SET weight = 0.1500, max_contribution = 0.25
    WHERE code = 'form_trend'    AND weight = 0.5000;
UPDATE valuation_factors SET weight = 0.1000, max_contribution = 0.15
    WHERE code = 'consistency'   AND weight = 0.3000;

-- ── New factor: Driver of the Day ───────────────────────────────────────
-- The raw fact is now captured per result, so give it a factor. Inserted
-- for every season that already has factors seeded; new seasons get it
-- from the preset.
INSERT INTO valuation_factors (season_id, code, label, weight, max_contribution, sort_order)
SELECT DISTINCT vf.season_id, 'driver_of_day', 'Driver of the Day', 0.0600, 0.10,
       (SELECT COALESCE(MAX(sort_order), 0) + 1
          FROM valuation_factors x WHERE x.season_id = vf.season_id)
  FROM valuation_factors vf
ON CONFLICT (season_id, code) DO NOTHING;

-- ── Backfill the normalization curve for existing seasons ───────────────
-- F1 25/26 is currently the only preset, so every existing season was
-- seeded from it and can take the same curve. New seasons get this from
-- bot/presets/f1.py instead. ON CONFLICT keeps re-application safe.
INSERT INTO position_scores
    (season_id, position, race_score, quali_score, points, is_win, is_podium, is_pole)
SELECT s.id, c.position, c.race_score, c.quali_score, c.points, c.is_win, c.is_podium, c.is_pole
  FROM seasons s
 CROSS JOIN (VALUES
    (1,  1.0000, 1.0000, 25, TRUE,  TRUE,  TRUE),
    (2,  0.8600, 0.9000, 18, FALSE, TRUE,  FALSE),
    (3,  0.7600, 0.8200, 15, FALSE, TRUE,  FALSE),
    (4,  0.6800, 0.7500, 12, FALSE, FALSE, FALSE),
    (5,  0.6100, 0.6900, 10, FALSE, FALSE, FALSE),
    (6,  0.5500, 0.6300,  8, FALSE, FALSE, FALSE),
    (7,  0.4900, 0.5800,  6, FALSE, FALSE, FALSE),
    (8,  0.4400, 0.5300,  4, FALSE, FALSE, FALSE),
    (9,  0.3900, 0.4800,  2, FALSE, FALSE, FALSE),
    (10, 0.3500, 0.4400,  1, FALSE, FALSE, FALSE),
    (11, 0.3100, 0.4000,  0, FALSE, FALSE, FALSE),
    (12, 0.2700, 0.3600,  0, FALSE, FALSE, FALSE),
    (13, 0.2400, 0.3200,  0, FALSE, FALSE, FALSE),
    (14, 0.2100, 0.2800,  0, FALSE, FALSE, FALSE),
    (15, 0.1800, 0.2400,  0, FALSE, FALSE, FALSE),
    (16, 0.1500, 0.2000,  0, FALSE, FALSE, FALSE),
    (17, 0.1200, 0.1600,  0, FALSE, FALSE, FALSE),
    (18, 0.0900, 0.1200,  0, FALSE, FALSE, FALSE),
    (19, 0.0600, 0.0800,  0, FALSE, FALSE, FALSE),
    (20, 0.0300, 0.0400,  0, FALSE, FALSE, FALSE),
    (21, 0.0100, 0.0200,  0, FALSE, FALSE, FALSE),
    (22, 0.0000, 0.0000,  0, FALSE, FALSE, FALSE)
 ) AS c(position, race_score, quali_score, points, is_win, is_podium, is_pole)
ON CONFLICT (season_id, position) DO NOTHING;

INSERT INTO results_config
    (season_id, tier_id, form_window_rounds, consistency_window_rounds, max_incident_points)
SELECT s.id, NULL, 5, 5, 6.00 FROM seasons s
ON CONFLICT DO NOTHING;
