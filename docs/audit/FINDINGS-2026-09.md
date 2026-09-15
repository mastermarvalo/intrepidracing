# Workflow audit, September 2026 — defects a league owner actually hits

> **This is a historical record, not a list of open bugs.** It is the audit
> exactly as written on **2026-09-14**, against the pre-Phase-9 tree
> (then-branch `feature/contract-carryover`). Most of it has since been
> fixed. **For current status see [`STATUS-2026-09.md`](STATUS-2026-09.md)**,
> which marks each finding FIXED / PARTIAL / OPEN with evidence from
> merged `main`. The companion GUI plan is
> [`GUI-CONSOLIDATION-2026-09.md`](GUI-CONSOLIDATION-2026-09.md).
>
> It is kept because the commit messages record what changed but not what
> was wrong or why it mattered. **All line numbers below are stale** —
> locate code by symbol, not by line.

Scope: `bot/cogs/*`, `bot/ui/*`, `bot/workflow.py`, `bot/approvals.py`, `bot/contracts/*`, `bot/market/*`, `migrations/`
Method: every finding below was traced from the click/command to the DB write and back to the message the user sees. Findings that could not be traced end to end were dropped.

Severity: **High** = data is wrong, lost, or the owner is stuck with no in-product way out · **Medium** = a real wrong outcome with a workaround · **Low** = misleading but recoverable.

Counted command surface at audit time: 84 slash commands — `/market-admin` 41, `/roster` 16, `/contract` 9, `/market` 7, `/trade` 5, `/sheets` 4, `/league` + `/help` 2. (`bot/cogs/panel.py` then claimed "62"; that docstring was corrected as part of the same update.)

---

## G1 — Editing one half of the league rules silently reverts the other half — **High**

**Where:** `bot/ui/config_modal.py:62-88` (`_save`), `:388-427` (`ConfigSectionView`), `:432-460` (`_SectionButton.callback`); entry points `bot/cogs/admin_market.py:497-508` and Setup → **Cap & rules**.

**What the user does:** `/market-admin config edit` (or Setup → Cap & rules) → **Money limits** → changes salary cap 60 → 75 → Save. The same message is still on screen, so they then click **Contract rules** on it, change max term 3 → 2, Save.

**What happens:** `_SectionButton` was constructed with a `current` config snapshot read once, before either edit (`config_modal.py:432-448`). `_save` (`:62-88`) calls `queries.upsert_league_config`, which rewrites **all ten columns**, filling the untouched half from that stale snapshot. The salary cap silently drops back to 60. Both saves report success (`:169-174`, `:303-307`) and the embed on the message still shows the pre-edit numbers, so nothing signals the revert. The docstring at `:66-71` only anticipates *two admins* racing; the single-admin sequential case is the common one.

**What should happen:** re-read the config row inside `_save`'s transaction and merge only the fields this modal owns (or rebuild `ConfigSectionView` + embed after every save so the snapshot and the display are never stale).

---

## G2 — A season created without the preset is a dead end with no in-product fix — **High**

**Where:** `bot/ui/setup_screen.py` `_SeasonModal` (preset field optional) and `bot/cogs/admin_market.py:161-200` (`season create`, `preset` optional); `bot/queries.py:682-696` (`fetch_league_config_row` — exact scope, no fallback); only writer of a config row is `bot/presets/f1.py:366`.

**What the user does:** creates the first season and leaves the preset blank (or picks no preset on the slash command).

**What happens:** no `league_config` row exists for that season, and **nothing in the product can create one**. The home panel then tells them to fix it: `bot/cogs/panel.py:140-141` → "Set the cap… → **Setup**". Setup → **Cap & rules** raises from `bot/workflow.py:796-841`: *"No league config row for the season default. Create a season with the F1 preset to seed one."* The typed path is worse — `bot/cogs/admin_market.py:487-495` says *"Seed one first by creating a season with `--preset f1`, or (for a tier override) copy from the season default by editing without a tier once, then re-run with a tier."* Both instructions are impossible or wrong: there is no `--preset` flag (it is a `preset` choice on `season create`), there is no command to apply a preset to an existing season, and "edit without a tier once then re-run with a tier" copies nothing (`_save` writes only the scope it is given, and `fetch_league_config_row` never falls back — `queries.py:686-687`). Valuations fail the same way (`bot/workflow.py:240-244`) and so do channels/commissioner (`bot/workflow.py:~592-600`, `~745-770`: "Run `/market-admin config edit` first" — which itself needs the row). There is no `season delete`, and re-creating with the same name is rejected, so the owner's only escape is renaming the season and starting over.

