# GUI consolidation spec — 84 commands into 12 panel screens

> **This is the design spec as written on 2026-09-14, before implementation.**
> The twelve screens described here were built and merged, but the spec was
> not rewritten to match what shipped, so **treat differences as the spec
> being out of date, not the code being wrong**. Known divergences are listed
> under "What shipped differently" at the foot of this document.
>
> Companions: [`FINDINGS-2026-09.md`](FINDINGS-2026-09.md) (defect ids `G1`–`G26`
> referenced below) and [`STATUS-2026-09.md`](STATUS-2026-09.md) (what is
> actually fixed). All line numbers here are from the pre-implementation tree
> and are stale — locate code by symbol.

## Why consolidate

- **Everything is typed with free text.** `rg 'autocomplete' bot/cogs/` finds autocomplete on exactly three arguments (`/contract offer` team + tier, `/sheets remove` name, `/roster graphic` slot). Every other `tier`, team `name`, `from_season`, `contract_id`, `offer_id`, `run_id`, `board_id` is untyped free text, so a typo is the default outcome and "No tier `T!`" is the default error.
- **Order matters and nothing states it.** Import before valuation, exact round labels (`G8`), and the offseason sequence close free agency → create → activate → carry-over (oldest season first) → budget rollover → award → driver sync-all → baseline valuation → reopen free agency (`G21`).
- **Money admin and the offseason have no panel surface at all.** `rg 'carry' bot/ui/ bot/cogs/panel.py` returns nothing; all nine `/market-admin budget *` and `admin adjust-cap` surfaces are typed-only.

## Design rules for every screen below

1. **Wizards carry state forward; screens never ask for an id.** Selects always come from a database read, never from user typing. `bot/ui/boards_screen.py:152-202` is the reference pattern (state on the view, components replaced per step).
2. **Every screen has a Back button and survives expiry.** Add `on_timeout` to `bot/ui/base.py`'s `OwnedView`/`AdminOwnedView` and to `bot/cogs/panel.py:281` `_OwnedView`: disable children, edit the message to "This panel expired — run `/league`", and name the typed fallback for any work in flight (`G10`).
3. **Admin screens re-check permission per click** — extend `AdminOwnedView` (`bot/ui/base.py`), not `panel.py`'s opener-only `_OwnedView` (`G15`).
4. **Selects hold 25 options and modals hold 5 inputs.** Any list that can exceed 25 gets a filter select plus prev/next paging (pattern: `bot/ui/history_screen.py:422-455`) — never a silent truncation (`G16`, `G17`).
5. **Destructive or irreversible actions get an explicit confirm step** that restates the consequence, and success notes state what did *not* happen (role not dropped, nothing announced) (`G6`, `G13`, `G20`, `G26`).
6. **Re-read before writing.** Never save from a snapshot captured when the screen opened (`G1`).
7. **Typed commands stay** as the scriptable fallback for every action, but the panel becomes the documented path and every error message names a route that exists (`G2`, `G3`).

---

# Screen map

| # | Screen | New or extend | Job |
|---|---|---|---|
| 0 | `/league` home | extend `bot/cogs/panel.py` | orientation + next step |
| 1 | First-time setup | extend `bot/ui/setup_screen.py` | get a league running |
| 2 | Race night | extend `bot/cogs/panel.py` | weekly loop |
| 3 | Approvals | extend `bot/ui/approvals_screen.py` | commissioner queue |
| 4 | Drivers & roster fixes | extend `bot/ui/drivers_screen.py` | fix a driver |
| 5 | Teams & money | **new** `bot/ui/money_screen.py` (seed from `setup_screen.py` `_TeamsView`) | budgets, cap, payroll |
| 6 | Offseason | **new** `bot/ui/offseason_screen.py` | roll the league over |
| 7 | Market browse | **new** `bot/ui/market_screen.py` (read-only, non-admin) | look things up |
| 8 | Boards & stat sheets | extend `bot/ui/boards_screen.py` | public auto-updating embeds |
| 9 | History | extend `bot/ui/history_screen.py` | audit + publish an old run |
| 10 | Contracts desk (Team Principal) | **new** `bot/ui/contracts_screen.py` | offer, counter, release |
| 11 | Trades desk | **new** `bot/ui/trades_screen.py` | propose and answer trades |
| 12 | Team identity | extend legacy `bot/flow.py` | create/edit a team |

