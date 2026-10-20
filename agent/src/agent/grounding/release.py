"""What happens after a rejection: correction, recovery, repair, redaction."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Sequence

from src.agent.grounding.identity import (
    _CANONICAL_SYMBOL_RE,
    _RESOLVER_TOOL,
    _normalize_symbol,
    _utc_now,
)
from src.agent.grounding.evidence import (
    EvidenceRecord,
    _column_field_for_path,
    _is_price_kind,
    _is_registered_metric_indicator,
    _is_registered_price_indicator,
    _latest_as_of,
    _leaf_name,
)
from src.agent.grounding.figures import (
    BLOCK_LANGUAGE,
    ROUNDED_BAND,
    Declaration,
    Figure,
    _lines_with_offsets,
    parse_figures_block,
    scan_figures,
    strip_figures_block,
)
from src.agent.grounding.policies import (
    ValidationResult,
    _close,
    _formula_in_note,
)

# Bounded read-only recovery (#1081) through `search_symbol` / `get_market_data`,
# budgeted separately from rejected drafts so real progress is never cut off.
MAX_GROUNDING_RECOVERY_ROUNDS = 6

# Rejected drafts before the run stops revising: the first gets a correction
# prompt, the second is released with its rejected figures cut (spec §0).
MAX_GROUNDING_REVISIONS = 2

# Issue codes whose figure can be cut out and the draft released.
_REDACTABLE_CODES = frozenset(
    {
        "numeric_claim_conflict",
        "numeric_claim_unavailable",
        "held_value_not_provided",
        "unsourced_symbol_figures",
        "figure_undeclared",
    }
)

# Provenance codes a data note repairs deterministically. The symbol is absent
# on purpose: it is the figure's subject, and a footnote naming it under a draft
# about another instrument would bless a misattribution. ``carried_price_not_dated``
# joins them because the ledger already knows the carried prices' as-of date.
_REPAIRABLE_PROVENANCE_CODES = frozenset(
    {"data_source_not_surfaced", "currency_not_surfaced", "carried_price_not_dated"}
)

# Assertions with no figure to cut: ``availability_claim_contradicts_evidence``
# flags a sentence that reports data the session actually retrieved as
# unretrieved. The cut path cannot repair it and fails closed, throwing away the
# whole analysis; ``partitioned_release`` omits the claiming unit and keeps the
# rest of the verified answer instead.
_ASSERTION_CODES = frozenset({"availability_claim_contradicts_evidence"})

_MAX_REDACTION_PASSES = 3

# A cut-down release ships only when the cut is small: past a handful of
# omissions the answer is a frame around gaps, and one honest sentence serves
# the reader better than a riddled half-answer (user-chosen degradation policy).
# "Small" scales with the answer: a long table can lose a few cells and still
# read, so the budget is a quarter of the draft's own declarations, within
# [MIN_REDACTED_FIGURES, MAX_REDACTED_FIGURES].
MIN_REDACTED_FIGURES = 3

MAX_REDACTED_FIGURES = 8


def _redaction_budget(declarations: int) -> int:
    """How many figures a cut-down release may omit, for a draft of that size.

    Args:
        declarations: The rejected draft's own declaration lines.

    Returns:
        The omission budget: a quarter of the declarations within [3, 8].
    """
    return max(MIN_REDACTED_FIGURES, min(MAX_REDACTED_FIGURES, declarations // 4))


# Observed ranges the footnote prints at most; the answer's own subjects come
# first, and a long session's other instruments stay out of the reader's way.
_MAX_NOTE_SYMBOLS = 8

# The omission mark replaces the number and its glued unit so the sentence still
# reads ("0.95 元" → "（略※）"); ※ points at the single footnote.
_REDACTION_MARKER_ZH = "（略※）"

_REDACTION_MARKER_EN = "(omitted※)"

MAX_SYMBOL_RESOLUTION_ATTEMPTS = 2

MAX_PRICE_EVIDENCE_ATTEMPTS = 3

#: Close-family leaves that name an EARLIER session ("昨收"), never the latest
#: close; the fail-closed answer must not present one as the newest print.
_PRIOR_CLOSE_FIELDS = frozenset(
    {"prev_close", "pre_close", "preclose", "previous_close", "昨收"}
)


def _format_price(value: float) -> str:
    """Render a price without scientific notation (``%g`` gives "1.23457e+06")."""
    return format(value, ".10g")


#: What the evidence says about a figure, keyed on the validator's reason: a
#: state of the check, never a word read from the answer.
_CORRECTION_REASONS = {
    "undeclared": "it is not declared at all",
    "no_evidence": "this session holds no evidence of that kind to check it against",
    "value_mismatch": "the observed evidence is {range}",
    "not_in_referenced_call": "call {ref} returned no value matching it",
    "ambiguous_field_ref": "{ref} names {sources}, which hold different values; use the one quoted as the ref",
    "tail_risk_needs_field_ref": "this session holds {sources}, which are different measurements; declare the figure with an exact field ref",
    "field_ref_needs_call_id": "{ref} uses a tool name before ::; use one exact call_id::field ref from {sources}",
    "no_formula": "its note states no arithmetic",
    "formula_not_evaluable": "its note is not an arithmetic expression over two or more operands",
    "formula_not_anchored": "no operand of its note is a value this session observed",
    "no_symbol": "the run holds evidence for more than one instrument and the note names none",
    "derivation_result_mismatch": "its own note evaluates to {result}",
    "outside_observed_range": "it is outside the observed range {range} and its note derives no value",
    "citation_without_source": "its note names no source",
    "cited_as_observed": "it is presented as an observed value of this instrument",
    "unparseable_declaration": "the declaration line could not be read as `value | role | note | ref`",
    "symbol_never_handled": "no tool call in this session passed in or returned that symbol",
    "additive_operand_not_observed": "a number its note adds or subtracts is not a value this session observed",
    "role_in_price_column": "it sits in a price column, which holds only observed prints",
    "proposed_not_a_price": "a proposed level is a price, not a percentage; state the price it implies",
    "symbol_mismatch": "it is declared for one instrument but the sentence writes it about another",
    "citation_not_visible": "its source is not named on the figure's own line, and the note is stripped before the user reads the answer",
    "not_in_user_messages": "no value it matches appears in the user's own messages or the recalled memories",
}

#: Reasons that leave a figure's note without a recomputable expression over the
#: session's observations; the correction then shows the values to build it from.
_FORMULA_REASONS = frozenset(
    {
        "no_formula",
        "formula_not_evaluable",
        "formula_not_anchored",
        "additive_operand_not_observed",
        "derivation_result_mismatch",
    }
)

#: Observed values one correction prompt quotes as rewrite material; bounded so
#: a long session cannot turn the correction into a data dump.
_MAX_CORRECTION_EVIDENCE = 16

#: Bounds of one ledger digest: instruments named, values per instrument, and
#: lines in total. A tools-withheld turn writes from a context the compaction
#: may have emptied, so the session's own observations are quoted back to it;
#: the caps keep a long ledger from flooding that turn.
_MAX_DIGEST_SYMBOLS = 12
_MAX_DIGEST_FIELDS_PER_SYMBOL = 6
_MAX_DIGEST_LINES = 40


#: A signed number as a rendered figure or formula result carries it: unicode
#: minus, thousands separators, and an optional trailing percent all read.
_SIGNED_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def _signed_number(text: str) -> float | None:
    """The signed number a rendered value or result string carries, if one.

    Args:
        text: Rendered figure text ("−2.03%") or formula result ("+2.02839%").

    Returns:
        The parsed value in percentage points for percent strings, else None.
    """
    normalized = (
        text.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    )
    match = _SIGNED_NUMBER_RE.search(normalized)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _correction_line(issue: dict[str, Any]) -> str:
    """Render one rejected figure as `written | declared role | evidence`.

    Args:
        issue: One figure-scoped validation issue.

    Returns:
        The line the correction prompt shows for that figure.
    """
    role = issue.get("role")
    declared = f"declared {role}" if role else "not declared"
    if issue.get("code") == "figure_undeclared":
        declared = "not declared"
    reason = str(issue.get("reason") or "")
    template = _CORRECTION_REASONS.get(reason, reason or "it could not be verified")
    low, high = issue.get("observed_min"), issue.get("observed_max")
    span = (
        f"{_format_price(float(low))}\u2013{_format_price(float(high))}"
        if isinstance(low, (int, float)) and isinstance(high, (int, float))
        else "empty"
    )
    result = issue.get("derived_result")  # already rendered in the figure's own units
    evidence = template.format(
        range=span,
        ref=issue.get("source_tool_call_ids", [""])[0] if issue.get("source_tool_call_ids") else "",
        sources=", ".join(str(source) for source in issue.get("ambiguous_sources") or []),
        result=result if result else "a different value",
    )
    if reason == "derivation_result_mismatch" and result:
        # The magnitude can agree while the sign does not, and "evaluates to
        # 2.02839%" alone reads as another value rather than a flipped sign
        # (D1 replay): name the direction and the two places it must change.
        written = _signed_number(str(issue.get("value") or ""))
        computed = _signed_number(str(result))
        if (
            written is not None
            and computed is not None
            and written != 0.0
            and computed != 0.0
            and written * computed < 0
            and abs(abs(written) - abs(computed)) <= abs(computed) * ROUNDED_BAND
        ):
            shown = str(result)
            if computed > 0:
                # ``derived_result`` renders positives unsigned; the whole point
                # of this clause is the sign, so print it.
                shown = f"+{shown}"
            evidence += (
                f"; the direction is reversed — the formula's result is {shown} while "
                f"you wrote {issue.get('value')}; align the direction and update both "
                "the prose and the figures block"
            )
    candidates = issue.get("field_ref_candidates") or []
    if candidates:
        evidence += "; valid field refs: " + ", ".join(str(item) for item in candidates)
    column = issue.get("field")
    if column:
        # The figure sat under a table header, so the check ran against that
        # column alone; naming it keeps the notice from reading as "the tool
        # never returned that field".
        evidence += f" (its `{column}` column)"
    fields = issue.get("observed_fields") or []
    if fields:
        # The fields the referenced call did return (#1433 follow-up): without
        # them, "returned no value matching it" was read as "the data was never
        # retrieved" and the model denied values the session really held.
        evidence += "; the fields it did return: " + ", ".join(str(item) for item in fields)
    nearest = issue.get("observed_nearest") or []
    if nearest:
        evidence += "; nearest observed " + ", ".join(_format_price(float(item)) for item in nearest)
    symbol = issue.get("symbol")
    subject = f"{issue.get('value')} ({symbol})" if symbol else str(issue.get("value"))
    return f"{subject} | {declared} | {evidence}"


def _strip_release_markers(content: str) -> str:
    """Remove redaction markers and ※ note lines the model wrote itself.

    Those are the gate's own statements about what it cut; a model-written copy
    contradicts the real footnote or claims a redaction that never happened.

    Args:
        content: The rejected draft.

    Returns:
        The draft with every marker and note-shaped line removed.
    """
    stripped = content.replace(_REDACTION_MARKER_ZH, "").replace(
        _REDACTION_MARKER_EN, ""
    )
    return "\n".join(
        line for line in stripped.splitlines() if not line.lstrip().startswith("※")
    )


class _ReleaseMixin:
    """Release behaviour of :class:`GroundingLedger`."""

    def correction_prompt(self, validation: ValidationResult) -> str:
        """Build per-figure feedback for one rejected model draft (spec 6).

        Each rejected figure gets one line: the figure as written, the role it
        was declared under, and what the evidence says. The three ways out are
        stated once.

        Args:
            validation: The rejected draft's validation result.

        Returns:
            The system message handed back to the model.
        """
        lines = [
            "[GROUNDING GATE] The previous draft was rejected and was not released to the user.",
            "Every figure below, exactly as you wrote it, with what you declared and what the evidence says:",
        ]
        figures, others = [], []
        for issue in validation.issues[:24]:
            (figures if issue.get("value") is not None else others).append(issue)
        lines.extend(f"- {_correction_line(issue)}" for issue in figures)
        lines.extend(
            f"- {issue.get('message', issue.get('code', 'grounding error'))}" for issue in others
        )
        if any(
            issue.get("code") == "availability_claim_contradicts_evidence"
            for issue in validation.issues
        ):
            lines.append(
                "For every retrieval claim above, the session DID retrieve that "
                "data — its ledger values are quoted in the line: either REMOVE "
                "the claim or REWRITE it around those observed values (quote "
                "them exactly, with their `call_id::field` refs when declaring "
                "them). Reporting retrieved data as unretrieved fails again."
            )
        if figures:
            lines.extend(
                [
                    "Fix EVERY figure above in one of exactly four ways:",
                    "  (1) DECLARE it with the role it really has, in the figures block;",
                    "  (2) REWRITE it to a value this session's tools actually returned;",
                    "  (3) REMOVE it from the answer;",
                    "  (4) DECLARE it `held` when the USER themselves supplied it — a position "
                    "amount, a cost basis, a limit, or a figure they quoted: it must appear in "
                    "their own messages or the recalled memories, quoted exactly as they wrote "
                    "it (thousands separators and full-width digits are read as written).",
                    "Restating a rejected value in another format is none of the four and fails again.",
                ]
            )
            repeated = self._repeatedly_rejected(figures)
            if repeated:
                lines.append(
                    "These value(s) have now been rejected across more than one draft: "
                    + ", ".join(repeated)
                    + ". Take option (2) or (3) for them."
                )
        lines.extend(
            [
                "End the answer with a ```figures``` block declaring every number that "
                "carries a decimal point, a percent sign, a currency mark or a table "
                "cell, one per line as `value | role | note | ref`, where role is one "
                "of observed / derived / proposed / cited / count / held.",
                "observed must appear in the tool results; derived needs a note that is "
                "the arithmetic itself, with one operand this session observed or the "
                "user supplied; held is a value the USER supplied (their positions, "
                "amounts, limits, a figure they quoted) and must match what they wrote; "
                "proposed must be derived or lie inside the observed price range; "
                "cited needs a source in its note; count is not checked.",
                "Reuse the exact locked symbol and venue.",
                "Do not attach figures to a symbol no tool call in this session handled; "
                "report it as not retrieved instead — a held figure is the exception, "
                "since a position the user wrote down is theirs whether or not the "
                "session fetched that instrument.",
            ]
        )
        block_malformed = any(
            issue.get("code") == "figures_block_malformed"
            for issue in validation.issues
        )
        if figures or block_malformed:
            evidence = self._correction_evidence_lines(
                [issue.get("symbol") for issue in figures]
            )
            if evidence:
                lines.append(
                    "Values this session's tools observed; quote one exactly, with its "
                    "`call_id::field` ref:"
                )
                lines.extend(evidence)
                if any(
                    str(issue.get("reason")) in _FORMULA_REASONS
                    for issue in validation.issues
                ):
                    lines.append(
                        "Figures rejected for their arithmetic need the note rebuilt from "
                        "the observed values above: it must be the calculation itself and "
                        "evaluate to the declared value (a percent figure's note is the "
                        "decimal ratio, e.g. `(1.053 \u2212 0.666) / 0.666`)."
                    )
        recovery = self.recovery_action(validation)
        if recovery == _RESOLVER_TOOL:
            lines.extend(
                [
                    "Instrument identity is unresolved. Call `search_symbol` for the "
                    "candidate name in a separate tool-call turn, lock the exact canonical "
                    "symbol and venue it returns, then call `get_market_data` before finalizing.",
                    "Do NOT ask the user to confirm or continue while this read-only recovery "
                    "remains available.",
                ]
            )
        elif recovery == "get_market_data":
            lines.extend(
                [
                    "Identity is locked but price evidence is missing. Call `get_market_data` "
                    "for the locked canonical symbol and venue in a separate tool-call turn, "
                    "then regenerate and re-validate the final answer.",
                    "Do NOT ask the user to confirm or continue while this read-only recovery "
                    "remains available.",
                ]
            )
        elif recovery == "get_fund_data":
            lines.extend(
                [
                    "Identity is locked to an off-exchange fund but NAV evidence is missing. "
                    "Call `get_fund_data` for the locked canonical `.OF` symbol in a separate "
                    "tool-call turn, then regenerate and re-validate the final answer.",
                    "Do NOT ask the user to confirm or continue while this read-only recovery "
                    "remains available.",
                ]
            )
        else:
            lines.append(
                "If evidence is genuinely unavailable or conflicting and recovery is "
                "exhausted, say so and ask only about what the user has never provided; "
                "do not guess, and never ask them to repeat information they already "
                "gave in this conversation or in a recalled memory — restate it and "
                "declare it `held` instead."
            )
        return "\n".join(lines)

    def _draft_note_operands(self) -> list[float]:
        """The operands of the last checked draft's derived/proposed notes.

        Returns:
            The distinct operand values those notes are arithmetic over, in
            reading order.
        """
        operands: list[float] = []
        for declaration in self._last_draft_declarations():
            if declaration.role not in ("derived", "proposed"):
                continue
            evaluated = _formula_in_note(declaration.note)
            if evaluated is None:
                continue
            for operand in evaluated[1]:
                if not any(_close(operand, seen) for seen in operands):
                    operands.append(operand)
        return operands

    def _last_draft_declarations(self) -> tuple[Declaration, ...]:
        """The last checked draft's declarations, rebuilt from the recorded block.

        Returns:
            The draft block's declarations in order; empty when no draft with a
            block was recorded yet.
        """
        if not self._validations:
            return ()
        body = str(self._validations[-1].get("figures_block") or "")
        if not body.strip():
            return ()
        # The recorded block is stored as its BODY, without the fences, so the
        # document the parser reads is rebuilt around it.
        return parse_figures_block(f"```{BLOCK_LANGUAGE}\n{body}\n```").declarations

    @staticmethod
    def _latest_indicator_records(
        observations: Sequence[EvidenceRecord], symbol: str
    ) -> list[EvidenceRecord]:
        """The newest reading of every indicator field one instrument has.

        A later call for the same field supersedes the earlier print, so only
        the fresh levels are listed here; the caller's own sweep still shows
        the older ones after them.

        Args:
            observations: Every quotable record, in ingestion order.
            symbol: The instrument whose readings to collect.

        Returns:
            One record per registered indicator field, ordered by field name.
        """
        latest: dict[str, EvidenceRecord] = {}
        for record in observations:
            if record.symbol != symbol:
                continue
            if _is_registered_price_indicator(
                record.tool, record.field
            ) or _is_registered_metric_indicator(record.tool, record.field):
                latest[record.field] = record
        return [latest[field] for field in sorted(latest)]

    def _correction_evidence_lines(self, symbols: Sequence[str | None]) -> list[str]:
        """Bounded ``call_id::field = value`` list a rewrite can quote from.

        Three ranks, most useful first: the records matching the operands of the
        draft's own derived/proposed notes (the exact values a rebuilt formula
        needs), the instruments' latest indicator readings (a long session's bar
        rows push them past the cap otherwise), then the remaining records in
        ingestion order with the rejected figures' subjects first.

        Args:
            symbols: The rejected figures' subjects, in order.

        Returns:
            At most :data:`_MAX_CORRECTION_EVIDENCE` bullet lines.
        """
        wanted = {symbol for symbol in symbols if symbol}
        observations = [
            record
            for record in self._evidence
            if record.status == "observed"
            and record.value is not None
            and record.call_id
            and record.field
        ]
        lines: list[str] = []
        seen: set[str] = set()

        def take(record: EvidenceRecord) -> None:
            line = (
                f"- {record.call_id}::{record.field} = "
                f"{_format_price(float(record.value))}"
            )
            if record.symbol:
                line += f" ({record.symbol})"
            if line in seen:
                return
            seen.add(line)
            lines.append(line)

        for operand in self._draft_note_operands():
            scoped = [
                record
                for record in observations
                if not wanted or record.symbol in wanted
            ]
            match = next(
                (record for record in scoped if _close(float(record.value), operand)),
                None,
            )
            if match is None:
                match = next(
                    (
                        record
                        for record in observations
                        if _close(float(record.value), operand)
                    ),
                    None,
                )
            if match is not None:
                take(match)
        for symbol in sorted(wanted):
            for record in self._latest_indicator_records(observations, symbol):
                take(record)
        preferred = [record for record in observations if record.symbol in wanted]
        others = [record for record in observations if record.symbol not in wanted]
        for record in [*preferred, *others]:
            take(record)
        return lines[:_MAX_CORRECTION_EVIDENCE]

    def verified_evidence_digest(self) -> str:
        """Bounded ``symbol: field=value (ref)`` digest of this session's observations.

        A convergence, correction, or final text-only turn writes with tools
        withheld; when layer-1 compaction had emptied the working context, the
        model reported data the session really observed as "not retrieved"
        (2026-10-08 run). The ledger still holds every observation, so each
        line names the instrument, its newest value per field, the
        ``call_id::field`` ref a figures block can cite, and the as-of date —
        the model restates rather than denies.

        Returns:
            Empty string when no record is observed, symboled and quotable;
            otherwise at most :data:`_MAX_DIGEST_LINES` bullet lines.
        """
        grouped: dict[str, dict[str, EvidenceRecord]] = {}
        for record in self._evidence:
            if record.status != "observed" or record.value is None:
                continue
            if not record.symbol or not record.call_id or not record.field:
                continue
            per_field = grouped.setdefault(record.symbol, {})
            current = per_field.get(record.field)
            if current is None or str(record.timestamp or "") >= str(
                current.timestamp or ""
            ):
                per_field[record.field] = record
        lines: list[str] = []
        for symbol in sorted(grouped)[:_MAX_DIGEST_SYMBOLS]:
            per_field = grouped[symbol]
            ordered = sorted(
                per_field.values(),
                key=lambda record: (not _is_price_kind(record), record.field),
            )[:_MAX_DIGEST_FIELDS_PER_SYMBOL]
            quoted = "; ".join(
                f"{record.field}={_format_price(float(record.value))} "
                f"({record.call_id}::{record.field})"
                for record in ordered
            )
            line = f"- {symbol}: {quoted}"
            as_of = _latest_as_of(per_field.values())
            if as_of:
                line += f" [as of {as_of}]"
            if all(record.carried_from for record in per_field.values()):
                line += " [inherited from an earlier run of this session]"
            lines.append(line)
            if len(lines) >= _MAX_DIGEST_LINES:
                break
        return "\n".join(lines)

    def _repeatedly_rejected(self, issues: Sequence[dict[str, Any]]) -> list[str]:
        """Figures in this draft that an earlier draft was already refused for.

        Args:
            issues: The current draft's figure issues.

        Returns:
            The repeated figures as written, in order, without duplicates.
        """
        current = {str(issue.get("value")) for issue in issues}
        repeated = [
            str(prior_issue.get("value"))
            for prior in self._validations[:-1]
            for prior_issue in prior.get("issues", [])
            if str(prior_issue.get("value")) in current
        ]
        return list(dict.fromkeys(repeated))

    def recovery_action(self, validation: ValidationResult) -> str | None:
        """Decide the next safe read-only recovery step for a rejected draft (#1081).

        Returns:
            ``search_symbol`` when identity is unresolved and resolution attempts
            remain; ``get_market_data`` (or ``get_fund_data`` when the locked
            identity is an off-exchange fund) when identity is locked and either
            a price claim lacks matching evidence or the session holds no
            comparable price at all, while fetch attempts remain; otherwise
            None, and the loop must ask the user or fail closed.
        """
        if self._recovery_rounds >= MAX_GROUNDING_RECOVERY_ROUNDS:
            return None
        if self._identity_required and self.identity_status == "unresolved":
            if self._symbol_resolution_attempts < MAX_SYMBOL_RESOLUTION_ATTEMPTS:
                return _RESOLVER_TOOL
            return None
        if self.identity_status == "locked":
            codes = {issue.get("code") for issue in validation.issues}
            cited = any(issue.get("value") is not None for issue in validation.issues)
            fetch_action = (
                "get_fund_data" if self._prefer_fund_recovery() else "get_market_data"
            )
            if codes & {"numeric_claim_unavailable", "unsourced_symbol_figures"}:
                if self._price_evidence_attempts < MAX_PRICE_EVIDENCE_ATTEMPTS:
                    return fetch_action
            # A rejected draft whose figures have no comparable price to check
            # against is one bounded fetch away from being verifiable. Without
            # this the run fell straight through to the fail-closed notice while
            # the market tool remained available, which is the behaviour the
            # notice's own text ("fetch the data first") was asking the model to
            # do.
            if (
                cited
                and not self._comparable_price_records()
                and self._price_evidence_attempts < MAX_PRICE_EVIDENCE_ATTEMPTS
            ):
                return fetch_action
        return None

    def _prefer_fund_recovery(self) -> bool:
        """Whether the missing price evidence belongs to off-exchange funds.

        An off-exchange fund has no OHLCV bars — its prices are the published
        NAV series, which only the fund tool serves. So a recovery that would
        otherwise name the market-data tool names the fund tool instead.

        Returns:
            True when every locked identity is an ``.OF`` fund; False when no
            symbol is locked (the market tool stays the default) or when any
            locked identity is an exchange-traded instrument.
        """
        locked = [symbol for symbol in self.authorized_symbols if symbol]
        return bool(locked) and all(
            str(symbol).upper().endswith(".OF") for symbol in locked
        )

    def record_recovery(self, action: str) -> None:
        """Account one bounded recovery attempt against its budget."""
        self._recovery_rounds += 1
        if action == _RESOLVER_TOOL:
            self._symbol_resolution_attempts += 1
        elif action in {"get_market_data", "get_fund_data"}:
            self._price_evidence_attempts += 1

    def record_release_outcome(self, *, path: str, cause: str | None = None) -> None:
        """Record which release path shipped the answer, for the artifact.

        ``fallback`` is the fail-closed path a postmortem needs to explain: the
        artifact's ``released`` block only covers the cut-down release, so
        without this record the others (clean / repaired / fallback and why)
        had to be reconstructed from the trace.

        Args:
            path: One of ``clean``, ``repaired``, ``redacted``, ``partial``, ``fallback``.
            cause: Why this path was taken, when it was not the clean one.
        """
        self._release_outcome = {
            "path": path,
            "cause": cause,
            "recorded_at": _utc_now(),
        }
        self.persist()

    def recovery_prompt(self, action: str, validation: ValidationResult) -> str:
        """Build an executable next-step message for one bounded recovery turn."""
        if action == _RESOLVER_TOOL:
            return (
                "[GROUNDING RECOVERY] Instrument identity is not yet locked and is "
                "recoverable with read-only tools. Call `search_symbol` for the candidate "
                "name in a separate assistant tool-call turn, lock and reuse the exact "
                "canonical symbol and venue it returns, then call `get_market_data`. "
                "Do NOT ask the user to confirm or continue while this read-only recovery "
                "remains available, and do NOT finalize yet."
            )
        if action == "get_market_data":
            return (
                "[GROUNDING RECOVERY] Identity is locked but this session holds no "
                "comparable price evidence for the draft's figures. Call "
                "`get_market_data` with the EXACT canonical symbol string the lock "
                "holds (do not retype the display name), in a separate tool-call turn, "
                "and use its existing bounded provider fallback, then regenerate and "
                "re-validate the final answer. Do NOT ask the user to confirm or "
                "continue while this read-only recovery remains available, and do NOT "
                "finalize yet."
            )
        if action == "get_fund_data":
            return (
                "[GROUNDING RECOVERY] Identity is locked to an off-exchange fund but "
                "this session holds no comparable NAV evidence for the draft's figures. "
                "Call `get_fund_data` with the EXACT canonical `.OF` symbol string the "
                "lock holds (do not retype the display name), in a separate tool-call "
                "turn, then regenerate and re-validate the final answer. Do NOT ask the "
                "user to confirm or continue while this read-only recovery remains "
                "available, and do NOT finalize yet."
            )
        return self.correction_prompt(validation)

    def safe_fallback(self) -> str:
        """Return a deterministic fail-closed answer after repeated rejection."""
        is_zh = self._user_writes_chinese()
        closes = self._latest_close_summary(is_zh)
        if closes is not None:
            # The session's newest close per instrument, dated and sourced: the
            # abstract "I will not give a price" text read as "the data was
            # never retrieved" and hid the values the gate itself verified.
            if is_zh:
                text = (
                    f"本会话已核验的各标的最新收盘价：{closes}。"
                    "这些收盘价来自本会话已取回并核验的数据，可直接参考。"
                    "本轮其余结论尚未取得可核验的数据支撑，故未一并给出。"
                )
            else:
                text = (
                    f"Latest verified closes this session retrieved: {closes}. "
                    "These come from data this session retrieved and checked, and can be "
                    "used directly. Other conclusions had no verifiable data support this "
                    "turn, so they are not included."
                )
        else:
            joined = self._observed_range_summary(is_zh)
            if joined is not None:
                if is_zh:
                    text = (
                        f"本会话已核验的行情记录区间：{joined}。"
                        "这些记录来自本会话已取回并核验的数据，可直接参考。"
                        "本轮其余结论尚未取得可核验的数据支撑，故未一并给出。"
                    )
                else:
                    text = (
                        f"Ranges covered by this session's checked records: {joined}. "
                        "These come from data this session retrieved and checked, and can be "
                        "used directly. Other conclusions had no verifiable data support this "
                        "turn, so they are not included."
                    )
            else:
                # No observed price: tell unresolved identity apart from a draft
                # citing prices this session never observed.
                issue_codes = {
                    code
                    for validation in self._validations
                    for code in (issue.get("code") for issue in validation.get("issues", []))
                }
                if issue_codes & _REDACTABLE_CODES:
                    if is_zh:
                        text = (
                            "本会话的工具没有返回可核验的行情数据，草稿中的价格数字无从核对。"
                            "请让我先用行情工具获取该标的的行情再给出结论（例如回复“先获取行情”），"
                            "或要求我给出不含价格数字的定性分析。"
                        )
                    else:
                        text = (
                            "No tool call in this session returned verifiable market data, so the "
                            "prices in the draft could not be checked. Ask me to fetch the "
                            "instrument's market data first (for example, \"fetch the quotes "
                            "first\") and I will answer from the observed prices — or ask for a "
                            "qualitative answer without price figures."
                        )
                else:
                    if is_zh:
                        text = (
                            "当前无法安全确认标的身份或价格证据，因此没有生成交易结论。"
                            "请确认候选证券代码和交易所后再继续。"
                        )
                    else:
                        text = (
                            "I could not safely lock the instrument identity or price evidence, so I did not "
                            "produce a trading conclusion. Please confirm the candidate symbol and venue."
                        )
        # ``fallback`` is a shipped outcome too: the artifact records why this
        # answer ended here, so a postmortem does not have to guess from traces.
        self.record_release_outcome(
            path="fallback",
            cause=self._fallback_cause
            or "exhausted grounding revisions without a releasable draft",
        )
        return text

    def _latest_close_summary(self, is_zh: bool, content: str | None = None) -> str | None:
        """Summarise the newest observed close per symbol, or None without one.

        The fail-closed answer printed each symbol's whole retrieved min–max
        range, which the reader took for "no current quote"; the newest close
        the session actually holds is the honest anchor, dated and sourced.
        Prior-close leaves ("昨收") name an earlier session, so they never
        stand in for the latest print.

        Args:
            is_zh: Whether to join the facts with Chinese punctuation.
            content: The answer the summary is attached to, if any; the symbol
                is then printed the way that answer spells it.

        Returns:
            One fact per symbol, or None when the run holds no close.
        """
        latest: dict[str, tuple[tuple[str, int], EvidenceRecord]] = {}
        for index, record in enumerate(self._evidence):
            if record.status != "observed" or record.value is None:
                continue
            if _column_field_for_path(record.field) != "close":
                continue
            if _leaf_name(record.field) in _PRIOR_CLOSE_FIELDS:
                continue
            stamp = str(record.timestamp or "")
            dated = stamp[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", stamp) else ""
            symbol = record.symbol or "unknown"
            key = (dated, index)
            if symbol not in latest or key > latest[symbol][0]:
                latest[symbol] = (key, record)
        if not latest:
            return None
        facts = []
        for symbol, (_key, record) in sorted(latest.items())[:_MAX_NOTE_SYMBOLS]:
            unit = (" 点" if is_zh else " points") if record.quote_unit == "points" else f" {record.currency}" if record.currency else ""
            source = record.source or "unknown"
            stamp = str(record.timestamp or "")
            as_of = stamp[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", stamp) else None
            cutoff = (
                (f"；数据截至 {as_of}" if is_zh else f"; as of {as_of}") if as_of else ""
            )
            spelling = self._answer_symbol_spelling(symbol, content)
            value = _format_price(float(record.value))
            if is_zh:
                facts.append(f"{spelling}：{value}{unit}（来源 {source}{cutoff}）")
            else:
                facts.append(f"{spelling}: {value}{unit} (source: {source}{cutoff})")
        return "；".join(facts) if is_zh else "; ".join(facts)

    def _user_writes_chinese(self) -> bool:
        """Return whether user-facing gate text should be Chinese."""
        return bool(re.search(r"[\u3400-\u9fff]", self.user_message))

    def _observed_range_summary(
        self,
        is_zh: bool,
        content: str | None = None,
        symbols: Iterable[str] | None = None,
    ) -> str | None:
        """Summarise the observed price range per symbol, or None without prices.

        The range is built from the session's observed price prints. When the
        only price evidence is a registered indicator (``latest_close`` /
        ``sma_*`` / ``bollinger.*``), the same comparable pool the validator
        checks a draft against is used and labelled: the release path used to
        look at OHLC prints only and conclude "no price" while the gate had
        just verified one, which is what threw verified drafts away.

        Args:
            is_zh: Whether to join the facts with Chinese punctuation.
            content: The answer the summary is attached to, if any; the symbol
                is then printed the way that answer spells it.
            symbols: When given, only these instruments are summarised — the
                footnote speaks about the answer's own subjects, not every
                symbol a long session happened to observe. Capped at
                ``_MAX_NOTE_SYMBOLS`` either way.

        Returns:
            One fact per symbol, or None when the run observed no price.
        """
        pooled = self._price_records()
        qualified = ""
        if not pooled:
            pooled = self._comparable_price_records()
            if pooled:
                qualified = "（含指标值）" if is_zh else " (indicator values included)"
        if not pooled:
            return None
        by_symbol: dict[str, list[EvidenceRecord]] = {}
        for record in pooled:
            by_symbol.setdefault(record.symbol or "unknown", []).append(record)
        if symbols:
            wanted = {_normalize_symbol(symbol) for symbol in symbols if symbol}
            by_symbol = {
                symbol: grouped
                for symbol, grouped in by_symbol.items()
                if _normalize_symbol(symbol) in wanted
            }
            # Nothing the caller named has a print: the count alone is the
            # honest note, and a session-wide range would answer a question the
            # reader did not ask.
            if not by_symbol:
                return None
        facts = []
        for symbol, grouped in sorted(by_symbol.items())[:_MAX_NOTE_SYMBOLS]:
            values = [float(record.value) for record in grouped if record.value is not None]
            currency = next((record.currency for record in grouped if record.currency), None)
            sources = sorted({record.source for record in grouped if record.source})
            source_label = "/".join(sources) if sources else "unknown"
            unit = f" {currency}" if currency else ""
            as_of = _latest_as_of(grouped)
            cutoff = (
                (f"；数据截至 {as_of}" if is_zh else f"; as of {as_of}")
                if as_of
                else ""
            )
            spelling = self._answer_symbol_spelling(symbol, content)
            low, high = _format_price(min(values)), _format_price(max(values))
            if is_zh:
                facts.append(
                    f"{spelling}：{low}–{high}{unit}（来源 {source_label}{cutoff}）{qualified}"
                )
            else:
                facts.append(
                    f"{spelling}: {low}–{high}{unit} (source: {source_label}{cutoff}){qualified}"
                )
        return "；".join(facts) if is_zh else "; ".join(facts)

    @staticmethod
    def _answer_symbol_spelling(canonical: str, content: str | None) -> str:
        """Return the spelling ``content`` uses for a canonical symbol."""
        if not content:
            return canonical
        for match in _CANONICAL_SYMBOL_RE.finditer(content):
            if _normalize_symbol(match.group(0)) == canonical:
                return match.group(0)
        return canonical

    def _provenance_note(self, content: str | None = None) -> str | None:
        """Build the one-line data note that satisfies the provenance checks.

        Args:
            content: The answer the note is appended to, so the symbol is
                printed the way that answer spells it.

        Returns:
            The note, or None when the run observed no price.
        """
        price_records = self._price_records() or self._comparable_price_records()
        if not price_records:
            return None
        is_zh = self._user_writes_chinese()
        by_symbol: dict[str, list[EvidenceRecord]] = {}
        for record in price_records:
            by_symbol.setdefault(record.symbol or "unknown", []).append(record)
        parts = []
        for symbol, records in sorted(by_symbol.items()):
            sources = sorted(
                {
                    record.source
                    for record in records
                    if record.source and record.source.casefold() not in {"auto", "unknown"}
                }
            )
            currency = next((record.currency for record in records if record.currency), None)
            source_label = "/".join(sources) if sources else ("未知" if is_zh else "unknown")
            currency_label = currency or ("未知" if is_zh else "unknown")
            spelling = self._answer_symbol_spelling(symbol, content)
            # Prices inherited from an earlier run of the session carry that
            # run's observation date, so the reader can tell how fresh they are.
            as_of = (
                _latest_as_of(records)
                if any(record.carried_from for record in records)
                else None
            )
            if is_zh:
                cutoff = f"，数据截至 {as_of}（本会话此前获取）" if as_of else ""
                parts.append(
                    f"{spelling}：行情来源 {source_label}，"
                    + ("单位 点" if all(r.quote_unit == "points" for r in records) else f"计价货币 {currency_label}")
                    + cutoff
                )
            else:
                cutoff = f", as of {as_of} (fetched earlier in this session)" if as_of else ""
                parts.append(
                    f"{spelling}: price source {source_label}, "
                    + ("unit points" if all(r.quote_unit == "points" for r in records) else f"quote currency {currency_label}")
                    + cutoff
                )
        if is_zh:
            return "数据说明：" + "；".join(parts) + "。"
        return "Data note: " + "; ".join(parts) + "."

    def repair_provenance(
        self,
        content: str,
        validation: ValidationResult,
    ) -> str | None:
        """Append a data note when the only defects are missing provenance words.

        Source and currency are known to the ledger, so naming them should not
        cost a model round. Any other issue returns None so the numeric checks
        keep their round; see ``_REPAIRABLE_PROVENANCE_CODES`` for why a missing
        symbol is not repaired.

        Args:
            content: The rejected draft.
            validation: Its validation result.

        Returns:
            The draft with a provenance note appended, or None when the issues
            are not provenance-only or there is no price evidence to cite.
        """
        codes = {issue.get("code") for issue in validation.issues}
        if not codes or not codes <= _REPAIRABLE_PROVENANCE_CODES:
            return None
        note = self._provenance_note(content)
        if note is None:
            return None
        return content.rstrip() + "\n\n" + note

    def redacted_release(self, content: str, validation: ValidationResult) -> str | None:
        """Release the last rejected draft with its unverified figures cut out.

        Each rejected figure becomes a marker at its own span, missing provenance
        is appended, unchecked restatements of a cut figure (bare, or inside a
        code fence, compared by normalized digits) are swept, and the result and
        its footnote are re-validated by the same gate. A block with an
        unreadable declaration line is dropped first, so every measured figure is
        checked as observed; if that alone passes, nothing is footnoted.
        Fail-closed: None when a market answer observed no comparable
        price, an issue cannot be cut (an identity finding), a flagged figure
        cannot be located, or the text still fails. A general answer with no tool
        evidence is released cut, with a footnote that names no range.

        Args:
            content: The rejected draft.
            validation: Its validation result.

        Returns:
            The redacted, re-validated answer without its declaration block and
            with a note stating how many figures were removed, or None.
        """
        # A market answer with no comparable price has nothing to stand on once
        # its figures are cut; a general answer (no instrument asked about) does.
        # "Comparable" is the validator's own idiom and includes registered price
        # indicators, so a draft the gate rejected against ``latest_close`` or a
        # moving average is still releasable with its figures cut; checking OHLC
        # prints only here is what turned those runs into the canned refusal.
        if self._identity_required and not self._comparable_price_records():
            return None
        text = _strip_release_markers(content)
        block = parse_figures_block(text)
        # The budget reads the draft as the model wrote it: declarations the
        # cuts happen to remove must not shrink the allowance mid-release.
        budget = _redaction_budget(len(block.declarations))
        # Stripping shifts offsets and the cuts anchor on issue spans, so the
        # verdict is retaken on the stripped text.
        check = validation if text == content else self._validate(text, record=False)
        # An unreadable declaration cannot be cut, but the block is not answer
        # text either: without it the draft is checked in undeclared mode.
        dropped_block = any(
            issue.get("code") == "figures_block_malformed" for issue in check.issues
        )
        if dropped_block:
            text = strip_figures_block(text, block)
            check = self._validate(text, record=False)
        removed = 0
        keys: set[str] = set()
        # The instruments the cuts are about, so the footnote shows the ranges
        # of the answer's own subjects instead of the whole session's.
        cut_symbols: set[str] = set()
        for _ in range(_MAX_REDACTION_PASSES):
            if check.valid:
                break
            codes = {issue.get("code") for issue in check.issues}
            if codes & _ASSERTION_CODES:
                # An unretrieved-data claim has no figure to cut, so the cut
                # loop can only fail closed and take the whole analysis with it.
                # Retain the independently validated paragraphs/rows and omit
                # the claiming unit: the reader keeps the verified analysis.
                partitioned = self.partitioned_release(content, validation)
                if partitioned is None:
                    self._fallback_cause = (
                        "an unretrieved-data claim could not be cut and no "
                        "independent validated unit remained"
                    )
                return partitioned
            if not codes <= (_REDACTABLE_CODES | _REPAIRABLE_PROVENANCE_CODES):
                return None
            cut, cut_keys = self._cut_flagged(text, check.issues)
            if cut is None:
                return None
            if cut_keys:
                text = cut
                removed += len(cut_keys)
                keys.update(cut_keys)
                cut_symbols.update(
                    str(issue["symbol"])
                    for issue in check.issues
                    if issue.get("symbol")
                )
                check = self._validate(text, record=False)
                if check.valid:
                    break
            repaired = self.repair_provenance(text, check)
            if repaired is None:
                if not cut_keys:
                    return None
                continue
            text = repaired
            check = self._validate(text, record=False)
        if not check.valid or (removed == 0 and not dropped_block):
            return None
        # A cut figure can still be standing where the shape rules never look.
        swept, swept_keys = self._sweep_same_values(text, keys - {""})
        if swept_keys:
            recheck = self._validate(swept, record=False)
            if not recheck.valid:
                return None
            text, check, removed = swept, recheck, removed + len(swept_keys)
        if removed > budget:
            # Numerous holes are unreadable. Retain independently validated
            # paragraphs/rows instead of erasing the entire analysis. The
            # paragraph containing a rejected premise loses its action too.
            partitioned = self.partitioned_release(content, validation)
            if partitioned is None:
                self._fallback_cause = "no independent validated unit remained after removing invalid premises"
            return partitioned
        body = check.released_text
        released = body
        if removed:
            note = self._release_note(removed, body, cut_symbols)
            # The note is answer text too (range, symbols), so it passes the
            # same gate, in undeclared mode.
            if not self._validate(note, record=False).valid:
                return None
            released = body.rstrip() + "\n\n" + note
        # Recorded beside the drafts, not as one: every recheck here is
        # unrecorded, so this is the artifact's only evidence of the release.
        self._released = {
            "released_at": _utc_now(),
            "content_sha256": hashlib.sha256(released.encode("utf-8")).hexdigest(),
            "figures_removed": removed,
            "revalidated": True,
        }
        self.record_release_outcome(
            path="redacted",
            cause=(
                f"{removed} unverified figure(s) cut before release"
                if removed
                else "unreadable declaration block dropped before release"
            ),
        )
        return released

    def partitioned_release(self, content: str, validation: ValidationResult) -> str | None:
        """Retain independent verified units; omit an invalid premise with its action.

        This is a partial report, never a completed personalized decision.
        Identity findings and unlocatable/semantic errors still fail closed.
        """
        codes = {issue.get("code") for issue in validation.issues}
        if not codes <= (
            _REDACTABLE_CODES | _REPAIRABLE_PROVENANCE_CODES | _ASSERTION_CODES
        ):
            return None
        block = parse_figures_block(content)
        declarations = "\n".join(content[start:end] for start, end in block.spans)
        body = strip_figures_block(content, block)
        provenance = self._provenance_note(body) or ""
        kept: list[str] = []
        omitted = 0
        table_dependency_unverified = False
        invalid_premise_seen = False

        def valid(unit: str) -> bool:
            return self._validate(
                unit + "\n\n" + declarations + "\n\n" + provenance, record=False
            ).valid

        for paragraph in re.split(r"\n\s*\n", body):
            if not paragraph.strip():
                continue
            if invalid_premise_seen and re.search(r"因此|所以|据此|建议|应当|满仓|清仓|买入|卖出|加仓|减仓|赎回|申购|(?:therefore|thus|recommend|buy|sell|hold|trim|allocate)\b", paragraph, re.I):
                omitted += 1
                continue
            if paragraph.lstrip().startswith("#"):
                table_dependency_unverified = False
            elif table_dependency_unverified:
                # Narrative following a table may summarize the rejected
                # rows without repeating their numbers. Keep it out until a
                # new independently checked section begins.
                omitted += 1
                continue
            lines = paragraph.splitlines()
            if len(lines) >= 3 and lines[0].count("|") >= 2 and re.fullmatch(r"[\s|:\-]+", lines[1]):
                header = "\n".join(lines[:2])
                rows = []
                for row in lines[2:]:
                    if valid(header + "\n" + row):
                        rows.append(row)
                    else:
                        omitted += 1
                        table_dependency_unverified = True
                        invalid_premise_seen = True
                if rows:
                    kept.append(header + "\n" + "\n".join(rows))
            elif valid(paragraph):
                kept.append(paragraph)
            else:
                omitted += 1
                invalid_premise_seen = True
        if not kept or not omitted or not any(not unit.lstrip().startswith("#") for unit in kept):
            return None
        note = (
            "部分段落或表格行未通过证据核对，已连同依赖它们的操作意见一起略去。"
            "以下保留内容只覆盖已核对部分；不能据此推断其他持仓无需调整。"
            if self._user_writes_chinese() else
            "Unverified paragraphs or rows, including their dependent actions, were omitted. "
            "The retained analysis covers only the checked portion; it does not imply "
            "that unassessed holdings need no adjustment."
        )
        candidate = "\n\n".join([*kept, note, declarations, provenance])
        check = self._validate(candidate, record=False)
        if not check.valid:
            return None
        released = check.released_text
        self._released = {
            "released_at": _utc_now(),
            "content_sha256": hashlib.sha256(released.encode("utf-8")).hexdigest(),
            "figures_removed": 0,
            "units_omitted": omitted,
            "revalidated": True,
        }
        self.record_release_outcome(path="partial", cause=f"{omitted} invalid independent unit(s) omitted")
        return released

    @staticmethod
    def _issue_span(text: str, issue: dict[str, Any]) -> tuple[int, int] | None:
        """Locate the flagged figure in ``text`` by its recorded character span.

        The span is the only reliable anchor: a bare cell "1.10" also matches
        inside "21.10".

        Args:
            text: The document the issue was raised against.
            issue: One validation issue.

        Returns:
            ``(start, end)`` or None when the issue carries no usable span.
        """
        span = issue.get("span")
        if not isinstance(span, (list, tuple)) or len(span) != 2:
            return None
        try:
            start, end = int(span[0]), int(span[1])
        except (TypeError, ValueError):
            return None
        if 0 <= start <= end <= len(text):
            return start, end
        return None

    def _cut_flagged(
        self,
        text: str,
        issues: Sequence[dict[str, Any]],
    ) -> tuple[str | None, list[str]]:
        """Replace every flagged figure with the omission marker.

        An issue without a value (``unsourced_symbol_figures``) cuts every
        measured figure inside its span; any other issue must name the span of
        a figure the scan located.

        Args:
            text: The document to rewrite.
            issues: The issues raised against it.

        Returns:
            ``(rewritten text, normalized digits of each figure cut)``, or
            ``(None, [])`` when a flagged figure cannot be located and release
            must fail closed.
        """
        figures = scan_figures(text, parse_figures_block(text))
        located = {(figure.start, figure.end): figure for figure in figures}
        cuts: dict[tuple[int, int], Figure] = {}
        for issue in issues:
            if issue.get("code") not in _REDACTABLE_CODES:
                continue
            span = self._issue_span(text, issue)
            if span is None:
                return None, []
            if issue.get("value") is None:
                cuts.update(
                    ((figure.start, figure.end), figure)
                    for figure in figures
                    if figure.shape == "measured"
                    and span[0] <= figure.start
                    and figure.end <= span[1]
                )
            elif span in located:
                cuts[span] = located[span]
            else:
                return None, []
        return self._apply_cuts(text, cuts.values())

    def _sweep_same_values(self, text: str, keys: set[str]) -> tuple[str, list[str]]:
        """Cut every unchecked restatement of a figure that was already cut.

        After "回撤 37%" is cut, "回撤 37 个百分点" still carries an unchecked bare
        "37", and a code fence can still print it. Figures compare by normalized
        digits, so an escaped spelling is the same restatement. Measured prose
        survivors were checked and grounded, and the figures block is not
        answer text, so neither is swept. A single-digit figure is never a
        sweep key.

        Args:
            text: The already-cut document.
            keys: Normalized digits of the figures that were cut.

        Returns:
            ``(rewritten text, normalized digits of each figure swept)``.
        """
        # One digit restates nothing: every "2" and "3" on the page shares it, and
        # sweeping them took list references and counts with the figure (#1471).
        keys = {key for key in keys if len(key) > 1}
        if not keys:
            return text, []
        stale = [
            figure
            for figure in scan_figures(text, parse_figures_block(text))
            if figure.digits in keys
            and (
                figure.shape == "bare"
                or (figure.fence is not None and figure.fence != BLOCK_LANGUAGE)
            )
        ]
        return self._apply_cuts(text, stale)

    def _apply_cuts(
        self,
        text: str,
        figures: Iterable[Figure],
    ) -> tuple[str, list[str]]:
        """Replace each figure and its glued marks with the marker in its line's script.

        Args:
            text: The document to rewrite.
            figures: The figures to remove.

        Returns:
            ``(rewritten text, normalized digits of each figure removed)``.
        """
        pieces: list[str] = []
        removed: list[str] = []
        cursor = 0
        for figure in sorted(figures, key=lambda item: (item.start, item.end)):
            if figure.start < cursor:
                continue
            removed.append(figure.digits)
            start, end = figure.extent or (figure.start, figure.end)
            start = max(cursor, start)
            marker = self._marker_for(text, start)
            if marker is _REDACTION_MARKER_ZH:
                # "建议买入价 0.95 元" → "建议买入价（略※）": flush against the word,
                # but not against a table pipe, which would break the row's padding.
                while (
                    start > cursor
                    and text[start - 1] == " "
                    and text[:start - 1].rstrip(" ")[-1:] not in {"|", ""}
                ):
                    start -= 1
            pieces.append(text[cursor:start])
            pieces.append(marker)
            cursor = end
        pieces.append(text[cursor:])
        return "".join(pieces), removed

    def _marker_for(self, text: str, position: int) -> str:
        """Pick the omission marker in the script of the line being cut.

        A line with neither script (a numeric table row) uses the user's language.
        """
        line = ""
        for candidate, start in _lines_with_offsets(text):
            if start <= position <= start + len(candidate):
                line = candidate
                break
        if any("\u3400" <= char <= "\u9fff" for char in line):
            return _REDACTION_MARKER_ZH
        if any(char.isascii() and char.isalpha() for char in line):
            return _REDACTION_MARKER_EN
        return (
            _REDACTION_MARKER_ZH
            if self._user_writes_chinese()
            else _REDACTION_MARKER_EN
        )

    def _release_note(
        self,
        removed: int,
        content: str | None = None,
        symbols: Iterable[str] | None = None,
    ) -> str:
        """Explain the redaction to the user, with the observed range.

        Args:
            removed: How many figures were cut.
            content: The answer the note is attached to, so the symbols are
                printed the way it spells them.
            symbols: The instruments the cuts were about, when known; the note
                then shows their ranges only, not the whole session's.
        """
        is_zh = self._user_writes_chinese()
        joined = self._observed_range_summary(is_zh, content, symbols)
        if joined is None:
            if is_zh:
                return f"※ 略去 {removed} 处无法与本会话工具数据对上的数值。"
            return (
                f"※ {removed} figure(s) that could not be matched to this session's "
                "tool data were omitted."
            )
        if is_zh:
            return (
                f"※ 略去 {removed} 处无法与本会话工具数据对上的数值。"
                f"已观测价格区间：{joined}。"
                "如需买入价，请让我基于已观测的收盘价或均线给出带公式的推导。"
            )
        return (
            f"※ {removed} figure(s) that could not be matched to this session's tool data "
            f"were omitted. Observed price range: {joined}. "
            "For an entry price, ask me to derive one with a visible formula from an "
            "observed close or moving average."
        )
