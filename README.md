# Intrepid Racing League — roster, market & contract bot

A Discord bot that runs a multi-tier sim-racing league: live roster
embeds that update themselves, a per-tier driver market that prices
drivers off real race results, team budgets that earn and bleed money
race by race, and contracts measured in races rather than seasons.

You operate it from one command — `/league` — which opens a control
panel that tells you what state your league is in and what to do next.
There are 84 slash commands behind it; you should rarely need to type
one.

**This page takes you from an empty server to a running league**, in the
order you actually do it. It is written for the person running the
league, not for a developer.

| If you want | Read |
|---|---|
| To set up and run a league (you are here) | this page |
| Far more detail on any step below | [`docs/RUNNING_YOUR_LEAGUE.md`](docs/RUNNING_YOUR_LEAGUE.md) |
| Something to hand your Team Principals and drivers | [`docs/MARKET_GUIDE.md`](docs/MARKET_GUIDE.md) |
| The full command reference | [`roster-bot/README.md`](roster-bot/README.md) or `/help` in Discord |
| To change the code | [`CLAUDE.md`](CLAUDE.md) and [`docs/audit/`](docs/audit/README.md) |

---

## What it does for you

- **Rosters keep themselves current.** Assign a team role, the roster
  embed updates. No one maintains a list by hand.
- **Drivers have prices.** After each round the bot re-values every
  driver in a tier from their results, and publishes a market board.
- **Teams have money.** Each team earns per championship point and
  loses money to DNFs, no-shows and incident points.
- **Contracts are real.** Offers, counteroffers, approvals, releases,
  buyouts, trades and season-to-season carry-over, all logged.
- **Drivers keep their salary.** Every race a driver is under contract
  adds to a lifetime earnings total that never resets, ranked on a
  leaderboard that carries across seasons. It costs the teams nothing
  extra.
- **Every money movement is auditable.** Nothing adjusts a balance
  silently; each change appends a ledger row you can read back.
- **The three tiers stay separate.** A Tier 2 driver is priced against
  Tier 2 only. Values never leak across tiers.

---

## The three numbers that govern money

Almost every confused question from a Team Principal comes from mixing
these up. They are three different things.

| | What it is | Who sets it |
|---|---|---|
| **Spending cap** | A league *rule*: the most payroll a team may commit. $145M in the F1 preset. Identical for all three tiers. | You, once |
| **Team budget** | That team's actual *cash*. Earned from results, drained by penalties and signings. | The bot, race by race |
| **Driver value** | What a driver is *worth* right now, from their results. | The bot, after each round |

A team can hold **more** cash than the cap and still not be allowed to
spend past it. The cap limits commitment; the budget limits ability to
pay. A team is blocked by whichever binds first.