Coverage: 82 of 84 commands are reachable from a panel below; `/league` and `/help` are the entry points, and `/roster relink` is the one mutating command that must stay typed (see the bottom section).

---

## Screen 0 — `/league` home

**Extend:** `bot/cogs/panel.py:100-167` (`build_status_embed`, `_next_step`) and `HomeView`.

**Title:** `🏁 <Season name> — league control`

**Shows:** active season; per-tier line (driver count, latest imported round, whether a market is published, whether a tier role is linked); pending offers/trades; board count and health; **new**: total payroll vs cap per tier, whether budgets are enforced, and whether carry-over has been run for the active season. One "Next step" line, already implemented at `:120-167`.

**Buttons:** `Setup` · `Race night` · `Drivers` · `Approvals (n)` · **`Teams & money`** (new) · **`Offseason`** (new) · `Boards` · `Market` (visible to non-admins too) · `Help`.

**Prefills:** everything from `workflow.fetch_league_status`; the Next-step button is highlighted (`ButtonStyle.primary`) so the owner never has to guess.

**Replaces:** `/help` becomes a button; nothing else. Fix `panel.py:4`'s "62 slash commands" count while here.

---

## Screen 1 — First-time setup (checklist + wizards)

**Extend:** `bot/ui/setup_screen.py` (1198 lines; already has season menu, tier menu, tier role, commissioner, channels, boards, free-agency toggle, teams/cap adjust).

**Title:** `⚙️ Setup — <Season name>`

**Shows:** the existing ticked checklist (season ✅ / tiers / tier roles / drivers / channels / commissioner role / rules / boards), each line naming the **screen** that completes it — never a command (`G3`).

**Buttons/selects:**
- `Season` → season select + **Create season** (preset is **required** when the guild has no `league_config` row, or the flow offers "Seed default rules" — `G2`).
- `Tiers` → tier list, `Add tier`, `Edit tier` (rank uniqueness validated — `G25`), `Link tier role` (role select).
- `Drivers` → jumps to Screen 4 with a prominent **Sync all tiers from roles** action; the checklist text must point here, not at the nonexistent `/roster add` (`G3`).
- `Channels` → channel selects for market / transactions / approvals, per season or tier.
- `Commissioner role` → role select.
- `Cap & rules` → `bot/ui/config_modal.py` chooser, re-reading the row before each modal and rebuilding the embed after each save (`G1`); scope select for season-default vs tier override.
- `Boards` → Screen 8. `Free agency: open/closed` toggle. `Back`.

**Prefills:** current values in every modal (already done); tier rank suggested as `len(tiers)+1`.

**Replaces:** `/market-admin season create`, `season activate`, `season list`, `tier add`, `tier edit`, `tier list`, `config show`, `config edit`, `config channel`, `config role`, `config free-agency` (11).

---

## Screen 2 — Race night

**Extend:** `bot/cogs/panel.py:454-478` (`RaceNightView`), `ImportModal`, `PricePromptView`, `PublishView`.

**Title:** `📥 Race night — <Season name>`

**Shows:** one row per tier with its state — last imported round, whether a valuation exists for it, whether that valuation is published — so the required order (import → price → publish) is visible rather than remembered. Tier buttons are currently capped at `_MAX_TIER_BUTTONS = 4`; replace with a tier **select** so leagues with more tiers are not pushed to the typed command.

