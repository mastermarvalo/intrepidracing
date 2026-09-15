# Driver Market & Contract System — league guide

A walkthrough of the market and contract features for people who
actually play in the league. If you're an engineer looking at how it's
built, `docs/ADR-001-f1-with-generic-future.md` and the phase commit
messages are the better starting point.

---

## The 30-second mental model

- The league lives in a **Season**. Everything below hangs off it.
- Each season has one or more **Tiers** (Tier 1 / Tier 2 / Tier 3 by
  default). Values, salary caps, and markets are **tier-isolated** —
  a Tier 2 driver's numbers never mathematically influence a Tier 1
  driver's.
- Every racing member has a **Driver** row per tier they compete in
  (a Tier 1 reserve who also races Tier 2 is two rows, with two
  independent market values).
- Drivers have a **Market Value** — a number that moves each week
  based on performance.
- When a team **Signs** a driver, they lock in a **Contract Value** —
  the salary the team pays. It's frozen at signing.
- The team's **Salary Cap** is measured against the *sum of contract
  values*, not market values. Under-market drivers give a team
  surplus; over-market drivers eat into cap space.
- Every mutation (offer, sign, trade, release, buyout, cap
  adjustment) writes a row to the **Contract Ledger** — the audit
  trail. Nothing is ever overwritten.

Two numbers per driver, always:

| Number         | Source                                                       | What it controls                    |
|----------------|--------------------------------------------------------------|-------------------------------------|
| Market Value   | Recomputed each valuation cycle from performance             | Free-agent cost, trade value, extension asks |
| Contract Value | Frozen when the team signed the driver                       | Cap compliance                      |

`P/L = Market Value − Contract Value`.
`+` = under-market (team surplus). `−` = above-market.

---

## Roles at a glance

| Role                | Discord permission needed                     | Can do                                                                                 |
|---------------------|-----------------------------------------------|----------------------------------------------------------------------------------------|
| **Commissioner**    | Manage Server                                 | Create seasons/tiers, edit config, run + publish valuations, approve offers & trades, void contracts, promote/relegate, adjust cap. |
| **Team Principal**  | The team's principal role (`teams.principal_role_id`) | Open offers to drivers, withdraw offers, propose trades, release/buyout their contracts. |
| **Driver**          | Any registered driver                         | Accept / decline / counter offers made to you.                                          |
| **Everyone**        | —                                             | View markets, movers, driver cards, cap sheets, dashboards, trade status.               |

Everything shows up as a **slash command** in Discord.

---

## Part 1 — Commissioner setup (one time per league)

Do this once before anyone else can use the feature.

### 1. Create the season

```
/market-admin season create name: "F1 2026 Season" preset: F1 25/26
```

`preset: F1 25/26` seeds sensible defaults for a 3-tier F1 league:

- Three tiers (`t1`, `t2`, `t3`) with no roles/colours yet
- All lookup rows (statuses, contract types, offer/trade states,
  transaction kinds, board kinds)
- Starting valuation weights (wins, race finish, quali, points,
  DNFs, poles, fastest laps, form, consistency)
- Default league_config: cap $145M, min salary $1M, weekly move cap
  ±$0.75M, exceptional move cap ±$1.25M, max term 3 seasons, max
  incentives 15%, offer TTL 48h, active driver slots 2

### 2. Activate it

```
/market-admin season activate name: "F1 2026 Season"
```

Only one season can be active per Discord server at a time.

### 3. Wire tier roles + colours

Whichever Discord roles you already use to mark tier membership:

```
/market-admin tier edit code: t1 label: "Tier 1" rank_order: 1 role: @Tier-1 accent_color: #ff1801
/market-admin tier edit code: t2 label: "Tier 2" rank_order: 2 role: @Tier-2 accent_color: #00d2be
/market-admin tier edit code: t3 label: "Tier 3" rank_order: 3 role: @Tier-3 accent_color: #6cd3bf
```

### 4. Tune numeric config

```
/market-admin config show                      # see current values
/market-admin config edit                      # opens a modal to change them
```

The modal edits the five most-tuned values (cap, min salary, weekly
cap, exceptional cap, max term). Other flags are set with:

