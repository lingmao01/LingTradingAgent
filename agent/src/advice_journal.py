"""The advice journal: calls that were given, kept so later turns can grade them.

An action the agent states once ("600519.SH, trim into strength, reference
1600") is gone with the turn that said it unless something keeps it. This
module keeps it, so the next turn — or the next week's turn — can look back
at what was actually advised and measure what happened since:

* :class:`AdviceJournalStore` — the JSON store under the runtime root
  (``advice_journal.json``), written atomically (temp file in the same
  directory + ``os.replace``) so an interrupted write can never truncate or
  half-write the journal.
* :class:`AdviceEntry` — one logged call: instrument, action, optional
  reference price, the rationale, and (once reviewed) the outcome note.
  Recording is append-only, because a journal is a log: a later call for the
  same symbol is a new entry, and the earlier one is closed when it has
  played out.
* :func:`render_journal_block` — the markdown lines injected into every
  turn's user message (inside the ``<advice-journal>`` tag). Only open
  entries are injected, newest first and bounded: a reviewed entry has
  already done its turn, and the block rides in every turn.

The journal is the agent's own record, not the user's statement: it is
deliberately absent from the grounding ledger's user corpus (a number the
model itself recorded must not reconcile as the user's) and is not evidence
of the current session. A recorded figure stays quotable — as a *cited*
figure naming this journal, which is what the ``cited`` role is for.

Storage shape (``version 1``)::

    {"version": 1, "updated_at": "...", "next_id": 3, "entries": [
        {"id": 1, "symbol": "600519.SH", "action": "reduce",
         "reference_price": 1600.0, "rationale": "...", "status": "open",
         "recorded_at": "...", "closed_at": "", "close_note": ""}]}
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config.paths import get_runtime_root
from src.position_book import canonical_book_symbol

logger = logging.getLogger(__name__)

#: Journal file name under the runtime root.
JOURNAL_FILENAME = "advice_journal.json"
#: On-disk schema version; bumped only on an incompatible shape change.
JOURNAL_VERSION = 1

#: The actions a logged call may carry. ``watch`` is "stay out / track it";
#: the others are the direct actions of the system prompt's Principle 4.
ACTIONS = ("add", "hold", "reduce", "exit", "watch")
#: A review has two states: still playing out, or graded and closed.
STATUSES = ("open", "closed")

# A rationale or outcome note is re-injected into every future turn's context
# (the rationale, at least), so each is capped — the same paste-safety reason
# book notes and playbook variables are capped.
_MAX_RATIONALE_CHARS = 300
_MAX_CLOSE_NOTE_CHARS = 300
#: The block rides in every turn, so only the most recent open calls fit.
_MAX_BLOCK_ENTRIES = 8


def _utc_now() -> str:
    """UTC ISO8601 timestamp (seconds precision), matching sibling stores."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _finite_number(value: Any, name: str) -> float:
    """Coerce a user-supplied number, rejecting bools, NaN and infinities.

    Raises:
        ValueError: The value is not a finite real number.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be a finite number")
    return parsed


def _one_line(value: Any, limit: int) -> str:
    """Collapse free text to one whitespace-normalized line, capped.

    The line rendering is one entry per line, so an embedded newline would
    break the block's shape; collapsing keeps what was written readable.
    """
    return " ".join(str(value or "").split())[:limit]


@dataclass(frozen=True)
class AdviceEntry:
    """One logged call: what was advised for an instrument when.

    Attributes:
        id: Stable integer id, assigned once and never reused.
        symbol: Canonical symbol.
        action: One of :data:`ACTIONS` — the direct action that was advised.
        reference_price: Optional price the call was anchored to when made.
        rationale: Optional short reason stated with the call.
        status: ``open`` while it is still playing out, ``closed`` once graded.
        recorded_at: UTC ISO8601 timestamp of the call.
        closed_at: UTC ISO8601 timestamp of the review, when closed.
        close_note: Optional outcome note written when the entry was closed.
    """

    id: int
    symbol: str
    action: str
    reference_price: float | None = None
    rationale: str = ""
    status: str = "open"
    recorded_at: str = ""
    closed_at: str = ""
    close_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready row."""
        return {
            "id": self.id,
            "symbol": self.symbol,
            "action": self.action,
            "reference_price": self.reference_price,
            "rationale": self.rationale,
            "status": self.status,
            "recorded_at": self.recorded_at,
            "closed_at": self.closed_at,
            "close_note": self.close_note,
        }

    @staticmethod
    def from_dict(row: Any) -> "AdviceEntry | None":
        """Parse one persisted row, or None when the row is unusable."""
        if not isinstance(row, dict):
            return None
        try:
            entry_id = int(row.get("id"))
        except (TypeError, ValueError):
            return None
        if entry_id < 1:
            return None
        symbol = canonical_book_symbol(row.get("symbol"))
        if symbol is None:
            return None
        action = str(row.get("action") or "").strip().lower()
        if action not in ACTIONS:
            return None
        status = str(row.get("status") or "open").strip().lower()
        if status not in STATUSES:
            status = "open"
        reference_price: float | None = None
        if row.get("reference_price") is not None:
            try:
                reference_price = _finite_number(
                    row["reference_price"], "reference_price"
                )
            except ValueError:
                reference_price = None
        return AdviceEntry(
            id=entry_id,
            symbol=symbol,
            action=action,
            reference_price=reference_price,
            rationale=str(row.get("rationale") or "").strip(),
            status=status,
            recorded_at=str(row.get("recorded_at") or ""),
            closed_at=str(row.get("closed_at") or ""),
            close_note=str(row.get("close_note") or "").strip(),
        )


