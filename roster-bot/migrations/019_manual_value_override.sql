-- 019: manual per-driver value overrides
--
-- Until now a driver's market value could only come from the valuation
-- engine. There was no way to correct a single wrong number — a bad
-- results import, a returning veteran the formula cannot see — without
-- editing the race results themselves, which corrupts the race record
-- to fix a pricing problem.
--
-- An override is stored as a *valuation run*, not as a side table.
-- Every reader (market boards, movers, cross-tier dashboard, the price
-- floor in offer validation) already resolves a value by finding the
-- tier's most recent published run and reading its rows. A side table
-- would have to be threaded through all of them, and any reader that
-- was missed would silently keep showing the stale value.
--
-- The consequence, enforced in the workflow layer: an override run must
-- contain a row for EVERY driver the previous run covered, carrying
-- their values forward unchanged. A run holding only the overridden
-- driver would make the market board render exactly one driver.
--
-- No BEGIN/COMMIT — the runner wraps each file in a transaction.

-- Run kinds as a data table, not an enum (ADR-001): adding a future
-- kind must not require a schema migration or a Python deploy.
CREATE TABLE IF NOT EXISTS valuation_run_kinds (
    code  TEXT PRIMARY KEY,
    label TEXT NOT NULL
);

INSERT INTO valuation_run_kinds (code, label) VALUES
    ('computed', 'Computed by the valuation engine'),
    ('manual',   'Manual override by an administrator')
ON CONFLICT (code) DO NOTHING;

-- Existing rows are all engine output, so 'computed' is the correct
-- default and backfill in one step.
ALTER TABLE valuation_runs
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'computed'
        REFERENCES valuation_run_kinds(code);

-- Which driver was overridden, and why. Both NULL for computed runs.
-- ON DELETE SET NULL rather than CASCADE: deleting a driver must not
-- delete the run, because the run also carries every other driver's
-- value and removing it would roll the whole tier back a cycle.
ALTER TABLE valuation_runs
    ADD COLUMN IF NOT EXISTS override_driver_id BIGINT
        REFERENCES drivers(id) ON DELETE SET NULL;

ALTER TABLE valuation_runs
    ADD COLUMN IF NOT EXISTS override_reason TEXT;

-- A manual run must name the driver it overrode and say why; a
-- computed run must claim neither. Without this a manual run with a
-- NULL reason would be indistinguishable from an engine run in the
-- audit trail, which defeats the point of recording it.
ALTER TABLE valuation_runs
    DROP CONSTRAINT IF EXISTS valuation_runs_override_coherent;
ALTER TABLE valuation_runs
    ADD CONSTRAINT valuation_runs_override_coherent CHECK (
        (kind = 'manual'
            AND override_driver_id IS NOT NULL
            AND override_reason IS NOT NULL
            AND length(btrim(override_reason)) > 0)
        OR
        (kind <> 'manual'
            AND override_driver_id IS NULL
            AND override_reason IS NULL)
    );

CREATE INDEX IF NOT EXISTS idx_valuation_runs_override_driver
    ON valuation_runs (override_driver_id)
    WHERE override_driver_id IS NOT NULL;