```
/market-admin config channel kind: Market channel: #market
/market-admin config channel kind: Transactions channel: #transactions
/market-admin config channel kind: Approvals channel: #commissioner
/market-admin config role role: @Commissioner
/market-admin config free-agency state: Open
```

### 5. Register drivers

The bot doesn't auto-populate a `drivers` row from Discord roles.
Every racer who should be in the market needs their driver row created
at least once. Today that's typically done manually per driver in your
staging; a bulk driver-import command is on the roadmap.

### 6. First valuation

```
/market-admin valuation run tier: t1 round_label: "Pre-season baseline"
/market-admin valuation publish run_id: <shown in the reply>
```

Repeat for `t2`, `t3`. Every driver in the tier is baselined at
`min_salary` for their first-ever valuation, then future runs move
them from there. Every valuation run is created **unpublished (dry
run)** first — you see a preview, and only clicking publish makes it
the live number.

### 7. Post the public boards

```
/market-admin board add kind: "Market table" channel: #market tier: t1
/market-admin board add kind: Movers channel: #market tier: t1
/market-admin board add kind: "Cross-tier dashboard" channel: #market
/market-admin board add kind: "Surplus (best P/L)" channel: #market tier: t1
/market-admin board add kind: "Underwater (worst P/L)" channel: #market tier: t1
```

Boards auto-refresh:

- Immediately after every `/market-admin valuation publish` for their
  tier (plus all cross-tier dashboards).
- Every 15 minutes as a safety-net poll.

If a board's message gets deleted, `/market-admin board list` shows it
as broken and `/market-admin board refresh` reposts.

---

## Part 2 — Team Principal: making an offer

### The two-stage flow

```
/contract offer team: red-bull tier: t1 driver: @Verstappen offer_kind: "New signing" ttl_hours: "48 hours"
```

That opens a modal asking for:

- **Salary ($M)** — the contract value you're offering
- **Term (seasons)** — how many seasons the deal runs
- **Signing bonus ($M)** — a one-time bonus at signing (counts
  against the cap)
- **Incentives** — free text; performance bonuses etc
- **Note to driver** — anything you want to say

Hit submit and you get a **review panel** (ephemeral — only you see
it) that shows:

- Your payroll before / after
- Your cap space before / after
- The driver's current market value + the P/L on your offer
- Every validation check with ✅ / ⚠ / ⛔ icons

You cannot submit an offer where any check is ⛔. Common blockers:

| Code                       | Fix                                                                 |
|----------------------------|---------------------------------------------------------------------|
| `salary_below_min`         | Raise the salary to at least the league minimum                     |
| `cap_exceeded`             | Reduce salary/bonus, release a driver, or use extension flow         |
| `no_seat_available`        | Release someone first, or use `trade_and_sign` to swap seats         |
| `active_contract_conflict` | The driver already has a deal; use `Extension` or `Trade-and-sign`   |
| `duplicate_pending_offer`  | You already have a live offer with this driver; withdraw it first    |
| `free_agency_closed`       | Wait for the commissioner to open the window                         |
| `term_too_long`            | League caps term length; shorten the deal                            |
| `incentives_over_cap`      | Incentives can't exceed the league's incentive percentage of salary  |
| `status_suspended`         | Driver is suspended; commissioner must reinstate them first          |

`⚠ status_inactive_warning` is a warning, not a blocker — the offer
goes through but the commissioner may push back on approval.

Click **Submit offer** in the review panel. The bot creates a private
thread in the approvals channel with the driver added, posts the
offer card there, and stores the offer id. Withdraw with:

```
/contract withdraw offer_id: <id>
```

### Extensions

For a driver you already have signed, use `offer_kind: Extension`.
The flow is the same as a new signing, but on approval the existing
contract is updated in place — no new row, external ref preserved,
history captured in the ledger.

### Trades

```
/trade propose my_team: red-bull other_team: mercedes my_contract_id: 42 their_contract_id: 87 ttl_hours: "48 hours" message: "cap dump"
```

Phase 5 supports one-contract-for-one-contract trades. The bot creates
a private thread in the approvals channel with both TPs added and
posts a trade card showing:

- What each side gives up
- Each team's payroll before / after
- Each team's cap space after

