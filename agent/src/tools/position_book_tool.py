"""User position book tool backed by :mod:`src.position_book`.

The book is the user's own statement of what they hold. Positions recorded
here are injected into every later turn (``<user-book>``) and into the
grounding user corpus, so a stated holding, quantity or cost is used from the
record instead of being asked for again. Every write persists immediately.
"""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.position_book import PositionBookStore


def _payload(status: str, **fields: Any) -> str:
    """Return a JSON result string with a status field."""
    return json.dumps({"status": status, **fields}, ensure_ascii=False)


class PositionBookTool(BaseTool):
    """Read and update the user's own position book."""

    name = "position_book"
    description = (
        "The user's own position book: the holdings they have told you they "
        "own, with quantity and average cost. set: record or update one "
        "position — call it whenever the user states a holding, a cost, or a "
        "change to one; it persists, every later turn reads it, and the user "
        "is never asked again for a position already in the book. list: read "
        "the book back. remove: drop one symbol. clear: empty the book. "
        "Symbols are canonicalized (<code>.SH/.SZ/.BJ, <code>.OF, TICKER.US, "
        "<code>.HK); a bare six-digit A-share code is qualified by its first "
        "digit, anything else needs an explicit suffix. Quantity is units "
        "held (negative = short); cost is the average cost per unit. The book "
        "is the user's own statement: quote it as theirs, and update it "
        "rather than re-asking when they correct it. account: read the effective "
        "account including dated user-memory holdings, unresolved groups, cash "
        "and pending orders. set_account: record only user-supplied cash and "
        "pending applications. set_identity/identity: record/read a dated "
        "user-declared feeder-to-ETF-to-index relationship and product terms; it "
        "never becomes verified market evidence. For a bare fund code supply "
        "instrument_type=off_exchange_fund. Never invent share quantities from "
        "reported monetary amounts or split a combined-code holding."
    )
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["set", "list", "remove", "clear", "account", "set_account", "identity", "set_identity"],
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
            "quantity": {
                "type": "number",
                "description": (
                    "Units held (set), exactly as the user stated them. "
                    "Negative for a short position."
                ),
            },
            "cost": {
                "type": "number",
                "description": (
                    "Average cost per unit (set), as the user stated it. Omit "
                    "to keep the cost already recorded for the symbol."
                ),
            },
            "name": {
                "type": "string",
                "description": "Optional display name (set), e.g. 贵州茅台.",
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional short note (set), e.g. the plan or limit stated "
                    "with the position."
                ),
            },
            "instrument_type": {"type": "string", "enum": ["off_exchange_fund", "etf_feeder", "listed_security"]},
            "reported_value": {"type": "number", "description": "User-reported monetary value; never share quantity."},
            "value_as_of": {"type": "string", "description": "Exact date of the reported value, if supplied."},
            "lots": {"type": "array", "items": {"type": "object"}, "description": "User-supplied lots: lot_id, quantity, confirmed_at (YYYY-MM-DD if known), optional cost."},
            "cash": {"type": "object", "description": "Account cash: amount, currency, optional as_of/source_ref; before pending reservations."},
            "pending_orders": {"type": "array", "items": {"type": "object"}, "description": "order_id, canonical symbol, action subscribe/redeem, amount/quantity, state, submitted_at."},
            "minimum_cash": {"type": "number", "minimum": 0, "description": "User-stated minimum account cash, never inferred."},
            "max_loss_amount": {"type": "number", "minimum": 0},
            "peak_value": {"type": "number", "minimum": 0, "description": "User-reported account peak; investment drawdown still requires cash-flow reconciliation."},
            "investment_horizon": {"type": "string", "description": "User-stated horizon, including conditional exit without a fixed term."},
            "share_class": {"type": "string"},
            "target_etf": {"type": "string"},
            "tracking_index": {"type": "string"},
            "source_ref": {"type": "string"},
            "as_of": {"type": "string"},
            "terms": {"type": "object", "description": "Product-specific effective_from, redemption_tiers (holding_days_below/fee_percent), cutoff_time. Unknown fields remain unknown."},
        },
        "required": ["action"],
    }
    repeatable = True

    def __init__(self, store: PositionBookStore | None = None) -> None:
        """Initialize PositionBookTool.

        Args:
            store: PositionBookStore instance (default store if omitted).
        """
        self._store = store or PositionBookStore()

    def execute(self, **kwargs: Any) -> str:
        """Execute a book action.

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
        if action in {"account", "set_account", "identity", "set_identity"}:
            return self._account_action(action, kwargs)
        if action == "remove":
            return self._remove(kwargs)
        if action == "clear":
            return self._clear()
        return _payload("error", error=f"Unknown action: {action}")

    def _set(self, kwargs: dict[str, Any]) -> str:
        """Record or update one position."""
        try:
            position = self._store.set_position(
                kwargs.get("symbol"),
                quantity=kwargs.get("quantity"),
                cost=kwargs.get("cost"),
                name=kwargs.get("name") or "",
                note=kwargs.get("note") or "",
                instrument_type=kwargs.get("instrument_type"),
                reported_value=kwargs.get("reported_value"),
                value_as_of=kwargs.get("value_as_of"),
                lots=kwargs.get("lots"),
            )
        except ValueError as exc:
            return _payload("error", error=str(exc))
        return _payload(
            "ok", message=f"Recorded: {position.symbol}", position=position.to_dict()
        )

    def _list(self) -> str:
        """Return the current book."""
        positions = self._store.load_effective()
        account = self._store.load_account()
        if account.get("errors"):
            return _payload("error", error="position book is unreadable; existing data preserved", errors=account["errors"])
        return _payload(
            "ok",
            count=len(positions),
            positions=[position.to_dict() for position in positions],
            holding_groups=account.get("holding_groups", []),
            cash=account.get("cash"), warnings=account.get("warnings", []),
        )

    def _account_action(self, action: str, kwargs: dict[str, Any]) -> str:
        from src.fund_identity import FundIdentityStore
        try:
            if action == "account":
                account = self._store.load_account()
                return _payload("error" if account.get("errors") else "ok", account=account)
            if action == "set_account":
                account = self._store.set_account(
                    cash=kwargs.get("cash"), pending_orders=kwargs.get("pending_orders"),
                    **{key: kwargs[key] for key in ("source_ref", "as_of", "minimum_cash", "max_loss_amount", "peak_value", "investment_horizon") if key in kwargs},
                )
                return _payload("ok", account=account)
            identities = FundIdentityStore(self._store.path.parent / "fund_identities.json")
            if action == "identity":
                record = identities.lookup(str(kwargs.get("symbol") or ""))
                return _payload("ok" if record else "not_found", identity=record)
            record = identities.record(
                str(kwargs.get("symbol") or ""),
                **{key: kwargs[key] for key in (
                    "name", "share_class", "target_etf", "tracking_index", "source_ref", "as_of", "terms",
                ) if key in kwargs},
            )
            return _payload("ok", identity=record)
        except (ValueError, OSError) as exc:
            return _payload("error", error=str(exc))

    def _remove(self, kwargs: dict[str, Any]) -> str:
        """Drop one symbol from the book."""
        try:
            removed = self._store.remove_position(kwargs.get("symbol"))
        except ValueError as exc:
            return _payload("error", error=str(exc))
        if removed:
            return _payload("ok", message=f"Removed: {kwargs.get('symbol')}")
        return _payload("not_found", message=f"Not in the book: {kwargs.get('symbol')}")

    def _clear(self) -> str:
        """Empty the book."""
        removed = self._store.clear()
        return _payload("ok", message=f"Cleared {removed} position(s)")
