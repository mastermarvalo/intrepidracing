-- Phase 9: race-based contract terms + salary escrow.
--
-- Two changes that share a migration because they share a unit.
--
-- ── 1. Terms are measured in RACES, not seasons ────────────────────────
--
-- `term_seasons` stays on the table as history (it is what was agreed on
-- every pre-Phase-9 contract) but `term_races` becomes the authoritative
-- term. A term can now expire mid-season and can run past a season
-- boundary, so "how much of the deal is done" is no longer derivable
-- from season_index alone.
--
-- The money unit does NOT change: `contract_value` is still a
-- PER-SEASON rate and `league_config.salary_cap` still counts it, so the
-- cap keeps meaning exactly what it meant before. Races only measure
-- how long the deal runs and how the cash is paid out.
--
-- Existing rows are backfilled term_races = term_seasons ×
-- races_per_season so every live contract keeps the length it was
-- actually signed for.
--
-- ── 2. Salary is escrowed, not merely committed ───────────────────────
--
-- Before this, a signing moved no money: payroll was compared against
-- the budget balance and only subtracted at rollover. Now each imported
-- race debits that race's share of the salary (contract_value ÷
-- races_per_season) from the team's cash into escrow, and the whole
-- holding is returned at the end of the term together with the driver's
-- P/L (market value at settlement − contract_value). A driver who
-- appreciates pays his team back more than it escrowed; one who does
-- not costs it the difference.
--
-- Escrow is per (contract row, holding team) and there is at most one
-- HELD holding per contract row, enforced by a partial unique index.
-- That is what makes a trade expressible: the old team's holding
-- settles, the new team opens a fresh one against the same contract.
--
-- Escrow applies to contracts signed or carried AFTER this migration.
-- Nothing here backfills a holding for a contract that already exists,
-- so a server mid-season does not wake up to surprise debits.
--
-- ── 3. Re-sign and length premiums ────────────────────────────────────
--
-- Both are price FLOORS on an offer, expressed as fractions and
-- defaulting to 0 so an existing league is unaffected until a
-- commissioner sets them. The floor is
--   base value × (1 + length_premium_pct × (term_races − min_term_races))
--              × (1 + resign_premium_pct if re-signing)
--
-- The length premium is charged PER RACE above the league minimum, so a
-- deal at exactly the minimum length pays no length premium and the
-- rate has to be small (0.005 = +0.5% per race).
-- computed in bot/contracts/rules.py, never here.
--
-- No BEGIN/COMMIT — the runner wraps each file in a transaction.


-- ── league_config: term bounds in races, premiums, calendar length ────

-- How many races a season is worth. This is the divisor that turns a
-- per-season salary into a per-race payment, so it is league data, not
-- a constant: a 12-race Tier 3 season and a 24-race Tier 1 season pay
-- out at different rates from the same salary. The default is a
-- placeholder for the commissioner to edit in `/market-admin config
-- edit`, not a claim about any real calendar.
ALTER TABLE league_config
    ADD COLUMN IF NOT EXISTS races_per_season INTEGER NOT NULL DEFAULT 24;

ALTER TABLE league_config
    DROP CONSTRAINT IF EXISTS league_config_races_per_season_positive;
ALTER TABLE league_config
    ADD CONSTRAINT league_config_races_per_season_positive
        CHECK (races_per_season >= 1);

-- Term bounds in races. Backfilled from the season bounds below so a
-- league that required "at least 2 seasons" still requires the
-- equivalent number of races and nobody's rules silently loosen.
ALTER TABLE league_config
    ADD COLUMN IF NOT EXISTS min_term_races INTEGER;

ALTER TABLE league_config
    ADD COLUMN IF NOT EXISTS max_term_races INTEGER;

UPDATE league_config
   SET min_term_races = GREATEST(1, min_term_seasons * races_per_season)
 WHERE min_term_races IS NULL;

UPDATE league_config
   SET max_term_races = GREATEST(1, max_term_seasons * races_per_season)
 WHERE max_term_races IS NULL;

ALTER TABLE league_config
    ALTER COLUMN min_term_races SET NOT NULL,
    ALTER COLUMN min_term_races SET DEFAULT 1;