**Flow (state carried, no ids typed):**
1. **Tier select** → **Import results** modal: round label, sheet URL, tab/range, date. Sheet URL and range prefilled from the **last import for that tier, read from the database** (not the in-memory dict — `G23`).
2. Import receipt: rows written, roster drivers with no row, **budget entries written (credits, penalties) and drivers with no active contract who were therefore not charged** (`G9`).
3. **Price this round** — the round label is carried from the import, so the mismatch that produces a silent baseline run cannot happen here; when it does happen (typed path) the warning at `panel.py:341-348` must also be added to `/market-admin valuation run` (`G8`).
4. **Publish** behind a confirm step that names the tier, the round and how many drivers move; if the panel expired, `on_timeout` names `run_id` and the publish command (`G10`).

**Buttons:** `Import <tier>` · `Price this round` · `Publish` / `Discard` · `Browse history` (Screen 9) · `Back`.

**Replaces:** `/market-admin results import`, `results list`, `results show`, `valuation run`, `valuation preview`, `valuation publish`, `valuation list` (7). Must extend `AdminOwnedView` (`G15`).

---

## Screen 3 — Approvals

**Extend:** `bot/ui/approvals_screen.py` (359 lines).

**Title:** `📋 Awaiting approval — n offers, m trades`

**Shows:** the queue with ids (already). Detail embed gains the context a commissioner needs, none of which is shown today (`G13`): team payroll before → after, cap space, budget headroom, driver's current market value and offer-vs-market delta, roster slots used, and the validation warnings recorded when the offer was submitted. Where the driver has **no published valuation**, say so explicitly — approving in that state permanently loses the P/L baseline (`G11`).

**Buttons/selects:** separate **Offers** and **Trades** selects, each paged, so trades cannot be crowded out of a full queue (`G17`) · `Approve` → **confirm step** ("This creates contract for X at $Ym and posts publicly") · `Reject` (modal reason, already) · `Back to queue` · `Back`.

**Result note must state:** contract id and ref, **and** any skipped side effect: role not assigned because the member is not in the server (`G12`), nothing announced because no transactions channel is set (`G26`).

**Replaces:** `/market-admin admin approve`, `admin reject`, `admin approve-trade`, `admin reject-trade` (4).

---

## Screen 4 — Drivers & roster fixes

**Extend:** `bot/ui/drivers_screen.py` (814 lines).

**Title:** `👤 Drivers — <tier or all tiers>`

**Shows:** driver list with tier, status, team, contract value and market value; blanks render as "— (needs a published run)" (the fix already landed in `build_driver_detail_embed`; apply the same tolerance everywhere).

**Buttons/selects:** **tier filter + paged driver select** (never a silent "top 25 of N" — `G16`) · `Enrol member` · **`Sync tier from role`** / **`Sync all tiers`** · driver detail actions: `Set status`, `Promote` (select limited to higher-ranked tiers), `Relegate` (lower-ranked only) — both stating the direction in the confirm and result (`G14`) · `Void contract` (confirm + result note that the team role was **not** dropped, or drop it here — `G6`; and refuse when the contract sits in an open trade — `G7`) · `Release`, `Buyout` (confirm showing dead-money impact) · `Sign to team`, `Drop from team`, `Bulk sign`, `Bulk drop` (the member-select views in `bot/cogs/roster.py:328-380` already exist; give them a team picker instead of a typed name) · `Free agents` · `Driver history` · `Back`.

**Prefills:** every action is scoped to the driver already selected; buyout amount prefilled with remaining contract value.

**Replaces:** `/market-admin driver add`, `driver sync`, `driver sync-all`, `admin void`, `admin set-status`, `admin promote`, `admin relegate`, `/contract release`, `/contract buyout`, `/contract status`, `/roster sign`, `roster drop`, `roster bulksign`, `roster bulkdrop`, `roster remove`, `roster list`, `roster view`, `roster freeagents`, `roster history`, `roster refresh` (20).

---

## Screen 5 — Teams & money (new: `bot/ui/money_screen.py`)

Seed from the existing `_TeamsView` / `_CapAdjustModal` in `bot/ui/setup_screen.py:1100-1198`, which already lists teams and records cap adjustments.

