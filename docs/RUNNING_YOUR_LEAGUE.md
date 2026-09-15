# Running your league — the owner's runbook

This is the operator guide. It takes you from an empty server to a
running driver market, then through a race night, then through an
offseason, in the order you actually do them.

It assumes nothing about the code. If you want the reference manual
instead, read [`roster-bot/README.md`](../roster-bot/README.md); for the
driver- and Team-Principal-facing walkthrough, hand people
[`MARKET_GUIDE.md`](MARKET_GUIDE.md).

**The one thing to remember:** run `/league`. It is a checklist that
fills itself in and tells you the single next step. Every slash command
in this guide is also a button in there. Type commands when you want
precision; press buttons when you want speed.

---

## Contents

1. [What this system actually is](#1-what-this-system-actually-is)
2. [Before you start](#2-before-you-start)
3. [Install and deploy](#3-install-and-deploy)
4. [Google Sheets access](#4-google-sheets-access)
5. [Discord structure: roles and channels](#5-discord-structure-roles-and-channels)
6. [Day one: create the league](#6-day-one-create-the-league)
7. [Set the money rules](#7-set-the-money-rules)
8. [Get your drivers in](#8-get-your-drivers-in)
9. [The first valuation](#9-the-first-valuation)
10. [Opening the market](#10-opening-the-market)
11. [Race night, every week](#11-race-night-every-week)
12. [The offseason](#12-the-offseason)
13. [Onboarding a league that already has history](#13-onboarding-a-league-that-already-has-history)
14. [Day-to-day admin jobs](#14-day-to-day-admin-jobs)
15. [When something looks wrong](#15-when-something-looks-wrong)
16. [Things to decide before you launch](#16-things-to-decide-before-you-launch)

---

## 1. What this system actually is

Three layers that share one database:

| Layer | What it does | Who uses it |
|---|---|---|
| **Rosters** | Live, self-updating team roster embeds driven by Discord roles | Everyone, passively |
| **Market** | Every driver has a money value, recalculated from race results | Team Principals |
| **Contracts** | Offers, negotiation, approval, trades, releases, buyouts, multi-season deals | TPs, drivers, you |

Roles are the source of truth for who is on a team. The bot watches role
changes and repairs itself every 15 minutes, so a role change made by
hand is never "invisible" to it.

Two numbers govern spending, and mixing them up is the most common
misunderstanding in the whole system:

| | **Spending cap** | **Team budget** |
|---|---|---|
| Meaning | League rule: the most payroll a team may commit | That team's own money |
| Same for everyone? | Yes | No |
| Moves? | Only if you change the rule | Constantly — results, prize money, rollover |
| Can a team exceed it? | Never | It may *hold* more than the cap; it may only *spend* up to the cap |

**Budget is what you have. Cap is what you're allowed to spend.** A team
sitting on $300M with a $145M cap can still only commit $145M of
payroll. A team with $40M can only commit $40M even though the cap is
$145M.

---

## 2. Before you start

- A machine that stays on — a $6/month VPS is plenty. Docker + Docker
  Compose installed.
- A Discord server where you have **Manage Server**.
- A Google account, if you want results imported from Sheets (strongly
  recommended — typing results by hand does not scale past one tier).
- 45 minutes for first-time setup. The per-week running cost after that
  is about five minutes per tier.

Decide these now, because they shape everything else:

- **How many tiers?** Each tier is its own isolated market — a Tier 2
  driver mathematically cannot move a Tier 1 value.
- **What is the spending cap?** The preset is $145M. Keep it unless you
  have a reason.
- **Do teams get their own budgets?** You can run cap-only (simpler) or
  cap + budget (teams earn and lose money). See §7.

---

## 3. Install and deploy

```sh
git clone https://github.com/mastermarvalo/intrepidracing.git
cd intrepidracing/roster-bot
cp .env.example .env
```

Create the Discord application:

1. Go to <https://discord.com/developers/applications> → **New
   Application**.
2. **Bot** → copy the token into `DISCORD_TOKEN` in `.env`.
3. **Bot → Privileged Gateway Intents** → enable **Server Members
   Intent**. The bot cannot see role changes without it and will do
   nothing useful.
4. **OAuth2 → URL Generator** → scopes `bot` and
   `applications.commands`; permissions: View Channels, Send Messages,
   Embed Links, Manage Roles, Manage Messages. Open the generated URL
   and invite it.
5. **Server Settings → Roles**: drag the bot's role **above every team
   role and every tier role.** Discord will not let it assign a role
   that sits above its own. This is the single most common setup
   failure.

Start it:

```sh
make up      # build and start, detached
make logs    # follow the logs — watch for "migrations applied" and "logged in as"
```

Postgres runs as its own service; data lives in the `postgres-data`
volume and survives rebuilds. Schema migrations apply automatically at
startup, so **upgrading is just `git pull && make restart`.** Read
[`roster-bot/MIGRATION.md`](../roster-bot/MIGRATION.md) after pulling —
it lists anything that changes behaviour, in reverse order.

Useful:

```sh
make restart   # rebuild and redeploy after a code change
make down      # stop
make logs      # follow
```

In Discord, type `/league`. If the command doesn't appear, give Discord
a minute to sync, then reload the client.

---

## 4. Google Sheets access

Needed for `/market-admin results import` and `/sheets` boards. Use a
**service account** — API keys only work on publicly shared sheets.

1. <https://console.cloud.google.com> → new project.
2. Enable the **Google Sheets API**.
3. **Credentials → Create credentials → Service account.** Create a JSON
   key and download it.
4. Either point `GOOGLE_SERVICE_ACCOUNT_JSON` at the file (the path must
   exist *inside* the container — uncomment the volumes block in
   `docker-compose.yml` and use `/run/secrets/service-account.json`), or
   paste the whole JSON as a single line into that variable.
5. Open your results spreadsheet → **Share** → paste the service
   account's `client_email` → **Viewer**. Do this for every sheet you
   intend to import. A sheet you forgot to share is the cause of
   essentially every "permission denied" on import.

Your results sheet needs these columns (extra columns are ignored, order
does not matter, header spelling does):

```
Driver,Pos,Grid,DNF,DNS,FL,DOTD,Incidents,Notes
ZeezinDomar,1,1,,,Y,,0,
CostaCoffee,4,5,,,,Y,1,Track limits warning
OfflineKids,DNF,2,,,,,2,Contact at turn 1
```

`Driver` must match the driver's Discord display name or their enrolled
name. `Pos` accepts `DNF`/`DNS` in place of a number. See
[`roster-bot/docs/results_template.csv`](../roster-bot/docs/results_template.csv).

---

## 5. Discord structure: roles and channels

Create these before you touch the bot. Names are yours; the bot learns
them from pickers, so you never type an id.

**Roles**

- One per team (`Mercedes`, `McLaren`, …). These drive rosters and are
  assigned by the bot on signing.
- One per tier (`Tier 1`, `Tier 2`, `Tier 3`) — optional but recommended.
- One **Team Principal** role per team, or one shared TP role. TPs can
  run `/roster sign` and `/roster drop` for their own team and make
  contract offers.
- One **Commissioner** role, if that isn't just you.

**Channels**

| Channel | Purpose |
|---|---|
| `#market` | Public, read-only: auto-updating market tables, movers, dashboard |
| `#transactions` | Public, read-only: every signing, trade, release — the audit feed |
| `#approvals` | Private to commissioners: pending offers and trades land here as threads |

Deny **Send Messages** to everyone in `#market` and `#transactions`;
they are bot output. Make sure the bot has **Send Messages** and **Embed
Links** in all three — a board with no message is almost always a
missing permission.

---

## 6. Day one: create the league

Press **Setup** in `/league` and work down the checklist, or type:

```text
/market-admin season create name: "Season 8" preset: F1 25/26
/market-admin season activate name: "Season 8"
```

The `F1 25/26` preset seeds three tiers (`t1`, `t2`, `t3`), driver
statuses, contract types and states, the valuation factors, the points
table, and a default league config with a $145M cap. Creating a season
without the preset gives you an empty shell you'd have to configure by
hand — use the preset.

Name tiers and attach their roles and colours:

```text
/market-admin tier edit code: t1 label: "Tier 1" rank_order: 1 role: @Tier-1
/market-admin tier edit code: t2 label: "Tier 2" rank_order: 2 role: @Tier-2
/market-admin tier edit code: t3 label: "Tier 3" rank_order: 3 role: @Tier-3
```

`rank_order` 1 is the top tier; promotion and relegation read it. Need a
fourth tier? `/market-admin tier add`.

Point the bot at your channels and commissioner role:

```text
/market-admin config channel kind: Market        channel: #market
/market-admin config channel kind: Transactions  channel: #transactions
/market-admin config channel kind: Approvals     channel: #approvals
/market-admin config role role: @Commissioner
```

Create your teams (this is the roster layer):

```text
/roster create name: Mercedes
```

It's a guided flow — it asks for the team role and the channel to post
the roster embed in. Repeat per team. `/roster list` to check.

---

## 7. Set the money rules

```text
/market-admin config show
/market-admin config edit
```

`config edit` opens two modals: money limits, and contract rules
(minimum and maximum contract length, minimum salary, maximum incentive
percentage). The preset defaults are a $145M cap and a $1M minimum
salary.

Then decide on budgets:

```text
/market-admin budget config
```

| Setting | Meaning |
|---|---|
| `enforce` | Off = cap-only league, budgets are recorded but never block anything. On = a team cannot commit payroll above its own money |
| `rollover` | Whether unspent money survives into next season |
| `opening_m` | What each team starts a season with (preset: $145M, i.e. exactly the cap) |
| `per_point_m` | Credit per championship point earned |
| `dnf_m` | Debit per DNF |
| `dns_m` | Debit per no-show |
| `per_incident_pt_m` | Debit per incident point |

> **These rates are placeholders, not tuned values.** `per_point_m 0.05`,
> `dnf_m 0.50`, `dns_m 1.00`, `per_incident_pt_m 0.25` were sized so a
> full season moves a budget by single-digit millions against a $145M
> cap. **Run one season with `enforce: false` and watch the numbers
> before you let them block signings.** Nobody has calibrated them
> against your points system.

A season with no budget config at all behaves exactly like a cap-only
league. Turning budgets on later is one command; you lose nothing by
waiting.

---

## 8. Get your drivers in

Roles first, bot second. Give people their tier role, then:

```text
/market-admin driver sync-all
```

This reads the tier roles and enrols everyone who holds one as a driver
in that tier, skipping anyone already enrolled. Run it again any time
people join — it is safe to repeat and only ever adds.

One person at a time, or someone who needs a specific status:

```text
/market-admin driver add member: @Zeezin tier: t1 status: active
```

Statuses matter. The six are `active`, `reserve`, `free_agent`,
`restricted_fa`, `inactive`, and `suspended` — a `restricted_fa` is a
free agent their old team retains rights over, which is how you
implement a qualifying-offer rule if you want one.
`/market-admin set-status` changes a status later and writes a ledger
note, so status history is auditable.

A driver can legitimately exist in two tiers — a Tier 1 reserve who
races Tier 2 is two separate driver records with two independent market
values. That is deliberate. Don't try to merge them.

---

## 9. The first valuation

Every driver needs a starting value before anyone can be offered a
contract. With no results imported, this baselines everybody at the
minimum salary:

```text
/market-admin valuation run tier: t1 round_label: "Pre-season baseline"
/market-admin valuation preview run_id: <the id it printed>
/market-admin valuation publish run_id: <same id>
```

Repeat per tier. A run is priced but invisible until you **publish** it —
preview is your chance to reject a bad import before drivers see it.

Then put the public boards up:

```text
/market-admin board add kind: Market table        channel: #market tier: t1
/market-admin board add kind: Movers              channel: #market tier: t1
/market-admin board add kind: Cross-tier dashboard channel: #market
```

These refresh themselves every 15 minutes. Tier boards need a tier; the
cross-tier dashboard must not have one.

---

## 10. Opening the market

```text
/market-admin config free-agency state: open
```

While free agency is **closed**, the only offers allowed are extensions
from a driver's own team. That's the lever you use during a season to
stop mid-season poaching, and the one you open in the offseason.

The flow from here is your TPs' job, not yours:

1. TP runs `/contract offer team: … tier: … driver: @… offer_kind: … ttl_hours: 48`
   and fills in salary, term, bonus, incentives.
2. The bot validates **before** the driver ever sees it: TP authority,
   tier eligibility, free-agency window, contract length bounds,
   minimum salary, cap headroom, and budget if enforced. An invalid
   offer is refused with a specific reason.
3. The driver gets the offer privately and can **accept**, **decline**,
   or **counter**. They cannot edit the money inside acceptance — that
   protects the audit trail.
4. An accepted offer lands in your `#approvals` queue.
5. You approve it. The contract commits, the Discord team role is
   assigned, and `#transactions` gets a receipt.

Your side is just:

```text
/league  → Approvals     (the queue, with terms, oldest first)
```

or `/market-admin approve <offer_id>` / `/market-admin reject <offer_id> [note]`.

Approving is the moment money becomes real. Everything before it is a
proposal.

---

## 11. Race night, every week

Press **Race Night** in `/league` — pick a tier, paste the sheet URL in
the popup, import, price, review, publish. It remembers each tier's URL
for the session, so a three-tier night is three pastes, not twelve.

The typed equivalent, per tier:

```text
/market-admin results import tier: t1 round_label: "R1 Bahrain" sheet: <url>
/market-admin valuation run tier: t1 round_label: "R1 Bahrain"
/market-admin valuation preview run_id: <id>
/market-admin valuation publish run_id: <id>
```

What the import does beyond recording finishes: it credits race earnings
per championship point and debits DNFs, no-shows, and incident points
against **the team each driver was contracted to at import time.** A
free agent's DNF is reported in the receipt and charges nobody.

**Re-importing a round after a stewards' decision is safe.** The bot
compares what the round *should* have charged against what it *did*
charge and writes only the difference, flagged as a correction. It never
double-bills and it never deletes history. Just import the corrected
sheet over the top.

Order matters in one place: **import results before running the
valuation.** A valuation run prices whatever is in the database at that
moment, so running it first gives you last week's numbers with this
week's label.

---

## 12. The offseason

This is the part most leagues get wrong, so it's a strict order.

### Step 1 — close the market

```text
/market-admin config free-agency state: closed
```

### Step 2 — create and activate the new season

```text
/market-admin season create name: "Season 9" preset: F1 25/26
/market-admin season activate name: "Season 9"
```

### Step 3 — carry contracts over

```text
/market-admin season carry-over from_season: "Season 8"
```

A contract's term is a real commitment. A 3-season deal signed in Season
8 is owed in Seasons 9 and 10 too, and this is the command that honours
it. For every active contract in the old season:

| Situation | What happens |
|---|---|
| The deal has seasons left | A new contract in the new season at **exactly the same money, term, type and incentives**. The driver keeps their team role. No signing bonus — that was paid once, at the original signing |
| The deal's final season is done | It expires. The driver becomes a free agent and their team role is removed |
| The driver already signed a fresh deal in the new season | Skipped and listed under "Needs attention" — you decide |
| The new season has no tier with that code | Skipped and listed the same way |

The receipt shows every line, plus any team whose payroll now exceeds
the spending cap. **Carried money is not blocked** — it's an obligation
you already agreed to. But a team over the cap cannot sign anyone until
you fix it with a release, buyout, or trade.

Re-running the command is safe: it only touches contracts still active
in the old season, so a second run does nothing.

`/contract status @driver` now reads "Term: season 2 of 3" so everyone
can see where a deal stands.

### Step 4 — roll the money over

```text
/market-admin budget rollover from_season: "Season 8"
```

Each team carries forward what it didn't spend, and gets its opening
balance for the new season. Idempotent — a second run is a no-op.

Steps 3 and 4 can be run in either order; they're built not to interfere.

### Step 5 — award prize money

```text
/market-admin budget award team: mclaren amount_m: 25 note: "S8 constructors P1"
/market-admin budget award team: haas    amount_m: 5  note: "S8 constructors P10"
```

This is where you reward last season's performance. It's a deliberate
manual step — you decide the payout curve, the bot doesn't assume one.

### Step 6 — sync drivers, baseline values, reopen

```text
/market-admin driver sync-all
/market-admin valuation run tier: t1 round_label: "S9 pre-season"
/market-admin valuation publish run_id: <id>
/market-admin config free-agency state: open
```

Then check `/market-admin budget show` for a few teams and
`/market team <name>` to confirm payrolls look like you expect before
the market opens.

---

## 13. Onboarding a league that already has history

The realistic case: your server has been running for seasons under
spreadsheets and vibes, and you're adding the bot now. You do **not**
need to backfill history. Two honest options:

### Option A — clean slate, rewarded teams (recommended)

Everyone starts at the same baseline value; last season's performance is
recognised as **team money**, not as inflated driver prices.

```text
/market-admin season create name: "Season 8" preset: F1 25/26
/market-admin season activate name: "Season 8"
# tiers, channels, roles, teams as in §6
/market-admin driver sync-all
/market-admin valuation run tier: t1 round_label: "S8 baseline"     # everyone at minimum salary
/market-admin valuation publish run_id: <id>
/market-admin budget award team: <last season's champion> amount_m: 30 note: "S7 constructors P1"
# … down the order
/market-admin config free-agency state: open
```

Why this is the better option: driver values become meaningful after
two or three imported rounds anyway, and nobody can argue with a clean
baseline. Arguments about a hand-assigned $20M valuation will consume
your entire preseason.

### Option B — retro-import last season

Import Season 7's rounds into a Season 7 record before creating Season
8. More faithful, considerably more work, and it needs your old results
in the sheet format from §4. Only worth it if you have the sheets
already and want carried driver values from day one.

Either way: **existing contracts have to be entered as new contracts.**
There's no import path for a deal that was agreed in a spreadsheet. Have
each TP make the offer at the agreed money and term, approve them in a
batch, and note the term honestly — a driver who has two years left
should be offered a 2-season deal so carry-over does the right thing
next offseason.

> If your league runs the bot across a season boundary *without* running
> `season carry-over`, old contracts stay active and keep counting
> against payroll forever. If you find yourself in that state, run the
> command once per past season, oldest first.

---

## 14. Day-to-day admin jobs

| You want to | Command |
|---|---|
| See what state the league is in | `/league` |
| Find any command | `/help` |
| Approve or reject pending offers and trades | `/league` → Approvals |
| Check a team's payroll and cap room | `/market team <name>` |
| Check a team's money | `/market-admin budget show <team>` |
| See a driver's value, contract and offer history | `/contract status @driver` |
| End a contract with cause | `/market-admin void <contract_id> [note]` |
| Release a driver (P/L recorded) | `/contract release <contract_id> <note>` |
| Buy a driver out (dead money against the cap) | `/contract buyout <contract_id> <buyout_m> <note>` |
| Move a driver up or down a tier, contract included | `/market-admin promote` / `relegate` |
| Correct a team's money | `/market-admin budget adjust <team> <delta_m> <note>` |
| Log a cap note that moves no money | `/market-admin adjust-cap <team> <delta_m> <note>` |
| Repost or fix the market boards | `/league` → Boards, or `/market-admin board refresh` |
| Stop mid-season poaching | `/market-admin config free-agency state: closed` |

`budget adjust` moves money. `adjust-cap` only writes an audit note.
They are not interchangeable.

---

## 15. When something looks wrong

**The bot didn't assign a team role.** Its role is below the team role.
Server Settings → Roles, drag the bot up. The contract still committed —
role failures are reported, never rolled back. Fix the hierarchy and
assign the role by hand once.

**A board is empty or missing.** The bot lacks **Send Messages** or
**Embed Links** in that channel. `/league` → Boards flags boards with no
message.

**Results import says permission denied.** The sheet isn't shared with
the service account email. §4, step 5.

**A driver's name didn't match on import.** The `Driver` column must
match their Discord display name or enrolled name. Fix the sheet and
re-import the same round — corrections are safe.

**An offer was refused and the TP doesn't know why.** The refusal names
the reason: `cap_exceeded` (league rule), `budget_exceeded` (that
team's own money), free agency closed, term outside bounds, salary below
minimum. `/market-admin config show` shows the current rules.

**A team is over the cap after carry-over.** Expected and allowed —
those are existing obligations. They cannot sign anyone until they're
back under, via release, buyout, or trade.

**Payroll includes drivers from a season that's over.** You haven't run
`/market-admin season carry-over` for that season. §12, step 3.

**Panel buttons do nothing.** If you're on a build from before the
Phase 8 update, panel clicks failed silently against current
discord.py. `git pull && make restart`.

**Commands don't appear in Discord.** Give it a minute, then reload your
client. Check `make logs` for `logged in as`.

Nothing in this system deletes money history. Every movement is an
append-only ledger row with a kind, an amount, and who did it. If a
number looks wrong, it can be traced, and corrections are additional
rows rather than edits.

---

## 16. Things to decide before you launch

Write these down and post them where your league can read them. The bot
will enforce whatever you configure; it can't referee an argument about
rules you never published.

- **Cap.** $145M unless you have a reason.
- **Contract lengths.** Minimum and maximum seasons. Longer terms make
  carry-over meaningful and the market more interesting; they also mean
  a bad signing hurts for years, which is the point.
- **Budgets on or off for season one.** Off is a legitimate answer.
- **Your prize-money curve.** What does a constructors' title pay versus
  last place? You award this by hand each offseason, so decide the curve
  in advance and publish it.
- **Whether penalty points and DNFs cost teams money,** and how much.
  Start with the preset rates, run a season with enforcement off, then
  calibrate.
- **Whether incentives pay out.** `max_incentives` is validated on every
  contract, but nothing pays it. If you want win bonuses to be real
  money, that's a manual `budget adjust` today, and a league rule you
  need to write down.
- **Who approves what.** Contracts and trades both need a commissioner.
  If that's only you, you are a bottleneck on race night; give a second
  person the commissioner role.