The other team's TP replies with `/trade accept trade_id: <id>`,
`/trade decline trade_id: <id>`, or their own counter (Phase 5 doesn't
have a trade-counter flow yet — decline and propose a new one).

Both sides agree → status = `pending_approval`. Commissioner runs
`/market-admin approve-trade trade_id: <id>` and the bot:

- Transfers each contract to the receiving team
- Contract values are **not touched** (transfer is untouched deal)
- Drops the driver from the old team role, adds them to the new team
  role (via the same code path `/roster sign` uses)
- Writes ledger entries on both cap sheets

You can withdraw with `/trade withdraw trade_id: <id>` any time before
the commissioner acts.

### Release & buyout

Ending a contract early:

```
/contract release contract_id: 42 note: "salary dump"
```

Release records the driver's frozen **P/L at release** in the ledger
(market − contract at the moment you released them). Team role
dropped, free-agent role restored, driver back on the market. No cap
hit.

Or with a buyout:

```
/contract buyout contract_id: 42 buyout_m: 3.00 note: "rebuild"
```

Same as release, but $3M lands as **dead money** on your cap for
the season — visible on `/market team` and counted against your cap
when you try to sign anyone else.

Neither release nor buyout is allowed while the contract is in an
open trade — resolve the trade first (accept, decline, or withdraw).

---

## Part 3 — Driver: responding to an offer

You get pinged in a private thread in the commissioner channel with
an offer card. Three commands:

```
/contract accept offer_id: <id>
/contract decline offer_id: <id> note: "not enough"
/contract counter offer_id: <id>
```

`/contract counter` opens a modal pre-filled with the team's terms so
you can tweak salary, term, bonus, incentives, and add a note. The
counter goes back to the team; they can then counter again, accept,
or withdraw.

Once you **accept**, the offer sits in `pending_approval` awaiting the
commissioner. You cannot change your mind after accept — cancel by
asking the commissioner to reject on approval.

To check what's on your table:

```
/contract status driver: @You
```

Shows your active contract, any open offers, and your contract
history.

---

## Part 4 — Public market surfaces (everyone)

All of these are ephemeral — replies are visible only to the person
who ran the command. Public artifacts live on the auto-updating boards.

```
/market view tier: t1                    # paginated market table with Prev/Next
/market movers tier: t1                  # top 5 risers + top 5 fallers
/market driver member: @Verstappen       # driver card: value, week movement, trend
/market dashboard                        # cross-tier top-of-tier summary

/market team name: red-bull              # cap sheet: payroll, cap space, per-driver P/L
/market surplus tier: t1                 # best contracts by P/L (biggest bargains)
/market underwater tier: t1              # worst contracts by P/L (biggest overpays)
```

Everything money-side is a **Decimal** end to end. No floats,
no rounding drift.

---

## Part 5 — Commissioner: valuations, approvals, admin

### Publishing values

Valuations are always **dry-run first**. Nothing is public until you
click publish.

```
/market-admin valuation run tier: t1 round_label: "Post-Abu Dhabi"    # dry-run
/market-admin valuation preview run_id: <id>                          # re-show
/market-admin valuation publish run_id: <id>                          # go live
/market-admin valuation list tier: t1                                 # last 20 runs
```

Every run records a per-driver breakdown (per-factor contribution +
clip flags) in `driver_valuations.breakdown` so any value is fully
explainable months later.

A run prices whichever round you name in `round_label`, provided you
have imported results under that exact label first. If no round matches,
the run still works but produces no movement — that is how you set a
pre-season baseline.

### Importing race results

This is the step that makes values actually move. The weekly rhythm is:

```
/market-admin results import tier: t1 round_label: "R14 Abu Dhabi" sheet: <sheet URL>
/market-admin results show   tier: t1 round_label: "R14 Abu Dhabi"      # check it
/market-admin valuation run  tier: t1 round_label: "R14 Abu Dhabi"      # dry-run
/market-admin valuation publish run_id: <id>                            # go live
```

Your sheet holds **raw facts only** — who finished where, who started
where, who retired, fastest lap, Driver of the Day, incident points. It
never holds money. The bot does the money, every time, from the same
weights, so any value can be explained months later from the stored
breakdown.

