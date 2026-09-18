# Roster Bot

Discord bot that maintains live, self-updating team roster embeds. Admins create teams via a guided `/roster create` flow; the bot keeps roster messages in sync by watching role changes and polling every 15 minutes. On top of the roster layer it runs a per-tier driver market, team budgets, and a full multi-season contract system.

**New here? Start at [`README.md`](../README.md) in the repository
root** — empty server to running league, in the order you do it. For
far more depth on any step, the owner's runbook is
[`docs/RUNNING_YOUR_LEAGUE.md`](../docs/RUNNING_YOUR_LEAGUE.md). This
README is the reference manual behind both; hand your Team Principals
and drivers [`docs/MARKET_GUIDE.md`](../docs/MARKET_GUIDE.md).

## Requirements

- Python 3.11+ (local dev only)
- Docker + Docker Compose (for deployment)
- A Discord bot token with **Server Members Intent** enabled in the developer portal

## Deployment (Docker)

```sh
cp .env.example .env
# fill in DISCORD_TOKEN
```

Then use the Makefile:

```sh
make up        # build image and start detached
make down      # stop and remove container
make restart   # rebuild image and redeploy (use after code changes)
make logs      # follow logs
make build     # build image only
```

The database is Postgres (service: `postgres`, image `postgres:16-alpine`); data
lives in the `postgres-data` named volume and persists across restarts/rebuilds.
Schema migrations apply automatically on bot startup from `migrations/`.

**Importing an existing SQLite roster.db** (one-time, e.g. after upgrading from
the SQLite-only version):

```sh
# 1. Stop the bot only (Postgres keeps running)
sudo docker compose stop roster-bot

# 2. Apply the schema (start the bot once so migrations run, then stop again,
#    OR psql -f migrations/001_init.sql)
sudo docker compose up -d roster-bot && sleep 5 && sudo docker compose stop roster-bot

# 3. Run the data migration (from inside a Python venv with asyncpg installed)
DATABASE_URL=postgresql://roster:roster@127.0.0.1:5432/roster \
  uv run python scripts/migrate_sqlite_to_pg.py /path/to/roster.db

# 4. Bring everything back up
make up
```

## Local development

```sh
cp .env.example .env
# fill in DISCORD_TOKEN

uv sync --extra dev      # create venv and install deps
uv run python -m bot     # start the bot
```

Run tests (no bot token needed):

```sh
uv run pytest
```

Lint:

```sh
uv run ruff check bot/
```

## Discord setup

1. Create a bot at <https://discord.com/developers/applications>
2. Under **Bot → Privileged Gateway Intents**, enable **Server Members Intent**
3. Invite with scopes `bot` + `applications.commands` and permissions:
   - Read Messages / View Channels
   - Send Messages
   - Embed Links
   - Manage Roles (for `/roster sign` and `/roster drop`)
   - Manage Messages (to delete roster messages on `/roster remove`)
4. Make sure the bot's role sits **above all team roles** in Server Settings → Roles, otherwise it won't be able to assign them

## What a fresh install starts with

If this is the bot's first run on your server, it is worth knowing what
you are and are not inheriting. The bot has **no knowledge of seasons
your league raced before you installed it** — however many that is.

| On a fresh install | State |
|---|---|
| Driver market values | Empty until you publish a baseline valuation per tier |
| Career earnings | Every driver at $0.00M; fills in from your first imported race |
| Contracts | None; every driver is a free agent until signed in the bot |
| Team budgets | No ledger rows until the first thing that touches a budget |
| Salary escrow | **On.** New seasons default to escrow enabled |
| Carry-over | Nothing to carry; your first season has no prior season |

Nothing needs backfilling and no catch-up commands need running. Call
your first season in the bot whatever you like — if your league is
really on its eighth, name it "Season 8" and the bot will happily treat
it as season one of its own records.

