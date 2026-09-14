-- Phase 5: trades, releases, buyouts, extensions.
--
-- Three new tables:
--
--   trades           two-party proposal record (proposer_team →
--                    other_team). Its own state machine, distinct
--                    from contract_offers because the counterparty
--                    is a team, not a driver.
--
--   trade_items      the contracts a trade moves. Two rows for a
--                    1-for-1 swap (one going each way). The schema
--                    is multi-item-ready even though Phase 5 MVP
--                    only ships the 1-for-1 flow.
--
--   dead_money       cap hits from released contracts (buyouts).
--                    A cap sheet's effective payroll adds these to
--                    the sum of active contract_value rows for the
--                    team. Season rollover (decaying dead money as
--                    seasons pass) is out of Phase 5 scope; each
--                    row applies in the season it was created in.
--
-- trade_states is a lookup table on the same pattern as
-- offer_states — data, not enums (ADR-001 rule 2). Seeded inline in
-- this migration so an upgrade doesn't have to re-run the F1 preset
-- to pick them up.
--
-- No BEGIN/COMMIT — the runner wraps each file in a transaction.

CREATE TABLE IF NOT EXISTS trade_states (
    code        TEXT    PRIMARY KEY,
    label       TEXT    NOT NULL,
    is_terminal BOOLEAN NOT NULL DEFAULT FALSE
);

INSERT INTO trade_states (code, label, is_terminal) VALUES
    ('draft',            'Draft',                       FALSE),
    ('pending_other',    'Pending — other team',        FALSE),
    ('accepted',         'Accepted',                    FALSE),
    ('pending_approval', 'Pending — commissioner',      FALSE),
    ('approved',         'Approved',                    TRUE),
    ('rejected',         'Rejected',                    TRUE),
    ('declined',         'Declined',                    TRUE),
    ('withdrawn',        'Withdrawn',                   TRUE),
    ('expired',          'Expired',                     TRUE)
ON CONFLICT (code) DO NOTHING;


CREATE TABLE IF NOT EXISTS trades (
    id                  BIGSERIAL   PRIMARY KEY,
    season_id           BIGINT      NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    proposing_team_id   BIGINT      NOT NULL REFERENCES teams(id),
    other_team_id       BIGINT      NOT NULL REFERENCES teams(id),
    proposed_by         BIGINT      NOT NULL,
    state               TEXT        NOT NULL REFERENCES trade_states(code),
    message             TEXT,
    expires_at          TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ,
    resolved_by         BIGINT,
    thread_id           BIGINT,
    approved_ref        TEXT,
    CHECK (proposing_team_id <> other_team_id)
);

CREATE INDEX IF NOT EXISTS idx_trades_state
    ON trades (state);

CREATE INDEX IF NOT EXISTS idx_trades_teams
    ON trades (proposing_team_id, other_team_id);

CREATE INDEX IF NOT EXISTS idx_trades_expires_open
    ON trades (expires_at)
    WHERE state IN ('draft', 'pending_other', 'accepted', 'pending_approval');


CREATE TABLE IF NOT EXISTS trade_items (
    trade_id     BIGINT NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    from_team_id BIGINT NOT NULL REFERENCES teams(id),
    contract_id  BIGINT NOT NULL REFERENCES contracts(id),
    PRIMARY KEY (trade_id, contract_id)
);

CREATE INDEX IF NOT EXISTS idx_trade_items_contract
    ON trade_items (contract_id);


CREATE TABLE IF NOT EXISTS dead_money (
    id                  BIGSERIAL     PRIMARY KEY,
    season_id           BIGINT        NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    tier_id             BIGINT        NOT NULL REFERENCES tiers(id),
    team_id             BIGINT        NOT NULL REFERENCES teams(id),
    amount              NUMERIC(12,2) NOT NULL,
    source_contract_id  BIGINT        REFERENCES contracts(id) ON DELETE SET NULL,
    note                TEXT,
    actor_id            BIGINT,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dead_money_team_season
    ON dead_money (team_id, season_id);
