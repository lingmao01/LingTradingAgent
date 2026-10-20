---
name: Advice review
description: Weekly review of the advice journal's open calls, graded against this run's retrieved prices and closed once their thesis has played out or failed.
markets: [global, cn, hk, us, crypto]
suggested_schedule: "0 18 * * 0"
suggested_timezone: Asia/Shanghai
data_capabilities:
  - The latest price for every symbol the open journal entries name, from the most recent completed session
  - The recent daily range for the same symbols, so a single session's move can be told from noise
  - Session dates that establish which session each price belongs to
  - Reference rates when the journal spans more than one currency
---

# Advice review

Review the advice journal's open calls against what the market did since they
were recorded, on a weekly cadence, and close the calls whose thesis has
played out or failed.

Resolve the current date from the run environment, and take the session dates
from the data actually retrieved: every call is graded against the session the
retrieved price belongs to, not the day the run happens to fire.

## Validated publication review

First read the validated journal's due publications and follow every next page until
all due publications have been read; the eight-entry context preview is not a
complete queue. For each publication run the published-decision review capability with its
publication id. This uses its recorded 5/20-observation review protocol and
paper baselines (hold, periodic invest, rebalance, benchmark) with explicit
cost sensitivity. Do not change the horizon or benchmark after seeing results.

Keep execution marked "not reported" unless the user supplied an execution
record. Paper wealth is not realized P&L, a conditional permit is not a trade,
and no success probability is inferred. Report incomplete horizons and missing
initial weights as they are; do not redistribute missing asset weights. A
computed publication review is saved by that tool; do not rewrite its original
decision. Review legacy notes separately below; their presence alone does not
prove they were published or executed.

## Inputs

The `<advice-journal>` block carried in this run's context holds this journal's
open entries, rebuilt fresh on every fire. This template freezes no entries of
its own: when both the block and the due-publication queue are empty, write
`no open entries` and stop for the week — never fall back to an earlier run's journal.

## Data to gather

1. The latest price for every symbol the open entries name, from the most
   recent completed session.
2. The recent daily range for those symbols, so a single session's move can be
   told from the instrument's ordinary noise.
3. The session date each price belongs to, so a stale print is never graded as
   this week's move.
4. Reference rates when the entries span more than one currency, with the rate
   date.

## Method

- Grade every open entry against the prices retrieved on this run, and report
  it in `## Journal review` below. A call points one way: add and hold point
  up, reduce and exit point down, and a watch entry points at a condition
  rather than a price; when the entry's own rationale named a condition or a
  level, grade against that condition exactly as it was recorded.
- Use the outcome words: `PLAYED OUT` when the move since the reference has
  gone the call's way by more than the instrument's recent daily range;
  `INVALIDATED` when it has gone against the call by more than that range;
  `RUNNING` while neither side is settled; `UNGRADED` when no price was
  retrieved and the entry cannot be scored at all.
- The reference price recorded with an entry is a figure from this journal:
  cite it as the journal's own record, never present it as a price observed in
  the current session.
- Closing is the only legacy-entry write this review performs. Close an entry when its
  thesis has played out or failed and the move is more than one session's
  noise, and write the outcome into the close note. A move smaller than the
  recent daily range is noise, not an outcome: keep such an entry open and
  grade it `RUNNING`. An entry left open is graded again next week.
- Never rewrite, reword, or remove a recorded entry to tidy the journal, and
  never close an entry just because its review is due.

## When data is missing

A source that returns nothing, errors out, or is not configured is a fact to
report, not a gap to fill.

- Name every missing item explicitly in a `Data gaps` section, with the reason
  when the failure gave one.
- Continue using only the evidence actually retrieved.
- Never substitute a value from memory, from a general prior, from a
  third-party summary, or from an earlier run of this playbook.
- An entry whose price was not retrieved is graded `UNGRADED` on its line,
  with the missing item named in `Data gaps`.
- If no price could be retrieved for any symbol, write `no data retrieved`
  under each section heading that has no evidence.

## Output

Markdown, in this order:

1. `## Journal review` — one line per open entry: `- #ID SYMBOL: OUTCOME - the
   reference, this run's price and the session it is as of, and one short
   reason`, with OUTCOME one of `PLAYED OUT`, `RUNNING`, `INVALIDATED`,
   `UNGRADED`. List `INVALIDATED` lines first and `UNGRADED` lines last. When
   the block is empty, write `no open entries` in one line. This section
   grades only.
2. `## Closed this run` — the entries closed on this run, one line each: the
   outcome word and the close note. When none were closed, write
   `no entries closed` — a week that closed nothing is a real answer.
3. `## Next check` — for every entry still `RUNNING`, at most one line: the
   conditional action (keep, replace, or step aside) with its trigger, its
   invalidation, and the next check. When nothing is running, keep the heading
   and write `no conditional action`.
4. `## Data gaps` — always present; write `none` when nothing was missing.
5. `## Verdict` — the machine-readable tail, and the only section nothing may
   follow. One line per entry reviewed this run:
   `- #ID SYMBOL: OUTCOME - one short reason`, with OUTCOME one of the same
   four words, ordered as in the review above.

## Boundaries

- Grading is evidence first: every score is bound to a price retrieved on this
  run, and says which session that price is as of.
- A figure read back from the journal stays a cited figure of this journal,
  never an observation of the current session.
- The `## Next check` lines are the only place a conditional action appears,
  one per running entry, each with its trigger and its invalidation; they stay
  inside the terms the entry itself stated.
- Do not place, modify, or cancel any order, and do not touch a live trading
  connector. The reader executes; the review never does.
- This is a review of the journal's own calls, not a self-appraisal: report
  what happened to each call plainly, and do not close or soften an entry to
  make the record read well.