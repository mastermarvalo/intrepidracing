-- Phase 4: contracts, contract offers, and the append-only ledger.
--
-- The three tables carry the whole money lifecycle:
--
--   contract_offers      the negotiation record (draft →
--                        pending_driver → countered → accepted →
--                        pending_approval → approved/rejected)
--   contracts            the signed deal (contract_value frozen at
--                        signing; state → active/expired/voided/
--                        terminated)
--   contract_ledger      every money mutation, append-only, with
--                        actor + reason (CLAUDE.md §2 rule 4)
--
-- Partial unique indexes below enforce the two hard business
-- invariants at the DB level:
--   * one active contract per driver
--   * one non-terminal offer per (team, driver) — a team may not
--     spam pending offers against a driver they already have on
--     the table
--
-- No BEGIN/COMMIT — the runner wraps each file in a transaction.

CREATE TABLE IF NOT EXISTS contracts (
    id               BIGSERIAL   PRIMARY KEY,
    season_id        BIGINT      NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    tier_id          BIGINT      NOT NULL REFERENCES tiers(id),
    driver_id        BIGINT      NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    team_id          BIGINT      NOT NULL REFERENCES teams(id),
    contract_value   NUMERIC(12,2) NOT NULL,
    signing_bonus    NUMERIC(12,2) NOT NULL DEFAULT 0,
    max_incentives   NUMERIC(12,2) NOT NULL DEFAULT 0,
    term_seasons     INTEGER     NOT NULL,
    contract_type    TEXT        NOT NULL REFERENCES contract_types(code),
    state            TEXT        NOT NULL REFERENCES contract_states(code),
    value_at_signing NUMERIC(12,2),
    signed_at        TIMESTAMPTZ,
    expires_after    INTEGER,
    voided_at        TIMESTAMPTZ,
    approved_by      BIGINT,
    external_ref     TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_contracts_driver ON contracts (driver_id);
CREATE INDEX IF NOT EXISTS idx_contracts_team_state ON contracts (team_id, state);
CREATE INDEX IF NOT EXISTS idx_contracts_season_tier ON contracts (season_id, tier_id);

-- At most one ACTIVE contract per driver. Partial unique index so
-- expired/voided/terminated rows can coexist for the same driver
-- (contract history).
CREATE UNIQUE INDEX IF NOT EXISTS uq_contracts_one_active_per_driver
    ON contracts (driver_id)
    WHERE state = 'active';


CREATE TABLE IF NOT EXISTS contract_offers (
    id              BIGSERIAL   PRIMARY KEY,
    season_id       BIGINT      NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    tier_id         BIGINT      NOT NULL REFERENCES tiers(id),
    driver_id       BIGINT      NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    team_id         BIGINT      NOT NULL REFERENCES teams(id),
    offered_by      BIGINT      NOT NULL,
    offer_kind      TEXT        NOT NULL,
    salary          NUMERIC(12,2) NOT NULL,
    term_seasons    INTEGER     NOT NULL,
    contract_type   TEXT        NOT NULL REFERENCES contract_types(code),
    signing_bonus   NUMERIC(12,2) NOT NULL DEFAULT 0,
    incentives      TEXT,
    message         TEXT,
    state           TEXT        NOT NULL REFERENCES offer_states(code),
    parent_offer_id BIGINT      REFERENCES contract_offers(id),
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    resolved_by     BIGINT,
    thread_id       BIGINT,
    validation      JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_offers_driver_state
    ON contract_offers (driver_id, state);

CREATE INDEX IF NOT EXISTS idx_offers_team_state
    ON contract_offers (team_id, state);

CREATE INDEX IF NOT EXISTS idx_offers_expires_open
    ON contract_offers (expires_at)
    WHERE state IN ('draft', 'pending_driver', 'pending_team',
                    'accepted', 'pending_approval');

-- At most one live offer per (team, driver). "Live" means a state
-- the negotiation can still act on — `countered` is terminal for
-- the parent (the child carries the negotiation forward), so a
-- parent+child pair for the same (team, driver) is legal.
-- approved / rejected / declined / withdrawn / expired / countered
-- can accumulate freely as history.
CREATE UNIQUE INDEX IF NOT EXISTS uq_offers_one_open_per_team_driver
    ON contract_offers (team_id, driver_id)
    WHERE state IN ('draft', 'pending_driver', 'pending_team',
                    'accepted', 'pending_approval');


CREATE TABLE IF NOT EXISTS contract_ledger (
    id          BIGSERIAL   PRIMARY KEY,
    season_id   BIGINT      NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    tier_id     BIGINT      NOT NULL REFERENCES tiers(id),
    driver_id   BIGINT      REFERENCES drivers(id) ON DELETE SET NULL,
    team_id     BIGINT      REFERENCES teams(id)   ON DELETE SET NULL,
    contract_id BIGINT      REFERENCES contracts(id) ON DELETE SET NULL,
    offer_id    BIGINT      REFERENCES contract_offers(id) ON DELETE SET NULL,
    kind        TEXT        NOT NULL REFERENCES transaction_kinds(code),
    amount      NUMERIC(12,2),
    detail      JSONB       NOT NULL,
    actor_id    BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ledger_driver_time
    ON contract_ledger (driver_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ledger_team_time
    ON contract_ledger (team_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ledger_contract
    ON contract_ledger (contract_id);
