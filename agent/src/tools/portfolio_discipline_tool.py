"""Portfolio discipline tool backed by :mod:`src.portfolio_discipline`.

The discipline is the user's own portfolio-level standing rules — the theme
ceiling, the regime windows that gate adding, the drawdown breaker, the
redemption-fee ladder and any guardrail they asked to be held to. It is
injected into every later turn (``<portfolio-discipline>``) and into the
grounding user corpus, so an answer is given with the user's own framework in
view. Every write persists immediately.

The tool writes data, never code: the ceilings and windows the user states are
stored in their own file and re-read on every turn, so changing a number never
touches the agent's source.
"""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.portfolio_discipline import (
    SECTIONS,
    PortfolioDisciplineStore,
    render_discipline_block,
)


def _payload(status: str, **fields: Any) -> str:
    """Return a JSON result string with a status field."""
    return json.dumps({"status": status, **fields}, ensure_ascii=False)


class PortfolioDisciplineTool(BaseTool):
    """Read and update the user's portfolio discipline."""

    name = "portfolio_discipline"
    description = (
        "The user's portfolio-level standing discipline: theme ceilings "
        "(a cluster's maximum share of the book), the broad-index regime "
        "windows that gate adding, the permission gates (tiers) those windows "
        "open, the drawdown circuit breaker, the conditions an add must "
        "satisfy, the redemption-fee ladder, and any guardrail the user asked "
        "to be held to. show: read it back. update: "
        "set only the sections you name — a section you omit keeps its value, "
        "so changing one ceiling never drops the rest. clear: remove it. "
        "Call it whenever the user states or changes one of these figures; it "
        "persists and every later turn reads it, so never re-ask for a rule "
        "already recorded. Record what the user states, never a figure you "
        "chose. This is data, not code: report a number here and the change "
        "takes effect on the next turn without any code edit. It holds rules "
        "only — no holdings and no amounts; cluster membership is matched by "
        "name keyword (members is the explicit override), so a position is "
        "never copied into this store."
    )
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["show", "update", "clear"],
                "description": "show | update | clear",
            },
            "cluster_caps": {
                "type": "array",
                "description": (
                    "update: theme ceilings, replacing the whole list. Each "
                    "entry is {label, max_percent, keywords: [...], members: "
                    "[...]}; max_percent is the cluster's ceiling as a "
                    "percentage of the book, keywords are name fragments that "
                    "place a holding in the cluster, members are explicit "
                    "holdings whose name carries no keyword. Pass [] to clear."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "max_percent": {"type": "number"},
                        "keywords": {"type": "array", "items": {"type": "string"}},
                        "members": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["label", "max_percent"],
                },
            },
            "regime": {
                "type": "object",
                "description": (
                    "update: the broad-index state machine. {indices: [...], "
                    "sma_short_days, sma_long_days, macd_confirm_days, note}. "
                    "Pass null to clear."
                ),
                "properties": {
                    "indices": {"type": "array", "items": {"type": "string"}},
                    "sma_short_days": {"type": "integer"},
                    "sma_long_days": {"type": "integer"},
                    "macd_confirm_days": {"type": "integer"},
                    "note": {"type": "string"},
                },
            },
            "tiers": {
                "type": "array",
                "description": (
                    "update: the permission gates of the regime state machine, "
                    "replacing the whole list. Each entry is {action, label, "
                    "when, note}: action is one of NO ADD | REDUCE | ADD "
                    "ALLOWED (the permission that gate grants), label is the "
                    "user's own name for it, when is the condition in prose "
                    "evaluated against this run's closes. A gate can carry "
                    "{short}, {long} and {confirm} placeholders, filled from "
                    "the regime's recorded window values when the block is "
                    "rendered. The gates are evaluated independently — several "
                    "may be open at once — not first-match-wins. Pass [] to "
                    "clear."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["NO ADD", "REDUCE", "ADD ALLOWED"],
                        },
                        "label": {"type": "string"},
                        "when": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["action", "label", "when"],
                },
            },
            "max_drawdown_percent": {
                "type": "number",
                "description": (
                    "update: drawdown from the peak, in percent, at which the "
                    "user's breaker trips. Pass null to clear."
                ),
            },
            "add_conditions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "update: the conditions an add must satisfy before it is "
                    "allowed, replacing the whole list. Pass [] to clear."
                ),
            },
            "redemption_fees": {
                "type": "array",
                "description": (
                    "update: the redemption-fee ladder, replacing the whole "
                    "list. Each entry is {holding_days_below, fee_percent}: "
                    "holding fewer than that many days charges that percent. "
                    "Pass [] to clear."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "holding_days_below": {"type": "integer"},
                        "fee_percent": {"type": "number"},
                    },
                    "required": ["holding_days_below", "fee_percent"],
                },
            },
            "guardrails": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "update: standing behavioural rules the user asked to be "
                    "held to, replacing the whole list. Pass [] to clear."
                ),
            },
            "note": {
                "type": "string",
                "description": "update: a short free-text remark. Pass \"\" to clear.",
            },
        },
        "required": ["action"],
    }
    repeatable = True

    def __init__(self, store: PortfolioDisciplineStore | None = None) -> None:
        """Initialize PortfolioDisciplineTool.

        Args:
            store: PortfolioDisciplineStore instance (default store if omitted).
        """
        self._store = store or PortfolioDisciplineStore()

    def execute(self, **kwargs: Any) -> str:
        """Execute a discipline action.

        Args:
            **kwargs: Must include action; other params depend on the action.

        Returns:
            JSON result string.
        """
        action = str(kwargs.get("action") or "show")
        if action == "show":
            return self._show()
        if action == "update":
            return self._update(kwargs)
        if action == "clear":
            return self._clear()
        return _payload("error", error=f"Unknown action: {action}")

    def _show(self) -> str:
        """Return the current discipline."""
        discipline = self._store.load()
        if discipline is None:
            return _payload("ok", recorded=False, discipline=None, block="")
        return _payload(
            "ok",
            recorded=True,
            discipline=discipline.to_dict(),
            block=render_discipline_block(self._store),
        )

    def _update(self, kwargs: dict[str, Any]) -> str:
        """Merge the named sections and persist the document."""
        sections = {name: kwargs[name] for name in SECTIONS if name in kwargs}
        if not sections:
            return _payload(
                "error",
                error="update needs at least one section: " + ", ".join(SECTIONS),
            )
        try:
            discipline = self._store.update(**sections)
        except ValueError as exc:
            return _payload("error", error=str(exc))
        return _payload(
            "ok",
            message="Discipline updated: " + ", ".join(sorted(sections)),
            discipline=discipline.to_dict(),
            block=render_discipline_block(self._store),
        )

    def _clear(self) -> str:
        """Remove the whole document."""
        if self._store.clear():
            return _payload("ok", message="Portfolio discipline cleared")
        return _payload("not_found", message="No portfolio discipline recorded")
