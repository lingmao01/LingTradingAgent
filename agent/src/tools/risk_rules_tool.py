"""Risk rules tool backed by :mod:`src.risk_rules`.

The rules are the user's own standing discipline — the stop-loss,
take-profit and position caps they stated. They are injected into every later
turn (``<risk-rules>``) and into the grounding user corpus, so an answer is
given with the user's limits in view and a level they stated is quoted as
theirs. Every write persists immediately.
"""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.risk_rules import RiskRulesStore


def _payload(status: str, **fields: Any) -> str:
    """Return a JSON result string with a status field."""
    return json.dumps({"status": status, **fields}, ensure_ascii=False)


class RiskRulesTool(BaseTool):
    """Record and read the user's standing risk rules."""

    name = "risk_rules"
    description = (
        "The user's standing risk rules: the stop-loss, take-profit and "
        "position caps they set per instrument. set: record or update one "
        "rule — call it whenever the user states a stop, a target, or a "
        "cap for an instrument; it persists, and every later turn reads it. "
        "list: read the rules back. remove: drop one symbol's rule. clear: "
        "empty the store. Merging, not replacing: an omitted limit keeps the "
        "value already recorded. The rules are the user's own discipline and "
        "outrank your judgement — check an instrument's stop-loss and "
        "take-profit against the latest price a tool returned before stating "
        "an action, say so first when a level is breached, and never edit a "
        "rule on your own. Symbols use the same canonical spellings as the "
        "position book."
    )
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["set", "list", "remove", "clear"],
                "description": "set | list | remove | clear",
            },
            "symbol": {
                "type": "string",
                "description": (
                    "Instrument code (set/remove): '600519.SH', '000001.SZ', "
                    "'000051.OF', 'AAPL.US', '00700.HK', or a bare six-digit "
                    "A-share code such as '600519'."
                ),
            },
            "stop_loss": {
                "type": "number",
                "description": (
                    "Stop-loss price level for the instrument (set), as the "
                    "user stated it. Omit to keep the recorded level."
                ),
            },
            "take_profit": {
                "type": "number",
                "description": (
                    "Take-profit price level (set). Omit to keep the "
                    "recorded level."
                ),
            },
            "max_quantity": {
                "type": "number",
                "description": (
                    "Cap on units held (set); a short position is capped by "
                    "its exposure. Omit to keep the recorded cap."
                ),
            },
        },
        "required": ["action"],
    }
    repeatable = True

    def __init__(self, store: RiskRulesStore | None = None) -> None:
        """Initialize RiskRulesTool.

        Args:
            store: RiskRulesStore instance (default store if omitted).
        """
        self._store = store or RiskRulesStore()

    def execute(self, **kwargs: Any) -> str:
        """Execute a rules action.

        Args:
            **kwargs: Must include action; other params depend on the action.

        Returns:
            JSON result string.
        """
        action = str(kwargs.get("action") or "list")
        if action == "set":
            return self._set(kwargs)
        if action == "list":
            return self._list()
        if action == "remove":
            return self._remove(kwargs)
        if action == "clear":
            return self._clear()
        return _payload("error", error=f"Unknown action: {action}")

    def _set(self, kwargs: dict[str, Any]) -> str:
        """Record or update one rule."""
        try:
            rule = self._store.set_rule(
                kwargs.get("symbol"),
                stop_loss=kwargs.get("stop_loss"),
                take_profit=kwargs.get("take_profit"),
                max_quantity=kwargs.get("max_quantity"),
            )
        except ValueError as exc:
            return _payload("error", error=str(exc))
        return _payload(
            "ok", message=f"Recorded: {rule.symbol}", rule=rule.to_dict()
        )

    def _list(self) -> str:
        """Return the current rules."""
        rules = self._store.load()
        return _payload(
            "ok",
            count=len(rules),
            rules=[rule.to_dict() for rule in rules],
        )

    def _remove(self, kwargs: dict[str, Any]) -> str:
        """Drop one symbol's rule."""
        try:
            removed = self._store.remove(kwargs.get("symbol"))
        except ValueError as exc:
            return _payload("error", error=str(exc))
        if removed:
            return _payload("ok", message=f"Removed: {kwargs.get('symbol')}")
        return _payload(
            "not_found", message=f"No rule for: {kwargs.get('symbol')}"
        )

    def _clear(self) -> str:
        """Empty the store."""
        removed = self._store.clear()
        return _payload("ok", message=f"Cleared {removed} rule(s)")