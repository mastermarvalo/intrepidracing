-- Phase 8: contract carry-over across seasons.
--
-- A contract signed with term_seasons = N covers N consecutive seasons.
-- Until now nothing tracked which of those N seasons a row represented,
-- nothing ever moved an active row out of `active` when the season
-- ended, and payroll (SUM of active contract_value) therefore carried
-- every old deal forever.
--
-- The model is one contracts row per season served:
--
--   season_index               1-based position within the term. The
--                              row signed in S7 on a 3-season deal is
--                              index 1; its S8 continuation is index 2;
--                              S9 is index 3 and expires at season end.
--   carried_from_contract_id   the previous season's row (NULL on the
--                              originally signed row).
--   origin_contract_id         the originally signed row, so the whole
--                              chain can be pulled with one predicate.
--
-- At carry-over the previous row leaves `active` and becomes `carried`
-- (a new terminal state) when the term continues, or `expired` when the
-- term is done. contract_value is copied verbatim — a carry-over is
-- not a renegotiation (CLAUDE.md §3 invariant 6).
--
-- No BEGIN/COMMIT — the runner wraps each file in a transaction.

ALTER TABLE contracts
    ADD COLUMN IF NOT EXISTS season_index INTEGER NOT NULL DEFAULT 1;

ALTER TABLE contracts
    ADD COLUMN IF NOT EXISTS carried_from_contract_id BIGINT
        REFERENCES contracts(id) ON DELETE SET NULL;

ALTER TABLE contracts
    ADD COLUMN IF NOT EXISTS origin_contract_id BIGINT
        REFERENCES contracts(id) ON DELETE SET NULL;

ALTER TABLE contracts
    DROP CONSTRAINT IF EXISTS contracts_season_index_positive;
ALTER TABLE contracts
    ADD CONSTRAINT contracts_season_index_positive
    CHECK (season_index >= 1);

-- A row may be carried forward at most once. Re-running carry-over
-- cannot fork a contract into two continuations.
CREATE UNIQUE INDEX IF NOT EXISTS uq_contracts_carried_once
    ON contracts (carried_from_contract_id)
    WHERE carried_from_contract_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_contracts_origin
    ON contracts (origin_contract_id)
    WHERE origin_contract_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_contracts_season_state
    ON contracts (season_id, state);

INSERT INTO contract_states (code, label, is_terminal)
VALUES ('carried', 'Carried to next season', TRUE)
ON CONFLICT (code) DO NOTHING;

INSERT INTO transaction_kinds (code, label) VALUES
    ('contract_carried', 'Contract carried over'),
    ('contract_expired', 'Contract expired')
ON CONFLICT (code) DO NOTHING;
