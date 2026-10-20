---
name: Pre-market Brief
description: Overnight global market moves plus today's watch items, assembled before the home session opens.
markets: [global, cn, hk, us]
suggested_schedule: "30 8 * * 1-5"
suggested_timezone: Asia/Shanghai
data_capabilities:
  - Daily index levels and percentage changes for the major overnight equity sessions
  - Overnight moves in benchmark commodities, major FX pairs and government bond yields
  - Headline news published since the previous home-market close, with publication timestamps
  - Recent daily price and volume history for the symbols on the user's watch list
  - The latest close for every position the current book or the standing rules name
  - Daily close history for the broad indices the portfolio-discipline block names, long enough for its moving-average windows and momentum confirmation
  - Macro releases and corporate events scheduled for the current date
variables:
  home_market: China A-shares
  watchlist: (no watch list configured)
---

# Pre-market brief

Produce a pre-open briefing for **{{home_market}}**.

Resolve the current date and time from the run environment on every run. This
instruction text is stored once and replayed on every fire, so it contains no
date of its own — never assume the day it was written is the day it is running.

## Inputs

- Home market: {{home_market}}
- Watch list: {{watchlist}}

If the watch list above is empty or says none is configured, skip the
per-symbol section entirely and say so in one line rather than inventing a
list of symbols.

The `<user-book>`, `<portfolio-discipline>` and `<risk-rules>` blocks carried
in this run's context are the reader's current book, their portfolio-level
discipline, and their standing per-instrument rules, rebuilt fresh on every
fire. They do not replace the watch list above — the watch list stays the list
configured here — but every rule the blocks name is checked and reported in
`## Risk rules` below, and the discipline is pre-checked and reported in
`## Portfolio discipline` below, whether or not its symbols are on the watch
list.

## Data to gather

1. Closing level and percentage change of the major equity indices whose
   sessions ran while the home market was shut, plus the home market's own
   previous close.
2. Overnight change in the benchmark energy and metals contracts, the main FX
   pairs relevant to the home market, and the 10-year government bond yield of
   the home market and of the United States.
3. Headlines published since the previous home-market close that name the home
   market, its index, its regulator, or any watch-list symbol. Keep the
   publication timestamp attached to each headline.
4. For each watch-list symbol: the last close, the change over the last five
   sessions, and volume against its recent average.
5. Anything scheduled for today — macro data releases, central bank events,
   earnings dates, index changes — that falls in the home market's session.
6. The latest close for every symbol the current book or the rules block
   names, so each recorded level is checked against a retrieved price and
   never against an assumed one.
7. Daily close history for every broad index the discipline block's regime
   names, long enough to compute the short and long moving averages it records
   and to count the momentum-confirmation sessions.

## Method

- Order the report by how much it can move the home session, not by how
  interesting the headline is.
- Separate what moved from why it moved. Only assert a cause when a retrieved
  source states that cause; otherwise report the move and the coincident
  headline as two separate facts.
- A move smaller than the instrument's own recent daily range is noise. Say so
  instead of narrating it.
- Check the standing rules before any tape item. Against this run's latest
  retrieved close, a long position is breached at or below its stop-loss and
  has reached its take-profit at or above it (short positions: the reverse);
  a position whose absolute size exceeds its maximum quantity is over the cap.
  Run the check for every rule the current book and the rules block name, and
  report it in `## Risk rules`. A rule that is not written in the context
  block is not a rule: never invent, move, or soften a level.
- Run the discipline pre-check alongside the rules, before the tape. Evaluate
  from the closes retrieved on this run: each `Gate` line the block records is a
  permission on an action, judged on its own recorded condition against this
  run's closes — report it `OPEN` or `CLOSED`. The gates are independent —
  several may be open at once — so never collapse them into a single
  first-match-wins verdict, and never invent a gate the block does not record.
  The windows a gate's condition is written in are the same block's regime line.
  Compute each cluster's share of the book — a holding joins a cluster when the
  name the reader gave it carries one of that cluster's keywords, and a holding
  the cap says it "always includes" joins whatever its name says — the current
  drawdown from the peak against the breaker, and, for every holding whose
  recorded purchase date falls inside a redemption tier, the fee a sale today
  would charge. Say the gate plainly: when the block's `NO ADD` gate is open,
  the brief opens on that fact rather than on the tape. Report it in
  `## Portfolio discipline`.
- The brief does not know the open. Say the check used the latest retrieved
  close, and never present an assumed opening print as an observation.

## When data is missing

A source that returns nothing, errors out, or is not configured is a fact to
report, not a gap to fill.

- Name every missing item explicitly in a `Data gaps` section, with the reason
  when the failure gave one.
- Continue using only the evidence actually retrieved.
- Never substitute a value from memory, from a general prior, from a
  third-party summary, or from an earlier run of this playbook. A number that
  did not come back from a source on this run does not appear in this report.