**Title:** `💰 Teams & money — <Season name>`

**Shows:** one row per team — payroll, cap and cap space, budget balance, available-to-spend, dead money, active slots used — plus a header stating whether budgets are **enforced** for this season/tier. Cap adjustments must be labelled as audit-only notes unless enforcement is wired in, and the "enforcement lands in Phase 5" promise removed (`G18`).

**Buttons/selects:** team select → team cap sheet · `Award prize money` (round select, preview of per-team credits before writing) · `Manual budget adjustment` (modal: amount, reason — sign made explicit) · `Budget settings` (modal: enforce on/off, rollover on/off, opening budget, earnings per point, DNF/DNS/incident rates; scope select for season vs tier) · `Cap adjustment` (existing modal, relabelled) · `Back`.

**Prefills:** current rates in the settings modal; award defaults to the most recent imported round.

**Replaces:** `/market-admin budget show`, `budget award`, `budget adjust`, `budget rollover` (rollover also appears in Screen 6), `budget config`, `admin adjust-cap`, `/market team` (7).

---

## Screen 6 — Offseason wizard (new: `bot/ui/offseason_screen.py`)

The highest-value new screen: today none of this exists in any panel, and doing it in the wrong order leaves the new season with no contracts and no budgets (`G21`).

**Title:** `🔄 Offseason — <old season> → <new season>`

**Shows:** a numbered, gated checklist where each step is only enabled once the previous one is done, with the current state read live:

1. **Close free agency** (`config free-agency`) — prevents offers landing mid-rollover.
2. **Create the new season** (preset required or rules seeded — `G2`).
3. **Activate it** — confirm step stating that all reads switch immediately and that no contracts exist until step 4.
4. **Carry over contracts** — one row per past season with unresolved active contracts, **ordered oldest first** and only clickable in that order; each shows a **preview** (contracts to carry, contracts to expire, teams that would end up over the cap — `workflow.carry_over_contracts` already returns exactly this in `CarryOverReport`, rendered by `admin_market.py:2149-2205`) before anything is written.
5. **Budget rollover** — preview of per-team carried balances.
6. **Award any outstanding prize money.**
7. **Sync drivers from tier roles** (`driver sync-all`).
8. **Baseline valuation per tier**, then publish — so every driver has a market value before the first offer (avoids `G11`).
9. **Reopen free agency.**

**Buttons:** the step buttons above (each with a preview → confirm), `Skip step` (records why), `Back`.

**Prefills:** old-season name from a select of past seasons — never typed (`carry-over` today takes free text with no autocomplete). Additionally, `/market-admin season activate` should end with a next-step line naming carry-over and rollover.

**Replaces:** `/market-admin season carry-over`, `budget rollover`, `config free-agency`, `driver sync-all` as a sequenced job (all remain individually available).

---

## Screen 7 — Market browse (new: `bot/ui/market_screen.py`, read-only)

The only screen that must be usable by every league member, not just admins — extend `OwnedView`, not `AdminOwnedView`.

**Title:** `📈 Market — <tier> · <round label>`

**Shows:** paged market table for a tier; movers; a single driver's card with valuation breakdown; a team's cap sheet; surplus and underwater tables; cross-tier dashboard. Missing valuations render as "— (needs a published run)", never as a crash or a zero.

**Buttons/selects:** view select (`Market table` / `Movers` / `Surplus` / `Underwater` / `Dashboard`) · tier select · page prev/next · `Driver…` select → driver card · `Team…` select → cap sheet · `Back`.

**Prefills:** defaults to the viewer's own tier if they hold a tier role, otherwise tier 1; page 1; latest published round label shown in the title so nobody misreads a stale board.

**Replaces:** `/market view`, `market movers`, `market driver`, `market team`, `market surplus`, `market underwater`, `market dashboard` (7). Renderers already exist in `bot/market/render.py` and `bot/contracts/render.py` — this screen is wiring, not new formatting.