Put a header row at the top. These column names are all understood, in
any capitalisation:

| Column | Required | Notes |
|---|---|---|
| `Driver` | yes | Must match the driver's display name in that tier |
| `Pos` | yes | `4`, `P4`, `4th`, or `DNF` / `Ret` / `DSQ` / `DNS` |
| `Grid` | no | Feeds the qualifying and pole factors |
| `DNF` / `DNS` | no | `Y`, `yes`, `true`, `1`, `x` |
| `FL` | no | Fastest lap |
| `DOTD` | no | Driver of the Day |
| `Incidents` | no | Incident or penalty points |
| `Notes` | no | Free text, kept with the row |

The import is all-or-nothing. A misspelled driver name, two drivers in
the same finishing position, or an unreadable cell aborts the whole
thing and tells you which sheet row to fix. Nothing is written until
every row is clean — a half-imported round means someone silently gets
no movement that week.

**Got a stewards' decision after the fact?** Fix the sheet and re-import
the same `round_label`. That corrects the round in place instead of
creating a phantom second race, then re-run and re-publish the
valuation.

### Why a win is worth more than a P2

Raw finishing positions are not fed to the engine directly. Each
position is looked up in a per-season **score curve** which maps P1 to
the highest score and last place to zero. The curve is intentionally
non-linear, so P1 can be worth disproportionately more than P2 — and
because it is stored data, you can reshape how steeply your league
rewards the front of the field without touching code.

Three factors are derived rather than read off the sheet:

- **Form trend** — recent-window average pace minus everything before
  it. A driver's first rounds read as neutral, not as a collapse.
- **Consistency** — how little a driver's results vary across the
  window. A single race scores zero here rather than a free perfect
  mark.
- **Exceptional weekend** — pole, win, and fastest lap in one round.
  That drive unlocks the wider `exceptional_move_cap` instead of the
  normal weekly one.

Because history is bounded by round order, re-running an earlier round
reproduces exactly the numbers it originally saw. A later race can never
leak backwards into an earlier valuation.

### Should an AI set the values?

No — and the split matters. An AI is genuinely useful for reading a
results screenshot into your sheet, judging how serious an incident was,
or writing the market commentary. It should never write driver values or
ledger rows directly. Money needs to be reproducible on demand when
someone disputes a contract, and the bot's stored per-factor breakdown
is that receipt. A value an AI produced once cannot be recomputed.

### Approving contracts

Driver-accepted offers show up as `pending_approval`:

```
/market-admin approve offer_id: <id>          # execute the sign / extension
/market-admin reject offer_id: <id> note: ""  # reject
```

Approval:

- Creates (or updates, for extensions) the active contract
- Snapshots `value_at_signing` from the driver's latest published
  market value
- Generates a public transaction id like `T2-S7-0042`
- Assigns the team role (using the same helper `/roster sign` uses)
- Posts a signed-contract embed to the transactions channel
- Writes `offer_approved` + `contract_signed` ledger rows

### Approving trades

```
/market-admin approve-trade trade_id: <id>
/market-admin reject-trade trade_id: <id> note: ""
```

Approve transfers contracts to the receiving teams and does the role
swaps. If a role swap fails (bot lacks permission, driver left the
server, etc.) the reply lists what went wrong — the money side still
lands as the authoritative record.

### Voiding & admin overrides

```
/market-admin void contract_id: <id> note: "punishment"
/market-admin set-status driver: @Someone status: Suspended
/market-admin adjust-cap team: red-bull delta_m: -5.00 note: "test-day breach"
/market-admin promote driver: @Someone new_tier: t1 note: "midseason promotion"
/market-admin relegate driver: @Someone new_tier: t3
```

**Note:** `adjust-cap` writes a ledger entry today but does **not**
yet reduce/expand the effective cap in the cap-sheet math. Buyouts
(via `/contract buyout`) do, via the dead-money mechanism. Full
`adjust-cap` enforcement is on the roadmap.

Promotion / relegation moves the driver *and* their active contract
to the new tier atomically. Cap-hit-free — the money follows the
driver. Next valuation run recalibrates their rank in the new tier.

---

## Cheat sheet

**Commissioner (Manage Server)**

