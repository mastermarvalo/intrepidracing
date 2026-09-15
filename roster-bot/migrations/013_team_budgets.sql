-- Team budgets: a per-team, per-season money BALANCE that is distinct
-- from the league-wide spending cap.
--
-- The spending cap (`league_config.salary_cap`) is a RULE: the most any
-- team may commit to payroll, identical for everyone. The budget is
-- MONEY: what a team actually has, which differs by team because it is
-- earned (prize money, race earnings) and lost (penalties, no-shows,
-- retirements). A signing must clear both independently:
--
--     committed payroll <= salary_cap        (cap_headroom_ok)
--     committed payroll <= budget balance    (budget_headroom_ok)
--
-- A team may hold MORE money than the cap — the cap simply stops being
-- the binding constraint once a team is rich enough to reach it. That is
-- the competitive-balance property: wealth cannot buy a lineup above the
-- league ceiling; it buys flexibility (absorbing buyouts, banking for a
-- future season).
--
-- There is deliberately no stored balance column. The balance is
-- SUM(amount) over `team_budget_ledger` for a (season, team), so every
-- dollar is explained by an append-only row with an actor and a reason.
-- This mirrors the `contract_ledger` invariant: nothing overwrites
-- history.
--
-- No BEGIN/COMMIT — the runner wraps each file in a transaction.

-- ── Entry kinds ─────────────────────────────────────────────────────────
-- Domain entities as data, per ADR-001: code looks these up by `code`,
-- never by a Python enum.
CREATE TABLE IF NOT EXISTS budget_entry_kinds (
    code       TEXT    PRIMARY KEY,
    label      TEXT    NOT NULL,
    -- Documentation of the expected sign; the ledger CHECK below
    -- enforces it so a "penalty" can never accidentally credit a team.
    direction  TEXT    NOT NULL CHECK (direction IN ('credit', 'debit', 'either'))
);

INSERT INTO budget_entry_kinds (code, label, direction) VALUES
    ('opening_balance',   'Opening balance',              'either'),
    ('rollover',          'Rollover from previous season', 'either'),
    ('prize_money',       'Prize money',                  'credit'),
    ('race_earnings',     'Race earnings',                'credit'),
    ('dnf_penalty',       'Retirement (DNF) penalty',     'debit'),
    ('dns_penalty',       'No-show (DNS) penalty',        'debit'),
    ('incident_penalty',  'Incident-point penalty',       'debit'),
    ('adjustment',        'Commissioner adjustment',      'either')
ON CONFLICT (code) DO NOTHING;

-- ── Ledger ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS team_budget_ledger (
    id              BIGSERIAL     PRIMARY KEY,
    season_id       BIGINT        NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    team_id         BIGINT        NOT NULL REFERENCES teams(id),
    kind            TEXT          NOT NULL REFERENCES budget_entry_kinds(code),
    -- Signed: credits positive, debits negative. NUMERIC, never float.
    amount          NUMERIC(12,2) NOT NULL,
    -- Provenance. Exactly one of these is set for automatic entries;
    -- all NULL for manual awards/adjustments (which carry a note).
    race_result_id  BIGINT        REFERENCES race_results(id) ON DELETE SET NULL,
    round_id        BIGINT        REFERENCES race_rounds(id)  ON DELETE SET NULL,
    from_season_id  BIGINT        REFERENCES seasons(id)      ON DELETE SET NULL,
    note            TEXT,
    detail          JSONB         NOT NULL DEFAULT '{}'::jsonb,
    -- A correction reverses or adjusts an earlier automatic charge after
    -- a round is re-imported with different facts (stewards removed a
    -- DNF, revised incident points). It carries the SAME kind as the
    -- row it corrects so per-kind totals stay honest, and is exempt
    -- from the sign check below because it points the other way.
    is_correction   BOOLEAN       NOT NULL DEFAULT FALSE,
    actor_id        BIGINT,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT team_budget_ledger_amount_nonzero CHECK (amount <> 0)
);