-- DEFAULT 24, one full season, NOT 1. Existing rows are backfilled from
-- max_term_seasons above and never see this default, but a future writer
-- that omits the column would otherwise get a one-race ceiling and every
-- multi-race offer in that league would be rejected as too long. A
-- permissive default fails safe here; the minimum can default to 1
-- because a low floor blocks nothing.
ALTER TABLE league_config
    ALTER COLUMN max_term_races SET NOT NULL,
    ALTER COLUMN max_term_races SET DEFAULT 24;

-- The same min <= max discipline migration 011 established for seasons.
-- A constraint rather than editor-only validation, so an unsatisfiable
-- range cannot be persisted by any writer.
ALTER TABLE league_config
    DROP CONSTRAINT IF EXISTS league_config_term_races_range;
ALTER TABLE league_config
    ADD CONSTRAINT league_config_term_races_range
        CHECK (min_term_races >= 1 AND max_term_races >= min_term_races);

-- Premiums as fractions of the driver's base value: 0.150 = 15%.
-- DEFAULT 0 reproduces today's behaviour exactly (no floor above
-- min_salary), so this migration changes no existing league's pricing
-- until somebody opts in.
ALTER TABLE league_config
    ADD COLUMN IF NOT EXISTS resign_premium_pct NUMERIC(6,3) NOT NULL DEFAULT 0;

ALTER TABLE league_config
    ADD COLUMN IF NOT EXISTS length_premium_pct NUMERIC(6,3) NOT NULL DEFAULT 0;

ALTER TABLE league_config
    DROP CONSTRAINT IF EXISTS league_config_premiums_nonnegative;
ALTER TABLE league_config
    ADD CONSTRAINT league_config_premiums_nonnegative
        CHECK (resign_premium_pct >= 0 AND length_premium_pct >= 0);


-- ── contracts: term in races ──────────────────────────────────────────

ALTER TABLE contracts
    ADD COLUMN IF NOT EXISTS term_races INTEGER;

-- Backfill from the term that was actually agreed. Uses the contract's
-- own season config where one exists so a tier with a shorter calendar
-- converts at its own rate, falling back to the season default row and
-- then to the column default.
UPDATE contracts c
   SET term_races = GREATEST(1, c.term_seasons * COALESCE(
           (SELECT lc.races_per_season FROM league_config lc
             WHERE lc.season_id = c.season_id AND lc.tier_id = c.tier_id),
           (SELECT lc.races_per_season FROM league_config lc
             WHERE lc.season_id = c.season_id AND lc.tier_id IS NULL),
           24))
 WHERE c.term_races IS NULL;

ALTER TABLE contracts
    ALTER COLUMN term_races SET NOT NULL,
    ALTER COLUMN term_races SET DEFAULT 1;

ALTER TABLE contracts
    DROP CONSTRAINT IF EXISTS contracts_term_races_positive;
ALTER TABLE contracts
    ADD CONSTRAINT contracts_term_races_positive CHECK (term_races >= 1);

-- Races already served by EARLIER rows in the same chain. A carried row
-- starts part-way through the term, and its own service rows only cover
-- its own races, so the offset is what makes "races served so far"
-- answerable without walking the whole chain.
ALTER TABLE contracts
    ADD COLUMN IF NOT EXISTS races_served_before INTEGER NOT NULL DEFAULT 0;

ALTER TABLE contracts
    DROP CONSTRAINT IF EXISTS contracts_races_served_before_nonneg;
ALTER TABLE contracts
    ADD CONSTRAINT contracts_races_served_before_nonneg
        CHECK (races_served_before >= 0);


-- ── contract_race_service: which races a contract has served ──────────
--
-- One row per (contract, round) the contract was live for. UNIQUE is the
-- whole point: a round re-imported after a stewards' decision must not
-- count twice toward the term or escrow twice.
CREATE TABLE IF NOT EXISTS contract_race_service (
    id          BIGSERIAL     PRIMARY KEY,
    contract_id BIGINT        NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    round_id    BIGINT        NOT NULL REFERENCES race_rounds(id) ON DELETE CASCADE,
    team_id     BIGINT        NOT NULL REFERENCES teams(id),
    -- The per-race share debited for this round, kept here as well as in
    -- the ledger so a term's escrow can be totalled without joining the
    -- ledger and without assuming the rate never changed mid-term.
    amount      NUMERIC(12,2) NOT NULL,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT contract_race_service_amount_nonneg CHECK (amount >= 0),
    CONSTRAINT uq_contract_race_service UNIQUE (contract_id, round_id)
);

