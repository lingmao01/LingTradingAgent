"""The user's own position book: holdings and cost as first-class state.

A position the user states once ("600519.SH, 100 shares, average cost 1500")
is the answer to a question the model would otherwise ask again on the next
turn. This module makes that statement persistent:

* :class:`PositionBookStore` — the JSON store under the runtime root
  (``position_book.json``), written atomically (temp file in the same
  directory + ``os.replace``) so an interrupted write can never truncate or
  half-write the book.
* :func:`canonical_book_symbol` — one canonical spelling per instrument, with
  the same suffix grammar the rest of the agent uses (``.SH``/``.SZ``/``.BJ``
  for A-shares, ``.OF`` for off-exchange funds, ``.US``, ``.HK``). A bare
  six-digit code is qualified by its first digit; anything else must carry an
  explicit suffix, because guessing a venue is how a book quietly changes
  meaning.
* :func:`render_book_block` — the markdown lines injected into every turn's
  user message (inside the ``<user-book>`` tag) and handed to the grounding
  ledger, whose user corpus a ``held`` figure is reconciled against. What the
  user recorded is their own statement, so it is quoted as theirs and never
  asked for again.

Storage shape (``version 1``)::

    {"version": 1, "updated_at": "...", "positions": [
        {"symbol": "600519.SH", "name": "贵州茅台", "quantity": 100,
         "cost": 1500.0, "note": "", "updated_at": "..."}]}
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config.paths import get_runtime_root
from src.core.file_lock import store_transaction
from src.account_ledger import available_cash, calendar_date, validate_cash, validate_lots, validate_pending

logger = logging.getLogger(__name__)

#: Book file name under the runtime root.
BOOK_FILENAME = "position_book.json"
#: On-disk schema version; bumped only on an incompatible shape change.
BOOK_VERSION = 1

# A name or note is re-injected into every future turn's context, so each is
# capped — the same paste-safety reason playbook variables are capped.
_MAX_NAME_CHARS = 80
_MAX_NOTE_CHARS = 500

_SUFFIXED_RE = re.compile(
    r"^(?P<body>[A-Z0-9][A-Z0-9\-]{0,15})\.(?P<suffix>SH|SZ|BJ|OF|US|HK)$"
)
_CN_BODY_RE = re.compile(r"^\d{6}$")
_HK_BODY_RE = re.compile(r"^\d{1,5}$")
_US_BODY_RE = re.compile(r"^[A-Z][A-Z0-9\-]{0,15}$")
_BARE_CN_RE = re.compile(r"^\d{6}$")

# First-digit exchange inference for a bare six-digit code: Shanghai carries
# stocks (6), funds (5) and B-shares (9); Shenzhen stocks and funds (0/1/2/3);
# Beijing (4/8). Any other leading digit stays unqualified — the caller must
# spell the venue out rather than have one guessed.
_BARE_EXCHANGE = {
    "0": "SZ",
    "1": "SZ",
    "2": "SZ",
    "3": "SZ",
    "4": "BJ",
    "8": "BJ",
    "5": "SH",
    "6": "SH",
    "9": "SH",
}


def _utc_now() -> str:
    """UTC ISO8601 timestamp (seconds precision), matching sibling stores."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_book_symbol(raw: Any, *, instrument_type: str | None = None, name: str = "") -> str | None:
    """Return the canonical spelling of an instrument code, or None.

    Accepted forms (case-insensitive, whitespace-stripped):
    ``600519``, ``600519.SH``, ``000001.SZ``, ``430139.BJ``, ``000051.OF``,
    ``AAPL.US``, ``00700.HK``. A bare six-digit code is qualified by its first
    digit (see :data:`_BARE_EXCHANGE`); a code that already carries a suffix
    must match that venue's body shape — ``.SH``/``.SZ``/``.BJ``/``.OF`` take
    six digits, ``.HK`` zero-pads to five, ``.US`` uppercases the ticker.

    Args:
        raw: The user-supplied code in any accepted spelling.

    Returns:
        The canonical symbol, or ``None`` when the value has no unambiguous
        canonical form.
    """
    text = str(raw or "").strip().upper().replace(" ", "")
    if not text:
        return None
    match = _SUFFIXED_RE.match(text)
    if match is None:
        if _BARE_CN_RE.match(text):
            if instrument_type in {"fund", "off_exchange_fund", "etf_feeder"} or "联接" in name or "场外" in name:
                return f"{text}.OF"
            suffix = _BARE_EXCHANGE.get(text[0])
            return f"{text}.{suffix}" if suffix is not None else None
        return None
    body, suffix = match.group("body"), match.group("suffix")
    if suffix in {"SH", "SZ", "BJ", "OF"}:
        return f"{body}.{suffix}" if _CN_BODY_RE.match(body) else None
    if suffix == "HK":
        return f"{body.zfill(5)}.HK" if _HK_BODY_RE.match(body) else None
    return f"{body}.US" if _US_BODY_RE.match(body) else None


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
class Position:
    """One row of the book: an instrument the user says they hold.

    Attributes:
        symbol: Canonical symbol.
        quantity: Units held; negative for a short position.
        name: Optional display name.
        cost: Optional average cost per unit.
        note: Optional short free-text note stated with the position.
        updated_at: UTC ISO8601 timestamp of the last write for this row.
    """

    symbol: str
    quantity: float | None
    name: str = ""
    cost: float | None = None
    note: str = ""
    updated_at: str = ""
    reported_value: float | None = None
    value_as_of: str | None = None
    reported_at: str | None = None
    source_ref: str | None = None
    purchase_date: str | None = None
    reported_return_pct: float | None = None
    lots: tuple[dict[str, Any], ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready row."""
        row = {
            **self.extra,
            "symbol": self.symbol,
            "name": self.name,
            "quantity": self.quantity,
            "cost": self.cost,
            "note": self.note,
            "updated_at": self.updated_at,
        }
        if self.reported_value is not None:
            row.update({
                "reported_value": self.reported_value, "value_as_of": self.value_as_of,
                "reported_at": self.reported_at, "source_ref": self.source_ref,
                "purchase_date": self.purchase_date, "reported_return_pct": self.reported_return_pct,
            })
        if self.lots:
            row["lots"] = [dict(lot) for lot in self.lots]
        return row

    @staticmethod
    def from_dict(row: Any) -> "Position | None":
        """Parse one persisted row, or None when the row is unusable."""
        if not isinstance(row, dict):
            return None
        symbol = canonical_book_symbol(row.get("symbol"), name=str(row.get("name") or ""))
        if symbol is None:
            return None
        try:
            reported = None if row.get("reported_value") is None else _finite_number(row["reported_value"], "reported_value")
            quantity = None if row.get("quantity") is None and reported is not None else _finite_number(row.get("quantity"), "quantity")
        except ValueError:
            return None
        cost: float | None = None
        if row.get("cost") is not None:
            try:
                cost = _finite_number(row["cost"], "cost")
            except ValueError:
                cost = None
        known = {item.name for item in Position.__dataclass_fields__.values()}
        extra = {key: value for key, value in row.items() if key not in known}
        try:
            lots = validate_lots(row.get("lots", []), quantity)
        except ValueError as exc:
            lots = ()
            extra["unparsed_lots"] = row.get("lots")
            extra["ledger_warnings"] = [*extra.get("ledger_warnings", []), str(exc)]
        return Position(
            symbol=symbol,
            quantity=quantity,
            name=str(row.get("name") or "").strip(),
            cost=cost,
            note=str(row.get("note") or "").strip(),
            updated_at=str(row.get("updated_at") or ""),
            reported_value=reported, value_as_of=row.get("value_as_of"),
            reported_at=row.get("reported_at"), source_ref=row.get("source_ref"),
            purchase_date=row.get("purchase_date"), reported_return_pct=row.get("reported_return_pct"),
            lots=lots, extra=extra,
        )


class PositionBookStore:
    """JSON-backed store for the user's own position book."""

    def __init__(self, path: Path | str | None = None) -> None:
        """Initialize the store.

        Args:
            path: Explicit book file path (tests); defaults to
                ``<runtime-root>/position_book.json``, resolved here at
                construction time.
        """
        self.path = (
            Path(path) if path is not None else get_runtime_root() / BOOK_FILENAME
        )

    def load(self) -> list[Position]:
        """Return the persisted positions.

        A missing file is an empty book. An unreadable or malformed file is
        logged and treated as empty rather than failing the turn that reads
        it; rows that fail to parse are dropped, never repaired by guessing.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "position book %s unreadable (%s); treated as empty", self.path, exc
            )
            return []
        rows = raw.get("positions") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return []
        positions: list[Position] = []
        for row in rows:
            position = Position.from_dict(row)
            if position is not None:
                positions.append(position)
        return positions

    def load_account(self) -> dict[str, Any]:
        """Effective account: structured book, else dated user-reported memory.

        An explicit empty book is authoritative; recalling old data must not
        resurrect a position the user deliberately removed.
        """
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return {"positions": [], "holding_groups": [], "pending_orders": [], "errors": [str(exc)], "quality": "unavailable"}
            if not isinstance(payload, dict):
                return {"positions": [], "errors": ["position book is not an object"], "quality": "unavailable"}
            rows = payload.get("positions")
            errors = list(payload.get("errors") or [])
            valid, rejected, seen = [], [], set()
            if not isinstance(rows, list):
                errors.append("position book positions must be a list")
                rows = []
            for index, row in enumerate(rows):
                position = Position.from_dict(row)
                if position is None or position.symbol in seen:
                    errors.append(f"position row {index} is invalid or duplicated; original file retained")
                    rejected.append(row)
                else:
                    seen.add(position.symbol)
                    valid.append(position.to_dict())
            payload = {**payload, "positions": valid}
            if errors:
                payload.update(errors=errors, unparsed_positions=rejected, quality="partial")
        else:
            from src.legacy_book import recalled_account
            payload = recalled_account(self.path.parent / "memory")
        errors = list(payload.get('errors') or [])
        for field, validator, empty in [('cash', validate_cash, {}), ('pending_orders', validate_pending, [])]:
            if payload.get(field) is None:
                continue
            try:
                payload[field] = validator(payload[field])
            except (ValueError, TypeError, KeyError) as exc:
                payload['unparsed_' + field] = payload[field]
                payload[field] = empty
                errors.append(f'invalid account {field}: {exc}; original file retained')
        for field in ('holding_groups', 'unreconciled_events'):
            rows = payload.get(field) or []
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                payload['unparsed_' + field] = rows
                payload[field] = []
                errors.append(f'invalid account {field}; original file retained')
        if errors:
            payload.update(errors=errors, quality='partial')
        payload["available_cash"] = None if errors else available_cash(payload)
        return payload

    def load_effective(self) -> list[Position]:
        return [
            position for row in self.load_account().get("positions", [])
            if (position := Position.from_dict(row)) is not None
        ]

    def _save(self, positions: list[Position], *, account: dict[str, Any] | None = None) -> None:
        """Write the book atomically (temp file in the same directory + rename)."""
        if account is None:
            if self.path.exists():
                account = json.loads(self.path.read_text(encoding="utf-8"))
            else:
                account = self.load_account()
        payload = {
            **account,
            "version": BOOK_VERSION,
            "updated_at": _utc_now(),
            "positions": [position.to_dict() for position in positions],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=self.path.parent, delete=False, encoding="utf-8") as handle:
            tmp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.path)

    @store_transaction
    def record_unreconciled_event(self, symbol, statement, *, as_of):
        account = self.load_account()
        if account.get('errors'):
            raise ValueError('cannot write an unreadable account')
        calendar_date(as_of)
        event = {'symbol': symbol, 'statement': statement, 'as_of': as_of}
        events = list(account.get('unreconciled_events') or [])
        if event not in events:
            events.append(event)
        account['unreconciled_events'] = events
        self._save(self.load_effective(), account=account)

    @store_transaction
    def set_account(self, *, cash: Any = None, pending_orders: Any = None, **constraints: Any) -> dict[str, Any]:
        """Record explicitly supplied account inputs; never infer risk preferences."""
        account = self.load_account()
        if account.get("errors"):
            raise ValueError("cannot overwrite an unreadable account")
        if cash is not None:
            account["cash"] = validate_cash(cash)
            account['unreconciled_events'] = [event for event in account.get('unreconciled_events', []) if event.get('symbol') != '__CASH__']
        if pending_orders is not None:
            account["pending_orders"] = validate_pending(pending_orders)
        allowed = {"peak_value", "max_loss_amount", "minimum_cash", "investment_horizon", "source_ref", "as_of"}
        if set(constraints) - allowed:
            raise ValueError("unsupported account constraint")
        for key, value in constraints.items():
            if key in {"peak_value", "max_loss_amount", "minimum_cash"}:
                value = _finite_number(value, key)
                if value < 0:
                    raise ValueError(f"{key} must not be negative")
            if key == "as_of":
                value = calendar_date(value)
            account[key] = value
        self._save(self.load_effective(), account=account)
        return self.load_account()

    @store_transaction
    def set_position(
        self,
        symbol: Any,
        *,
        quantity: Any = None,
        cost: Any = None,
        name: Any = "",
        note: Any = "",
        instrument_type: str | None = None,
        lots: Any = None,
        reported_value: Any = None,
        value_as_of: str | None = None,
    ) -> Position:
        """Insert or update one position and persist the book.

        Merge, not replace: a call that omits ``cost``, ``name`` or ``note``
        keeps the value already recorded for that symbol, so a quantity-only
        update never erases what the user said earlier.

        Args:
            symbol: Instrument code in any accepted spelling.
            quantity: Units held; negative for a short position.
            cost: Optional average cost per unit.
            name: Optional display name.
            note: Optional short note.

        Returns:
            The stored row.

        Raises:
            ValueError: The symbol has no canonical form, or a number is not
                finite (including a negative cost).
        """
        canonical = canonical_book_symbol(symbol, instrument_type=instrument_type, name=str(name or ""))
        if canonical is None:
            raise ValueError(f"unsupported symbol: {symbol!r}")
        if self.load_account().get("errors"):
            raise ValueError("cannot overwrite an unreadable position book; restore its saved data first")
        positions = self.load_effective()
        if _BARE_CN_RE.fullmatch(str(symbol or "").strip()) and instrument_type is None and "联接" not in str(name) and "场外" not in str(name):
            existing_matches = [p.symbol for p in positions if p.symbol.split(".")[0] == str(symbol).strip()]
            if len(existing_matches) == 1:
                canonical = existing_matches[0]
            elif str(symbol).startswith(("0", "1", "2", "3")) and not str(symbol).startswith("15"):
                raise ValueError("ambiguous bare code: provide .OF/.SZ/.SH or an explicit instrument_type")
        quantity_value = None if quantity is None else _finite_number(quantity, "quantity")
        reported_value_parsed = None if reported_value is None else _finite_number(reported_value, "reported_value")
        if quantity_value is None and reported_value_parsed is None:
            raise ValueError("quantity or explicitly reported_value is required")
        if value_as_of is not None:
            calendar_date(value_as_of)
        cost_value = None if cost is None else _finite_number(cost, "cost")
        if cost_value is not None and cost_value < 0:
            raise ValueError("cost must not be negative")
        name_text = str(name or "").strip()[:_MAX_NAME_CHARS]
        note_text = str(note or "").strip()[:_MAX_NOTE_CHARS]

        existing = next((p for p in positions if p.symbol == canonical), None)
        parsed_lots = validate_lots(lots, quantity_value) if lots is not None else (existing.lots if existing else ())
        if parsed_lots:
            validate_lots(parsed_lots, quantity_value)
        updated = Position(
            symbol=canonical,
            quantity=quantity_value,
            name=name_text or (existing.name if existing else ""),
            cost=(
                cost_value
                if cost_value is not None
                else (existing.cost if existing else None)
            ),
            note=note_text or (existing.note if existing else ""),
            updated_at=_utc_now(),
            reported_value=reported_value_parsed if reported_value is not None else (existing.reported_value if existing else None),
            value_as_of=value_as_of if value_as_of is not None else (existing.value_as_of if existing else None),
            reported_at=existing.reported_at if existing else None,
            source_ref=existing.source_ref if existing else None,
            purchase_date=existing.purchase_date if existing else None,
            reported_return_pct=existing.reported_return_pct if existing else None,
            lots=parsed_lots, extra=dict(existing.extra) if existing else {},
        )
        if existing is None:
            positions.append(updated)
        else:
            positions[positions.index(existing)] = updated
        account = self.load_account()
        account['unreconciled_events'] = [event for event in account.get('unreconciled_events', []) if event.get('symbol') != canonical]
        self._save(positions, account=account)
        return updated

    @store_transaction
    def remove_position(self, symbol: Any) -> bool:
        """Remove the row for *symbol*.

        Returns:
            True when a row was removed, False when the book held no such row.

        Raises:
            ValueError: The symbol has no canonical form.
        """
        if self.load_account().get("errors"):
            raise ValueError("cannot remove from an unreadable position book; original rows retained")
        canonical = canonical_book_symbol(symbol)
        if canonical is None:
            raise ValueError(f"unsupported symbol: {symbol!r}")
        positions = self.load_effective()
        if _BARE_CN_RE.fullmatch(str(symbol or "").strip()):
            matches = [p.symbol for p in positions if p.symbol.split(".")[0] == str(symbol).strip()]
            if len(matches) == 1:
                canonical = matches[0]
            elif len(matches) > 1:
                raise ValueError("ambiguous bare code: provide its explicit suffix")
        remaining = [p for p in positions if p.symbol != canonical]
        if len(remaining) == len(positions):
            return False
        account = self.load_account()
        account['unreconciled_events'] = [event for event in account.get('unreconciled_events', []) if event.get('symbol') != canonical]
        self._save(remaining, account=account)
        return True

    @store_transaction
    def clear(self) -> int:
        """Empty the book.

        Returns:
            The number of positions removed.
        """
        account = self.load_account()
        count = len(account.get("positions", [])) + len(account.get("holding_groups", []))
        self._save([], account={"holding_groups": [], "pending_orders": []})
        return count


def _fmt_number(value: float) -> str:
    """Render a stored number without a trailing ``.0`` for whole units."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _position_line(position: Position) -> str:
    """One markdown line for :func:`render_book_block`."""
    head = f"{position.symbol} {position.name}".strip()
    parts = [f"quantity {_fmt_number(position.quantity)}"] if position.quantity is not None else ["quantity not supplied"]
    if position.reported_value is not None:
        parts.append(f"user-reported value {_fmt_number(position.reported_value)} CNY as of {position.value_as_of or 'unspecified historical date'}")
    if position.cost is not None:
        parts.append(f"average cost {_fmt_number(position.cost)} per unit")
    line = f"- {head}: {', '.join(parts)}"
    if position.note:
        line += f"; {position.note}"
    if position.updated_at:
        line += f" (recorded {position.updated_at[:10]})"
    return line


def render_book_block(store: PositionBookStore | None = None) -> str:
    """Render the book as the markdown lines injected into a turn, or "".

    The lines are the user's own statement of what they hold, so callers
    quote them as theirs: they go inside the ``<user-book>`` block of the
    user message and into the grounding ledger's user corpus that a ``held``
    figure is reconciled against.

    Args:
        store: Store to read; a fresh default store when omitted, so the
            runtime root is resolved at call time.

    Returns:
        One ``- SYMBOL ...`` line per position, or "" when the book is empty.
    """
    effective_store = store or PositionBookStore()
    positions = effective_store.load_effective()
    account = effective_store.load_account()
    lines = [_position_line(position) for position in positions]
    for group in account.get("holding_groups", []):
        lines.append(
            f"- Unresolved holding group {' / '.join(group['candidate_symbols'])} {group.get('name', '')}: "
            f"user-reported total {_fmt_number(group['reported_value'])} CNY; split and share quantities unknown"
        )
    cash = account.get("cash")
    if isinstance(cash, dict):
        lines.append(f"- User-reported cash: {_fmt_number(cash['amount'])} {cash['currency']}; as of {cash.get('as_of') or 'unspecified historical date'}")
    if account.get("valuation_date_range"):
        lines.append("- Historical valuation date range: " + " to ".join(account["valuation_date_range"]))
    if not lines:
        return ""
    return "\n".join(lines)