Escrow is the switch that decides whether the cap and the budget are
really different in practice, and you control it — see
[Part 5](#part-5--set-the-money-rules). With escrow **on**, each race
**debits that race's share of the driver's salary from the team's cash**
and holds it. Nothing is charged at signing; the money accumulates as
the contract is served. When the contract ends the team gets the held
total back, adjusted by how the driver's value moved:

- Driver gained value → team gets the escrow back **plus** the gain.
- Driver lost value → team gets back **less**.
- A team can lose its entire escrow on a contract but **never more than
  it put in**.
- End a contract early and the gain or loss is pro-rated by races
  served.

So a long contract on an expensive driver drains cash steadily across a
season rather than in one hit, and a team that signs late pays only for
the races it actually gets.

With escrow **off**, none of that happens: a contract counts against the
cap and nothing leaves the balance. That is the simpler model, and a
perfectly reasonable way to run a league — it is how this one ran for
its first seven seasons.

Re-signing your own driver costs **more** than their base value, and
longer contracts cost more again. Both premiums stack. That is
deliberate: keeping a driver should cost something.

---

## Before you start

- A Discord server where you have **Manage Server**
- A machine that can stay on — the bot is not serverless
- Docker and Docker Compose on it
- A Discord bot token
- Optional: a Google account, if you want results read from
  spreadsheets rather than entered by hand

Budget about an hour for first setup, most of it Discord roles.

---

## Part 1 — Put the bot on your server

Create the application at
<https://discord.com/developers/applications>. Then:

1. Under **Bot → Privileged Gateway Intents**, enable **Server Members
   Intent**. The bot cannot see role changes without it, and rosters
   will silently never update.
2. Invite it with scopes `bot` and `applications.commands`, and these
   permissions: View Channels, Send Messages, Embed Links, Manage Roles,
   Manage Messages.
3. In **Server Settings → Roles**, drag the bot's role **above every
   team role**. Discord will not let it assign a role that sits above
   its own. This is the single most common setup mistake.

Then install it:

```sh
cd roster-bot
cp .env.example .env      # put your DISCORD_TOKEN in this file
make up                   # builds and starts the bot and Postgres
make logs                 # watch it come up
```

That is the whole install. Postgres runs alongside the bot in the same
Compose project and its data lives in a named volume, so restarts and
rebuilds do not lose anything. Database migrations apply themselves
every time the bot starts — you never run them by hand.

Useful later:

```sh
make restart   # rebuild and redeploy, after pulling new code
make logs      # follow the logs
make down      # stop
```

To confirm it worked, type `/league` in your server. If the panel
appears, you are done with this part.

---

## Part 2 — Discord structure

Create these before you configure the bot, so you can point it at them:

**Roles** — one per tier (`@Tier-1`, `@Tier-2`, `@Tier-3`), one per
team (`@Mercedes`…), one `@Commissioner`, and one for Team Principals if
you want TPs making their own offers.

**Channels** — the bot writes to four kinds:

| Channel | What lands there | Who should post |
|---|---|---|
| `#market` | Market boards, driver values, movers | Bot only |
| `#transactions` | Every signing, release, trade | Bot only |
| `#approvals` | Things waiting on a commissioner | Bot only |
| Team channels | That team's roster embed | Bot only |

Make the bot-only ones read-only for everyone else. They are your audit
trail; you do not want chat in them.

---

## Part 3 — Spreadsheets (optional)

If you already keep race results in Google Sheets, the bot can read them
directly and you skip manual entry.

You need a Google service account key, pointed at by `GOOGLE_APPLICATION_CREDENTIALS`
in your `.env`, and each results sheet **shared with that service
account's email** as a viewer. The full walkthrough with screenshots of
each Google console page is in
[the runbook, section 4](docs/RUNNING_YOUR_LEAGUE.md).

You can skip this entirely and enter results by hand. Nothing else
depends on it.

---

## Part 4 — Day one: create the league

Run `/league`, press **Setup**, and follow the next step it offers. The
panel walks this exact sequence and tells you which piece is missing.
The typed equivalents, if you prefer:

```text
/market-admin season create name: "Season 9" preset: F1 25/26
/market-admin season activate name: "Season 9"
```

**Use the preset.** It seeds your three tiers, driver statuses, contract
types, the valuation factors, the points table, and a league config with
the $145M cap. A season created without it is an empty shell you would
have to configure by hand.

If you did create one without the preset, you are not stuck. Open
`/league` → **Setup** and press **Seed settings**, or type:

```text
/market-admin season seed-preset name: "Season 9"
```

Either route works whether or not you have already added tiers by hand.
If you have, your tiers are kept exactly as you built them and only the
missing settings — the scoring table, the valuation factors and the
$145M cap — are filled in.

It refuses once a season already has settings, because re-seeding a
running season would reset the valuation factors and the cap underneath
contracts already signed against them. It is a recovery path, not a
reset button. To change settings on a running season, use **Cap &
rules** on the Setup screen.

Name your tiers and attach the roles:

```text
/market-admin tier edit code: t1 label: "Tier 1" rank_order: 1 role: @Tier-1
/market-admin tier edit code: t2 label: "Tier 2" rank_order: 2 role: @Tier-2
/market-admin tier edit code: t3 label: "Tier 3" rank_order: 3 role: @Tier-3
```

`rank_order: 1` is your top tier. Promotion and relegation read it.

Point the bot at your channels and commissioner role:

```text
/market-admin config channel kind: Market        channel: #market
/market-admin config channel kind: Transactions  channel: #transactions
/market-admin config channel kind: Approvals     channel: #approvals
/market-admin config role role: @Commissioner
```

Then create teams. `/roster create name: Mercedes` starts a guided flow
that asks for the team role and where to put the roster embed. Repeat
per team and check with `/roster list`.

---

## Part 5 — Set the money rules

Two separate settings, and it is worth knowing which is which.

**League rules** — the cap, salary floor and ceiling, contract length
bounds:

```text
/market-admin config show
/market-admin config edit
```

**Team money** — opening budget, and what results are worth:

```text
/market-admin budget config
    opening_m: 50
    per_point_m: 0.05
    dnf_m: 0.5
    dns_m: 1.0
    per_incident_pt_m: 0.1
    enforce: true
    rollover: true
```

That reads as: every team starts with $50M, earns $0.05M per
championship point, loses $0.5M per DNF, $1M for not showing up, and
$0.1M per incident point. `enforce: true` makes those limits binding
rather than advisory; `rollover: true` carries unspent money into next
season.

Leave `enforce: false` for your first week if you want to watch the
numbers move before they can block anybody.

**Escrow** is the third toggle, alongside enforcement and rollover:

```text
/market-admin budget config escrow: false
```

On means each race debits that race's share of salary from team cash and
holds it until the contract ends; off means contracts count against the
cap only and cash is never drawn down. A brand new config row starts
with escrow **on**, so set it explicitly if you want the simpler model.
Running `budget config` with no options prints the current settings
without changing anything.

You can override any of these for a single tier by passing `tier:`.
Without it you are setting the season default. Omitting an option leaves
that setting as it was — editing a penalty will not quietly move your
escrow decision.

---

## Part 6 — Get drivers in and price them

Give each driver their tier role. That is what registers them.

Then take a baseline valuation per tier, which starts everyone at the
salary floor:

```text
/market-admin valuation run tier: t1 round_label: "Pre-season baseline"
/market-admin valuation publish run_id: <the id it prints>
```

Two steps on purpose. `run` prices drivers and shows you the result
without anyone seeing it; `publish` makes it official. You can discard a
run you do not like. Nothing is visible to your league until you
publish.

---

## Part 7 — Open the market

Put live boards in your market channel:

```text
/market-admin board add kind: Market table channel: #market tier: t1
/market-admin board add kind: Movers channel: #market tier: t1
/market-admin board add kind: Cross-tier dashboard channel: #market
```

Boards refresh themselves as valuations publish. Add them once.

Now your Team Principals can work. They use `/contract offer`, or the
**Contracts** button in `/league`, to offer a driver a deal — salary,
length in races, and type. The bot checks cap space, budget, roster
room and tier eligibility **before** the offer reaches the driver, so a
TP cannot accidentally make an invalid one. The driver accepts,
declines, or counters. A commissioner approves. Everything lands in
`#transactions`.

Hand your TPs [`docs/MARKET_GUIDE.md`](docs/MARKET_GUIDE.md) and you
will answer far fewer questions.

---

## Part 8 — Race night, every week

This is the whole weekly job:

```text
/league  →  Race Night  →  pick tier  →  paste sheet URL  →  Publish
```

Which does four things: imports the results, pays out earnings and
applies penalties to every team's budget, re-values every driver in the
tier, and refreshes the boards.

By command, per tier:

```text
/market-admin results import tier: t1 round_label: "R14 Abu Dhabi" sheet: <url>
/market-admin valuation run tier: t1 round_label: "R14 Abu Dhabi"
/market-admin valuation publish run_id: <the id it prints>
```

Both routes run identical code — the panel calls the same functions the
commands do. Review before publishing; the run step shows you exactly
what will change.

---

## Part 9 — When contracts end

Contracts count down in **races**, not seasons. After each round the
bot advances every active contract and tells you which ones expired.

When one ends, escrow settles: the team gets back the salary it was
charged race by race, plus or minus how the driver's value moved while
under contract. The driver
returns to the market at their current value.

You set the shortest contract anyone may sign, and you can change it at
any time — useful mid-season if teams start parking drivers on one-race
deals. It lives with the other contract rules:

```text
/market-admin config edit
```

which opens the rule editors, including the race-term rules where the
minimum length sits. Changing it affects new offers only; contracts
already signed keep the terms they were signed under.

Three ways to end a contract early, and they are not the same:

| | What it does | Cash effect |
|---|---|---|
| **Release** | Drops the driver, ends the deal | Escrow returns, pro-rated by races served, P/L applied |
| **Buyout** | Pays to exit | Same as release, plus the buyout cost recorded as dead money |
| **Void** | Marks the contract as never having been valid | Same settlement as release |

All three return the escrow the same way, so void is not a cheaper exit.
What differs is the record: release and buyout *end* a contract, void
says it should never have existed. Use void only for genuine data-entry
mistakes — a deal typed wrong — and read the rough edges below first,
because void currently skips two things the other two do.

---

## Part 10 — The offseason

Run `/league → Off-season` and work down it. It handles promotion and
relegation by tier rank, carrying contracts into the new season, and
rolling over unspent budget if you enabled that.

Carry-over moves **no money**. A contract that spans two seasons keeps
its original terms, its escrow, and the races already served. It is
repointed at the new season, not re-signed, so nobody pays twice.

---

## Driver career earnings

Separate from everything above, and the one number that belongs to the
**driver** rather than the team.

Every time you import a race, each driver under contract is credited one
race's share of their salary — the same figure the team side is charged
— and it is added to a lifetime total that never resets.

```text
/league → Market → "Career earnings leaderboard"
```

or `/market earnings`. A driver can check their own with
`/market my-earnings`, which shows their career total, their total this
season, and their recent pay. It also appears on their driver card.

**This costs the teams nothing extra.** Career earnings are a record of
what a driver has been paid, not a second movement of money. Budgets,
the cap, escrow and every P/L figure behave exactly as they would
without this feature. Nothing lets a driver spend the total — it is a
leaderboard number, stored as real money so it can mean something later
if you decide it should.

**It does not depend on escrow.** A driver earned their race salary
whether or not your league models team cash, so earnings accrue with
escrow on or off.

**It carries forward by itself.** A driver's earnings are tied to their
Discord account, not their seat, so a new season, tier or team changes
nothing. There is no offseason step for this and nothing to remember.

**Pay is per race imported, not per finish.** Everyone under contract is
paid for the round, including a driver who did not show up, because that
is what the team is charged for. If you would rather dock a no-show:

```text
/market-admin earnings adjust <@driver> -0.50 "DNS, R4"
```

On a fresh install every driver starts at $0.00M and the board fills in
from your first imported race. If you want your pre-bot seasons on the
leaderboard you can seed opening totals by hand with
`/market-admin earnings carry-in` — optional, and covered in
[section 20 of the runbook](docs/RUNNING_YOUR_LEAGUE.md).

---

## Already running a league?

If you have seasons of history and are adopting the bot now, or are
carrying an existing league into a new season, read
[section 16 of the runbook](docs/RUNNING_YOUR_LEAGUE.md) rather than
following Part 4 above. It covers importing standings, backfilling
existing contracts, and setting driver values to something sensible
instead of a cold baseline.

**Decide about escrow before you start.** Escrow — salary being charged
from a team's cash race by race — is **on by default** for any season
you create from now on.

That means a **brand new install gets escrow on**, including the case
where your league has years of history but the bot does not: the bot has
no prior season to inherit a setting from, so the default applies. If
your league has always run commitment-only, where a contract counts
against the cap and nothing leaves the balance, you have to switch
escrow off deliberately. It will not stay off on its own.

The only seasons that start with escrow **off** are ones that already
existed inside the bot before the update that introduced it.

Check where you stand and set it deliberately:

```text
/market-admin budget config
```

with no other options, which prints the current rules including escrow.
Then either leave it or change it:

```text
/market-admin budget config escrow: false
```

Or use `/league → Money → Budget settings`, where escrow is a toggle
next to enforcement and rollover. Both routes state what the change
does before it takes effect.

Switching escrow on **does not reach backwards**. Nothing is charged
retroactively: contracts already running simply start being debited from
the next race you import. Contracts that ran their whole term while
escrow was off have nothing held and so settle no money when they end.
That makes it safe to turn on mid-season, though your roster will be
mixed for a while.

---

## If something looks wrong

| Symptom | Cause |
|---|---|
| Rosters never update | Server Members Intent is off, or the bot's role sits below the team roles |
| Bot cannot assign a role | Same — move the bot's role up |
| "No league config for that scope" | No active season, or a season created without the preset. See Part 4 |
| Valuation ran but nobody can see it | You ran it but did not publish it |
| Signings not blocked by budget | `enforce: false` in `budget config` |
| Team cash never moves on salary | Escrow is off. `budget config` shows it; `escrow: true` turns it on |
| A board stopped updating | Bot lost permission to that channel. Re-add the board |
| Commands missing in Discord | Slash commands can take an hour to propagate after first invite |

`/league` is the fastest diagnosis: it opens on a status screen that
names what is missing and what to do next. Section 18 of the runbook
covers harder cases.

---

## Rough edges worth knowing

Honest list, verified against the current code. Full detail, with
status per item, is in [`docs/audit/`](docs/audit/README.md).

- **The earnings and penalty rates are placeholders.** The per-point
  earnings, DNF, DNS and incident-point figures ship sized against the
  $145.00M cap, not against your league's actual points system. Run
  your first season with budget enforcement **off**, watch what the
  numbers do over a few rounds, then turn it on. This is the one
  setting you should expect to tune yourself.
- **One step still needs a typed command.** Recovering a team whose
  database row was lost but whose roster message survives is
  `/roster relink`. Discord does not allow one dialog to open another
  and that flow starts with a dialog, so the Teams screen hands you the
  exact command instead.

---

## For developers

`roster-bot/` is the package; `make` targets are run from inside it.
[`CLAUDE.md`](CLAUDE.md) is the architecture guide and the list of rules
the CI guards enforce — read it before changing anything under
`bot/market/`, `bot/contracts/` or `bot/valuation.py`, which are not
allowed to contain literal numbers.
[`docs/ADR-001-f1-with-generic-future.md`](docs/ADR-001-f1-with-generic-future.md)
explains why. [`docs/audit/`](docs/audit/README.md) records 27 defects
traced from click to database write, with current status — useful
context for why several screens are shaped the way they are. All 27
are now fixed; what remains above is tuning and one Discord platform
limit, not known defects.