**What should happen:** either make the preset mandatory when a guild has no config row, or add a "seed default rules" action that inserts a `league_config` row; the error text must name an action that exists.

---

## G3 — The setup checklist tells the owner to run a command that does not exist — **High**

**Where:** `bot/ui/setup_screen.py:68` ("none yet, add them with `/roster add`") and `:111` ("Add drivers with `/roster add`…"); also `roster-bot/README.md:131`.

**What the user does:** follows the Setup screen's next step for the "Drivers" checklist item and types `/roster add`.

**What happens:** Discord shows no such command. `bot/cogs/roster.py` defines `list, remove, create, edit, relink, config, view, sign, drop, graphic, bulksign, bulkdrop, history, freeagents, refresh, dm` — no `add`. The real routes are `/market-admin driver add` (`bot/cogs/admin_market.py:941`), `/market-admin driver sync|sync-all` (`:1012`, `:1058`), or `/league` → **Drivers**.

**What should happen:** point at `/market-admin driver sync-all` (the bulk path) or the Drivers screen, from the checklist and the README.

---

## G4 — Moving a driver who legitimately races in two tiers crashes with a raw DB error — **High**

**Where:** `bot/contracts/service.py:952-1001` (`move_driver_between_tiers`) → `bot/queries.py:1811` (`set_driver_tier`, a plain `UPDATE drivers SET tier_id`) against `UNIQUE (season_id, tier_id, member_id)` in `migrations/003_drivers_and_tier_membership.sql:25`. Callers: `bot/workflow.py:1181-1228` (`move_driver_to_tier`), `bot/cogs/admin_market.py:1306`/`:1321` (`promote`/`relegate`), `bot/ui/drivers_screen.py:713-797`.

**What the user does:** a driver is enrolled in T2 and also T3 (explicitly allowed — `CLAUDE.md` §5 says a member may hold rows in two tiers). The owner promotes the T3 row to T2.

**What happens:** the `UPDATE` collides with the unique index. asyncpg raises `UniqueViolationError`, which is **not** a `TransitionError`, so `workflow.move_driver_to_tier` does not convert it and neither the cog nor the panel catches it (there is no global handler — see G5). The panel click appears to do nothing; the typed command shows "The application did not respond" or a raw traceback in the log.

**What should happen:** detect the existing row in the target tier and either merge/refuse with a plain-language `TransitionError` ("already enrolled in T2 — remove that enrolment first"), or make the move an insert/delete pair.

---

## G5 — Nothing handles an unexpected exception, anywhere — **High**

**Where:** `rg 'on_error|tree\.error|CommandInvokeError|app_commands\.error' bot/` returns zero hits. Every cog catches only its own domain error (`workflow.WorkflowError`, `approvals.ApprovalError`, `service.TransitionError`).

**What the user does:** anything that trips a non-domain exception: Google Sheets auth/quota failure inside an import, `discord.Forbidden` while assigning a role, a `UniqueViolation` (G4), a modal parse path that raises something other than `ConfigError`.

**What happens:** most panel handlers call `interaction.response.defer(ephemeral=True)` first (e.g. `bot/ui/boards_screen.py:132`, `bot/ui/drivers_screen.py:629`, `bot/cogs/panel.py:210`). After a deferral, an uncaught exception produces **no follow-up at all** — the button looks like it did nothing, and the owner does not know whether the write happened. Before a deferral, Discord shows the generic "This interaction failed".

