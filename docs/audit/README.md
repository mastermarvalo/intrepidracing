# Workflow audit, September 2026

A three-document record of the audit that preceded the Phase 9 update
(salary escrow, race-denominated contracts, the twelve panel screens).

| Document | What it is | Trust it for |
|---|---|---|
| [`FINDINGS-2026-09.md`](FINDINGS-2026-09.md) | The audit as written 2026-09-14, 26 findings `G1`–`G26`, each traced click → DB write → message | **Why** something was wrong and why it mattered |
| [`STATUS-2026-09.md`](STATUS-2026-09.md) | Per-finding FIXED / PARTIAL / OPEN verified against `main` at `528fe0e` on 2026-09-15 | **Where things stand**, with file + symbol evidence |
| [`GUI-CONSOLIDATION-2026-09.md`](GUI-CONSOLIDATION-2026-09.md) | The pre-implementation design spec for the twelve screens, plus a list of what shipped differently | The **intent** behind a screen's shape |

As of the last verification: **19 fixed, 4 partial, 3 open.**

Still open — all three concern administrative contract erasure and driver
movement, none are reachable by an ordinary Team Principal:

- **G4** — moving a driver who legitimately races in two tiers fails with a
  raw database error instead of a plain-language reason.
- **G6** — voiding a contract leaves the driver holding the Discord team role,
  with no warning. Release and buyout do drop it.
- **G7** — void skips the open-trade guard that release has, which can leave a
  pending trade unresolvable by any button.

Partial — in every case the panel path is correct and the typed command is
not: **G2** (a season given tiers but no config row is still a dead end),
**G18** (cap adjustments still promise enforcement that does not exist),
**G20** (typed board commands report success without checking), **G21** (typed
season commands offer no next step and carry-over has no dry-run).

## Reading these later

Line numbers in `FINDINGS` and `GUI-CONSOLIDATION` are from the tree at the
time of writing and are **stale** — search by symbol. `STATUS` was written
against a single commit; re-verify before relying on it. Neither `FINDINGS`
nor `GUI-CONSOLIDATION` was rewritten after implementation, on purpose: they
are the record of what was believed at the time, and rewriting them would
destroy that.

Current open items for the codebase as a whole live in `CLAUDE.md` §17, which
is the list to maintain. These documents are history.
