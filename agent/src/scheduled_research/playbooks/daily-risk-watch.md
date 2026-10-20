---
name: Daily risk watch
description: Weekday after-close inspection of the reader's standing risk rules, checked against the latest completed session.
markets: [global, cn, hk, us, crypto]
suggested_schedule: "0 16 * * 1-5"
suggested_timezone: Asia/Shanghai
data_capabilities:
  - The latest daily close for every symbol the current book or the standing rules name, from the most recent completed session
  - The prior session's close for the same symbols, for the day's move
  - Session dates that establish which session each close belongs to
  - Reference rates when the book spans more than one currency
  - Daily close history for the broad indices the portfolio-discipline block names, long enough for its moving-average windows and momentum confirmation
---

# Daily risk watch

Inspect the reader's standing risk rules against the most recent completed
session, on a weekday cadence after the close.

Resolve the current date from the run environment, and take the session dates
from the data actually retrieved: the watch reports the session the latest
close belongs to, not the day the run happens to fire.

## Inputs

The `<user-book>`, `<portfolio-discipline>` and `<risk-rules>` blocks carried
in this run's context are the reader's current book, their portfolio-level
discipline, and their standing per-instrument rules, rebuilt fresh on every
fire. This template freezes no holdings, no ceilings and no levels of its own:
when the blocks are empty, say so — never fall back to an earlier run's book,
discipline or rules.

## Data to gather

1. The latest daily close for every symbol the current book or the rules block
   names, from the most recent completed session.
2. The prior session's close for the same symbols, for the day's move.
3. The session date each close belongs to, so a stale print is never reported
   as today's.
4. Reference rates when the book spans more than one currency, with the rate
   date.
5. Daily close history for every broad index the discipline block's regime
   names, long enough to compute the short and long moving averages it records
   and to count the momentum-confirmation sessions.

## Method

- Check the rules before anything else, for every position the current book
  and the rules block name. Against this run's latest retrieved close, a long
  position is breached at or below its stop-loss and has reached its
  take-profit at or above it (short positions: the reverse); a position whose
  absolute size exceeds its maximum quantity is over the cap. Run the check
  for every rule in the block and report it in `## Risk rules` below.
- A rule that is not written in the context block is not a rule: never invent,
  move, or soften a level, and never re-propose one the reader removed.
- A position with no rule recorded is still watched: report its move, and say
  it carries no rule rather than inventing one.
- Read the reader's portfolio discipline from the `<portfolio-discipline>`
  block and evaluate — never assume — today's gates and compliance from the
  closes retrieved on this run. Each `Gate` line the block records is a
  permission on an action, judged on its own recorded condition against this
  run's closes: report it `OPEN` or `CLOSED`. The gates are independent —
  several may be open at once — so never collapse them into a single
  first-match-wins verdict, and never invent a gate the block does not record.
  The windows a gate's condition is written in are the same block's regime
  line. Compute each cluster's share of the book from the values the reader
  recorded — a holding joins a cluster when the name the reader gave it carries
  one of that cluster's keywords, and a holding the cap says it "always
  includes" joins whatever its name says — the current drawdown from the peak,
  and, for every holding whose recorded purchase date falls inside a redemption
  tier, the fee a sale today would charge. Report all of it in
  `## Portfolio discipline`.
- Hold the discipline as a gate, not as colour. When the block's `NO ADD` gate
  is open, nothing may be added: say so before any move line. When a cluster
  sits at or over its ceiling, name it. When the drawdown has passed the
  breaker, say the breaker has tripped. When a sale would pay a punitive
  redemption fee, warn before any trim is suggested. A recorded guardrail is in
  force: when the current setup is the one a guardrail names, say so and say
  why.
- Compute every move from prices retrieved on this run, and report the session
  each close is as of. Never compare closes from two different sessions
  without saying so.

## When data is missing

A source that returns nothing, errors out, or is not configured is a fact to
report, not a gap to fill.

- Name every missing item explicitly in a `Data gaps` section, with the reason
  when the failure gave one.
- Continue using only the evidence actually retrieved.
- Never substitute a value from memory, from a general prior, from a
  third-party summary, or from an earlier run of this playbook.
- A rule that cannot be checked is said, not guessed: when a rule's symbol has
  no price retrieved on this run, or no position in the current book, say so
  on its line in `## Risk rules` and name the missing item in `Data gaps`.
- A discipline item that cannot be computed is said, not estimated: when no
  regime index close was retrieved, or no book value is available for a
  cluster, say the gate or the share could not be computed on this run and name
  the missing item in `Data gaps`.
- If no close could be retrieved for any symbol, write `no data retrieved`
  under each section heading that has no evidence.

## Output

