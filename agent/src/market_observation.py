"""Dated observation metadata shared by market tools and portfolio decisions."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def market_now() -> datetime:
    return datetime.now(SHANGHAI)


def quote_unit(symbol: str, instrument_type: str | None = None) -> str | None:
    """Price units are distinct from an index's underlying currency exposure."""
    symbol = symbol.upper()
    if instrument_type == "index" or symbol.startswith("^") or re.fullmatch(r"(?:000\d{3}\.SH|399\d{3}\.SZ)", symbol):
        return "points"
    if symbol.endswith(".OF"):
        return "currency_per_unit"
    return None


def expected_cn_session(symbol: str, end_date: str, *, now: datetime | None = None) -> str | None:
    """Latest daily session expected for a current China request; unknown stays unknown.

    Historical requests retain their explicit window. Other markets need their
    own authoritative calendar rather than borrowing China's date.
    """
    if not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", symbol, re.I):
        return None
    moment = now or market_now()
    moment = moment.replace(tzinfo=SHANGHAI) if moment.tzinfo is None else moment.astimezone(SHANGHAI)
    if date.fromisoformat(end_date[:10]) < moment.date():
        return None
    from src.market_calendar import is_trading_day, previous_trading_day
    opened = is_trading_day(moment.date())
    if opened is None:
        return None
    if opened and moment.time() >= time(9, 30):
        return moment.date().isoformat()
    previous = previous_trading_day(moment.date())
    return previous.isoformat() if previous else None


def frame_as_of(frame: Any) -> str | None:
    """Newest dated bar, without treating an integer row index as a timestamp."""
    if getattr(frame, "empty", False):
        return None
    index = getattr(frame, "index", None)
    if getattr(index, "dtype", None) is not None and getattr(index.dtype, "kind", None) == "M":
        return str(index.max())[:10]
    columns = getattr(frame, "columns", ())
    for name in ("trade_date", "date", "datetime", "timestamp"):
        if name in columns:
            values = [str(v)[:10] for v in frame[name] if re.match(r"^\d{4}-\d{2}-\d{2}", str(v))]
            return max(values) if values else None
    return None


def through_date(frame: Any, end_date: str) -> Any:
    """Remove provider rows beyond the requested information cutoff."""
    index = getattr(frame, "index", None)
    if getattr(index, "dtype", None) is not None and getattr(index.dtype, "kind", None) == "M":
        return frame.loc[index.strftime("%Y-%m-%d") <= end_date[:10]].copy()
    for name in ("trade_date", "date", "datetime", "timestamp"):
        if name in getattr(frame, "columns", ()):
            return frame.loc[frame[name].astype(str).str.slice(0, 10) <= end_date[:10]].copy()
    return frame


def observation_metadata(symbol: str, as_of: str | None, expected: str | None = None) -> dict[str, Any]:
    now = market_now()
    finality = "unknown"
    if as_of:
        if as_of < now.date().isoformat():
            finality = "closed_session"
        elif as_of == now.date().isoformat() and re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", symbol, re.I):
            finality = "closed_session" if now.time() >= time(15) else "in_progress"
    return {
        "as_of": as_of,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "expected_session": expected,
        "quality": "unknown" if not as_of or not expected else "stale" if as_of < expected else "current",
        "finality": finality,
        "quote_unit": quote_unit(symbol),
    }
