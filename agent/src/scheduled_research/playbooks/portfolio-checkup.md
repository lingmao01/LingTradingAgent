---
name: Portfolio Checkup
description: Periodic risk x-ray of a stated book — exposure, concentration, correlation and drawdown, measured from retrieved prices.
markets: [global, cn, hk, us, crypto]
suggested_schedule: "0 9 * * 6"
suggested_timezone: Asia/Shanghai
data_capabilities:
  - Daily price history long enough to compute volatility, correlation and drawdown for every position
  - Portfolio-level risk decomposition — position weights, volatility contribution, correlation structure, drawdown
  - Sector, industry and listing-venue classification for each holding
  - Benchmark index history for the same window as the holdings
  - Currency reference rates when the book spans more than one currency
variables:
  holdings: (no holdings provided — the report cannot run without them)
  benchmark: the broad index of the book's home market
  lookback_days: "252"
---

# Portfolio checkup

Run a risk x-ray on the book below over a {{lookback_days}}-session lookback,
against **{{benchmark}}**.

Resolve the current date from the run environment; the lookback window ends on
the most recent session for which prices were actually retrieved, not on an
assumed date.

## Inputs

- Holdings: {{holdings}}
- Benchmark: {{benchmark}}
- Lookback: {{lookback_days}} sessions

If no holdings are supplied, stop and say so in one line. Do not invent a book,
do not reuse a book from an earlier run, and do not fall back to an index as a
stand-in portfolio.

The `<user-book>` and `<risk-rules>` blocks carried in this run's context are
the reader's current book and standing rules. The Holdings line above was
frozen when this playbook was saved; when the two disagree, the blocks are the
current state — measure the book the blocks describe, and say in one line
which book this run used.

## Data to gather

1. Daily closes for every holding covering the full lookback, plus the same
   window for the benchmark.
2. Position weights implied by the supplied quantities and the latest retrieved
   close. If the input gives weights rather than quantities, use them as given
   and say so.
3. A risk decomposition of the book: per-position volatility contribution,
   pairwise correlation structure, realised portfolio volatility, maximum
   drawdown and the dates it ran between.
4. Sector, industry and listing venue for each holding.
5. Where holdings settle in different currencies, the reference rates needed to
   express everything in one currency, with the rate date.

## Method

- Check the standing rules before any statistic. Against this run's latest
  retrieved close, a long position is breached at or below its stop-loss and
  has reached its take-profit at or above it (short positions: the reverse);
  a position whose absolute size exceeds its maximum quantity is over the cap.
  Run the check for every position the current book and the rules block name,
  and report it in `## Risk rules` below. A rule that is not written in the
  context block is not a rule: never invent, move, or soften a level.
- Compute every statistic from the price series retrieved on this run. Report
  the exact number of sessions each statistic used.
- A holding whose price history is shorter than the lookback is measured over
  its own available window, and that shorter window is printed beside its
  numbers. Never pad, interpolate, or back-fill a missing series.
- Concentration is reported on three axes at once: single position, sector, and
  listing venue or currency. A book can look diversified on one and be
  concentrated on another.
- Drawdown is stated with both its depth and its date range, and whether the
  book has recovered.
- If the book contains both long and short exposure, say so and state which
  statistics remain meaningful under netting rather than reporting a single
  blended number as if the book were long-only.

## When data is missing

A source that returns nothing, errors out, or is not configured is a fact to
report, not a gap to fill.

- Name every missing item explicitly in a `Data gaps` section, with the reason
  when the failure gave one.
- Continue using only the evidence actually retrieved.
- Never substitute a value from memory, from a general prior, from a
  third-party summary, or from an earlier run of this playbook. A price series
  that did not come back on this run does not enter any calculation.
- Never present a stale figure as current. Print the as-of date of the last
  close used for every holding.
- If a whole section has no evidence, keep its heading and write
  `no data retrieved` under it.
- A holding whose prices could not be retrieved is excluded from the
  statistics, listed by name in `Data gaps`, and its weight is reported as
  unmeasured — never silently dropped and never redistributed across the rest.
- A rule that cannot be checked is said, not guessed: when a rule's symbol has
  no price retrieved on this run, or no position in the current book, say so
  on its line in `## Risk rules` and name the missing item in `Data gaps`.

## Output

Markdown, in this order:

1. `## Book summary` — number of positions, currencies, as-of date, sessions
   used.
2. `## Risk rules` — the rule check from Method, breached lines first, then
   the levels still standing. One line per recorded rule:
   `- SYMBOL: STATUS - the rule level, this run's close, and the date that
   close is as of`, with STATUS one of `BREACHED`, `REACHED`, `OVER CAP`,
   `CLEAR`, `UNKNOWN`. Write `no risk rules recorded` in one line when the `<risk-rules>`
   block is empty, and a plain sentence of why on the line when a rule could
   not be checked. This section restates the reader's own levels; it proposes
   none.
3. `## Exposure` — table of holding, weight, sector, venue, last close, as-of
   date.
4. `## Concentration` — top position weight, top three combined, largest sector
   weight, largest venue or currency weight.
5. `## Risk` — realised volatility, volatility contribution ranking, the most
   correlated pairs, and the same volatility figure for the benchmark.
6. `## Drawdown` — maximum drawdown, its date range, current distance from
   peak; the same for the benchmark.
7. `## Stance` — one line per holding: the conditional action (hold, add,
   trim, or stay out) with its trigger, its invalidation, and the next check;
   trigger and invalidation levels are proposed figures.
8. `## Data gaps` — always present; write `none` when nothing was missing.
9. `## Verdict` — the machine-readable tail, and the only section
   nothing may follow. One line per symbol tracked this run:
   `- SYMBOL: STATE - one short reason`, with STATE one of `DRIFT`, `FLAT`.
   When nothing moved, write the heading with no lines under it; that
   is a real answer, not an absence. This section reports state only: the
   Boundaries above still apply.

## Boundaries

- Measurement first: every statistic is computed from this run's prices and
  says which window it used.
- The book above is the reader's own, so it is in scope for stance: give each
  holding at most one conditional action — hold, add, trim, or stay out —
  with its trigger, its invalidation, the evidence behind it, and the next
  check; trigger and invalidation levels are proposed figures, never observed
  prints. Weight changes stay inside the terms the reader's own plan states:
  no target weights and no position sizes invented here.
- The levels in the risk rules are the reader's own figures, quoted back at
  them exactly as recorded: never move, widen, drop or re-propose a stop, a
  target or a cap, and never turn a breach into an instruction of your own.
- Do not place, modify, or cancel any order, and do not touch a live trading
  connector. The reader executes; the report never does.
- Historical statistics describe the window measured. Do not present them as a
  forecast of future risk.


For an off-exchange feeder book, use its own disclosed NAV and confirmed lots,
not its listed ETF price. Preserve UNKNOWN states for undefined conditions or
cash-flow-reconciled account drawdown; a missing observation is never CLOSED.
An explicit confirmation-date fee scenario is an estimate, not settlement.
