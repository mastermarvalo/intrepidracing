# Running your league — the owner's runbook

**Version 2.** Covers the race-term and escrow economics.

This is the operator guide. It takes you from an empty server to a
running driver market, then through a race night, then through the end
of a contract, in the order you actually do them.

It assumes nothing about the code. For a shorter start-to-finish
version read [`README.md`](../README.md); for the full command
reference read [`roster-bot/README.md`](../roster-bot/README.md); for
the driver- and Team-Principal-facing walkthrough, hand people
[`MARKET_GUIDE.md`](MARKET_GUIDE.md).

> **Everything in this guide is live.** It previously carried "new"
> marks against the race-term and escrow sections, which were then
> unreleased. That update has shipped and merged, so the marks are gone
> — if it is described here, it is in the build. Where a rule replaced
> an older one the old behaviour is still described, because a league
> mid-season may remember it.
>
> Shorter version of this guide, for first-time setup:
> [`README.md`](../README.md) at the repository root.

**The one thing to remember:** run `/league`. It is a checklist that
fills itself in and tells you the single next step. Every slash command
in this guide is also a button in there. Type commands when you want
precision; press buttons when you want speed.

---

## Contents

1. [The three numbers that govern money](#1-the-three-numbers-that-govern-money)
2. [Before you start](#2-before-you-start)
3. [Install and deploy](#3-install-and-deploy)
4. [Google Sheets access](#4-google-sheets-access)
5. [Discord structure: roles and channels](#5-discord-structure-roles-and-channels)
6. [Day one: create the league](#6-day-one-create-the-league)
7. [Set the money rules](#7-set-the-money-rules)
8. [Contract length, in races](#8-contract-length-in-races)
9. [Pricing: base value and the two premiums](#9-pricing-base-value-and-the-two-premiums)
10. [Get your drivers in](#10-get-your-drivers-in)
11. [The first valuation](#11-the-first-valuation)
12. [Opening the market](#12-opening-the-market)
13. [Race night, every week](#13-race-night-every-week)
14. [When a contract ends](#14-when-a-contract-ends)
15. [The offseason](#15-the-offseason)
16. [Onboarding a league that already has history](#16-onboarding-a-league-that-already-has-history)
17. [Day-to-day admin jobs](#17-day-to-day-admin-jobs)
18. [When something looks wrong](#18-when-something-looks-wrong)
19. [Things to decide before you launch](#19-things-to-decide-before-you-launch)
20. [Driver career earnings](#20-driver-career-earnings)

---

## 1. The three numbers that govern money

Three layers share one database: **rosters** (live embeds driven by
Discord roles), the **market** (every driver has a value recalculated
from results), and **contracts** (offers, negotiation, approval, trades,
releases, buyouts, multi-race deals).

Roles are the source of truth for who is on a team. The bot watches role
changes and repairs itself every 15 minutes, so a role change made by
hand is never invisible to it.

Money is where owners get confused, so learn these three:

| | **Spending cap** | **Cash** | **Escrow** |
|---|---|---|---|
| What it is | A league rule | The team's money | Money locked inside live contracts |
| Same for every team? | Yes | No | No |
| Moves? | Only if you change the rule | Constantly | Grows race by race, returns at term end |
| Can a team exceed it? | Never | It may hold more than the cap; it may only commit up to the cap | n/a |

**The cap is what you're allowed to commit. Cash is what you can
actually pay. Escrow is what you've already paid out and will get back.**

A team sitting on $300M with a $145M cap can still only commit $145M of
payroll. A team with $40M of cash cannot escrow a contract it can't
fund, whatever the cap says.

###: what changed, and why it matters

Before this update a signing moved no money at all. Payroll was
*compared* against the budget balance and only subtracted at the season
rollover. Signing a driver cost a team nothing until the season ended.

Now **salary is escrowed as it is earned.** Each imported race debits
that race's share of the salary from the team's cash. When the term
finishes, the whole holding comes back **plus the driver's P/L**:

\[
\text{returned} = \text{total escrowed} + \bigl(\text{market value at settlement} - \text{contract value}\bigr)
\]

That is the same P/L your surplus and underwater boards already show:
market value minus contract value. So a driver who appreciates pays his
team back more than it escrowed, and one who doesn't costs the team the
difference. **This is the main way teams now make and lose money.**

### A worked example

Season is 24 races. Cap is $145M. McLaren signs a driver at
**$24.00M per season** on a **36-race** term.

| Quantity | Value | Where it comes from |
|---|---|---|
| Counts against the cap | $24.00M | The per-season salary, unchanged |
| Per-race escrow | $1.00M | $24.00M ÷ 24 races |
| Total escrowed over the term | $36.00M | $1.00M × 36 races |

The driver ends the term valued at **$27.50M**:

\[
\text{P/L} = \$27.50\text{M} - \$24.00\text{M} = +\$3.50\text{M}
\]
\[
\text{returned} = \$36.00\text{M} + \$3.50\text{M} = \$39.50\text{M}
\]

McLaren is $3.50M up. Had he fallen to $21.00M instead, P/L would be
−$3.00M, $33.00M would come back, and McLaren would be $3.00M down on a
driver it paid $36.00M to run.

**The salary figure is still per season and the cap still counts it at
$145M.** Races only decide how long a deal runs and how the cash is
paid out. Nothing about your cap arithmetic changes.

### Where cash comes from and goes

| Money in | Trigger |
|---|---|
| Opening balance | Auto-credited the first time a team is touched in a season (preset $145M) |
| Rollover | `/market-admin budget rollover` at the season boundary |
| Race earnings | Automatic on results import: rate × championship points |
| Prize money | `/market-admin budget award` — commissioner only, credit only |
| Escrow returned | Automatic when a term ends |
| Positive P/L | Automatic at settlement, when a driver appreciated |

| Money out | Trigger |
|---|---|
| Salary escrow | Automatic per imported race |
| Negative P/L | Automatic at settlement, when a driver declined |
| Retirement, no-show, incident points | Automatic on results import |
| Commissioner adjustment | `/market-admin budget adjust` |

Race earnings are charged to the team the driver was contracted to at
import time; a free agent's result is reported as unattributed and pays
nobody. A retirement earns no points, so a DNF can never also draw
earnings.

---

## 2. Before you start

- A machine that stays on — a $6/month VPS is plenty. Docker and Docker
  Compose installed.
- A Discord server where you have **Manage Server**.
- A Google account, if you want results imported from Sheets. Strongly
  recommended: typing results by hand does not scale past one tier, and
  escrow and contract terms both advance off imported results,
  so importing is now how the economy moves at all.
- 45 minutes for first-time setup; about five minutes per tier per week
  after that.

Decide these now, because they shape everything else:

- **How many tiers?** Each is its own isolated market — a Tier 2 driver
  mathematically cannot move a Tier 1 value.
- **What is the spending cap?** The preset is $145M.
- **How many races in a season?** This is the divisor that
  turns a per-season salary into a per-race payment, so it has to be
  right before anyone signs anything.
- **Do teams get their own cash?** You can run cap-only or cap + cash.

---

## 3. Install and deploy

```sh
git clone https://github.com/mastermarvalo/intrepidracing.git
cd intrepidracing/roster-bot
cp .env.example .env
```

Create the Discord application:

1. <https://discord.com/developers/applications> → **New Application**.
2. **Bot** → copy the token into `DISCORD_TOKEN` in `.env`.
3. **Bot → Privileged Gateway Intents** → enable **Server Members
   Intent**. Without it the bot cannot see role changes and does nothing
   useful.
4. **OAuth2 → URL Generator** → scopes `bot` and
   `applications.commands`; permissions View Channels, Send Messages,
   Embed Links, Manage Roles, Manage Messages. Open the URL, invite it.
5. **Server Settings → Roles**: drag the bot's role **above every team
   role and tier role.** Discord will not let it assign a role above its
   own. This is the single most common setup failure.

Start it:

```sh
make up      # build and start, detached
make logs    # watch for "migrations applied" and "logged in as"
```

Postgres runs as its own service; data lives in the `postgres-data`
volume and survives rebuilds. Migrations apply automatically at startup,
so **upgrading is `git pull && make restart`.** Read
[`roster-bot/MIGRATION.md`](../roster-bot/MIGRATION.md) after pulling —
it lists anything that changes behaviour, newest first.

```sh
make restart   # rebuild and redeploy after a code change
make down      # stop
make logs      # follow
```

In Discord, type `/league`. If it doesn't appear, give Discord a minute
and reload the client.

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
   paste the whole JSON as one line into that variable.
5. Open your results spreadsheet → **Share** → paste the service
   account's `client_email` → **Viewer**. Do this for every sheet you
   import. A sheet you forgot to share causes essentially every
   "permission denied".

Your results sheet needs these columns. Extra columns are ignored, order
does not matter, header spelling does:

```
Driver,Pos,Grid,DNF,DNS,FL,DOTD,Incidents,Notes
ZeezinDomar,1,1,,,Y,,0,
CostaCoffee,4,5,,,,Y,1,Track limits warning
OfflineKids,DNF,2,,,,,2,Contact at turn 1
```

`Driver` must match the driver's Discord display name or enrolled name.
`Pos` accepts `DNF`/`DNS` in place of a number. Template at
[`roster-bot/docs/results_template.csv`](../roster-bot/docs/results_template.csv).

---

## 5. Discord structure: roles and channels

Create these before you touch the bot. Names are yours; the bot learns
them from pickers, so you never type an id.

**Roles**

- One per team (`Mercedes`, `McLaren`, …) — drives rosters, assigned by
  the bot on signing.
- One per tier (`Tier 1`, `Tier 2`, `Tier 3`) — optional but recommended.
- One **Team Principal** role per team, or one shared. TPs can run
  `/roster sign` and `/roster drop` for their own team and make offers.
- One **Commissioner** role, if that isn't just you.

**Channels**

| Channel | Purpose |
|---|---|
| `#market` | Public, read-only: auto-updating market tables, movers, dashboard |
| `#transactions` | Public, read-only: every signing, trade, release, settlement — the audit feed |
| `#approvals` | Private to commissioners: pending offers and trades arrive here |

Deny **Send Messages** to everyone in `#market` and `#transactions` —
they are bot output. Make sure the bot has **Send Messages** and **Embed
Links** in all three; a board with no message is almost always a missing
permission.

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
without the preset gives you an empty shell to configure by hand — use
the preset.

If you already created a season without it, you are no longer
stuck. Previously the preset could only be applied at creation, so a
season made without one had no tiers and no league config and nothing
could give it either — every command failed with advice to "create a
season with the preset", which you could not do for the season you had
already named. Recover from `/league` → **Setup** by pressing **Seed
settings**, or type:

```text
/market-admin season seed-preset name: "Season 8"
```

**This works even if you have already added tiers by hand.** That was
the last corner of the same problem: adding a tier through Setup →
**Tiers** does not create the league config row, so a season could end
up with tiers but no settings — and seeding used to refuse the moment
any tier existed, while the config panel it redirected you to had no row
to edit. Now the tiers are kept exactly as you built them and only the
missing settings are filled in. The Setup checklist has a **League
settings** line so you can see at a glance whether the row exists.

It refuses to run once the season has a config row, because re-seeding a
live season would reset the valuation factors and the cap underneath
contracts already signed against them. It is a recovery path, not a
reset button. To change settings on a season that is already running,
use the config panel instead.

Name tiers and attach roles and colours:

```text
/market-admin tier edit code: t1 label: "Tier 1" rank_order: 1 role: @Tier-1
/market-admin tier edit code: t2 label: "Tier 2" rank_order: 2 role: @Tier-2
/market-admin tier edit code: t3 label: "Tier 3" rank_order: 3 role: @Tier-3
```

`rank_order` 1 is the top tier; promotion and relegation read it. A
fourth tier is `/market-admin tier add`.

Point the bot at your channels and commissioner role:

```text
/market-admin config channel kind: Market        channel: #market
/market-admin config channel kind: Transactions  channel: #transactions
/market-admin config channel kind: Approvals     channel: #approvals
/market-admin config role role: @Commissioner
```

Create teams:

```text
/roster create name: Mercedes
```

A guided flow — it asks for the team role and the channel for the roster
embed. Repeat per team, then `/roster list` to check.

---

## 7. Set the money rules

```text
/market-admin config show
/market-admin config edit
```

`config edit` covers the cap, salary bounds, contract-length bounds, and
incentive limits. it also carries **races per season**, the
two premium rates, and the length bounds in races.

Then the cash rules:

```text
/market-admin budget config
```

| Setting | Meaning |
|---|---|
| `enforce` | Off = cap-only league; cash is recorded but never blocks. On = a team cannot commit beyond its own money |
| `escrow` | Whether salary is actually drawn from cash per race and settled at term end. Off keeps the old commitment-only model |
| `rollover` | Whether cash survives into next season |
| `opening_m` | What each team starts a season with (preset $145M, equal to the cap) |
| `per_point_m` | Credit per championship point |
| `dnf_m` | Debit per retirement |
| `dns_m` | Debit per no-show |
| `per_incident_pt_m` | Debit per incident point |

> **These rates are placeholders, not tuned values.** `per_point 0.05`,
> `dnf 0.50`, `dns 1.00`, `per_incident_pt 0.25` were sized so a full
> season moves cash by single-digit millions against a $145M cap.
> **Run one season with `enforce: false` and watch the numbers before you
> let them block signings.** Nobody has calibrated them against your
> points system.

All of these are also on `/league → Money → Budget settings`, where
enforcement, rollover, and escrow are toggles and the rates open in a
modal. Omitting an option on the command leaves that setting untouched,
so editing a rate will not move your escrow or rollover decision.

A season with no budget config behaves exactly like a cap-only league.
Turning cash on later is one command; you lose nothing by waiting.

> **Escrow defaults to on.** Every season whose budget config you create
> from now on starts with escrow enabled — including season one of a
> brand new install, which has no earlier season to inherit a setting
> from. If your league runs commitment-only, where a contract counts
> against the cap and nothing leaves the balance, set `escrow: false`
> explicitly. It will not stay off by itself.
>
> The only seasons that begin with escrow **off** are ones that already
> existed inside the bot before the update that added it.
>
> Turning escrow on never charges retroactively — contracts already
> running simply start being debited from the next race imported.

---

## 8. Contract length, in races
Contract terms are measured in **races**, not seasons. A deal is "24
races", and it is done when its 24th race has been imported — which may
land mid-season.

| Setting | Where | Meaning |
|---|---|---|
| Races per season | `/market-admin config edit` | The divisor that turns a per-season salary into a per-race payment |
| Minimum contract length | `/market-admin config edit` | Fewest races any offer may run |
| Maximum contract length | `/market-admin config edit` | Most races any offer may run |

Both bounds were already admin-editable before this update (in seasons);
they keep the same home in the config modal and only change unit. The
absolute floor of one race stays hardcoded, because a zero-race contract
isn't a policy choice, it's a nonsense value — above that, the minimum
is whatever you set.

What this changes in practice:

- **A term can expire mid-season.** When the last race of a deal is
  imported, the contract completes, escrow settles, the driver's team
  role is removed, and he becomes a free agent that night.
- **A term can run past a season boundary.** A 36-race deal in a 24-race
  season has 12 races left in the next one. Carry-over moves the
  remainder rather than adding a whole season.
- **Races only count when results are imported** for that driver's tier.
  A race you never import never advances any term and never escrows a
  cent. If you skip an import, the whole economy pauses.
- **Re-importing a round is still safe.** Service is recorded once per
  (contract, round), so a corrected re-import never double-counts a race
  toward the term or double-charges escrow.

Migrating from the old model, every existing contract is converted at
`term_races = term_seasons × races per season`, so nobody's deal gets
shorter or longer than what was agreed.

---

## 9. Pricing: base value and the two premiums
Until now the only price rule was a flat league-wide floor (`min_salary`,
$1M in the preset) — the driver's own market value was not part of offer
validation at all. Now an offer must clear a floor derived from what the
driver is actually worth:

\[
\text{floor} = \text{base value}
\times \bigl(1 + \text{length premium} \times (\text{term races} - \text{minimum races})\bigr)
\times \bigl(1 + \text{re-sign premium}\bigr)
\]

- **Base value** is the driver's latest published market value.
- **The length premium is charged per race above the league minimum.** A
  deal at exactly the minimum length pays none. Because it compounds per
  race, the rate must be small: `0.005` is +0.5% per race.
- **The re-sign premium applies only when the offering team is the
  driver's current team** — extensions and any other same-team path.
  This is the anti-cycling lever: keeping your own driver costs more
  than his open-market value, so churning contracts to reset terms is
  expensive.
- The floor never falls below `min_salary`.
- If no valuation has ever been published for the driver, there is no
  base value, the floor cannot be computed, and the offer passes with a
  note rather than being blocked. Publish a run before you rely on this.

### A worked example

League minimum is 5 races, length premium `0.005`, re-sign premium
`0.150`. A driver's base value is **$20.00M**. Williams wants to keep him
on a **36-race** deal:

\[
\text{length multiplier} = 1 + 0.005 \times (36 - 5) = 1.155
\]
\[
\text{floor} = \$20.00\text{M} \times 1.155 \times 1.150 = \$26.57\text{M}
\]

Williams must offer at least $26.57M per season. A rival team offering
the same 36-race term pays no re-sign premium, so its floor is
$20.00M × 1.155 = **$23.10M**. Keeping your own driver is deliberately
the expensive option.

Then remember §1: that $26.57M salary is also what gets escrowed, at
$26.57M ÷ races per season each race, and the P/L at settlement is
measured against it. **Overpaying at the floor is not free — it raises
the bar the driver's market value has to clear for you to break even.**
That is the intended pressure toward honest contract lengths.

> **Both rates default to 0**, which reproduces today's behaviour
> exactly. Nothing about your pricing changes until you set them, and
> the starting values above are untuned suggestions, not calibrated
> numbers. Set them low, watch one season, adjust.

---

## 10. Get your drivers in

Roles first, bot second. Give people their tier role, then:

```text
/market-admin driver sync-all
```

This reads the tier roles and enrols everyone holding one as a driver in
that tier, skipping anyone already enrolled. Safe to repeat and only
ever adds.

One at a time, or someone needing a specific status:

```text
/market-admin driver add member: @Zeezin tier: t1 status: active
```

The six statuses are `active`, `reserve`, `free_agent`,
`restricted_fa`, `inactive`, and `suspended`. A `restricted_fa` is a
free agent his old team retains rights over, which is how you implement
a qualifying-offer rule if you want one. `/market-admin set-status`
changes a status later and writes a ledger note, so status history is
auditable.

A driver can legitimately exist in two tiers — a Tier 1 reserve who
races Tier 2 is two driver records with two independent market values.
That is deliberate. Don't try to merge them.

---

## 11. The first valuation

Every driver needs a value before anyone can be offered a contract.
this matters more than it used to: with no published value
there is no base value, so the price floor in §9 cannot be applied.

With no results imported, this baselines everybody at the minimum salary:

```text
/market-admin valuation run tier: t1 round_label: "Pre-season baseline"
/market-admin valuation preview run_id: <the id it printed>
/market-admin valuation publish run_id: <same id>
```

Repeat per tier. A run is priced but invisible until you **publish** it —
preview is your chance to reject a bad import before drivers see it.

Then put the public boards up:

```text
/market-admin board add kind: Market table         channel: #market tier: t1
/market-admin board add kind: Movers               channel: #market tier: t1
/market-admin board add kind: Cross-tier dashboard channel: #market
```

`Surplus` and `Underwater` boards rank drivers by P/L — worth adding
once escrow is live, because that P/L is now real money. Boards refresh
every 15 minutes. Tier boards need a tier; the cross-tier dashboard must
not have one.

---

## 12. Opening the market

```text
/market-admin config free-agency state: open
```

While free agency is **closed**, the only offers allowed are extensions
from a driver's own team. That is the lever that stops mid-season
poaching, and the one you open in the offseason.

The flow from here is your TPs' job:

1. TP runs `/contract offer team: … tier: … driver: @… offer_kind: …
   ttl_hours: 48` and fills in salary, term, bonus, incentives.
2. The bot validates **before** the driver sees it: TP authority, tier
   eligibility, free-agency window, term bounds, minimum salary, cap
   headroom, cash, and the price floor from §9. An invalid
   offer is refused with a specific reason.
3. The driver gets the offer privately and can **accept**, **decline**,
   or **counter**. He cannot edit the money inside acceptance — that
   protects the audit trail.
4. An accepted offer lands in your `#approvals` queue.
5. You approve. The contract commits, the team role is assigned,
   an escrow holding opens at zero, and `#transactions` gets a
   receipt.

Your side is just `/league` → **Approvals**, or
`/market-admin approve <offer_id>` / `reject <offer_id> [note]`.

Approving is the moment the commitment becomes real. Everything before
it is a proposal. note that no cash moves at approval — the
first debit lands with the first imported race.

---

## 13. Race night, every week

Press **Race Night** in `/league` — pick a tier, paste the sheet URL,
import, price, review, publish.

Each tier's sheet URL and tab are now remembered in the
database, not just for the session. Paste them once and every later
race night pre-fills both boxes — including after a bot restart. Change
the URL any time by typing a new one; the last successful import wins.
If all three tiers share one workbook, the season-level default covers
them all, so you paste it once ever rather than once per tier.

Typed equivalent, per tier:

```text
/market-admin results import tier: t1 round_label: "R1 Bahrain" sheet: <url>
/market-admin valuation run tier: t1 round_label: "R1 Bahrain"
/market-admin valuation preview run_id: <id>
/market-admin valuation publish run_id: <id>
```

Watch the round label. If you type a label that has no
imported results — a typo like `R1 Bharain` — the valuation has nothing
to price against and produces a **baseline** run where nobody moves.
The panel always warned about this; the typed command used to render
those flat values in exactly the same layout as a real run, so a typo
could be published straight over a live market. It now says so
explicitly and tells you to check the label.

What the import does, beyond recording finishes:

- Credits race earnings per championship point; debits retirements,
  no-shows, and incident points, charged to the team each driver was
  contracted to at import time.
- Advances every live contract by one race and debits that
  race's share of salary into escrow.
- Completes any contract whose final race this was: escrow
  settles at the driver's current value, his team role comes off, and he
  becomes a free agent.

**Order matters in one place: import results before running the
valuation.** A valuation prices whatever is in the database at that
moment, so running it first gives you last week's numbers under this
week's label. it matters a little more now, because a
settlement that fires during the import is valued off the last published
run — so publish before a term's final race if you want that race's
performance in the settlement.

**Re-importing a round after a stewards' decision is safe.** The bot
compares what the round should have charged against what it did charge
and writes only the difference, flagged as a correction. It never
double-bills and never deletes history. Race service is recorded once
per (contract, round), so terms and escrow are equally safe. Just import
the corrected sheet over the top.

---

## 14. When a contract ends
Four ways a deal ends, and what each does to the escrow:

| Ending | What happens |
|---|---|
| **Term completed** | The final race is imported. Escrow returns in full plus P/L at the driver's current value. Team role removed; driver becomes a free agent |
| **Release** (`/contract release`) | Settles immediately at the driver's current value — escrow back, plus or minus P/L as it stands today |
| **Buyout** (`/contract buyout`) | Same settlement, and the buyout amount stays as dead money against the cap for the season |
| **Void** (`/market-admin void`) | Same settlement, recorded with the commissioner's note. Refused while a trade is pending on the contract — resolve the trade first |
| **Trade** | The old team's holding settles at current value; the new team opens a fresh holding and escrows from that race on |

You chose "settle at current value" for early exits, so a team **can**
cash out a driver who has appreciated — and eats the loss on one who
hasn't. Two consequences worth telling your TPs:

- Releasing an appreciating driver is a way to realise a profit. If that
  turns into a strategy you dislike, the lever is the re-sign premium
  and the length bounds, not the settlement rule.
- Releasing a declining driver crystallises the loss immediately instead
  of hoping he recovers by term end.

Every settlement writes two ledger rows — the escrow return and the P/L
— so `/market-admin budget show` always explains itself. A `#transactions`
receipt shows what was held, the driver's value, the P/L, races served,
and the reason.

**Terms end whether or not escrow is on.** A contract's term is counted
in races imported, not in money moved, so a commitment-only league still
sees its deals run down and close on schedule. There is simply nothing
to settle: the receipt says the contract closed and that no escrow was
held against it, and the ledger records the term completing. The same
applies to a contract signed before you switched escrow on — it has no
holding, so no money comes back, but it still ends on time rather than
running forever.

---

## 15. The offseason

Strict order. This is the part most leagues get wrong.

### Step 1 — close the market

```text
/market-admin config free-agency state: closed
```

### Step 2 — create and activate the new season

```text
/market-admin season create name: "Season 9" preset: F1 25/26
/market-admin season activate name: "Season 9"
```

Check `/market-admin config show` on the new season before anyone signs:
races per season and the premium rates are per-season config
and a fresh season takes preset defaults, not last season's edits.

### Step 3 — carry contracts over

```text
/market-admin season carry-over from_season: "Season 8"
```

A term is a real commitment, and this is the command that honours it.
For every active contract in the finished season:

| Situation | What happens |
|---|---|
| Races remain on the term | A new row in the new season at **exactly the same money, type and incentives**, carrying the remaining race count. The driver keeps his team role. No signing bonus — that was paid once and the escrow holding carries with it |
| The term is complete | It expires. Escrow settles, the driver becomes a free agent, his team role is removed |
| The driver already signed a fresh deal in the new season | Skipped and listed under "Needs attention" — you decide |
| The new season has no tier with that code | Skipped, same treatment |

The receipt lists every line, plus any team whose payroll now exceeds
the cap. **Carried money is not blocked** — it is an obligation you
already agreed to. But a team over the cap cannot sign anyone until it
is fixed with a release, buyout, or trade.

Re-running is safe: only contracts still active in the old season are
touched, so a second run does nothing.

`/contract status @driver` reads "Term: 14 of 36 races".

### Step 4 — roll the money over

```text
/market-admin budget rollover from_season: "Season 8"
```

Each team carries forward its cash and gets its opening balance for the
new season. Idempotent.

> **— this number means something different now.** Under the old
> model rollover carried `balance − payroll`, because payroll had never
> actually been taken. With escrow, salary has already left the cash
> balance race by race, so rollover carries **the balance as it stands**.
> Subtracting payroll again would charge every team twice. If you are
> comparing a Season 8 rollover against a Season 9 one, that's why the
> shape changed.

Steps 3 and 4 can run in either order; they're built not to interfere.

### Step 5 — award prize money

```text
/market-admin budget award team: mclaren amount_m: 25 note: "S8 constructors P1"
/market-admin budget award team: haas    amount_m: 5  note: "S8 constructors P10"
```

Where you reward last season's performance. Deliberately manual — you
decide the payout curve, the bot doesn't assume one.

### Step 6 — sync drivers, baseline values, reopen

```text
/market-admin driver sync-all
/market-admin valuation run tier: t1 round_label: "S9 pre-season"
/market-admin valuation publish run_id: <id>
/market-admin config free-agency state: open
```

Then spot-check `/market-admin budget show` and `/market team <name>`
before the market opens.

---

## 16. Onboarding a league that already has history

The realistic case: your server has run for seasons on spreadsheets and
you're adding the bot now. You do **not** need to backfill history.

Career earnings are the one place you *may* optionally want to, if you
want your pre-bot seasons on the leaderboard — see §20. Everything else
below assumes a clean start, which is the recommended route.

### Option A — clean slate, rewarded teams (recommended)

Everyone starts at the same baseline value; last season's performance is
recognised as **team cash**, not as inflated driver prices.

```text
/market-admin season create name: "Season 8" preset: F1 25/26
/market-admin season activate name: "Season 8"
# tiers, channels, roles, teams as in §6
/market-admin driver sync-all
/market-admin valuation run tier: t1 round_label: "S8 baseline"
/market-admin valuation publish run_id: <id>
/market-admin budget award team: <last season's champion> amount_m: 30 note: "S7 constructors P1"
# … down the order
/market-admin config free-agency state: open
```

Driver values become meaningful after two or three imported rounds
anyway, and nobody can argue with a clean baseline. Arguments about a
hand-assigned $20M valuation will consume your entire preseason.

### Option B — retro-import last season

Import Season 7's rounds into a Season 7 record before creating Season 8.
More faithful, considerably more work, and it needs your old results in
the sheet format from §4. Worth it only if you have those sheets and
want carried driver values from day one.

Either way, **existing contracts have to be entered as new contracts.**
There is no import path for a deal agreed in a spreadsheet. Have each TP
make the offer at the agreed money and term, approve them in a batch,
and state the remaining length honestly — a driver with 12 races left
should be signed for 12 races so the term and its settlement land
correctly.

**— escrow does not apply retroactively.** Contracts that already
exist when you deploy this update keep running under the old model and
settle nothing; escrow engages on the next signing or carry-over. That
is deliberate, so a mid-season server does not wake up to a column of
surprise debits. It also means your Season 8 rosters will be a mix:
older deals commitment-only, new ones escrowed. If you want everyone on
one model, enter the whole grid fresh at a season boundary.

> If your league runs across a season boundary *without* running
> `season carry-over`, old contracts stay active and keep counting
> against payroll forever. If you are already in that state, run the
> command once per past season, oldest first.

---

## 17. Day-to-day admin jobs

| You want to | Command |
|---|---|
| See what state the league is in | `/league` |
| Find any command | `/help` |
| Approve or reject pending offers and trades | `/league` → Approvals |
| Check a team's payroll and cap room | `/market team <name>` |
| Check a team's cash and escrow | `/market-admin budget show <team>` |
| See a driver's value, contract, races served ****, and history | `/contract status @driver` |
| End a contract with cause | `/market-admin void <contract_id> [note]` |
| Release a driver | `/contract release <contract_id> <note>` |
| Buy a driver out | `/contract buyout <contract_id> <buyout_m> <note>` |
| Move a driver up or down a tier, contract included | `/market-admin promote` / `relegate` |
| Correct a team's cash | `/market-admin budget adjust <team> <delta_m> <note>` |
| Log a cap note that moves no money | `/market-admin adjust-cap <team> <delta_m> <note>` |
| Repost or fix the boards | `/league` → Boards, or `/market-admin board refresh` |
| Stop mid-season poaching | `/market-admin config free-agency state: closed` |

`budget adjust` moves money. `adjust-cap` only writes an audit note.
They are not interchangeable.

---

## 18. When something looks wrong

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
match his Discord display name or enrolled name. Fix the sheet and
re-import the same round.

**An offer was refused and the TP doesn't know why.** The refusal names
the reason: `cap_exceeded` (league rule), `budget_exceeded` (that team's
cash), `below_price_floor` (§9), free agency closed, term
outside bounds, salary below minimum. `/market-admin config show` lists
the current rules.

**A team is over the cap after carry-over.** Expected and allowed —
existing obligations. It cannot sign anyone until it's back under.

**Payroll includes drivers from a season that's over.** You haven't run
`/market-admin season carry-over` for that season. §15, step 3.

**— nobody's escrow is moving.** Escrow advances only on results
import. Check that you are importing every round for every tier, and
that `escrow` is on in `/market-admin budget config`.

**— a settlement looks wrong by a race.** Settlement values off
the **last published** valuation. If you settled before publishing the
final round, that round isn't in the number. Publish first, then import
the term's last race.

**— a team's cash dropped and nobody signed anything.** That is
escrow working: every imported race debits the per-race share of every
live salary. `/market-admin budget show` itemises it by contract.

**Panel buttons do nothing.** Pull and restart — a discord.py change
broke panel clicks in builds before Phase 8.

**The Drivers panel threw an error on a driver.** Fixed in Phase 9: a
driver holding a contract with no published valuation used to crash the
detail view. Pull and restart.

**Commands don't appear.** Give it a minute, reload the client, check
`make logs` for `logged in as`.

Nothing in this system deletes money history. Every movement is an
append-only ledger row with a kind, an amount, and an actor. If a number
looks wrong it can be traced, and corrections are additional rows rather
than edits.

---

## 19. Things to decide before you launch

Write these down and post them where your league can read them. The bot
enforces whatever you configure; it cannot referee an argument about
rules you never published.

- **Cap.** $145M unless you have a reason.
- **Races per season.** Get this right before anyone signs — it is
  the divisor for every per-race payment.
- **Contract length bounds, in races.** Long deals now mean long
  exposure to a driver's value, which is the point.
- **Your two premium rates.** Start low. The length premium
  compounds per race, so 0.5% per race is already +15% over 30 races.
- **Cash and escrow on or off for season one.** Off is a legitimate
  answer while you calibrate.
- **Your prize-money curve.** What does a title pay versus last place?
  You award it by hand, so decide the curve in advance and publish it.
- **Whether retirements and incident points cost cash,** and how much.
  Start with the preset rates, run a season with enforcement off, then
  calibrate.
- **Whether incentives pay out.** `max_incentives` is validated on every
  contract and counted against the cap, but nothing pays it. Win bonuses
  are a manual `budget adjust` today, and a league rule you must write
  down.
- **Who approves what.** Contracts and trades both need a commissioner.
  If that's only you, you are a bottleneck on race night — give a second
  person the role.
- **Whether to seed career earnings** from seasons you raced before the
  bot. Optional; see §20.

---

## 20. Driver career earnings

This is the one money number that belongs to the **driver** instead of
the team, and it is the simplest thing in the bot: drivers keep what
they are paid, forever.

### What happens

Every time you import a race, each driver under contract is credited one
race's share of their salary. A driver on a $24.00M contract in a
24-race season earns $1.00M per race, and it is added to a lifetime
total that never resets.

That share is calculated by the same code that charges the team side, so
the driver's credit and the team's debit can never disagree.

### Where to see it

```text
/league → Market → "Career earnings leaderboard"
```

| Route | Shows |
|---|---|
| `/market earnings` | The leaderboard. Defaults to all-time; a scope option narrows it to the active season |
| `/market my-earnings [@driver]` | Career total, season total, and recent pay lines |
| Driver card | A career-earnings line alongside market value and P/L |
| Race-night receipt | What the round paid out, and a reminder that no team budget moved |

The leaderboard is the only market view that works with no active
season, because career totals outlive seasons. You can read it in the
offseason.

### What it costs the teams

**Nothing.** This is the point most worth publishing to your league,
because it is the first thing a Team Principal will ask.

Career earnings are a *record* of money already accounted for, not a
second payment. Adding this changed no team budget, no cap arithmetic,
no escrow behaviour and no P/L figure. A team that signs a driver for
$24.00M pays exactly what it paid before.

Nor can a driver spend the total. There is no shop, no transfer, no
mechanism at all — it is a leaderboard number today. It is stored as
real money, to the cent, so that if you later decide drivers should be
able to buy something with it, the history is already there and nothing
needs rebuilding.

### It does not depend on escrow

Earnings accrue whether escrow is on or off. A driver earned their race
salary regardless of whether your league models team cash, so this runs
as its own step rather than as part of the escrow charge. A league that
never turns escrow on still gets a complete leaderboard.

### It carries forward by itself

A driver's earnings are keyed to their **Discord account**, not their
seat. A new season, a move to another team, a promotion to another tier,
even deleting an old season — none of it affects their career total.

There is no offseason step for this. §15 gains nothing to do.

### Pay is per round, not per finish

Everyone under contract is credited once per imported round, including a
driver who did not show up. That is deliberate: it mirrors what the team
is charged, so the two sides reconcile.

If your league would rather not pay a no-show, dock it by hand:

```text
/market-admin earnings adjust @driver -0.50 "DNS, R4 Suzuka"
```

Decide this before season one and publish it either way.

### Optional: seeding seasons you raced before the bot

On a fresh install every driver starts at **$0.00M** and the board fills
in from your first imported race. That is the clean default and needs no
setup.

If your league has history you want on the board, seed each driver an
opening total:

```text
/market-admin earnings carry-in @driver 180.00 "S1–S7 total, from the standings sheet"
/market-admin earnings adjust  @driver -12.50 "S4 double-counted"
```

Both require a reason, both are logged against your user id, and both
are additive — a carry-in does not overwrite, so a driver already
accruing this season keeps accruing on top of the seeded figure.

> **This needs your verification, not the bot's.** The bot has no way to
> know what your league paid before it was installed. A carry-in figure
> is only as good as the spreadsheet you take it from. Check it against
> your own records before entering it — you can correct it afterwards,
> but the ledger keeps both rows, and the second one is a correction in
> public.

A reasonable middle ground, if your old records are patchy: skip the
seeding and let the leaderboard be an honest record of the bot era.
Announce it as starting fresh and nobody has to trust a reconstructed
number.