class AdviceJournalStore:
    """JSON-backed store for the advice journal."""

    def __init__(self, path: Path | str | None = None) -> None:
        """Initialize the store.

        Args:
            path: Explicit journal file path (tests); defaults to
                ``<runtime-root>/advice_journal.json``, resolved here at
                construction time.
        """
        self.path = (
            Path(path) if path is not None else get_runtime_root() / JOURNAL_FILENAME
        )

    def load(self) -> list[AdviceEntry]:
        """Return the persisted entries, oldest first.

        A missing file is an empty journal. An unreadable or malformed file
        is logged and treated as empty rather than failing the turn that
        reads it; rows that fail to parse are dropped, never repaired by
        guessing.
        """
        return self._read_payload()[0]

    def _read_payload(self) -> tuple[list[AdviceEntry], int]:
        """Read the journal and its next id, tolerating a bad file.

        Returns:
            The parsed entries and the id the next record will take. A
            missing or corrupt ``next_id`` falls back to one past the highest
            stored id, so an id already in the journal can never be reused.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], 1
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "advice journal %s unreadable (%s); treated as empty", self.path, exc
            )
            return [], 1
        rows = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return [], 1
        entries: list[AdviceEntry] = []
        for row in rows:
            entry = AdviceEntry.from_dict(row)
            if entry is not None:
                entries.append(entry)
        highest = max((entry.id for entry in entries), default=0)
        next_id = raw.get("next_id") if isinstance(raw, dict) else None
        if not isinstance(next_id, int) or isinstance(next_id, bool):
            next_id = highest + 1
        return entries, max(next_id, highest + 1)

    def _save(self, entries: list[AdviceEntry], next_id: int) -> None:
        """Write the journal atomically (temp file in the same directory + rename)."""
        payload = {
            "version": JOURNAL_VERSION,
            "updated_at": _utc_now(),
            "next_id": next_id,
            "entries": [entry.to_dict() for entry in entries],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp_path, self.path)

    def record(
        self,
        symbol: Any,
        *,
        action: Any,
        reference_price: Any = None,
        rationale: Any = "",
    ) -> AdviceEntry:
        """Append one call to the journal and persist it.

        Args:
            symbol: Instrument code in any accepted spelling.
            action: The direct action advised; one of :data:`ACTIONS`.
            reference_price: Optional price the call was anchored to.
            rationale: Optional short reason stated with the call.

        Returns:
            The stored entry.

        Raises:
            ValueError: The symbol has no canonical form, the action is not
                one of :data:`ACTIONS`, or the reference price is not a
                finite number (including a negative one).
        """
        canonical = canonical_book_symbol(symbol)
        if canonical is None:
            raise ValueError(f"unsupported symbol: {symbol!r}")
        action_text = str(action or "").strip().lower()
        if action_text not in ACTIONS:
            raise ValueError(
                f"unsupported action: {action!r}; use one of {', '.join(ACTIONS)}"
            )
        price_value: float | None = None
        if reference_price is not None:
            price_value = _finite_number(reference_price, "reference_price")
            if price_value < 0:
                raise ValueError("reference_price must not be negative")

        entries, next_id = self._read_payload()
        entry = AdviceEntry(
            id=next_id,
            symbol=canonical,
            action=action_text,
            reference_price=price_value,
            rationale=_one_line(rationale, _MAX_RATIONALE_CHARS),
            status="open",
            recorded_at=_utc_now(),
        )
        entries.append(entry)
        self._save(entries, next_id + 1)
        return entry

    def close(self, entry_id: Any, *, note: Any = "") -> AdviceEntry | None:
        """Close one entry as reviewed and persist the journal.

        Closing is idempotent: a second close updates the outcome note rather
        than failing, so a review can be amended without re-recording.

        Args:
            entry_id: The entry's id.
            note: Optional outcome note — where the call stood or fell.

        Returns:
            The closed entry, or None when no entry carries that id.

        Raises:
            ValueError: The id is not an integer.
        """
        target = _entry_id(entry_id)
        entries, next_id = self._read_payload()
        for index, entry in enumerate(entries):
            if entry.id != target:
                continue
            updated = AdviceEntry(
                id=entry.id,
                symbol=entry.symbol,
                action=entry.action,
                reference_price=entry.reference_price,
                rationale=entry.rationale,
                status="closed",
                recorded_at=entry.recorded_at,
                closed_at=_utc_now(),
                close_note=_one_line(note, _MAX_CLOSE_NOTE_CHARS) or entry.close_note,
            )
            entries[index] = updated
            self._save(entries, next_id)
            return updated
        return None

    def remove(self, entry_id: Any) -> bool:
        """Remove one entry from the journal.

        Args:
            entry_id: The entry's id.

        Returns:
            True when an entry was removed, False when the journal held no
            such entry.

        Raises:
            ValueError: The id is not an integer.
        """
        target = _entry_id(entry_id)
        entries, next_id = self._read_payload()
        remaining = [entry for entry in entries if entry.id != target]
        if len(remaining) == len(entries):
            return False
        self._save(remaining, next_id)
        return True


def _entry_id(value: Any) -> int:
    """Coerce an entry id argument, accepting the ``#3`` spelling shown in a block.

    Raises:
        ValueError: The value is not an integer id.
    """
    text = str(value or "").strip().lstrip("#")
    try:
        parsed = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"id must be an integer: {value!r}") from exc
    if parsed < 1:
        raise ValueError(f"id must be an integer: {value!r}")
    return parsed


def _fmt_number(value: float) -> str:
    """Render a stored number without a trailing ``.0`` for whole units."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _entry_line(entry: AdviceEntry) -> str:
    """One markdown line for :func:`render_journal_block`."""
    parts = [f"{entry.action}"]
    if entry.reference_price is not None:
        parts.append(f"reference {_fmt_number(entry.reference_price)}")
    line = f"- #{entry.id} {entry.symbol}: {', '.join(parts)}"
    if entry.rationale:
        line += f" — {entry.rationale}"
    if entry.recorded_at:
        line += f" (recorded {entry.recorded_at[:10]})"
    return line


def render_journal_block(store: AdviceJournalStore | None = None) -> str:
    """Render the open journal entries as the markdown lines injected into a turn, or "".

    Only entries still playing out are injected: a closed entry has been
    reviewed, and this block rides in every turn. Newest first, bounded to
    the most recent :data:`_MAX_BLOCK_ENTRIES` entries.

    Args:
        store: Store to read; a fresh default store when omitted, so the
            runtime root is resolved at call time.

    Returns:
        One ``- #id SYMBOL ...`` line per open entry, or "" when nothing is
        open.
    """
    entries = [
        entry
        for entry in (store or AdviceJournalStore()).load()
        if entry.status == "open"
    ]
    if not entries:
        return ""
    newest = sorted(entries, key=lambda entry: entry.id, reverse=True)
    return "\n".join(
        _entry_line(entry) for entry in newest[:_MAX_BLOCK_ENTRIES]
    )