**What should happen:** a `tree.on_error` / `Bot.on_error` hook that logs and always sends one honest ephemeral message ("Something failed: <type>. Nothing was written / state may be partial — check `/market-admin …`").

---

## G6 — Voiding a contract from the panel leaves the driver holding the team role — **High**

**Where:** `bot/ui/drivers_screen.py:603-646` (`_VoidButton` → `_VoidNoteModal`) → `bot/workflow.py` `void_active_contract` → `bot/contracts/service.py:546-570` (`void_contract`). Compare `bot/cogs/contracts.py:513-524` (release) and `:588-596` (buyout), which **do** call `roster_ops.drop_from_team`.

**What the user does:** Drivers → pick driver → **Void contract** → types a reason → submit.

**What happens:** the contract row is voided and a `contract_voided` ledger entry is written, but no role change happens: `void_contract` is DB-only and `workflow.void_active_contract` is Discord-free by design, so nothing drops the team role. The driver keeps the team role, still appears on the team roster embed and in `/roster view`, and the panel reports an unqualified `✅ Voided **X**'s contract.` (`drivers_screen.py:643-646`). The owner has no hint that a manual role removal is outstanding. (`CLAUDE.md` lists "void/release do not drop the role" as a known gap, but release/buyout *do* drop it via the cogs — only the void path is still broken, and only the panel exposes void.)

**What should happen:** either perform the role drop in the Discord-aware caller (as approvals does) or state it in the success note: "Role not dropped — remove @Team manually."

---

## G7 — Void bypasses the open-trade guard that release has, permanently jamming the trade — **High**

**Where:** `bot/contracts/service.py:546-570` (`void_contract`, no trade check) vs `:588-600` (`release_contract`, which calls `queries.fetch_trade_involves_contract` and refuses); enforcement point `bot/contracts/service.py:894-899`.

**What the user does:** a trade involving driver X is pending approval; the commissioner voids X's contract from the Drivers panel (or `/market-admin admin void`), then approves the trade.

**What happens:** the void succeeds because it never checks for open trades. Trade approval then raises *"Contract N is no longer active — trade cannot be approved."* The trade is stuck in `pending_approval` forever; the message never says what to do (reject it and re-propose), and the approvals queue keeps showing it (`bot/ui/approvals_screen.py:78-86`). Whole-transaction rollback (`bot/db.py:49-56`) means nothing half-applies, but the queue item is unresolvable by any button.

**What should happen:** apply the same `fetch_trade_involves_contract` guard to `void_contract`, and if approval does fail, tell the commissioner to reject the trade.

---

## G8 — A mistyped round label produces a garbage valuation, and the typed command never warns — **High**

**Where:** `bot/workflow.py:268` (`fetch_race_round` by exact `round_label`), `:336` (`priced_round=round_row is not None`); `bot/cogs/admin_market.py:668-687` (`valuation run` — ignores `priced_round`); contrast `bot/cogs/panel.py:341-348`, which does warn.