CREATE INDEX IF NOT EXISTS idx_team_budget_ledger_team_season
    ON team_budget_ledger (team_id, season_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_team_budget_ledger_season
    ON team_budget_ledger (season_id);

-- Re-importing a round (which upserts race_results in place) must not
-- double-charge. There is no uniqueness constraint here on purpose: the
-- ingest path nets what has already been charged per (result, kind) and
-- writes only the DIFFERENCE as a correction row, so an identical
-- re-import writes nothing and a revised one writes exactly the change.
CREATE INDEX IF NOT EXISTS idx_team_budget_ledger_result
    ON team_budget_ledger (race_result_id)
    WHERE race_result_id IS NOT NULL;

-- Rollover is written once per (team, target season).
CREATE UNIQUE INDEX IF NOT EXISTS idx_team_budget_ledger_rollover_once
    ON team_budget_ledger (team_id, season_id)
    WHERE kind = 'rollover';

-- Opening balance is written once per (team, season).
CREATE UNIQUE INDEX IF NOT EXISTS idx_team_budget_ledger_opening_once
    ON team_budget_ledger (team_id, season_id)
    WHERE kind = 'opening_balance';

-- Sign discipline: a debit kind can never credit, a credit kind can
-- never debit. Implemented as a trigger because a CHECK cannot look up
-- another table.
CREATE OR REPLACE FUNCTION team_budget_ledger_check_sign() RETURNS trigger AS $$
DECLARE
    dir TEXT;
BEGIN
    IF NEW.is_correction THEN
        RETURN NEW;
    END IF;
    SELECT direction INTO dir FROM budget_entry_kinds WHERE code = NEW.kind;
    IF dir = 'credit' AND NEW.amount < 0 THEN
        RAISE EXCEPTION 'budget kind % is credit-only but amount is %', NEW.kind, NEW.amount;
    ELSIF dir = 'debit' AND NEW.amount > 0 THEN
        RAISE EXCEPTION 'budget kind % is debit-only but amount is %', NEW.kind, NEW.amount;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_team_budget_ledger_sign ON team_budget_ledger;
CREATE TRIGGER trg_team_budget_ledger_sign
    BEFORE INSERT ON team_budget_ledger
    FOR EACH ROW EXECUTE FUNCTION team_budget_ledger_check_sign();

-- ── Budget config ───────────────────────────────────────────────────────
-- Kept out of `league_config` for the same reason `results_config` is:
-- league_config's upsert path rewrites every column and is shared by the
-- contract/cap code, and its two edit modals are already full (5 inputs
-- each). Same season-default + optional tier-override pattern.
--
-- All rates are in $M, matching every other money figure. Penalty rates
-- are stored POSITIVE and applied as debits by the ingest code, so a
-- commissioner never has to think about signs when editing.
--
-- NO ROW for a season means budgets are not enforced there. That is the
-- upgrade path: an existing league gets this migration applied and
-- nothing changes until the F1 preset (new season) or a commissioner
-- (`/market-admin budget config`) writes a row.
CREATE TABLE IF NOT EXISTS budget_config (
    id                       BIGSERIAL     PRIMARY KEY,
    season_id                BIGINT        NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    tier_id                  BIGINT        REFERENCES tiers(id) ON DELETE CASCADE,
    enforce_budget           BOOLEAN       NOT NULL DEFAULT TRUE,
    rollover_enabled         BOOLEAN       NOT NULL DEFAULT TRUE,
    -- Credited as `opening_balance` when a team first appears in the
    -- season with no ledger rows.
    opening_budget           NUMERIC(12,2) NOT NULL,
    -- Per championship point scored, credited as `race_earnings`.
    earnings_per_point       NUMERIC(12,4) NOT NULL,
    -- Flat debits per event.
    dnf_penalty              NUMERIC(12,2) NOT NULL,
    dns_penalty              NUMERIC(12,2) NOT NULL,
    -- Per incident point (race_results.incident_points), debited as
    -- `incident_penalty`.
    penalty_per_incident_pt  NUMERIC(12,4) NOT NULL,
    UNIQUE (season_id, tier_id),
    CONSTRAINT budget_config_opening_nonneg     CHECK (opening_budget >= 0),
    CONSTRAINT budget_config_earnings_nonneg    CHECK (earnings_per_point >= 0),
    CONSTRAINT budget_config_dnf_nonneg         CHECK (dnf_penalty >= 0),
    CONSTRAINT budget_config_dns_nonneg         CHECK (dns_penalty >= 0),
    CONSTRAINT budget_config_incident_nonneg    CHECK (penalty_per_incident_pt >= 0)
);

-- Season default row must be uniquely identifiable even though NULL never
-- equals NULL in a UNIQUE constraint.
CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_config_season_default
    ON budget_config (season_id)
    WHERE tier_id IS NULL;