```
/market-admin season create|activate|list
/market-admin tier add|edit|list
/market-admin config show|edit|channel|role|free-agency
/market-admin results import|list|show
/market-admin valuation run|preview|publish|list
/market-admin board add|remove|refresh|list
/market-admin approve|reject|void|set-status|adjust-cap
/market-admin approve-trade|reject-trade|promote|relegate
```

**Team Principal** (team's principal role)

```
/contract offer                       # new / extension / trade_and_sign
/contract offers                      # my team's open offers
/contract withdraw offer_id: <id>
/contract release contract_id: <id> note: ""
/contract buyout contract_id: <id> buyout_m: <M> note: ""
/trade propose                        # 1-for-1 swap
/trade withdraw trade_id: <id>
```

**Driver**

```
/contract accept offer_id: <id>
/contract decline offer_id: <id> [note]
/contract counter offer_id: <id>
```

**Everyone**

```
/market view|movers|driver|team|surplus|underwater|dashboard
/contract status driver: @Someone
/trade status trade_id: <id>
/trade accept|decline trade_id: <id>          # (if you're the other team's TP)
```

---

## Glossary

- **Season** — one competitive year of the league.
- **Tier** — a competitive division within a season (T1/T2/T3 by
  default). Markets are tier-isolated.
- **Driver** — a per-(season, tier, member) row that carries market
  value and status. One member can have multiple driver rows across
  tiers.
- **Contract Value** — salary the team locked in at signing. Frozen
  except by extension/renegotiation. Counts against the cap.
- **Market Value** — the recomputed number that reflects
  performance. Drives free-agent cost, trade value, extension
  demands. Does NOT count against the cap.
- **P/L** — market − contract. `+` is a bargain, `−` is an overpay.
- **Valuation run** — one computed set of market values for one
  tier. Always created as a dry-run; published later.
- **Movement cap** — the max amount a driver's market value can move
  in one cycle (default ±$0.75M standard, ±$1.25M "exceptional").
- **Payroll** — sum of active contract values for a team.
- **Effective payroll** — payroll + dead money. This is what cap
  compliance checks against.
- **Dead money** — a cap hit from a buyout that persists for the
  season.
- **Ledger** — the append-only log of every money mutation. Nothing
  in this system overwrites; everything appends.
- **External ref** — human-readable contract id like `T2-S7-0042`.

---

## FAQ

**Can two drivers with the same market value happen?**
Yes. The engine ranks descending by value with a stable tiebreak on
driver id.

**What happens to my dead money next season?**
Phase 5 records dead money per season. Season-rollover automation
(carrying dead money into future seasons with a decay schedule) isn't
in yet — right now each row applies only in the season it was created.

**Can a team trade a driver + cash / draft pick / cap space?**
Not in Phase 5. Phase 5 ships 1-for-1 contract swaps. Multi-contract
trades and non-contract assets are on the roadmap.

**A driver's status is `inactive` — can they still be offered?**
Yes, but with a `⚠ status_inactive_warning` — the offer flows through
and the commissioner can approve it. `suspended` is a hard block.

**Does market value affect cap compliance?**
No. Cap compliance is measured on contract value only. Market value
drives the *cost* of new signings, trades, and extensions.

**Do trades change contract values?**
No — that's the whole point of pure trades (CLAUDE.md §2 rule 6). If
you want to renegotiate, do a trade-and-sign (offer_kind on `/contract
offer`) after the trade lands.

**The bot didn't assign the team role on approval — what now?**
The commissioner's approve command reports which role swap failed
(usually because the bot's role sits below the team roles). Fix the
role ordering and run `/roster sign name: <team> member: @driver` to
finish the assignment manually. The money-side record is already
authoritative in the DB.

**A message the bot manages was deleted (roster embed, market board).**
Run `/roster refresh` or `/market-admin board refresh` and it reposts.

---

## Something look wrong?

The bot writes every state change to the ledger, including the actor.
For anything money-side, `/contract status driver: @Someone` shows
what happened to that driver. For team-level questions, `/market team
name: <team>` is the summary; the commissioner has DB-level access to
the full ledger for deeper audits.

For missing features / bugs, file an issue on the repo.