**What the user does:** imports results as `R14 Abu Dhabi`, then runs `/market-admin valuation run tier:t1 round_label:R14 AbuDhabi` (or "Post-Abu Dhabi", as the command's own `describe` at `:654` suggests).

**What happens:** no round matches, so `observations` stays empty (`workflow.py:268-287`) and every driver is priced from `prev_values`/`min_salary` with zero movement — a baseline run. `_render_valuation_preview` shows a full, plausible-looking table and says nothing about the mismatch, because only the panel checks `priced_round`. The run is publishable (`valuation publish run_id:`), which overwrites the tier's market with flat values and refreshes every board. Nothing in `docs/RUNNING_YOUR_LEAGUE.md` says the label must match the import byte-for-byte.

**What should happen:** the typed path must surface the same "⚠ no imported results under that round label — this is a baseline run" warning the panel shows, or offer the recorded round labels as choices/autocomplete.

---

## G9 — Importing from the panel hides the money it just moved — **Medium-High**

**Where:** `bot/cogs/panel.py:238-262` (`ImportModal.on_submit` receipt) vs `bot/cogs/admin_market.py:2017-2039` (`_render_budget_outcome`) used only by `/market-admin results import`. Data exists on the return value: `bot/workflow.py:56-65` (`ImportOutcome.budget`, `.budget_unattributed`).

**What the user does:** Race Night → **Import t1** → submits the sheet.

**What happens:** the import writes budget entries — prize money credited, DNF/DNS/incident penalties debited (`bot/market/budget_ops.py:254-308`) — and records which drivers had no active contract so no team was charged (`bot/market/budget.py:137-141`). The panel receipt reports only `written` and `missing_drivers`; the entire budget block and the unattributed list are dropped. Team balances change with no receipt, and the owner never learns that, say, four free agents' DNFs cost nobody anything. The typed command reports both.

**What should happen:** render `outcome.budget` and `outcome.budget_unattributed` in the panel receipt — the renderer already exists and can be shared.

---

## G10 — Views never expire gracefully; a race night longer than 10 minutes loses the panel — **Medium-High**

**Where:** `bot/ui/base.py` (`PANEL_TIMEOUT_SECONDS = 600`, no `on_timeout` on `OwnedView`/`AdminOwnedView`), `bot/cogs/panel.py:281-301` (`_OwnedView`, same). `rg 'on_timeout' bot/` → nothing.

**What the user does:** imports T1, prices it, gets pulled away, comes back 15 minutes later and clicks **Publish** on the price preview (`bot/cogs/panel.py` `PublishView`). Or is halfway through a Setup wizard (`_EnrolMemberFlow`, `_TierRoleFlow`, `_ChannelFlow` in `bot/ui/setup_screen.py`) when the timeout lands.

**What happens:** after 600 s discord.py stops dispatching to the view. The buttons stay visibly enabled, and clicking one shows Discord's generic "This interaction failed". Nothing explains that the panel expired or that the priced run still exists. Wizard state held on the view (`_AddBoardFlow.kind`/`tier_code`, `boards_screen.py:164-165`; the tier/channel/member picks in `setup_screen.py`) is gone with no resume, and the remembered sheet URL goes with it (G23).

**What should happen:** implement `on_timeout` on the base views: disable the children, edit the message to "This panel expired — run `/league` again", and for the publish step name the run id and `/market-admin valuation publish run_id: N` so the work is not orphaned.

---

## G11 — Approving an offer for a driver with no published valuation silently creates a contract with no P/L baseline — **Medium-High**

**Where:** `bot/approvals.py:169-178` (`fetch_latest_published_valuation` → `value_at_signing=market_value`, may be `None`); render skips it at `bot/contracts/render.py:212-221`; the surplus/underwater boards read `queries.fetch_tier_contracts_with_market` (`bot/market/boards.py:210-237`).

**What the user does:** enrols a new driver mid-season and approves their first contract before running a valuation that includes them (very likely: `/market-admin driver add` then straight to the offer).

**What happens:** `value_at_signing` is stored `NULL`. The public signed post quietly omits the "P/L vs market at signing" field, `/market team` and the surplus/underwater boards have nothing to compare against for that contract, and the number can never be recovered later because it is a snapshot. Approval reports plain success — no warning, no prompt to price the tier first.

**What should happen:** warn at approval time ("no published market value for X — P/L cannot be tracked for this contract; run a valuation first?") and let the commissioner cancel.

---

## G12 — Offer approval can silently skip the team-role assignment; the trade path warns but the offer path does not — **Medium-High**

**Where:** `bot/approvals.py:194-206` (offer: `member = guild.get_member(...)`, `if member is not None and team is not None:` — no `else`) vs `bot/approvals.py:302-306` (trade: appends `"member … not in guild — role not swapped"` to `role_warnings`).

**What the user does:** approves an offer for a driver whose member object is not in cache (member intent not chunked, or the driver has left the server).

**What happens:** the contract is created and the public signing post goes out, but no role is assigned and `role_warning` stays `None`, so the panel prints `✅ Approved offer #N → contract …` with nothing else (`bot/ui/approvals_screen.py:271-276`). The driver never gets the team role; the roster embed and the contract disagree. The trade path in the same file warns for exactly this case, which is what makes this a bug rather than a policy.

**What should happen:** set `role_warning = "driver not found in the server — role not assigned"` when `member is None` (and likewise when `team is None`).

---

## G13 — Approve is one click, irreversible, and shown without any cap or market context — **Medium**

**Where:** `bot/ui/approvals_screen.py:258-292` (`approve` button — `defer` then execute, no confirmation step), `:92-113` (`_build_offer_detail`).

**What the user does:** Approvals → picks an offer → clicks **Approve**, possibly by mis-click, since Approve/Reject sit next to each other.

**What happens:** the contract is created, budget/cap state changes and a public post goes out immediately. Reject at least opens a modal (`:294-300`), so the destructive-looking action has friction while the irreversible one has none. Undoing means voiding the contract, which is itself broken (G6, G7). The review embed shows salary/term/type/bonus/kind only — no team payroll before/after, no cap space, no budget headroom, no driver market value, and none of the validation warnings recorded at offer time — so the commissioner cannot see whether they are approving a cap-legal deal. If the cap situation changed since submission, the click fails after the fact with a `TransitionError` from `commissioner_approve`.

**What should happen:** show payroll before → after, cap and budget headroom, and market value vs salary on the detail embed, then an explicit "Confirm approval" step (the pattern `bot/cogs/panel.py` `PublishView` already uses for publishing).

---

## G14 — Promote and Relegate are the same button; direction is not enforced — **Medium**

**Where:** `bot/ui/drivers_screen.py:713-797` (`_MoveTierButton`: `self.direction` is used only in the select placeholder) → `bot/workflow.py:1181-1228` (`move_driver_to_tier`, which derives the direction from `rank_order`).

**What the user does:** Drivers → driver → **Promote** → the select lists *every* other tier, including lower ones → picks T3 by mistake.

**What happens:** the driver is relegated. The result note says "Moved X to tier T3" with no direction word and no confirmation step, so a mis-click reads like a success. Whatever role/valuation consequences follow a tier change are applied.

**What should happen:** filter the select to tiers above (Promote) or below (Relegate) the driver's current `rank_order`, and state the direction in the confirmation and result note.

---

## G15 — Race-night panel actions never re-check Manage Server — **Medium**

**Where:** `bot/cogs/panel.py:281-301` defines a local `_OwnedView` that checks **only** the opener id; `HomeView`, `RaceNightView`, `PricePromptView`, `PublishView`, `ImportModal` all extend it. `bot/ui/base.py`'s `AdminOwnedView` (used by every `bot/ui/*_screen.py`) re-checks Manage Server on every click, and `CLAUDE.md` §8 states admin actions must do so.

**What the user does:** an admin opens `/league` → Race Night, then their Manage Server permission is removed (or they hand the ephemeral session over).

**What happens:** import, price and publish keep working for the whole 600 s window — including publishing a market and writing budget entries — because the opener check is the only gate. `_is_admin` is consulted when *building* the home embed (`panel.py` `_BackHomeButton`) but not when acting.

**What should happen:** make the race-night views extend `AdminOwnedView`, or add the same `interaction_check`.

---

## G16 — Past 25 drivers, the Drivers panel cannot reach most of the league — **Medium**

**Where:** `bot/ui/drivers_screen.py:501-535` (`_DriverPickerSelect`, `[:SELECT_MAX_OPTIONS]` with a "showing top 25 of N" note and nothing else).

**What the user does:** a three-tier league with 60 drivers; the owner wants to set status / void / promote driver #40.

**What happens:** the select shows the first 25 only. There is no paging, no tier filter, no name search, and no "use `/market-admin admin set-status` instead" hint — unlike `bot/cogs/panel.py:609-613`, which names the typed fallback when tiers exceed `_MAX_TIER_BUTTONS`. Every per-driver admin action in the panel is unreachable for the rest of the league.

**What should happen:** add a tier filter plus prev/next paging (the pattern in `bot/ui/history_screen.py:422-455` already exists), and name the typed fallback.

---

## G17 — With a busy queue, pending trades disappear from the approvals picker — **Medium**

**Where:** `bot/ui/approvals_screen.py:33` (`_QUEUE_LIMIT = 25`), `:133-165` (`_QueueSelect` builds offers **then** trades into one list and truncates with `options[:SELECT_MAX_OPTIONS]`); `bot/approvals.py:101-115` applies the limit to each list separately, so up to 50 items arrive.

**What the user does:** free-agency week: 25+ offers pending, plus two trades.

**What happens:** the embed lists both trades with their ids (`:78-86`), but the select is already full of offers, so the trades have no option and cannot be actioned from the panel at all. Nothing says why. The only route is `/market-admin admin approve-trade trade_id:`, which the screen's own docstring (`:4-7`) was written to eliminate.

**What should happen:** either separate offer and trade selects, or page the combined queue and reserve slots for trades.

---

## G18 — Cap adjustments are recorded and then read by nothing — **Medium**

**Where:** writers `bot/cogs/admin_market.py:1284-1291` (`admin adjust-cap`) and `bot/ui/setup_screen.py:1147` (Teams → cap adjust). `rg 'cap_adjustment' bot/` shows **only** writers, the kind's label in `bot/presets/f1.py:81`, and a comment saying "render layers can filter on kind = 'cap_adjustment'". No cap or payroll computation reads it (`bot/contracts/rules.py`, `bot/market/budget.py`).

**What the user does:** grants a team +5.0 M of cap relief for a mid-season penalty, then that team tries to sign a driver that fits only with the relief.

**What happens:** the signing is refused. The adjustment exists purely as a ledger note; enforcement never consults it. Worse, the Teams shelf in Setup tells the owner enforcement is coming — "(Ledger only — enforcement lands in Phase 5.)" — while Phase 5 shipped and closed, so the promise will never be met and the message reads as "not yet" rather than "never".

**What should happen:** either feed cap adjustments into `cap_headroom_ok`, or relabel the action ("audit note only — does not change the cap") and drop the Phase 5 promise.

---

## G19 — A dry-run in History cannot be published, and the embed does not say how — **Medium**

**Where:** `bot/ui/history_screen.py:170-205` (`_ValuationRunSelect` → preview), `:207-222` (`_ValuationPreviewView` — a single Back button), `:224-247` (`build_valuation_preview_embed` — prints "⚪ DRY-RUN" and no command).

**What the user does:** prices a round, gets interrupted or clicks **Discard**, later reopens Race Night → **Browse history** → Valuation runs → picks the unpublished run to publish it.

**What happens:** the run renders with a DRY-RUN badge and no Publish button and no hint of `/market-admin valuation publish run_id: N`. (The two places that *do* name it are the discard message, `bot/cogs/panel.py:444-449`, and `_join_preview`, `admin_market.py:2010-2012` — neither is on screen here.) The publish path exists in exactly one place in the UI: the ephemeral `PricePromptView` immediately after pricing, which is also the thing that times out (G10).

**What should happen:** add a **Publish this run** button to the history preview for unpublished runs, guarded by the same confirmation as race night.

---

## G20 — "Board posted" and "Refreshed every board" are printed without checking either — **Medium**

**Where:** `bot/ui/boards_screen.py:146-149` and `:306-315`; `bot/market/boards.py:86-91` (channel not a cached `TextChannel` → log and return), `:93-101` (`discord.Forbidden` on send → log and return), `:113-117` (Forbidden on edit → log and return).

**What the user does:** adds a market board in a channel where the bot lacks Send Messages / Embed Links, or hits **Refresh all**.

**What happens:** `add_board` returns a board id normally, and the panel says `✅ Board 7 posted in #market` although nothing was posted; the failure only exists in the bot's log. The board list does then show "⚠ not yet posted" (`:44-68`), which contradicts the note the owner just read. **Refresh all** reports `✅ Refreshed every board.` even when every single render was skipped for permissions.

**What should happen:** have `refresh_board`/`refresh_boards` return a per-board result and report it: "posted", or "could not post — the bot needs Send Messages + Embed Links in #market".

---

## G21 — Offseason order is load-bearing, undocumented at the point of use, and half of it has no panel surface — **High**

**Where:** `bot/cogs/admin_market.py:203-217` (`season activate`), `:222-244` (`season carry-over`), `:1750` (`budget rollover`), `:1680` (`budget award`), `:1058` (`driver sync-all`), `:590` (`config free-agency`); engine `bot/workflow.py:1766-1801` (`carry_over_contracts`). `rg 'carry' bot/ui/ bot/cogs/panel.py` → **no panel surface at all**.

**What the user does:** ends a season the way the panel invites them to: Setup → Seasons → creates the new season and activates it, then goes back to running the league.

**What happens:**
- `season activate` replies only `✅ **X** is now the active season.` It says nothing about carry-over, budget rollover or free agency, and the Setup screen has no button for any of them. Every read (payroll, cap, market, boards) now points at the new season, where **no contracts exist** — multi-season deals are simply not there until `season carry-over` is run per past season, and unspent budget is not there until `budget rollover` runs.
- `carry_over_contracts` (`workflow.py:1766-1801`) always carries **into the active season** from a season named by free text, with no autocomplete, no dry-run and no confirmation, and it is idempotent-by-skip: rows it cannot carry stay active in the old season and are only mentioned in the report (`admin_market.py:2149-2205`). With more than one past season the documented oldest-first order is enforced by nothing.
- Free agency is not closed first, so offers can be submitted against a season whose contracts have not landed yet.

The same class of trap exists inside a race night: results must be imported **before** the valuation run, and the label must match exactly (G8). The panel enforces the order for the tiers it shows; the typed commands do not, and nothing tells a first-time owner the order exists.

**What should happen:** an offseason wizard that performs the sequence in order with a preview at each step (see `CONSOLIDATION.md`, Screen 7), plus a next-step line on `season activate` naming carry-over and rollover, and a dry-run mode for carry-over.

---

## G22 — An uncommitted Phase 9 migration will apply itself to the live database on the next restart — **High**

**Where:** `bot/db.py:59-75` (`_run_migrations` executes every `migrations/*.sql` not yet in `schema_migrations`, on every `db.init()`), and the untracked working-tree file `roster-bot/migrations/015_race_terms_and_escrow.sql` (327 lines; `git status` shows it as `??`).

**What the user does:** restarts the bot.

**What happens:** migration 015 is applied silently: `league_config` gains `races_per_season`, `min_term_races`, `max_term_races` (both `NOT NULL DEFAULT 1`), `resign_premium_pct`, `length_premium_pct`; `contracts` gains `term_races NOT NULL DEFAULT 1` and `races_served_before`; four new tables and new ledger kinds appear (`:66-312`). No code reads or writes any of it — `rg 'term_races|escrow' bot/` returns nothing — so from then on every newly approved contract records `term_races = 1` (the column default) regardless of the term actually agreed, and `min/max_term_races` sit at 1 while the season-based rules the UI edits stay authoritative. There are no down migrations, and once the filename is recorded in `schema_migrations` later edits to that file never run.

**What should happen:** keep WIP migrations out of `migrations/`, or gate application on a committed manifest; ship 015 together with the code that reads it.

---

## G23 — The "remembered" sheet URL is forgotten the moment you press Back — **Low**

**Where:** `bot/cogs/panel.py:454-478` (`RaceNightView._sheets` is per-instance), `:234` (`remember_sheet`), `:176-179` (docstring: "remembered per tier between rounds, so week two is three fields and week three is usually two"), `:500-513` (`_BackHomeButton` builds a fresh `HomeView`; returning to Race Night builds a fresh `RaceNightView` with an empty dict).

**What the user does:** imports T1, goes Back to Home to check the status line, returns to Race Night and imports T2 — or simply runs `/league` again next week.

**What happens:** the URL field is empty again; there is no persistence anywhere (no `league_config`/`tier` column holds it). Not destructive, but the promise the flow was built on does not hold, and re-typing the URL is where a wrong-sheet import comes from.

**What should happen:** persist the last sheet URL and range per (season, tier) and prefill from the database.

---

## G24 — Adding a board with no tiers wedges the message it was opened from — **Low**

**Where:** `bot/ui/boards_screen.py:172-184` — `advance()` calls `self.clear_items()` at `:176`, and in the no-tiers branch calls `report_error(...)` and `return`s **without** editing the message.

**What the user does:** opens Boards → **Add board** → picks a tier-scoped kind at a moment when the active season has no tiers.

**What happens:** they get the error ("No tiers in the active season yet. Add a tier in Setup first."), but the message still displays the old components while the view object has none. Every later click on that message — including **Cancel** — dispatches to a view with no matching child and dies as "This interaction failed". The owner must dismiss the ephemeral message and re-run `/league`.

**What should happen:** re-add the components (or re-render the boards screen) before returning from the error branch.

---

## G25 — Duplicate tier ranks are accepted, which makes promote/relegate meaningless — **Low**

**Where:** `migrations/002_seasons_tiers.sql:33` (`rank_order INTEGER NOT NULL`; the only `UNIQUE` is `(season_id, code)` at `:35`); tier creation/edit modals in `bot/ui/setup_screen.py` suggest `len(tiers)+1` but accept any value; direction logic `bot/workflow.py:1181-1228`.

**What the user does:** edits a tier's rank and gives two tiers the same `rank_order`.

**What happens:** it is stored. `move_driver_to_tier` decides "promotion" vs "relegation" by comparing `rank_order`, so moves between the two equal-ranked tiers report a direction that is neither, and any ordering-dependent display becomes arbitrary.

**What should happen:** a unique index on `(season_id, rank_order)`, or validation in the tier modal.

---

## G26 — A signing with no transactions channel configured is announced nowhere, silently — **Low**

**Where:** `bot/approvals.py:209-231` — the public post is wrapped in `if guild_config.transactions_channel_id and team is not None and tier is not None:`; nothing is reported when the condition is false, and `discord.Forbidden` is logged only.

**What the user does:** approves the league's first signing before setting the transactions channel in Setup → Channels.

**What happens:** the contract exists but no public record was posted, and the approval receipt does not say so. Because there is no backfill, the announcement for that signing is lost permanently.

**What should happen:** append "⚠ no transactions channel set — nothing was announced (Setup → Channels)" to the approval note, and the same for a Forbidden.

---

## Cross-cutting notes (verified, not counted as separate defects)

- **No autocomplete on any identifier.** `rg 'autocomplete' bot/cogs/` finds it only on `/contract offer` (team, tier), `/sheets remove` (name) and `/roster graphic` (slot). Every `tier`, team `name`, `from_season`, `contract_id`, `offer_id`, `run_id` and `board_id` argument across `/market-admin`, `/roster`, `/trade` and `/contract` is free text, so a typo is the normal failure mode and the errors ("No tier `T!`") are the normal experience. This is the strongest argument for the panel consolidation.
- **Tier config overrides are ignored on the trade path.** `bot/cogs/trades.py:308` and `:351` look up `league_config` with the season default only, so a tier override does not apply to trade validation. Documented as a known gap in `CLAUDE.md`; noted here because it is real and reachable.
- **Doc drift.** `bot/cogs/panel.py:4` claims 62 slash commands (84 exist); `roster-bot/README.md:131` repeats the nonexistent `/roster add` (G3).
- **Correctly guarded, checked and cleared:** the `assert role is not None` pairs at `bot/cogs/admin_market.py:1040` and `:1080` are preceded by `_tier_role_unavailable` checks; `bot/market/budget.py:224` is preceded by the `team_id is None` skip at `:139-141`; `bot/approvals.py:183` runs inside the same transaction that just created the row; `bot/ui/boards_screen.py:173` is set by the only path that reaches it. Multi-item trade approval cannot half-apply because `bot/db.py:49-56` wraps every `connect()` in a transaction.
