-- ─────────────────────────────────────────────────────────────────────
-- 016: remember each tier's results spreadsheet
--
-- G23. The Race Night panel kept the sheet URL and tab range in memory
-- on the view object, so it survived exactly as long as one panel: the
-- view times out after 600 seconds, and a bot restart cleared it
-- outright. Every race, the commissioner pasted the same long Google
-- Sheets URL again from scratch — the single most repeated piece of
-- typing in running a season, and the easiest place to typo a URL into
-- an import that then fails for reasons that look unrelated.
--
-- Stored on `results_config` rather than `league_config` because it is
-- per (season, tier) exactly like the rest of the results settings, and
-- because each tier races off its own sheet.
--
-- Both columns are nullable: a league that has never imported has no
-- remembered sheet, and "no memory yet" must be distinguishable from an
-- empty string. Nothing is backfilled — there is nowhere to recover a
-- previous value from, so the first import of season 9 fills it in.
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE results_config
    ADD COLUMN IF NOT EXISTS sheet_url TEXT;

ALTER TABLE results_config
    ADD COLUMN IF NOT EXISTS sheet_range TEXT;

-- A remembered value that is present but blank is a bug, not a state
-- the UI knows how to render: it would prefill an empty box while
-- reporting that a sheet was remembered. Reject it at the schema.
ALTER TABLE results_config
    DROP CONSTRAINT IF EXISTS results_config_sheet_url_not_blank;
ALTER TABLE results_config
    ADD CONSTRAINT results_config_sheet_url_not_blank
    CHECK (sheet_url IS NULL OR length(btrim(sheet_url)) > 0);

ALTER TABLE results_config
    DROP CONSTRAINT IF EXISTS results_config_sheet_range_not_blank;
ALTER TABLE results_config
    ADD CONSTRAINT results_config_sheet_range_not_blank
    CHECK (sheet_range IS NULL OR length(btrim(sheet_range)) > 0);