---

## Screen 8 — Boards & stat sheets

**Extend:** `bot/ui/boards_screen.py` (368 lines; the add wizard already asks kind → tier → channel in the right order).

**Title:** `📊 Boards & stat sheets`

**Shows:** every market board (kind, scope, channel, health) and every `/sheets` stat board (title, channel/thread). Health warnings already exist at `:59-68`.

**Buttons/selects:** `Add market board` (existing 3-step wizard, with the no-tiers branch re-rendering instead of wedging the message — `G24`) · `Add stat sheet` (channel select + modal: title, sheet URL, range) · `Remove…` (select, **plus a confirm step** — removal deletes the posted message and cannot be undone) · `Refresh one` / `Refresh all`, both reporting **per-board outcomes** rather than an unconditional "✅ posted / refreshed" (`G20`) · `Back`.

**Replaces:** `/market-admin board add`, `board remove`, `board refresh`, `board list`, `/sheets add`, `/sheets remove`, `/sheets list`, `/sheets refresh` (8).

---

## Screen 9 — History

**Extend:** `bot/ui/history_screen.py` (544 lines; already has tier filters, run and round browsers, Back everywhere).

**Title:** `🕓 History — valuation runs / rounds`

**Shows:** runs (id, tier, round, published/dry-run, date) and imported rounds with their result tables.

**Buttons/selects:** tier filter · run select (paged) · round select (paged) · **`Publish this run`** on any unpublished run, behind the same confirm as race night — today a dry-run reached from History is a dead end with no button and no command named (`G19`) · `Back to runs` / `Back`.

**Replaces:** `/market-admin valuation list`, `valuation preview`, `results list`, `results show` (shared with Screen 2).

---

## Screen 10 — Contracts desk (Team Principal) (new: `bot/ui/contracts_screen.py`)

Today a TP must type `/contract offer` with team, tier, driver, salary, term, type, bonus and TTL, and drivers must type `/contract accept offer_id:`.

**Title:** `📝 Contracts — <Team name> (<tier>)`

**Shows (header, always):** payroll, cap space, budget available, roster slots used, free-agency open/closed. Then the team's open offers with their state and expiry.

**Flow:** `New offer` → driver select (free agents first, then contracted drivers with a "trade or buyout required" flag) → offer modal (salary, term, bonus, incentives, note) with the driver's **market value and the team's remaining cap shown above the fields** → review panel restating cap validation and offer-vs-market → `Submit to league office`.

**Buttons:** `New offer` · `My offers` · `Withdraw offer…` (select) · `Release driver…` / `Buy out…` (confirm + dead-money preview) · `Driver status…` · `Back`.

**Driver-facing:** the offer card DM keeps its `Accept` / `Decline` / `Request negotiation` buttons (counter opens a modal); the driver never edits money in the accept path, which preserves the audit trail.

**Replaces:** `/contract offer`, `offers`, `withdraw`, `accept`, `decline`, `counter`, `status` (7).

---

## Screen 11 — Trades desk (new: `bot/ui/trades_screen.py`)

**Title:** `🔁 Trades — <Team name>`

**Shows:** open trades involving the viewer's team with state and the contracts moving each way; both teams' payroll/cap before and after.

**Flow:** `Propose trade` → other-team select → contract multi-select from each side (labels are driver + salary + term, so no contract ids are typed) → review panel with both teams' cap and budget validation → submit.

**Buttons:** `Propose trade` · `Accept…` · `Decline…` · `Withdraw…` · `Back`. Accept/decline/withdraw appear as buttons on the trade detail, so nothing needs a `trade_id`.

**Replaces:** `/trade propose`, `accept`, `decline`, `withdraw`, `status` (5). Note: trade validation currently ignores tier config overrides (`bot/cogs/trades.py:308`, `:351`) — fix behind this screen rather than duplicating the lookup a third time.

---

## Screen 12 — Team identity

