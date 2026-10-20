"""Validation and reservation arithmetic for an advisory fund account."""
from __future__ import annotations

import math
from datetime import date
from typing import Any


def number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f"{label} is outside its valid range")
    return result


def calendar_date(value: Any, label: str = "date") -> str:
    text = str(value or "")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid {label}: {text!r}") from exc
    return text


def validate_lots(rows: Any, quantity: float | None) -> tuple[dict[str, Any], ...]:
    if not isinstance(rows, (tuple, list)):
        raise ValueError("lots must be a list")
    parsed = []
    identifiers = set()
    for raw in rows:
        if not isinstance(raw, dict) or not str(raw.get("lot_id") or "").strip():
            raise ValueError("each lot requires a lot_id")
        row = dict(raw)
        if row["lot_id"] in identifiers:
            raise ValueError("duplicate lot_id")
        identifiers.add(row["lot_id"])
        row["quantity"] = number(row.get("quantity"), "lot quantity", minimum=0)
        for field in ("confirmed_at", "purchase_date"):
            if row.get(field) is not None:
                row[field] = calendar_date(row[field], f"lot {field} date")
        if row.get("cost") is not None:
            row["cost"] = number(row["cost"], "lot cost", minimum=0)
        parsed.append(row)
    if parsed and quantity is not None and not math.isclose(sum(r["quantity"] for r in parsed), quantity, abs_tol=1e-8):
        raise ValueError("lot quantities must equal the recorded position quantity")
    return tuple(parsed)


def validate_cash(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("cash must carry amount, currency and optional as_of")
    row = dict(raw)
    row["amount"] = number(row.get("amount"), "cash amount", minimum=0)
    currency = str(row.get("currency") or "").upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("cash currency is required")
    row["currency"] = currency
    if row.get("as_of") is not None:
        row["as_of"] = calendar_date(row["as_of"], "cash as_of date")
    return row


def validate_pending(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError("pending_orders must be a list")
    from src.position_book import canonical_book_symbol
    output, identifiers = [], set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("pending order must be an object")
        row = dict(raw)
        ident = str(row.get("order_id") or "").strip()
        if not ident or ident in identifiers:
            raise ValueError("pending order requires a unique order_id")
        identifiers.add(ident)
        symbol = canonical_book_symbol(row.get("symbol"))
        if not symbol:
            raise ValueError("pending order requires an explicit canonical symbol")
        row["symbol"] = symbol
        if row.get("action") not in {"subscribe", "redeem"}:
            raise ValueError("pending order action must be subscribe or redeem")
        if row.get("state", "pending") not in {"pending", "confirmed", "cancelled"}:
            raise ValueError("pending order state is invalid")
        row["state"] = row.get("state", "pending")
        key = "amount" if row["action"] == "subscribe" else "quantity"
        row[key] = number(row.get(key), f"pending {key}", minimum=0)
        calendar_date(str(row.get("submitted_at") or "")[:10], "pending submitted date")
        output.append(row)
    return output


def available_cash(account: dict[str, Any]) -> float | None:
    if account.get('errors'):
        return None
    if any(isinstance(event, dict) and event.get('symbol') == '__CASH__' for event in account.get('unreconciled_events', []) or []):
        return None
    cash = account.get("cash")
    if not isinstance(cash, dict) or cash.get("amount") is None:
        return None
    reserved = sum(
        float(row.get("amount") or 0) for row in account.get("pending_orders", [])
        if row.get("action") == "subscribe" and row.get("state") == "pending"
    )
    # A negative result is a conflict to report, not money to quietly clamp away.
    return float(cash["amount"]) - reserved
