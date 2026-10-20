"""Advice journal tool backed by :mod:`src.advice_journal`.

The journal is the agent's own record of the direct calls it has given. Open
entries are injected into every later turn (``<advice-journal>``), so a
repeat of an earlier call is made with its outcome in view instead of from
memory. Every write persists immediately.

The recorded numbers are the agent's, not the user's: quoting one in a final
answer is a ``cited`` figure naming this journal, never an observation of
the current session.
"""

from __future__ import annotations

import json
from typing import Any

from src.advice_journal import ACTIONS, AdviceJournalStore
from src.agent.tools import BaseTool


def _payload(status: str, **fields: Any) -> str:
    """Return a JSON result string with a status field."""
    return json.dumps({"status": status, **fields}, ensure_ascii=False)


class AdviceJournalTool(BaseTool):
    """Record and review the agent's own earlier calls."""

    name = "advice_journal"
    description = (
        "The advice journal: the direct calls you have already given the "
        "user, kept so a later turn can review what happened since. record: "
        "log one call — call it whenever you state a direct action for an "
        "instrument (add / hold / reduce / exit / watch) with the price it "
        "was anchored to; it persists, and every later turn reads the open "
        "entries. list: read the journal back, closed entries included. "
        "close: mark one entry reviewed, with the outcome noted in the "
        "note. remove: drop one entry. An open entry appears in every later "
        "turn: check it against what happened since before repeating or "
        "replacing the call, and grade it out loud. A recorded figure is "
        "your own record, not this session's observation — quote it as "
        "cited from the journal, never as an observed print. Symbols use "
        "the same canonical spellings as the position book. published/due: read validated final "
        "personal-advice publications with offset/limit pagination; due lists every pending review, "
        "including entries beyond the context's eight-row preview. Legacy record notes are not "
        "verified publication or proof that the user traded."
    )
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["record", "list", "close", "remove", "published", "due"],
                "description": "record | list | close | remove",
            },
            "symbol": {
                "type": "string",
                "description": (
                    "Instrument code (record): '600519.SH', '000001.SZ', "
                    "'000051.OF', 'AAPL.US', '00700.HK', or a bare six-digit "
                    "A-share code such as '600519'."
                ),
            },
            "advice": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": (
                    "The call being recorded (record): add | hold | reduce | "
                    "exit | watch."
                ),
            },
            "reference_price": {
                "type": "number",
                "description": (
                    "The price the call was anchored to (record), as it "
                    "stood when the call was made. Omit when no price was "
                    "stated."
                ),
            },
            "rationale": {
                "type": "string",
                "description": (
                    "Optional short reason stated with the call (record), "
                    "e.g. the trigger or the evidence behind it."
                ),
            },
            "id": {
                "type": "integer",
                "description": "Journal entry id (close/remove), e.g. 3.",
            },
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "as_of": {"type": "string", "description": "Due-review cutoff YYYY-MM-DD, default today in Shanghai."},
            "note": {
                "type": "string",
                "description": (
                    "Optional outcome note (close): where the call stood or "
                    "fell since it was recorded."
                ),
            },
        },
        "required": ["action"],
    }
    repeatable = True

    def __init__(self, store: AdviceJournalStore | None = None) -> None:
        """Initialize AdviceJournalTool.

        Args:
            store: AdviceJournalStore instance (default store if omitted).
        """
        self._store = store or AdviceJournalStore()

    def execute(self, **kwargs: Any) -> str:
        """Execute a journal action.

        Args:
            **kwargs: Must include action; other params depend on the action.

        Returns:
            JSON result string.
        """
        action = str(kwargs.get("action") or "list")
        if action in {"published", "due"}:
            from src.advisor.journal import PublishedAdviceStore
            from src.market_observation import market_now
            try:
                result = PublishedAdviceStore(self._store.path.parent / "published_advice.json").page(
                    offset=kwargs.get("offset", 0), limit=kwargs.get("limit", 20),
                    due_as_of=(kwargs.get("as_of") or market_now().date().isoformat()) if action == "due" else None,
                )
                return _payload("ok", **result)
            except (OSError, ValueError, TypeError) as exc:
                return _payload("error", error=str(exc))
        if action == "record":
            return self._record(kwargs)
        if action == "list":
            return self._list()
        if action == "close":
            return self._close(kwargs)
        if action == "remove":
            return self._remove(kwargs)
        return _payload("error", error=f"Unknown action: {action}")

    def _record(self, kwargs: dict[str, Any]) -> str:
        """Append one call to the journal."""
        try:
            from src.advisor.snapshots import evidence_root
            try:
                advisor_run = (evidence_root() / "artifacts/advisor_snapshot.json").exists()
            except ValueError:
                advisor_run = False
            if advisor_run:
                return _payload("deferred", message="Personal advice is published automatically only after final snapshot/decision validation; draft notes are not published decisions.")
            entry = self._store.record(
                kwargs.get("symbol"),
                action=kwargs.get("advice"),
                reference_price=kwargs.get("reference_price"),
                rationale=kwargs.get("rationale") or "",
            )
        except ValueError as exc:
            return _payload("error", error=str(exc))
        return _payload(
            "ok", message=f"Recorded: #{entry.id} {entry.symbol}", entry=entry.to_dict()
        )

    def _list(self) -> str:
        """Return the whole journal, newest first."""
        entries = self._store.load()
        newest = sorted(entries, key=lambda entry: entry.id, reverse=True)
        return _payload(
            "ok",
            count=len(newest),
            open_count=sum(1 for entry in newest if entry.status == "open"),
            entries=[entry.to_dict() for entry in newest],
        )

    def _close(self, kwargs: dict[str, Any]) -> str:
        """Close one entry as reviewed."""
        try:
            entry = self._store.close(kwargs.get("id"), note=kwargs.get("note") or "")
        except ValueError as exc:
            return _payload("error", error=str(exc))
        if entry is None:
            return _payload("not_found", message=f"No journal entry: {kwargs.get('id')}")
        return _payload(
            "ok", message=f"Closed: #{entry.id} {entry.symbol}", entry=entry.to_dict()
        )

    def _remove(self, kwargs: dict[str, Any]) -> str:
        """Drop one entry."""
        try:
            removed = self._store.remove(kwargs.get("id"))
        except ValueError as exc:
            return _payload("error", error=str(exc))
        if removed:
            return _payload("ok", message=f"Removed: #{kwargs.get('id')}")
        return _payload("not_found", message=f"No journal entry: {kwargs.get('id')}")