**Extend:** the legacy wizard in `bot/flow.py` (1254 lines), which `/roster create|edit|relink` already delegate to (`bot/cogs/roster.py:102-140`). Do not rebuild it; give it an entry point from Setup → Teams and migrate it onto `bot/ui/base.py` views so it inherits the opener lock, admin re-check and timeout handling.

**Title:** `🏎 Teams`

**Shows:** teams with their team role, principal role, colour, logo and slot layout; roster embed channel.

**Buttons:** team select → `Edit team` · `Create team` · `Roster settings` (`/roster config`) · `Generate roster graphic` (slot select) · `DM a role` (role select → message modal → existing confirm view at `bot/cogs/roster.py:512-520`) · `Back`.

**Replaces:** `/roster create`, `roster edit`, `roster config`, `roster graphic`, `roster dm` (5).

---

# Commands that must stay typed

| Command | Why a panel cannot express it |
|---|---|
| `/league` | It *is* the panel entry point. |
| `/help` | Needed when the panel is the thing being explained; keep as a button too. |
| `/roster relink` (`bot/cogs/roster.py:126-140`) | Emergency recovery for a team whose database row was lost. A picker is built from the database, so by definition the broken team cannot appear in one — the operator must name it. |

Everything else can be driven from a screen. Two caveats to keep the typed surface honest rather than hidden:

- **Keep every mutating command registered** as the fallback for an expired panel, for scripting, and for a queue deeper than a paged select. The requirement is that panels are the documented path and that every error message names a route that exists (`G2`, `G3`), not that commands disappear.
- **Ids in messages stay copyable.** Contract, offer, trade, run and board ids should keep appearing in receipts and embeds precisely so the typed fallback remains usable when a panel times out.

# Suggested build order

1. `bot/ui/base.py` `on_timeout` + move `panel.py`'s race-night views onto `AdminOwnedView` — unblocks every screen and fixes `G10`, `G15`.
2. Screen 6 (Offseason) and Screen 5 (Teams & money) — the two jobs with **no** panel coverage today and the worst ordering trap (`G21`, `G18`).
3. Screen 4 paging + confirmations (`G6`, `G7`, `G14`, `G16`) and Screen 3 context + confirm (`G11`, `G12`, `G13`, `G17`).
4. Screen 2 receipt and prefill fixes (`G9`, `G23`), Screen 9 publish button (`G19`).
5. Screen 7 (Market browse) and Screen 10/11 (TP-facing desks) — the widest audience, but nothing is broken today; these are adoption work.


---

## What shipped differently

Checked against merged `main` at `528fe0e`:

- **Screens 10 and 11 (Contracts desk, Trades desk) shipped read-only.** The
  spec has them offering, countering, releasing and answering trades. Those
  transitions live in `bot/contracts/service.py` and the cogs, not in
  `bot/workflow.py`, so wiring them into a panel meant moving them — out of
  scope for that update. Both screens instead emit the exact typed command to
  run and state plainly that nothing was written.
- **Design rule 4 (paging) was applied where a list can exceed 25**, notably
  the Drivers picker (`G16`) and both approvals queues (`G17`). `page_count`
  and `page_slice` ended up duplicated across screens rather than living in
  `bot/ui/base.py`; that duplication is still outstanding.
- **Design rule 5 (confirm step) was applied to approvals** (`_ItemView` arms
  then acts) and to publishing an old run, but **not** to every destructive
  action. Void from the Drivers screen still commits on submit, which is part
  of why `G6` remains open.
- **Rule 7 ("every error message names a route that exists") is not fully
  met.** `workflow.py::run_valuation` still tells the owner to seed a config
  row by creating a season with the preset, which cannot be done for a season
  that already exists — the residual half of `G2`.
- **Screen 5 (Teams & money) also absorbed the cap-adjustment surface**, where
  it now states that adjustments are an audit note that changes nothing. The
  typed `admin adjust-cap` and Setup's cap modal still promise enforcement
  "lands in Phase 5" — the residual half of `G18`.