- Never present a stale figure as current. If the freshest value available is
  older than the window this brief covers, print its as-of date beside it.
- If a whole section has no evidence, keep its heading and write
  `no data retrieved` under it.
- Every figure carries its as-of date and the source it came from.
- A rule that cannot be checked is said, not guessed: when a rule's symbol has
  no price retrieved on this run, or no position in the current book, say so
  on its line in `## Risk rules` and name the missing item in `Data gaps`.
- A discipline item that cannot be computed is said, not estimated: when no
  regime index close was retrieved, or no book value is available for a
  cluster, say the gate or the share could not be computed on this run and name
  the missing item in `Data gaps`.

## Output

Markdown, in this order:

1. `## Risk rules` — the check from Method, breached lines first, then the
   levels still standing. One line per recorded rule:
   `- SYMBOL: STATUS - the rule level, the latest retrieved close, and the
   date that close is as of`, with STATUS one of `BREACHED`, `REACHED`,
   `OVER CAP`, `CLEAR`; when every recorded rule reads `CLEAR`, one summary
   line is enough. Write `no risk rules recorded` in one line when the
   `<risk-rules>` block is empty, and a plain sentence of why on the line when
   a rule could not be checked. This section restates the reader's own levels;
   it proposes none.
2. `## Portfolio discipline` — the pre-check from Method, terse, one line per
   recorded item: the regime (each broad index the block names, this run's
   latest close against the short and long averages); each gate the block
   records (its label, the condition as recorded, and `OPEN` or `CLOSED` from
   this run's closes — the gates are independent, so several may be open); each
   cluster's share of the book against its recorded ceiling with a state of
   `OVER CAP`, `AT CAP` or `CLEAR`; the current drawdown against the recorded
   breaker; and one line per holding whose recorded purchase date sits inside a
   redemption tier — the days held and the fee a sale today would charge. A
   purchase date that is not recorded is said, not assumed: never guess how long
   a holding has been held. Write `no portfolio discipline recorded` in one line
   when the `<portfolio-discipline>` block is empty. This section restates the
   reader's own ceilings, windows and fees; it proposes none.
3. `## Overnight tape` — a table of index / commodity / FX / yield, level,
   change, as-of timestamp.
4. `## What changed overnight` — at most five bullets, each tied to a dated
   headline.
5. `## Watch list` — one line per symbol: last close, 5-day change, volume vs
   average, any dated news naming it, and — when actions are in scope — the
   conditional action with its trigger and invalidation.
6. `## On today's calendar` — scheduled releases and events, with local times.
7. `## Data gaps` — always present; write `none` when nothing was missing.
8. `## Verdict` — the machine-readable tail, and the only section
   nothing may follow. One line per symbol tracked this run:
   `- SYMBOL: STATE - one short reason`, with STATE one of `HOT`, `MIXED`, `QUIET`.
   Then, after those lines, the portfolio lines: one
   `- PORTFOLIO: GATE - one short reason` line per gate the block records, with
   GATE that gate's own action word (`NO ADD`, `REDUCE` or `ADD ALLOWED`) when
   the gate is open and `CLOSED` when it is not; one
   `- PORTFOLIO: OVER CAP - one short reason` line for each cluster at or over
   its ceiling; and, when the breaker has tripped, one
   `- PORTFOLIO: TRIPPED - one short reason` line. When nothing moved and the
   discipline block records nothing to report, write the heading with no lines
   under it; that is a real answer, not an absence. This section reports state
   only: the Boundaries above still apply.

Keep the whole brief under roughly 800 words.

## Boundaries

- This is a factual briefing, not a forecast: it reports the setup and does
  not predict the day's direction.
- Stance lives in one place only — the watch list. When the watch list carries
  the reader's own instruments, give each symbol at most one conditional
  action — hold, add, trim, or stay out — with its trigger, its invalidation,
  the evidence behind it, and the next check; trigger and invalidation levels
  are proposed figures, never observed prints. Every other section is
  factual: no stance, no target prices, no position sizing, no leverage
  suggestions.
- The levels in the risk rules are the reader's own figures, quoted back at
  them exactly as recorded: never move, widen, drop or re-propose a stop, a
  target or a cap, and never turn a breach into an instruction of your own.
- The discipline is the reader's own framework, enforced from this run's
  retrieved closes: each gate, each cluster share, the drawdown and the
  redemption-fee window are computed, never assumed, and a ceiling, a window or
  a fee is never moved, widened or dropped to fit the day. When the discipline
  and an impulse disagree, the discipline wins: the brief says the gate is
  closed rather than talking past it.
- Do not place, modify, or cancel any order, and do not touch a live trading
  connector. The reader executes; the report never does.
