"""The user's standing risk rules: stops, targets and position caps.

A discipline the user states once ("600519.SH, stop at 1400, cap at 200
shares") is a standing instruction, not a fact about one turn. This module
makes it persistent, so every later answer is given with the user's own
limits in view:

* :class:`RiskRulesStore` — the JSON store under the runtime root
  (``risk_rules.json``), written atomically (temp file in the same directory +
  ``os.replace``) so an interrupted write can never truncate or half-write
  the rules.
* :class:`RiskRule` — one rule per instrument, with any of three optional
  limits: a stop-loss, a take-profit and a position cap. A rule must state at
  least one of them; a row with none says nothing and is not stored.
* :func:`render_risk_block` — the markdown lines injected into every turn's
  user message (inside the ``<risk-rules>`` tag) and merged into the grounding
  ledger's user corpus: the limits are the user's own statement, so a ``held``
  figure quoting one reconciles as theirs. When the position book shows a
  holding past a rule's cap, the line carries the overage.

The trigger side is deliberately not computed here. A stop-loss and a
take-profit are price levels, and this module has no price source — the model
compares them against the latest price a tool returned in the turn. The cap is
the one rule measurable from stored state alone (the book's quantity), so it
is the one flagged from here; a short position is capped by its exposure, so
the comparison uses the quantity's absolute value.

Storage shape (``version 1``)::

    {"version": 1, "updated_at": "...", "rules": [
        {"symbol": "600519.SH", "stop_loss": 1400.0, "take_profit": 1900.0,
         "max_quantity": 200, "updated_at": "..."}]}
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
from src.position_book import PositionBookStore, canonical_book_symbol

logger = logging.getLogger(__name__)

#: Rules file name under the runtime root.
RISK_RULES_FILENAME = "risk_rules.json"
#: On-disk schema version; bumped only on an incompatible shape change.
RISK_RULES_VERSION = 1


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


@dataclass(frozen=True)
class RiskRule:
    """One instrument's standing limits.

    Attributes:
        symbol: Canonical symbol.
        stop_loss: Optional price level the user exits a loss at.
        take_profit: Optional price level the user takes profit at.
        max_quantity: Optional cap on units held; a short position is capped
            by its exposure (the absolute quantity).
        updated_at: UTC ISO8601 timestamp of the last write for this rule.
    """

    symbol: str
    stop_loss: float | None = None
    take_profit: float | None = None
    max_quantity: float | None = None
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready row."""
        return {
            "symbol": self.symbol,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "max_quantity": self.max_quantity,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(row: Any) -> "RiskRule | None":
        """Parse one persisted row, or None when the row is unusable.

        A limit whose stored value is not a finite positive number is dropped
        like an absent one — sibling stores are lenient per field — and a row
        left with no limit at all is unusable.
        """
        if not isinstance(row, dict):
            return None
        symbol = canonical_book_symbol(row.get("symbol"))
        if symbol is None:
            return None
        limits: dict[str, float | None] = {}
        for name in ("stop_loss", "take_profit", "max_quantity"):
            raw = row.get(name)
            if raw is None:
                limits[name] = None
                continue
            try:
                parsed = _finite_number(raw, name)
            except ValueError:
                limits[name] = None
                continue
            limits[name] = parsed if parsed > 0 else None
        if all(value is None for value in limits.values()):
            return None
        return RiskRule(
            symbol=symbol,
            stop_loss=limits["stop_loss"],
            take_profit=limits["take_profit"],
            max_quantity=limits["max_quantity"],
            updated_at=str(row.get("updated_at") or ""),
        )


class RiskRulesStore:
    """JSON-backed store for the user's standing risk rules."""

    def __init__(self, path: Path | str | None = None) -> None:
        """Initialize the store.

        Args:
            path: Explicit rules file path (tests); defaults to
                ``<runtime-root>/risk_rules.json``, resolved here at
                construction time.
        """
        self.path = (
            Path(path)
            if path is not None
            else get_runtime_root() / RISK_RULES_FILENAME
        )

    def load(self) -> list[RiskRule]:
        """Return the persisted rules.

        A missing file is an empty set of rules. An unreadable or malformed
        file is logged and treated as empty rather than failing the turn that
        reads it; rows that fail to parse are dropped, never repaired by
        guessing.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "risk rules %s unreadable (%s); treated as empty", self.path, exc
            )
            return []
        rows = raw.get("rules") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return []
        rules: list[RiskRule] = []
        for row in rows:
            rule = RiskRule.from_dict(row)
            if rule is not None:
                rules.append(rule)
        return rules

    def _save(self, rules: list[RiskRule]) -> None:
        """Write the rules atomically (temp file in the same directory + rename)."""
        payload = {
            "version": RISK_RULES_VERSION,
            "updated_at": _utc_now(),
            "rules": [rule.to_dict() for rule in rules],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp_path, self.path)

    def set_rule(
        self,
        symbol: Any,
        *,
        stop_loss: Any = None,
        take_profit: Any = None,
        max_quantity: Any = None,
    ) -> RiskRule:
        """Insert or update one instrument's rule and persist the store.

        Merge, not replace: a call that omits a limit keeps the value already
        recorded for that symbol, so setting a stop never erases a cap. A
        fresh rule must carry at least one limit; storing a row that says
        nothing would only add a line to every later turn.

        Args:
            symbol: Instrument code in any accepted spelling.
            stop_loss: Optional stop price; omitted keeps the recorded one.
            take_profit: Optional target price; omitted keeps the recorded one.
            max_quantity: Optional position cap; omitted keeps the recorded one.

        Returns:
            The stored rule.

        Raises:
            ValueError: The symbol has no canonical form, a limit is not a
                finite positive number, or a fresh rule states no limit.
        """
        canonical = canonical_book_symbol(symbol)
        if canonical is None:
            raise ValueError(f"unsupported symbol: {symbol!r}")
        limits: dict[str, float | None] = {}
        for name, raw in (
            ("stop_loss", stop_loss),
            ("take_profit", take_profit),
            ("max_quantity", max_quantity),
        ):
            if raw is None:
                limits[name] = None
                continue
            parsed = _finite_number(raw, name)
            if parsed <= 0:
                raise ValueError(f"{name} must be positive")
            limits[name] = parsed

        rules = self.load()
        existing = next((rule for rule in rules if rule.symbol == canonical), None)
        updated = RiskRule(
            symbol=canonical,
            stop_loss=(
                limits["stop_loss"]
                if limits["stop_loss"] is not None
                else (existing.stop_loss if existing else None)
            ),
            take_profit=(
                limits["take_profit"]
                if limits["take_profit"] is not None
                else (existing.take_profit if existing else None)
            ),
            max_quantity=(
                limits["max_quantity"]
                if limits["max_quantity"] is not None
                else (existing.max_quantity if existing else None)
            ),
            updated_at=_utc_now(),
        )
        if (
            updated.stop_loss is None
            and updated.take_profit is None
            and updated.max_quantity is None
        ):
            raise ValueError(
                "a rule needs at least one of stop_loss, take_profit, "
                "max_quantity"
            )
        if existing is None:
            rules.append(updated)
        else:
            rules[rules.index(existing)] = updated
        self._save(rules)
        return updated

    def remove(self, symbol: Any) -> bool:
        """Remove the rule for *symbol*.

        Returns:
            True when a rule was removed, False when no rule carried the
            symbol.

        Raises:
            ValueError: The symbol has no canonical form.
        """
        canonical = canonical_book_symbol(symbol)
        if canonical is None:
            raise ValueError(f"unsupported symbol: {symbol!r}")
        rules = self.load()
        remaining = [rule for rule in rules if rule.symbol != canonical]
        if len(remaining) == len(rules):
            return False
        self._save(remaining)
        return True

    def clear(self) -> int:
        """Empty the store.

        Returns:
            The number of rules removed.
        """
        rules = self.load()
        if not rules:
            return 0
        self._save([])
        return len(rules)


def _fmt_number(value: float) -> str:
    """Render a stored number without a trailing ``.0`` for whole units."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _rule_line(rule: RiskRule, over_cap_quantity: float | None = None) -> str:
    """One markdown line for :func:`render_risk_block`."""
    parts: list[str] = []
    if rule.stop_loss is not None:
        parts.append(f"stop-loss {_fmt_number(rule.stop_loss)}")
    if rule.take_profit is not None:
        parts.append(f"take-profit {_fmt_number(rule.take_profit)}")
    if rule.max_quantity is not None:
        parts.append(f"max {_fmt_number(rule.max_quantity)} units")
    line = f"- {rule.symbol}: {', '.join(parts)}"
    if rule.updated_at:
        line += f" (recorded {rule.updated_at[:10]})"
    if over_cap_quantity is not None:
        line += f" — position {_fmt_number(over_cap_quantity)} is over the cap"
    return line


def render_risk_block(
    store: RiskRulesStore | None = None,
    book: PositionBookStore | None = None,
) -> str:
    """Render the risk rules as the markdown lines injected into a turn, or "".

    The lines are the user's own standing limits, so callers quote them as
    theirs: they go inside the ``<risk-rules>`` block of the user message and
    into the grounding ledger's user corpus that a ``held`` figure is
    reconciled against. When *book* is given and its row for a symbol sits
    over that symbol's cap (by absolute quantity, so a short is capped by its
    exposure), the line says so; the stop-loss and take-profit lines are
    levels for the model to compare against prices, not states computed here.

    Args:
        store: Store to read; a fresh default store when omitted, so the
            runtime root is resolved at call time.
        book: Optional position book for the cap cross-check.

    Returns:
        One ``- SYMBOL ...`` line per rule, or "" when no rules are recorded.
    """
    rules = (store or RiskRulesStore()).load()
    if not rules:
        return ""
    quantities: dict[str, float] = {}
    if book is not None:
        try:
            quantities = {
                position.symbol: position.quantity for position in book.load()
            }
        except Exception as exc:  # noqa: BLE001 - a broken book drops the cap note only
            logger.debug("risk-rule cap lookup failed: %s", exc)
    lines: list[str] = []
    for rule in rules:
        quantity = quantities.get(rule.symbol)
        over_cap = (
            quantity
            if rule.max_quantity is not None
            and quantity is not None
            and abs(quantity) > rule.max_quantity
            else None
        )
        lines.append(_rule_line(rule, over_cap))
    return "\n".join(lines)