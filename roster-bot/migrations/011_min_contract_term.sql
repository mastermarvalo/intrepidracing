-- Make the contract-length floor a league tunable.
--
-- `max_term_seasons` was already configurable, but the minimum was a
-- hardcoded `< 1` check in bot/contracts/rules.py, so a commissioner
-- could cap contracts at two seasons but could not require them to be
-- at least two. Both bounds are now data.
--
-- DEFAULT 1 reproduces the previous hardcoded floor exactly, so every
-- existing row keeps behaving as it did before this migration and no
-- backfill is needed.
--
-- The min <= max relationship is enforced by a CHECK here as well as in
-- the editing path: the constraint is the thing that makes an
-- unsatisfiable range impossible to persist, whatever writes the row.
--
-- Forward-only, no BEGIN/COMMIT (db._run_migrations wraps each file).

ALTER TABLE league_config
    ADD COLUMN IF NOT EXISTS min_term_seasons INTEGER NOT NULL DEFAULT 1;

-- A floor below one season is not a shorter contract, it is no contract.
ALTER TABLE league_config
    DROP CONSTRAINT IF EXISTS league_config_min_term_positive;
ALTER TABLE league_config
    ADD CONSTRAINT league_config_min_term_positive
    CHECK (min_term_seasons >= 1);

-- An inverted range would block every possible offer, with the rejection
-- appearing to come from whichever bound the TP happened to trip first.
ALTER TABLE league_config
    DROP CONSTRAINT IF EXISTS league_config_term_range_satisfiable;
ALTER TABLE league_config
    ADD CONSTRAINT league_config_term_range_satisfiable
    CHECK (min_term_seasons <= max_term_seasons);
