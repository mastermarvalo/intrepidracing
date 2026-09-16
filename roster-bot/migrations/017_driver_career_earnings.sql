-- ─────────────────────────────────────────────────────────────────────
-- 017: lifetime driver earnings
--
-- Drivers are paid a salary, and until now nothing recorded that they
-- had been. Escrow moves a team's cash and settles it back; the driver
-- side of the same transaction was never written down, so there was no
-- answer to "who has earned the most in this league" — the number a
-- seven-season league most wants to show off.
--
-- This is a DRIVER-SIDE TALLY, NOT A SECOND WALLET. Nothing here debits
-- a team. Team budgets, escrow, and the spending cap behave exactly as
-- they did; crediting a driver writes one row in this ledger and
-- touches no other table. A driver's total is a number to display and
-- compete over, with no purchasing power attached. Give it purchasing
-- power later by spending against this same ledger (a debit row), which
-- is why `amount` is signed rather than a running positive total.
--
-- ── Keyed on (guild_id, member_id), deliberately ──
--
-- Not on `drivers.id`. A `drivers` row is per (season, tier), so a
-- driver who moves tier or plays a ninth season is a different row, and
-- a career total keyed that way would reset every year. Keying on the
-- Discord member means carry-forward needs no carry step at all: the
-- total is simply every row that member has ever been credited, and
-- season 9 adds to season 8's number by existing. `drivers.id` is kept
-- alongside, nullable, for provenance.
--
-- ── Why the foreign keys are ON DELETE SET NULL ──
--
-- Everywhere else in this schema, season-scoped rows CASCADE from
-- `seasons`. Here that would be wrong: deleting an old season would
-- silently erase the career history it contributed, which is the exact
-- thing this table exists to preserve. The links are provenance, so
-- losing them costs a row its context but never its amount.
--
-- `guild_id` and `member_id` are therefore the only NOT NULL identity
-- columns, and they reference nothing — there is no members table
-- (ADR-001), and a driver who leaves the server keeps their history.
-- ─────────────────────────────────────────────────────────────────────

-- Kinds are a data table, not an Enum (ADR-001): a league can add a
-- kind without a code change, and the FK below keeps typos out.
CREATE TABLE IF NOT EXISTS driver_earning_kinds (
    code     TEXT    PRIMARY KEY,
    label    TEXT    NOT NULL,
    -- Whether the bot writes this kind by itself during a race import.
    -- `race_salary` is automatic; the rest are only ever written by an
    -- admin action, which is what the earnings panel filters on when it
    -- shows where a total came from.
    is_auto  BOOLEAN NOT NULL DEFAULT FALSE
);

INSERT INTO driver_earning_kinds (code, label, is_auto) VALUES
    ('race_salary', 'Race salary',         TRUE),
    ('carry_in',    'Opening career total', FALSE),
    ('adjustment',  'Manual adjustment',    FALSE)
ON CONFLICT (code) DO NOTHING;

CREATE TABLE IF NOT EXISTS driver_earnings_ledger (
    id          BIGSERIAL     PRIMARY KEY,
    guild_id    BIGINT        NOT NULL,
    member_id   BIGINT        NOT NULL,
    kind        TEXT          NOT NULL REFERENCES driver_earning_kinds(code),
    -- Signed. Credits are positive; a correction or a future purchase is
    -- negative. Never zero: a zero row records nothing and would show up
    -- in the history panel as an event that did not happen.
    amount      NUMERIC(12,2) NOT NULL,
    season_id   BIGINT        REFERENCES seasons(id)     ON DELETE SET NULL,
    tier_id     BIGINT        REFERENCES tiers(id)       ON DELETE SET NULL,
    driver_id   BIGINT        REFERENCES drivers(id)     ON DELETE SET NULL,
    contract_id BIGINT        REFERENCES contracts(id)   ON DELETE SET NULL,
    round_id    BIGINT        REFERENCES race_rounds(id) ON DELETE SET NULL,
    note        TEXT,
    actor_id    BIGINT,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT driver_earnings_amount_nonzero CHECK (amount <> 0)
);

-- One salary row per contract per race. This is what makes a re-import
-- after a stewards' decision safe: the second import conflicts and
-- writes nothing, exactly as `contract_race_service` does for escrow.
-- Partial, so manual adjustments (which carry no round) are unaffected
-- and a driver can receive any number of them.
CREATE UNIQUE INDEX IF NOT EXISTS uq_driver_earnings_race_salary
    ON driver_earnings_ledger (contract_id, round_id)
    WHERE kind = 'race_salary';

-- The leaderboard reads this: every row for a guild, grouped by member.
CREATE INDEX IF NOT EXISTS idx_driver_earnings_member
    ON driver_earnings_ledger (guild_id, member_id);

-- Per-season totals, for "who earned most in season 8" and for the
-- season summary on a driver's card.
CREATE INDEX IF NOT EXISTS idx_driver_earnings_season
    ON driver_earnings_ledger (season_id, member_id);

-- A driver's own history, newest first.
CREATE INDEX IF NOT EXISTS idx_driver_earnings_member_recent
    ON driver_earnings_ledger (guild_id, member_id, created_at DESC);