The only optional catch-up step is seeding historical driver earnings,
covered under
[Driver career earnings](#driver-career-earnings-phase-10).

## The control panel — start here

Two commands cover everything an admin actually needs day to day:

| Command | What it does |
|---|---|
| `/league` | Guided control panel: shows what's set up, what's missing, and the single next step. Buttons for Setup, Race Night, Approvals and Boards. |
| `/help` | Browsable command reference, grouped by job. Built from the live command list, so it's never out of date. |

`/league` opens on a status screen that answers the two questions admins
actually have — *what state is my league in*, and *what do I do next*:

```text
🏁 League Control
Active season: Season 7

Tiers
t1 · 20 driver(s) · last import: R14 Abu Dhabi · ⏳ run #38 unpublished
t2 · 18 driver(s) · last import: R14 Abu Dhabi · no published market yet
t3 · 16 driver(s) · no results imported

⚠ Waiting on you
3 contract offer(s) and 1 trade(s) awaiting approval.

Next step
Run #38 for t1 is priced but not published. Review and publish it → Race Night

[ Setup ]  [ Race Night ]  [ Approvals (4) ]  [ Boards ]  [ All commands ]
```

### Setup

**Setup** is a checklist that fills itself in. Each line is either ticked
or is the next thing to do, and each button opens the dialog for it —
no argument names, no channel or role ids to copy.

```text
⚙️ League setup
✅ Season — Season 7 is active
✅ Tiers — 3 configured (t1, t2, t3)
✅ League settings — cap, valuation factors and scoring table seeded
⬜ Drivers — none yet — bulk-enrol with /market-admin driver sync-all,
   or press Drivers

Tiers
t1 Tier 1 · 0 driver(s) · 🏷 role linked
t2 Tier 2 · 0 driver(s)
t3 Tier 3 · 0 driver(s)

[ 📅 Season ]  [ 🧱 Tier ]  [ 💰 Cap & rules ]  [ 🌱 Seed settings ]
[ 🏷 Tier role ]  [ 🧑‍⚖️ Commissioner role ]  [ 📣 Channels ]
[ 📊 Boards ]  [ 🏎 Teams ]  [ 🚦 Free agency ]  [ ◀ Back to home ]
```

| Button | Replaces |
|---|---|
| Season | `season create` + `season activate` |
| Tier | `tier add` |
| Cap & rules | `config edit` |
| Seed settings | `season seed-preset` |
| Tier role | `tier edit role:` |
| Commissioner role | `config role` |
| Channels | `config channel` |
| Boards | `board add` / `remove` / `refresh` / `list` |
| Teams | `/roster create` / `edit` / `remove` + `market-admin adjust-cap` |
| Free agency | `config edit free_agency_open:` |

##### Teams

Everything a team needs, without a command. **Create team** opens the
same nine-step wizard `/roster create` does — name, colour, roles,
staff slots and driver slots — with the team key as one extra field,
because Discord will not let one dialog open another. **Edit** reopens
that wizard on an existing team, **Adjust cap** moves one team's
effective spending ceiling, and **Delete** removes a team and its
roster message.

Deleting is refused for any team that has ever signed a driver:

```text
McLaren cannot be deleted — it still has 3 contracts, 12 budget ledger
rows. Deleting it would take the league's money history with it.

Release or trade its drivers first, or leave the team in place: a team
with no drivers signed costs nothing and keeps the records intact.
```

That used to be a raw database error with no explanation. Contracts,
offers, trades, dead money and the budget ledger all point at the team
row, and none of them cascade.

Buttons for steps that cannot work yet are greyed out — you can't add a
tier before a season exists. **Seed settings** is the inverse: it is
offered only while the season has no league settings, and greys out once
it does, because seeding over a live config row would reset the cap and
the valuation factors underneath contracts already signed. It fills in
the settings around tiers you have already built by hand, leaving those
tiers untouched. Roles and channels use Discord's own
pickers, so ids are never typed by hand. Choosing the **f1** preset when
creating a season seeds three tiers, the scoring table, the valuation
factors and the default **$145.00M** salary cap in one step.

#### Cap & rules

Discord allows five inputs per dialog and there are ten numeric league
settings, so **Cap & rules** first asks which half you want:

```
💰 League rules
Editing the season default. Pick a section to change.

Money limits
Salary cap $145.00M
Salary floor $1.00M · ceiling none
Weekly move cap ±$0.75M
Exceptional move cap ±$1.25M

Contract rules
Contract length 1–3 seasons
Active driver slots 2 per team
Max incentives 15.0% of salary
Offers expire after 48h

[ 💰 Money limits ]  [ 📝 Contract rules ]  [ ◀ Back to setup ]
```

**Contract rules** is where you set how long Team Principals may sign
drivers for. Both ends are editable:

| Setting | Meaning |
|---|---|
| Min contract length | Shortest deal a TP may offer, in seasons |
| Max contract length | Longest deal a TP may offer, in seasons |
| Active driver slots | Seats per team per tier |
| Max incentives | Performance bonus ceiling, as a % of salary |
| Offer expiry | Hours before an unanswered offer lapses |

Setting both bounds to the same number locks the league to a single
contract length. An offer outside the range is rejected before it ever
reaches the driver, with a message naming the league's own limit.

Impossible ranges are refused at the point of editing rather than at
offer time — a minimum above the maximum would make every offer illegal
while looking like the TP's mistake. The database carries the same
constraint, so no other code path can write one either.

Both bounds can be set per tier as well as league-wide:
`/market-admin config edit tier:t2` edits the Tier 2 override only. Tiers
without an override inherit the season default.

### Race Night

**Race Night** is the weekly loop in the order it happens: pick a tier →
paste the sheet URL in a popup → import → price → review the movers →
publish. The panel remembers each tier's sheet URL for the rest of the
session, so a three-tier race night means pasting three URLs once, not
re-typing them at each step.

### Approvals

**Approvals** is a live queue, not a list of ids to go and type. Every
pending offer and trade is listed oldest-first with its terms; pick one
and approve or reject it in place. Rejecting prompts for an optional
reason, which lands in the audit log.

```text
📋 Awaiting approval
3 contract offer(s) and 1 trade(s) in Season 7.

Contract offers
17 · ZeezinDomar → McLaren (t1) · $22.00M/season × 2
18 · DuelExploration → Aston Martin (t2) · $6.25M/season × 1

Trades
42 · Williams ⇄ Aston Martin · 2 contract(s)

[ Pick an item to review… ▼ ]
[ ◀ Back ]
```

Approving runs exactly the same code as `/market-admin approve`: the
money commits inside the transaction, the team role is assigned after
it, and a role failure is reported without undoing the contract.

### Boards

**Boards** lists every auto-updating market embed with its id, kind,
tier and channel, and flags any that have no message yet — almost always
missing **Send Messages** or **Embed Links** in that channel. Adding one
is a three-step wizard (kind → tier → channel) that refuses the invalid
combinations: tier boards must have a tier, the cross-tier dashboard
must not.

#### Google Sheets boards

The same screen has a **Google Sheets boards** button leading to the
*other* kind of board — the ones that mirror a spreadsheet you already
maintain into a Discord message and refresh it every ten minutes. Add
one by pasting the sheet link and picking a channel; the link is
validated before you are asked where to put it. A board whose first
read of the sheet fails is still created and says so, rather than
reporting success:

```text
⚠️ Standings was created in #stats, but the first read of the sheet
failed. The posted message shows why. Check that the sheet is shared
publicly (or with the bot's service account) and press Refresh all.
```

`/sheets add|remove|list|refresh` still work and drive the same code.

Everything the panel does is also still a slash command, and the panel
calls the same code as the commands — nothing was removed or renamed.
All 88 commands are still there. Setup, Approvals and Boards route
through `bot/workflow.py` and `bot/approvals.py`, which the
`/market-admin` commands now call too, so there is one code path per
operation regardless of which route you take.
The panel is a shortcut, not a replacement. Buttons are usable only by
the person who opened the panel, and admin-only actions still check
Manage Server.

## Commands

Full reference below, or run `/help` in Discord for the same thing
grouped by job.

### Admin commands (require Manage Server)

| Command | Description |
|---|---|
| `/roster create <name>` | Open the 9-step setup flow for a new team (also `/league` → Setup → Teams → **Create team**) |
| `/roster edit <name>` | Re-open the setup flow, pre-filled with current values |
| `/roster list` | List all teams configured in this server |
| `/roster remove <name>` | Delete a team and its roster message. Refused if the team has contracts, offers, trades, dead money or budget history — those do not cascade, and deleting the team would take the league's money history with it |
| `/roster config` | Set the server-wide Free Agent role |

### Sign / drop (require Manage Server **or** the team's principal role)

| Command | Description |
|---|---|
| `/roster sign <name> <member>` | Give a member the team role; removes Free Agent role if set |
| `/roster drop <name> <member>` | Remove a member's team role; restores Free Agent role if set |

### Open to everyone

| Command | Description |
|---|---|
| `/roster view <name>` | Show a team's current roster embed |
| `/roster freeagents` | List members who have the Free Agent role and at least one Tier role |
| `/league` | Guided control panel (admin actions inside it still require Manage Server) |
| `/help` | Browsable command reference |

## Market & Contracts

The market/contract system lives alongside `/roster` and is being built in
phases.

- **Phase 1** — schema, F1 preset, and commissioner setup (season, tier,
  config).
- **Phase 2** — valuation engine (`bot/market/valuation.py`), money
  boundary (`bot/market/money.py`), dry-run→publish flow.
- **Phase 3** — public `/market` surfaces + self-updating market boards
  that mirror the `stat_boards` refresh pattern (`bot/market/render.py`,
  `bot/market/boards.py`).
- **Phase 4** — contracts: offer → negotiate → approve cycle with cap
  enforcement, per-driver P/L, offer expiry, and an append-only ledger
  (`bot/contracts/{rules,service,render}.py`).
- **Phase 6** — race-results ingestion and normalization: results
  imported from a Google Sheet into `race_results`, normalized through
  the season's `position_scores` curve
  (`bot/market/results.py`, `bot/results_ingest.py`), then fed to the
  Phase 2 engine. This is what makes the market actually move.
- **Phase 5** — trades (1-for-1 contract swaps with two-party
  approval), release (frozen P/L in ledger), buyout (dead-money row
  that counts against the effective cap), extension (updates the
  existing active contract per CLAUDE.md §2 rule 6), and
  promotion/relegation (moves driver + active contract across tiers).
- **Phase 7** — team budgets: each team owns a season balance in an
  append-only `team_budget_ledger`, separate from the league-wide
  spending cap. Race results credit earnings per point and debit DNFs,
  no-shows, and incident points; commissioners award prize money;
  unspent budget rolls over between seasons
  (`bot/market/budget.py`, `bot/market/budget_ops.py`).
- **Phase 8** — contract carry-over: a multi-season deal follows the
  driver into the next season at the same money, a finished deal
  expires and frees the driver, and payroll stops counting old
  seasons' contracts (`bot/contracts/carryover.py`).

**For a league-member walkthrough of the whole thing (setup,
offering, trading, releasing, buyouts, promotion/relegation), see
[`docs/MARKET_GUIDE.md`](../docs/MARKET_GUIDE.md).**

Every dollar amount is a `Decimal` end to end (no floats); every business
number the league can tune (salary cap, movement caps, contract term
limits, valuation weights) lives in DB config rows seeded by the F1
preset — never as a Python literal in market/contract code. See
`docs/ADR-001-f1-with-generic-future.md` for the rules and
`scripts/check_magic_numbers.py` for the CI guard that enforces them.

The September 2026 workflow audit is in
[`docs/audit/`](../docs/audit/README.md): 26 defects traced from click to
database write to the message the user sees, each marked fixed, partial or
open against current `main`. Worth reading before changing a panel screen or
an admin command — it records why several of them are shaped the way they are.

Valuations are **tier-isolated by construction**: the engine takes one
tier's inputs at a time and has no notion of tier structure, so a Tier-2
driver mathematically cannot influence a Tier-1 value.

### `/market-admin` (require Manage Server)

**Setup (Phase 1)**

| Command | Description |
|---|---|
| `/market-admin season create <name> [preset]` | Create a season; `preset: F1 25/26` seeds tiers, lookups, valuation factors, and default league config |
| `/market-admin season activate <name>` | Make a season the active one for this guild |
| `/market-admin season list` | List all seasons |
| `/market-admin season carry-over <from_season>` | Carry every active contract from a finished season into the active one: multi-season deals get their next row, finished deals expire and lose the team role; idempotent |
| `/market-admin tier add <code> <label> <rank> [role] [color]` | Add a tier to the active season |
| `/market-admin tier edit <code> ...` | Edit an existing tier (labels, roles, colors) |
| `/market-admin tier list` | List tiers for the active season |
| `/market-admin tier remove <code> [override]` | Remove a tier. Refuses if it has contract history; shows what would be destroyed and asks to confirm. `override: true` deletes the history too and requires typing the tier code |
| `/market-admin config show [tier]` | Show league config (season default or tier override) |
| `/market-admin config edit [tier]` | Edit money limits or contract rules (incl. min/max contract length) |
| `/market-admin config channel <kind> <#channel> [tier]` | Set market / transactions / approvals channel |
| `/market-admin config role <@role> [tier]` | Set the commissioner role |
| `/market-admin config free-agency <open\|closed> [tier]` | Open or close the free-agency window |

**Valuations (Phase 2)**

| Command | Description |
|---|---|
| `/market-admin valuation run <tier> <round_label>` | Create an *unpublished* dry-run for a tier; shows the preview |
| `/market-admin valuation preview <run_id>` | Re-show a run's preview |
| `/market-admin valuation publish <run_id>` | Flip a dry-run to published — makes it the live market value |
| `/market-admin valuation list [tier]` | 20 most recent runs |

A run now prices the round whose `round_label` you pass, if results for
that label have been imported (see **Race results** below). With no
matching round the run falls back to empty observations and produces no
movement — which is how you create a pre-season baseline.

**Race results (Phase 6)**

| Command | Description |
|---|---|
| `/market-admin results import <tier> <round_label> <sheet> [tab] [held_on]` | Import a round's results from a Google Sheet |
| `/market-admin results list [tier]` | Imported rounds, per tier, in calendar order |
| `/market-admin results show <tier> <round_label>` | Raw results plus the normalized observation each one produced |

The sheet needs a header row. Column names are matched
case-insensitively against a set of aliases, so `Pos`, `Position` and
`Finishing Position` are all understood:

| Column | Required | Accepts |
|---|---|---|
| `Driver` | yes | Must match the driver's Discord display name in that tier |
| `Pos` | yes | `4`, `P4`, `4th`, or `DNF` / `Ret` / `DSQ` / `DNS`; blank means did not start |
| `Grid` | no | Same formats; drives the qualifying and pole factors |
| `DNF` | no | `Y` / `yes` / `true` / `1` / `x`. Overrides a recorded position |
| `DNS` | no | Same truthy values |
| `FL` | no | Fastest lap |
| `DOTD` | no | Driver of the Day |
| `Incidents` | no | Incident or penalty points, scaled against `results_config.max_incident_points` |
| `Notes` | no | Free text, stored with the row |

Imports are **all-or-nothing**. If any row has an unreadable position,
a duplicated driver, two drivers in the same finishing position, or a
name that does not match anyone in the tier, the command reports every
problem and writes nothing. A partial import would mean a driver
silently receives no market movement for the round, which is far harder
to spot later than a failed command now.

Re-importing the same `round_label` **corrects that round in place**
rather than creating a second one — so a stewards' decision after
publication is a one-command fix. Results are facts, not money, so
correcting them is not a ledger event; re-run the valuation afterwards
to reprice.

Normalization turns raw facts into observations in `[0, 1]` before they
reach the engine. This is not cosmetic:

- Finishing position is looked up in `position_scores`, where P1 carries
  the highest score. Fed raw against a positive weight, a P20 scored
  twenty times a win.
- Championship points are expressed as a share of the maximum award, so
  a win and a midfield points finish stay distinguishable instead of
  both clipping to the same per-factor cap.
- `form_trend` and `consistency` are derived over the windows in
  `results_config` from stored history, bounded by `round_order` so
  re-running an earlier round reproduces exactly what it originally saw.
- Pole + win + fastest lap in one round flags the drive as exceptional
  and unlocks the wider `exceptional_move_cap`.

The curve itself is data. Edit `position_scores` to reshape how steeply
the league rewards the front of the field; nothing in
`bot/market/results.py` hard-codes a threshold (ADR-001).

**Boards (Phase 3)**

| Command | Description |
|---|---|
| `/market-admin board add <kind> <#channel> [tier]` | Post a self-updating board (`market`, `movers`, `dashboard`) |
| `/market-admin board remove <board_id>` | Delete a board (also deletes its Discord message) |
| `/market-admin board refresh [board_id]` | Re-render a specific board or all boards |
| `/market-admin board list` | List boards in the active season with health status |

Every board auto-refreshes after `/market-admin valuation publish` for
its tier (plus every cross-tier dashboard) and again every 15 minutes
via the safety poll — same pattern the roster embeds use. Board kinds
in Phase 3 + 4: `market`, `movers`, `dashboard`, `surplus`,
`underwater`.

### `/market` (open to everyone, ephemeral replies)

| Command | Description |
|---|---|
| `/market view <tier> [page]` | Paginated tier market with Prev/Next buttons |
| `/market movers <tier>` | Top risers and fallers from the last published run |
| `/market driver <@member>` | Driver card: current value, week's movement, trend of last N runs |
| `/market team <name>` | Cap sheet: payroll, cap space, per-driver P/L |
| `/market surplus <tier>` | Drivers with the biggest positive P/L (market − contract) |
| `/market underwater <tier>` | Drivers with the biggest negative P/L |
| `/market dashboard` | Cross-tier top-of-tier summary (display only) |
| `/market earnings [scope] [page]` | Driver career-earnings leaderboard, all time or this season |
| `/market my-earnings [@member]` | One driver's career total, season total, and recent pay |

### `/contract` (open to everyone; TP / driver / commissioner scopes)

| Command | Who | Description |
|---|---|---|
| `/contract offer <team> <tier> <driver> <kind> <ttl>` | TP for team | Two-stage flow (slash args → modal → review → submit); ephemeral review shows cap arithmetic + validation checks |
| `/contract offers` | TP | My team's open offers |
| `/contract withdraw <offer_id>` | TP | Cancel an offer I own |
| `/contract accept <offer_id>` | Driver | Move to commissioner queue |
| `/contract decline <offer_id> [note]` | Driver | End negotiation |
| `/contract counter <offer_id>` | Driver | Open a counter modal |
| `/contract status <@driver>` | Anyone | Active contract + open offers + history |

### `/market-admin` contract commands (Manage Server)

| Command | Description |
|---|---|
| `/market-admin approve <offer_id>` | Convert an accepted offer into an active contract (extension offers update the existing contract in place; new-signing offers insert a new row). Creates ledger + assigns team role via the shared `/roster sign` path, posts to transactions channel |
| `/market-admin reject <offer_id> [note]` | Reject an offer in `pending_approval` |
| `/market-admin void <contract_id> [note]` | End an active contract |
| `/market-admin set-status <@driver> <status>` | Change a driver's status (writes a `status_change` ledger entry) |
| `/market-admin adjust-cap <team> <delta_m> <note>` | Log a cap adjustment in the ledger (audit trail only — it does not move money; use `/market-admin budget adjust` for that) |
| `/market-admin approve-trade <trade_id>` | Execute an accepted trade — transfers each item's contract to the receiving team, drops/re-adds Discord roles via the shared helper, writes ledger rows for both sides |
| `/market-admin reject-trade <trade_id> [note]` | Reject an accepted trade |
| `/market-admin promote <@driver> <new_tier> [note]` | Move a driver + their active contract to a higher tier |
| `/market-admin relegate <@driver> <new_tier> [note]` | Move a driver + their active contract to a lower tier |

### `/contract` release / buyout (Phase 5)

| Command | Description |
|---|---|
| `/contract release <contract_id> <note>` | End an active contract; ledger captures P/L (market − contract) at release. Blocked if the contract is caught up in an open trade |
| `/contract buyout <contract_id> <buyout_m> <note>` | Release + record a `dead_money` row for the season. Dead money counts against the team's effective cap on the next `/market team` (and in the cap headroom check on new offers) |

### `/trade` (Phase 5)

Team-to-team contract swaps. Phase 5 ships 1-for-1 (one contract from
each side); the schema is multi-item ready.

| Command | Who | Description |
|---|---|---|
| `/trade propose <my_team> <other_team> <my_contract_id> <their_contract_id> <ttl>` | Proposing TP | Creates the trade in `pending_other` state, posts to the approvals channel as a private thread with both TPs added |
| `/trade accept <trade_id>` | Other-team TP | Move to `pending_approval` |
| `/trade decline <trade_id> [note]` | Other-team TP | End the negotiation |
| `/trade withdraw <trade_id>` | Proposing TP | Cancel the trade |
| `/trade status <trade_id>` | Anyone | Full trade embed: items on each side + cap impact for both teams |

### Quick start for a new league

The short version: run `/league`, press **Setup**, and follow the next
step it gives you. It walks the same sequence below and tells you which
piece is missing at each stage.

The equivalent commands, if you'd rather type them:

```text
/market-admin season create name: "F1 2026 Season" preset: F1 25/26
/market-admin season activate name: "F1 2026 Season"
/market-admin tier edit code: t1 label: "Tier 1" rank_order: 1 role: @Tier-1
/market-admin tier edit code: t2 label: "Tier 2" rank_order: 2 role: @Tier-2
/market-admin tier edit code: t3 label: "Tier 3" rank_order: 3 role: @Tier-3
/market-admin config show
/market-admin config edit               # cap, salaries, contract length bounds

# First valuation snapshot per tier (baselines every driver at min_salary)
/market-admin valuation run tier: t1 round_label: "Pre-season baseline"
/market-admin valuation publish run_id: <printed above>

# Live market boards in a public channel
/market-admin board add kind: Market table channel: #market tier: t1
/market-admin board add kind: Movers channel: #market tier: t1
/market-admin board add kind: Cross-tier dashboard channel: #market
```

### Spending cap vs. team budget (Phase 7)

Two different numbers gate every signing and trade:

| | Spending cap | Team budget |
|---|---|---|
| What it is | League rule: the most payroll any team may commit | The team's own money for the season |
| Where it lives | `league_config.salary_cap` (F1 preset: $145M) | `SUM(team_budget_ledger.amount)` per team per season |
| Same for every team? | Yes | No — it moves with results, prize money, and rollover |
| Can a team exceed it? | Never (payroll ≤ cap) | A team may *hold* more than the cap; it may only *spend* up to the cap |
| Failure code on an offer | `cap_exceeded` | `budget_exceeded` |

Rule of thumb: **budget is how much you have, cap is how much you are
allowed to spend.** A team with $300M in the bank and a $145M cap can
still only commit $145M of payroll. A team with $40M in the bank and a
$145M cap can only commit $40M.

**How a budget moves.** Every change is a ledger row with a kind and an
actor; nothing is overwritten.

| Kind | Sign | Written by |
|---|---|---|
| `opening_balance` | credit | automatically, once per team per season, the first time the budget is touched |
| `rollover` | either | `/market-admin budget rollover` — last season's balance minus committed payroll |
| `prize_money` | credit | `/market-admin budget award` |
| `race_earnings` | credit | results import — championship points × `per_point_m` |
| `dnf_penalty` | debit | results import — one per DNF |
| `dns_penalty` | debit | results import — one per no-show |
| `incident_penalty` | debit | results import — incident points × `per_incident_pt_m` |
| `adjustment` | either | `/market-admin budget adjust` |

> `race_earnings` here is **team** prize money for championship points.
> It is unrelated to a driver's career earnings, which live in their own
> ledger and never touch a team budget — see
> [Driver career earnings](#driver-career-earnings-phase-10).

Charges land on the team the driver is **contracted to at import time**.
A free agent's DNF is reported in the import receipt but charges nobody.
Re-importing a round is safe: the bot compares what the round *should*
have charged against what it *did* charge and writes only the difference
as flagged correction rows, so a stewards' revision never double-bills.

**Commands** (Manage Server):

| Command | Description |
|---|---|
| `/market-admin budget show <team>` | Balance, committed payroll, available-to-spend, and which limit (cap or budget) currently binds, plus the last 8 ledger rows |
| `/market-admin budget award <team> <amount_m> <note>` | Credit prize money (positive only, note required) |
| `/market-admin budget adjust <team> <delta_m> <note>` | Manual correction, either sign, note required |
| `/market-admin budget rollover <from_season>` | Carry every team's unspent budget from a past season into the active season; idempotent — a second run is a no-op |
| `/market-admin budget config [tier] [enforce] [rollover] [opening_m] [per_point_m] [dnf_m] [dns_m] [per_incident_pt_m]` | Show the active season's rules, or set them; passing `tier` writes a per-tier override that falls back to the season default for anything you leave blank |

**Starting Season 8 with unequal budgets.** Every team opens at the
configured `opening_m` (default equals the cap, so day-one behaviour is
unchanged). To reward last season's standings, award prize money before
free agency opens:

```text
/market-admin budget award team: mclaren amount_m: 25 note: "S7 constructors P1"
/market-admin budget award team: haas    amount_m: 5  note: "S7 constructors P10"
```

**Turning it off.** `/market-admin budget config enforce: false` keeps
the ledger but stops budgets from blocking signings and trades and stops
results imports from writing charges. A season with no `budget_config`
row at all behaves exactly as before Phase 7.

> The preset rates (`per_point_m` 0.05, `dnf_m` 0.50, `dns_m` 1.00,
> `per_incident_pt_m` 0.25) are starting points, not tuned values. Check
> them against a full season of your results before relying on them.

### Contract carry-over between seasons (Phase 8)

A contract's `term_seasons` is a real commitment: a 3-season deal signed
in Season 7 is owed in Seasons 8 and 9 too. The schema stores **one
`contracts` row per season served**, linked by `carried_from_contract_id`
→ `origin_contract_id` and numbered by `season_index` (1-based). `/contract
status` shows this as "Term: season 2 of 3".

**Season-end procedure** (after `season activate` on the new season):

```text
/market-admin season carry-over from_season: "S7"
/market-admin budget rollover    from_season: "S7"
/market-admin budget award ...                       # prize money
```

The order of the first two does not matter — budget rollover reads the
payroll the source season actually carried, not the live active rows.

What `carry-over` does with each active contract in the source season:

| Situation | Result |
|---|---|
| `season_index < term_seasons` | A new `active` row in the target season with the **same** value, type, term, and max incentives (`signing_bonus` 0 — it was a one-time payment on the origin row). Old row → `carried`. Ledger: `contract_carried` in both seasons. |
| `season_index = term_seasons` | Old row → `expired`; the driver is a free agent and their team role is removed. Ledger: `contract_expired`. |
| Driver already has a fresh active deal in the target season | **Skipped**, old row left `active`, listed under "Needs attention". |
| Target season has no tier with the same code | **Skipped**, same treatment. |

The driver's target-season row is reused if a commissioner already
created it (even in a different tier — the contract follows the driver),
otherwise it is created in the same tier code.

**The cap is reported, not enforced, here.** Carried money is an existing
obligation, so the run never blocks; any team whose target-season payroll
lands over the spending cap is called out in the receipt for the
commissioner to resolve through a release, buyout, or trade before that
team signs anyone new.

**Payroll has a season boundary.** Live payroll
(`fetch_team_payroll`, the signing rule's input) reflects only current
obligations, because carry-over is what moves a finished contract out of
`active`.

On a **fresh install there is nothing to carry over** — your first
season in the bot has no prior season to inherit from, so you will not
touch `carry-over` until the end of it. The only case that needs a
catch-up run is a bot that was already importing races across a season
boundary before this feature existed; then run `carry-over` once per
past season, oldest first.

An **extension** starts a new term: `update_contract_terms` resets
`season_index` to 1 alongside the new `term_seasons`.

### Driver career earnings (Phase 10)

Drivers **keep** what they are paid. Every time you import a race, each
driver under contract is credited one race's share of their salary —
the same figure the team side is charged — and that amount is added to a
lifetime total that never resets.

```text
/league  →  Market  →  "Career earnings leaderboard"
```

or `/market earnings`. `/market my-earnings` shows one driver their
career total, their total this season, and their recent pay lines. A
driver's career total also appears on their driver card.

**This takes nothing extra from any team.** Career earnings are a
record of what a driver has been paid, not a second transfer of money.
Team budgets, the spending cap, escrow and every P/L figure behave
exactly as they did before this feature existed. Nothing in the bot
lets a driver spend the total — it is a leaderboard number for now, and
it is stored as real money so it can become spendable later without a
migration.

**It works with escrow on or off.** A driver earned their race salary
whether or not your league models team cash, so earnings are credited on
a separate pass and do not follow the `escrow_enabled` switch. A league
that has never turned escrow on still builds a full leaderboard.

**It carries forward by itself.** A driver's identity for earnings is
their Discord account, not their driver row, so a new season, a new
tier, or a new team changes nothing. There is no carry-over step to run
and nothing to remember at the season boundary. Deleting an old season
does not erase career history either — the season link is provenance
only, and it is cleared rather than cascaded.

**Pay is per round, not per finish.** Every active contract in the tier
is credited once per imported round, including a driver who did not show
up, because that mirrors what the team is charged. If you would rather
dock a no-show, use `/market-admin earnings adjust` with a negative
amount — it is audit-logged with your reason.

#### Optional: seeding seasons you raced before the bot

On a fresh install every driver starts at **$0.00M** and the leaderboard
fills in from your first imported race. That is the clean default and it
needs no setup at all.

If your league raced seasons before the bot existed and you want that
history on the leaderboard, you can seed each driver an opening total by
hand. This is entirely optional and skipping it costs you nothing but
historical flavour.

| Command | What it does |
|---|---|
| `/market-admin earnings carry-in <@member> <amount_m> <note>` | Sets a driver's opening career total from your old records |
| `/market-admin earnings adjust <@member> <delta_m> <note>` | Corrects a total, either direction |

Both require a reason, both are written to the ledger with your user id
against them, and both are additive — a carry-in does not overwrite
anything, so a driver who has already been paid this season keeps
accruing on top of the figure you seed.

> **Verify before you seed.** The bot has no way to know what your
> league paid in earlier seasons, so a carry-in figure is only as good
> as the spreadsheet you take it from. Check it against your own records
> before entering it; correcting it later is possible but the ledger
> keeps both rows.

### The weekly race-night loop

```text
/league  →  Race Night  →  pick tier  →  paste sheet URL  →  Publish
```

Or by command, per tier:

```text
/market-admin results import tier: t1 round_label: "R14 Abu Dhabi" sheet: <url>
/market-admin valuation run tier: t1 round_label: "R14 Abu Dhabi"
/market-admin valuation publish run_id: <printed above>
```

Both routes run identical code — the panel calls the same workflow
functions the commands do, so a race night imported through the panel is
indistinguishable from one imported by hand.

## Team setup flow

`/roster create` walks through 7 steps:

1. **Name / tagline / logo URL** — display info for the embed header
2. **Team role** — the Discord role that marks someone as on this team
3. **Principal role** *(optional)* — a role whose members can sign/drop players for this team without needing Manage Server
4. **Staff slots** — add as many labelled staff slots as needed (each has a label, quantity, and role)
5. **Driver slots** — same as staff
6. **Channel** — the text channel where the roster embed is posted
7. **Confirm** — preview the embed and post it

`/roster edit <name>` reopens the same flow pre-filled with existing values.

## Google Sheets access

Two features read Google Sheets: the `/sheets` stat boards and `/market-admin results import`.
Neither works until the bot has credentials. Set up a **service account** — it is the only
option that works with a private sheet, and your results sheet should be private.

### 1. Create the service account

1. Open the [Google Cloud console](https://console.cloud.google.com/) and create a project
   (or reuse one).
2. Enable the Google Sheets API:
   **APIs & Services → Library → "Google Sheets API" → Enable**.
3. **APIs & Services → Credentials → Create credentials → Service account**.
   Give it a name like `roster-bot-sheets`. No project roles are needed — the account gets
   its access from the sheet share in section 3 below, not from IAM.
4. Open the new service account → **Keys → Add key → Create new key → JSON**. A `.json`
   file downloads. This is a secret; treat it like the bot token.
5. Copy the service account's email address from the **Details** tab. It looks like
   `roster-bot-sheets@your-project.iam.gserviceaccount.com`.

### 2. Point the bot at the key

In `.env`, either give a path to the file:

```bash
GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/service-account.json
```

or paste the whole JSON as a single line, which is easier on hosts that only offer
environment-variable secrets:

```bash
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account","project_id":"...","private_key":"..."}
```

The bot accepts both and detects which it was given.

> **Docker: a host path will not resolve inside the container.**
> `docker-compose.yml` passes `.env` through with `env_file`, but the container has its own
> filesystem. If you use the path form, uncomment the `volumes` block on the `roster-bot`
> service to mount the key read-only, and set the variable to the **container** path:
>
> ```yaml
> volumes:
>   - ./service-account.json:/run/secrets/service-account.json:ro
> ```
> ```bash
> GOOGLE_SERVICE_ACCOUNT_JSON=/run/secrets/service-account.json
> ```
>
> Pasting the raw JSON instead avoids the mount entirely.

Add the key file to `.gitignore` if you keep it in the repo directory.

### 3. Share each sheet with the service account

The service account is a separate Google identity. It cannot see anything until you share it in.

Open the spreadsheet → **Share** → paste the service account email → **Viewer** → Send.
Untick "Notify people"; the address cannot receive mail.

Viewer is sufficient and correct. The bot only ever reads — results are pulled into Postgres
and all valuation happens there, so nothing is written back.

Repeat for every sheet the bot reads, including each tier's results sheet.

### Alternative: API key

```bash
GOOGLE_SHEETS_API_KEY=AIza...
```

Simpler, but it only works on sheets published to anyone with the link. That means your
results sheet is world-readable, and anyone who finds the URL can see it before you import.
Acceptable for public stat boards, a poor fit for results. If both variables are set, the
service account wins.

### Verifying it works

Run a `/sheets` board or a results import. Failures name the cause:

| Message | Cause |
|---|---|
| `No Google Sheets credentials configured` | Neither variable is set, or the container never received `.env` |
| `Access denied (403)` | The sheet is not shared with the service account email, or the API key is being used on a private sheet |
| `Sheet or range not found (404)` | Bad spreadsheet ID, or a `tab` name that does not exist — check spelling and spaces |
| `Network error fetching sheet` | Egress blocked, or the Sheets API is not enabled on the project |

A 403 immediately after setup is almost always a missed section 3 — the key is valid, the
sheet just was not shared with it.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Bot token |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `DATABASE_URL` | `postgresql://roster:roster@postgres:5432/roster` (set by compose) | Postgres connection string |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `roster` | Postgres credentials (compose only) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | *(unset)* | Path to the service account JSON key, **or** the raw JSON itself. Required for `/sheets` and `results import`. See [Google Sheets access](#google-sheets-access). |
| `GOOGLE_SHEETS_API_KEY` | *(unset)* | Fallback for publicly shared sheets only. Ignored when a service account is set. |

## Architecture notes

- **No members table** — source of truth is Discord. Member lists are queried live from `guild.members` at render time (requires Members Intent and member caching).
- **Render is idempotent** — `build_embed` edits the stored message in place. If the message was deleted, `message_id` is cleared and `/roster list` flags the team as broken.
- **Two update triggers**: `on_member_update` (event-driven, immediate) + 15-min poll (safety net for missed events).
- **In-flight flow state** lives in memory, keyed by `(guild_id, user_id)`. A bot restart abandons any in-progress create/edit flows.
- **Postgres 16** via asyncpg with a connection pool; migrations run automatically on startup from the `migrations/` directory. Historical SQLite migration files (001–012) are archived under `migrations/sqlite/` for reference; the live PG schema is the single baseline `001_init.sql`.