CREATE INDEX IF NOT EXISTS idx_contract_race_service_contract
    ON contract_race_service (contract_id);


-- ── escrow_states: lifecycle as data, per ADR-001 ─────────────────────

CREATE TABLE IF NOT EXISTS escrow_states (
    code        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    is_terminal BOOLEAN NOT NULL DEFAULT FALSE
);

INSERT INTO escrow_states (code, label, is_terminal) VALUES
    ('held',    'Held in escrow', FALSE),
    ('settled', 'Settled',        TRUE)
ON CONFLICT (code) DO NOTHING;


-- ── contract_escrow: one holding per (contract row, holding team) ─────

CREATE TABLE IF NOT EXISTS contract_escrow (
    id                 BIGSERIAL     PRIMARY KEY,
    contract_id        BIGINT        NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    origin_contract_id BIGINT        NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    season_id          BIGINT        NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    team_id            BIGINT        NOT NULL REFERENCES teams(id),
    -- Running total actually taken from this team's cash for this
    -- contract row. Grows by one per-race share per imported race, so it
    -- starts at 0 and a holding with 0 has cost the team nothing yet.
    amount_held        NUMERIC(12,2) NOT NULL DEFAULT 0,
    -- How many of the contract's races were already served when this
    -- holding opened. Zero for a signing; non-zero when a trade moves a
    -- contract mid-term, because the receiving team must be settled
    -- against the races IT served, not the whole term. Without this, a
    -- team acquiring a contract with 30 of 36 races already run would be
    -- pro-rated as though it had served them, and would collect almost
    -- the full term's P/L for six races of exposure.
    races_served_at_open INTEGER   NOT NULL DEFAULT 0,
    state              TEXT          NOT NULL DEFAULT 'held'
                                     REFERENCES escrow_states(code),
    opened_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    settled_at         TIMESTAMPTZ,
    CONSTRAINT contract_escrow_amount_nonneg CHECK (amount_held >= 0),
    CONSTRAINT contract_escrow_served_at_open_nonneg
        CHECK (races_served_at_open >= 0),
    CONSTRAINT contract_escrow_settled_has_time
        CHECK (state <> 'settled' OR settled_at IS NOT NULL)
);

-- At most one live holding per contract row. This is the invariant that
-- makes double-escrow impossible and makes a trade legal: settle the
-- old team's holding, then open the new team's.
CREATE UNIQUE INDEX IF NOT EXISTS uq_contract_escrow_one_held
    ON contract_escrow (contract_id)
    WHERE state = 'held';

CREATE INDEX IF NOT EXISTS idx_contract_escrow_origin
    ON contract_escrow (origin_contract_id);

CREATE INDEX IF NOT EXISTS idx_contract_escrow_team_season
    ON contract_escrow (team_id, season_id);


-- ── escrow_settlements: the audit record of one settlement event ──────
--
-- Money lives in team_budget_ledger; this table is the narrative: what
-- was held, what the driver was worth, what the P/L came to, and why the
-- deal ended. Reasons are data, not an enum.
CREATE TABLE IF NOT EXISTS escrow_settlement_reasons (
    code  TEXT PRIMARY KEY,
    label TEXT NOT NULL
);

INSERT INTO escrow_settlement_reasons (code, label) VALUES
    ('term_complete', 'Term completed'),
    ('release',       'Released'),
    ('buyout',        'Bought out'),
    ('void',          'Voided by league office'),
    ('trade',         'Traded away'),
    ('season_end',    'Settled at season end')
ON CONFLICT (code) DO NOTHING;

