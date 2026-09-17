-- Offers carry a race term, so a race term can actually be offered.
--
-- Migration 015 made `contracts.term_races` the authoritative term, but
-- `contract_offers` kept only `term_seasons`. Races were derived at
-- signing (`term_seasons x races_per_season`), which meant the only
-- terms a Team Principal could express were whole multiples of the
-- calendar. A league could set a 5-race minimum and no TP could offer a
-- 5-race deal: the shortest offerable term was one full season.
--
-- Two consequences this fixes:
--
--   1. A short deal -- a 6-race audition, a 10-race stand-in -- was
--      unofferable, so `min_term_races` below one season was dead
--      config.
--   2. Re-signing a driver mid-term could not match the races actually
--      remaining. The runbook's own guidance in S14 ("a driver with 12
--      races left should be signed for 12 races") was impossible to
--      follow through the offer form.
--
-- `term_seasons` stays on the table. It is what every pre-018 offer was
-- agreed in, it is still what the season-denominated bounds
-- (`min_term_seasons` / `max_term_seasons`) are checked against, and it
-- is still what the per-season salary rate is quoted against. From here
-- it is DERIVED from the race term as CEIL(term_races / races_per_season)
-- -- the number of seasons the deal can touch -- while `term_races` is
-- what the deal actually runs for.
--
-- Backfill is the same conversion migration 015 used for contracts, and
-- the same one `queries.derive_term_races` applies, including the
-- 24-race fallback when no league_config row carries a calendar. An
-- offer written before this migration and one written after therefore
-- describe the same deal.

ALTER TABLE contract_offers
    ADD COLUMN IF NOT EXISTS term_races INTEGER;

UPDATE contract_offers o
   SET term_races = GREATEST(1, o.term_seasons * COALESCE(
           (SELECT lc.races_per_season FROM league_config lc
             WHERE lc.season_id = o.season_id AND lc.tier_id = o.tier_id),
           (SELECT lc.races_per_season FROM league_config lc
             WHERE lc.season_id = o.season_id AND lc.tier_id IS NULL),
           24))
 WHERE o.term_races IS NULL;

-- DEFAULT 24 -- one full season -- and NOT 1. Existing rows are
-- backfilled above and never see this default, but a future writer that
-- omits the column would otherwise create a one-race offer, and on any
-- league with a race minimum above 1 every such offer would be rejected
-- as too short. A one-season default fails safe; it is a term every
-- league can legally express.
ALTER TABLE contract_offers
    ALTER COLUMN term_races SET NOT NULL,
    ALTER COLUMN term_races SET DEFAULT 24;

-- A zero- or negative-race offer is not a policy choice, it is a
-- nonsense value. Mirrors the CHECK migration 015 put on contracts.
ALTER TABLE contract_offers
    DROP CONSTRAINT IF EXISTS contract_offers_term_races_positive;

ALTER TABLE contract_offers
    ADD CONSTRAINT contract_offers_term_races_positive
    CHECK (term_races >= 1);