Markdown, in this order:

1. `## Risk rules` — the check from Method, breached lines first, then the
   levels still standing. One line per recorded rule:
   `- SYMBOL: STATUS - the rule level, this run's close, and the date that
   close is as of`, with STATUS one of `BREACHED`, `REACHED`, `OVER CAP`,
   `CLEAR`, `UNKNOWN`. Write `no risk rules recorded` in one line when the `<risk-rules>`
   block is empty, and a plain sentence of why on the line when a rule could
   not be checked. This section restates the reader's own levels; it proposes
   none.
2. `## Portfolio discipline` — the review from Method, one line per recorded
   item:
   - Regime: each broad index the block names, this run's close against the
     short and long averages, and the session each close is as of.
   - Gates: one line per gate the block records — its label, the condition as
     recorded, and `OPEN` or `CLOSED` from this run's closes. Evaluate the
     gates independently: several may be open at once.
   - Cluster caps: one line per cluster — its current share of the book against
     its recorded ceiling, and a state of `OVER CAP`, `AT CAP` or `CLEAR`, `UNKNOWN`.
   - Drawdown breaker: the current drawdown from the peak against the recorded
     level, and whether it has tripped.
   - Redemption fees: one line per holding whose recorded purchase date falls
     inside a tier — the days held and the fee a sale today would charge; write
     `none` when no holding is inside a window. A purchase date that is not
     recorded is said, not assumed: never guess how long a holding has been
     held.
   - Guardrails: one line per recorded guardrail, and whether the current setup
     is the one it names.
   Write `no portfolio discipline recorded` in one line when the
   `<portfolio-discipline>` block is empty. This section restates the reader's
   own ceilings, windows and fees; it proposes none.
3. `## Moves` — one line per watched symbol: last close, move against the
   prior close, and the session the close is as of; symbols with no rule are
   marked as such.
4. `## Stance` — only for symbols whose rule is breached, reached or over the
   cap: at most one line each, the conditional action (hold, add, trim, or
   stay out) with its trigger, its invalidation, and the next check; trigger
   and invalidation levels are proposed figures. When nothing triggered, keep
   the heading and write `no conditional action` — a quiet day is a real
   answer.
5. `## Data gaps` — always present; write `none` when nothing was missing.
6. `## Verdict` — the machine-readable tail, and the only section nothing may
   follow. One line per rule checked this run:
   `- SYMBOL: STATE - one short reason`, with STATE one of `BREACHED`,
   `REACHED`, `OVER CAP`, `CLEAR`, `UNKNOWN`. Then, after those lines, the portfolio
   lines: one `- PORTFOLIO: GATE - one short reason` line per gate the block
   records, with GATE that gate's own action word (`NO ADD`, `REDUCE` or
   `ADD ALLOWED`) when the gate is open and `CLOSED` when it is demonstrably false and `UNKNOWN` when the observation or quantitative definition is missing; one
   `- PORTFOLIO: OVER CAP - one short reason` line for each cluster at or over
   its ceiling; and, when the breaker has tripped, one
   `- PORTFOLIO: TRIPPED - one short reason` line. When the rules block is
   empty and the discipline block records nothing to report, write the heading
   with no lines under it; that is a real answer, not an absence. This section
   reports state only: the Boundaries below still apply.

## Boundaries

- Inspection first: every figure is computed from this run's prices and says
  which session it is as of.
- The levels in the risk rules are the reader's own figures, quoted back at
  them exactly as recorded: never move, widen, drop or re-propose a stop, a
  target or a cap, and never turn a breach into an instruction of your own.
- The discipline is the reader's own framework, enforced from this run's
  retrieved closes: each gate, each cluster share, the drawdown and the
  redemption-fee window are computed, never assumed, and a ceiling, a window or
  a fee is never moved, widened or dropped to fit the day. When the discipline
  and an impulse disagree, the discipline wins: the report says the gate is
  closed rather than talking past it, and it never re-proposes a level the
  reader removed.
- The stance lines are the only place a conditional action appears: one per
  triggered symbol, each with its trigger, its invalidation, and the next
  check; trigger and invalidation levels are proposed figures, never observed
  prints. They stay inside the terms the reader's own rules and plan state.
- Do not place, modify, or cancel any order, and do not touch a live trading
  connector. The reader executes; the report never does.
- A daily watch describes one session. Do not present it as a forecast, and do
  not let a quiet session soften a standing level.

For an off-exchange feeder book, use its own disclosed NAV and confirmed lots,
not its listed ETF price. Preserve UNKNOWN states for undefined conditions or
cash-flow-reconciled account drawdown; a missing observation is never CLOSED.
An explicit confirmation-date fee scenario is an estimate, not settlement.