CREATE TABLE IF NOT EXISTS escrow_settlements (
    id                 BIGSERIAL     PRIMARY KEY,
    escrow_id          BIGINT        NOT NULL REFERENCES contract_escrow(id) ON DELETE CASCADE,
    contract_id        BIGINT        NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    origin_contract_id BIGINT        NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    season_id          BIGINT        NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    team_id            BIGINT        NOT NULL REFERENCES teams(id),
    driver_id          BIGINT        NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    reason             TEXT          NOT NULL REFERENCES escrow_settlement_reasons(code),
    amount_returned    NUMERIC(12,2) NOT NULL,
    -- NULL when no valuation was ever published for the driver: the P/L
    -- is then unknowable and only the escrow comes back. Recorded as
    -- NULL rather than 0 so "flat" and "unknown" stay distinguishable.
    market_value       NUMERIC(12,2),
    contract_value     NUMERIC(12,2) NOT NULL,
    pl                 NUMERIC(12,2),
    races_served       INTEGER       NOT NULL DEFAULT 0,
    term_races         INTEGER       NOT NULL DEFAULT 1,
    note               TEXT,
    actor_id           BIGINT,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_escrow_settlements_team_season
    ON escrow_settlements (team_id, season_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_escrow_settlements_escrow
    ON escrow_settlements (escrow_id);


-- ── budget ledger: escrow kinds + contract provenance ─────────────────

INSERT INTO budget_entry_kinds (code, label, direction) VALUES
    ('salary_escrow',  'Salary escrowed (per race)', 'debit'),
    ('escrow_return',  'Escrow returned',            'credit'),
    ('escrow_pl',      'Contract P/L settled',       'either')
ON CONFLICT (code) DO NOTHING;

-- Provenance for the three kinds above. The ledger already carries
-- race_result_id / round_id / from_season_id; a contract reference is
-- what lets `/market-admin budget show` explain an escrow line.
ALTER TABLE team_budget_ledger
    ADD COLUMN IF NOT EXISTS contract_id BIGINT
        REFERENCES contracts(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_team_budget_ledger_contract
    ON team_budget_ledger (contract_id)
    WHERE contract_id IS NOT NULL;

-- Escrow is opt-outable per season, like enforcement and rollover, so a
-- league can keep the old commitment-only model.
--
-- New seasons default ON; seasons that already exist are switched OFF.
-- That asymmetry is deliberate and it matters on a live server. Under
-- escrow, `available_to_spend` stops subtracting payroll because the
-- cash has already been taken race by race. A season that is already
-- part-way through has NOT had any of that cash taken — so flipping it
-- on would make every team's spendable money jump by its entire
-- payroll overnight, and teams could commit to deals no cash ever
-- backed. An in-flight season must finish under the rules it started
-- under; a commissioner can still switch it on deliberately from
-- `/market-admin budget config`.
--
-- Written as add-nullable / backfill / set-default rather than
-- `ADD COLUMN NOT NULL DEFAULT TRUE` so the backfill can distinguish
-- "row predates escrow" (NULL) from "row deliberately set TRUE". That
-- keeps re-running this migration idempotent: on a second run there are
-- no NULLs left, so the UPDATE touches nothing and a commissioner's
-- deliberate TRUE is never clobbered back to FALSE.
ALTER TABLE budget_config
    ADD COLUMN IF NOT EXISTS escrow_enabled BOOLEAN;

UPDATE budget_config SET escrow_enabled = FALSE WHERE escrow_enabled IS NULL;

ALTER TABLE budget_config
    ALTER COLUMN escrow_enabled SET DEFAULT TRUE;
ALTER TABLE budget_config
    ALTER COLUMN escrow_enabled SET NOT NULL;


-- ── transaction kinds for the contract ledger ─────────────────────────

INSERT INTO transaction_kinds (code, label) VALUES
    ('escrow_opened',   'Salary escrow opened'),
    ('escrow_charged',  'Salary escrowed for a race'),
    ('escrow_settled',  'Salary escrow settled'),
    ('term_completed',  'Contract term completed')
ON CONFLICT (code) DO NOTHING;

INSERT INTO contract_states (code, label, is_terminal)
VALUES ('completed', 'Term completed', TRUE)
ON CONFLICT (code) DO NOTHING;
