"""Off-exchange fund data: daily NAV series and fund profiles for ``.OF``.

A-share OTC funds — the ETF feeder funds (ETF联接基金) included — do not
trade on an exchange, so their "price" is the daily NAV the manager publishes
once per trading day rather than intraday OHLCV bars. This module serves them
through the same free public endpoints the loader layer already leans on and
hands back a JSON-ready envelope shaped for the grounding evidence chain:

* ``nav`` / ``snapshot`` — the fund's published NAV series, newest first. A
  NAV-quoted fund reports unit NAV (单位净值) and cumulative NAV (累计净值); a
  money-market fund reports income per 10 000 units (每万份收益) and the
  seven-day yield (7日年化) instead. The two calibers are labelled distinctly
  in ``_provenance.caliber`` and never presented under one set of field names.
* ``profile`` — static fund facts (full name, inception, company, manager,
  size, benchmark) through an optional third-party package.

Provider-internal plumbing: this module knows the endpoint's field layout
(``FSRQ``/``DWJZ``/``LJJZ``/``JZZZL``/``SGZT``/``SHZT``) and nothing about how
an answer is validated. Everything user-facing (tool description, parameter
names) stays provider-neutral; source names live in ``_provenance`` only.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

from backtest.loaders.eastmoney_client import get_json

logger = logging.getLogger(__name__)

#: Views this module serves.
VIEWS = ("nav", "snapshot", "profile", "identity")

#: Default per-code row cap — about three months of trading days.
DEFAULT_MAX_ROWS = 60

_NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"
# The NAV history API answers only when the request carries its own web
# front-end as referer; without one the body comes back empty.
_NAV_REFERER = "https://fundf10.eastmoney.com/"
# The endpoint serves 20 rows per page regardless of a larger pageSize (and
# answers TotalCount=0 for pageSize>200), so pagination steps 20 rows at a time.
_NAV_PAGE_SIZE = 20
# Hard bound on pagination: 60 pages = 1200 rows, about five years of trading
# days — enough for any trend question, bounded against a runaway loop.
_NAV_MAX_PAGES = 60

#: money-market funds carry this FundType when SYType is absent.
_MONEY_MARKET_FUND_TYPE = "005"

_NAV_SOURCE = "eastmoney"
_PROFILE_SOURCE = "xueqiu"

_FUND_CODE_RE = re.compile(r"^(?P<code>\d{6})(?:\.OF)?$", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Process-level memo: a NAV series changes at most once per trading day, so a
# short TTL spares the endpoint when the same question is asked twice in one
# session. When a refresh fails, a memoized copy (up to a week old) is served
# with ``_provenance.stale`` set rather than dropping evidence the session
# already read.
_CACHE_TTL_S = 600.0
_STALE_MAX_AGE_S = 7 * 24 * 3600.0
_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


def canonical_fund_symbol(raw: Any) -> str | None:
    """Return the canonical ``<code>.OF`` symbol for a fund-code spelling.

    Args:
        raw: A bare six-digit code (``"000051"``) or the suffixed form
            (``"000051.OF"``, any case).

    Returns:
        The canonical uppercase ``<code>.OF`` symbol, or ``None`` when the
        value is not a fund-code spelling.
    """
    match = _FUND_CODE_RE.match(str(raw or "").strip())
    if match is None:
        return None
    return f"{match.group('code')}.OF"


def _to_float(value: Any) -> float | None:
    """Coerce an endpoint cell to a finite float, or None when it is not one."""
    if value is None or value in ("", "--"):
        return None
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    # Reject NaN/inf without importing math for one comparison.
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def _to_int(value: Any) -> int | None:
    """Coerce an endpoint cell to an int, or None when it is not one."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any) -> str:
    """Return a stripped text cell, mapping pandas-style missing values to ''."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in {"<NA>", "nan", "NaN", "NaT", "None"} else text


def _clean_date(value: Any) -> str | None:
    """Return a ``YYYY-MM-DD`` day from an endpoint cell, or None."""
    text = _clean_text(value)[:10]
    return text if _ISO_DATE_RE.match(text) else None


def _in_window(day: str, start_date: str | None, end_date: str | None) -> bool:
    """Whether a dated row falls inside the requested window."""
    if start_date and day < start_date:
        return False
    if end_date and day > end_date:
        return False
    return True


def _parse_nav_row(
    raw: dict[str, Any],
    *,
    money_market: bool,
    include_status: bool,
) -> dict[str, Any] | None:
    """Convert one raw NAV row into an envelope row, or None when undated.

    A money-market fund quotes income per 10 000 units and a seven-day yield
    under the same columns a NAV fund uses for unit/cumulative NAV, so the
    field names follow the caliber rather than the column.
    """
    day = _clean_date(raw.get("FSRQ"))
    if day is None:
        return None
    if money_market:
        row: dict[str, Any] = {
            "date": day,
            "income_per_10k": _to_float(raw.get("DWJZ")),
            "seven_day_yield": _to_float(raw.get("LJJZ")),
        }
    else:
        row = {
            "date": day,
            "unit_nav": _to_float(raw.get("DWJZ")),
            "cum_nav": _to_float(raw.get("LJJZ")),
            "daily_growth": _to_float(raw.get("JZZZL")),
        }
    if include_status:
        row["purchase_status"] = _clean_text(raw.get("SGZT"))
        row["redemption_status"] = _clean_text(raw.get("SHZT"))
    return row


def _fetch_nav_entry(
    code: str,
    *,
    start_date: str | None,
    end_date: str | None,
    max_rows: int,
    include_status: bool,
) -> dict[str, Any]:
    """Fetch one fund's NAV series (newest first) as an envelope entry.

    Args:
        code: Bare six-digit fund code.
        start_date: Optional inclusive ``YYYY-MM-DD`` window start.
        end_date: Optional inclusive ``YYYY-MM-DD`` window end.
        max_rows: Row cap; 0 serves as many rows as the hard page bound allows.
        include_status: Attach the per-row purchase/redemption status cells.

    Returns:
        ``{"data": rows, "_provenance": {...}}``. An unknown code yields empty
        ``data`` (the caller records it as unresolved).

    Raises:
        requests.RequestException: Network failure, for the caller's
            memoized-stale fallback.
        requests.HTTPError: Non-2xx response status.
        ValueError: Body is not valid JSON.
    """
    rows: list[dict[str, Any]] = []
    money_market = False
    available: int | None = None
    for page_index in range(1, _NAV_MAX_PAGES + 1):
        payload = get_json(
            _NAV_URL,
            params={
                "fundCode": code,
                "pageIndex": page_index,
                "pageSize": _NAV_PAGE_SIZE,
                "startDate": start_date or "",
                "endDate": end_date or "",
            },
            headers={"Referer": _NAV_REFERER},
        )
        data = payload.get("Data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            break
        money_market = bool(_clean_text(data.get("SYType"))) or (
            _clean_text(data.get("FundType")) == _MONEY_MARKET_FUND_TYPE
        )
        listing = data.get("LSJZList")
        if not isinstance(listing, list) or not listing:
            break
        for raw in listing:
            if not isinstance(raw, dict):
                continue
            parsed = _parse_nav_row(
                raw, money_market=money_market, include_status=include_status
            )
            if parsed is not None and _in_window(parsed["date"], start_date, end_date):
                rows.append(parsed)
        total = _to_int(payload.get("TotalCount"))
        if total is not None:
            available = total
        if total and len(rows) >= total:
            break
        if len(listing) < _NAV_PAGE_SIZE:
            break
        if max_rows > 0 and len(rows) >= max_rows:
            break
    if max_rows > 0:
        rows = rows[:max_rows]
    return {
        "data": rows,
        "_provenance": {
            "source": _NAV_SOURCE,
            "as_of": rows[0]["date"] if rows else None,
            "caliber": "money_market" if money_market else "nav",
            "currency": "CNY",
            "rows_available": available,
        },
    }


def _profile_rows(frame: Any) -> list[dict[str, str]]:
    """Convert the profile frame's item/value rows into plain text dicts."""
    if frame is None or bool(getattr(frame, "empty", False)):
        return []
    if not hasattr(frame, "to_dict"):
        return []
    rows: list[dict[str, str]] = []
    for record in frame.to_dict("records"):
        if not isinstance(record, dict):
            continue
        item = _clean_text(record.get("item"))
        if item:
            rows.append({"item": item, "value": _clean_text(record.get("value"))})
    return rows


def _fetch_profile_entry(code: str) -> dict[str, Any]:
    """Fetch one fund's static profile as an envelope entry.

    The profile is text and scalars only, so it contributes no numeric price
    evidence downstream; it exists to answer "what is this fund" (name,
    inception, company, manager, size, benchmark).

    Raises:
        ImportError: The optional profile package is not installed.
        Exception: Provider errors, for the caller's memoized-stale fallback.
    """
    import akshare as ak  # lazy: optional dependency

    frame = ak.fund_individual_basic_info_xq(symbol=code)
    return {
        "data": _profile_rows(frame),
        "_provenance": {
            "source": _PROFILE_SOURCE,
            "as_of": None,
            "caliber": "profile",
            "currency": "CNY",
            "rows_available": None,
        },
    }


def _stale_copy(entry: dict[str, Any]) -> dict[str, Any]:
    """Mark a memoized envelope entry as served after a failed refresh."""
    provenance = dict(entry.get("_provenance") or {})
    provenance["stale"] = True
    return {**entry, "_provenance": provenance}


def _code_entry(
    symbol: str,
    *,
    view: str,
    start_date: str | None,
    end_date: str | None,
    max_rows: int,
    include_status: bool,
    no_cache: bool,
) -> dict[str, Any] | None:
    """Serve one fund's view through the process memo, with stale fallback.

    Returns:
        The envelope entry, or ``None`` when the view could not be served at
        all (no memoized copy to fall back on).
    """
    key = (view, symbol, start_date or "", end_date or "", max_rows, include_status)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None and not no_cache and now - cached[0] <= _CACHE_TTL_S:
        return cached[1]
    try:
        if view == "profile":
            entry = _fetch_profile_entry(symbol.split(".", 1)[0])
        else:
            entry = _fetch_nav_entry(
                symbol.split(".", 1)[0],
                start_date=start_date,
                end_date=end_date,
                max_rows=max_rows,
                include_status=include_status,
            )
    except Exception as exc:  # noqa: BLE001 - one provider hiccup is not fatal
        if cached is not None and now - cached[0] <= _STALE_MAX_AGE_S:
            logger.warning(
                "fund %s %s refresh failed (%s); serving the memoized copy",
                symbol, view, exc,
            )
            return _stale_copy(cached[1])
        logger.warning("fund %s %s fetch failed: %s", symbol, view, exc)
        return None
    entry["_provenance"] = {**entry.get("_provenance", {}), "fetched_at": datetime.now(timezone.utc).isoformat()}
    with _CACHE_LOCK:
        _CACHE[key] = (now, entry)
    return entry


def _session_context() -> dict[str, Any]:
    """Return the market-session facts for the envelope, or an unknown block.

    Kept as a module-level indirection so tests can pin the calendar without
    reaching the calendar provider, and so an unavailable calendar degrades to
    explicit ``None`` fields instead of failing the fund payload that is
    otherwise complete.
    """
    try:
        from src.market_calendar import session_context

        return session_context()
    except Exception as exc:  # noqa: BLE001 - an absent calendar is "unknown"
        logger.warning("market calendar unavailable: %s", exc)
        return {"is_trading_day": None, "latest_nav_date": None, "source": None}


def fetch_fund_data(
    codes: list[str],
    *,
    view: str = "nav",
    start_date: str | None = None,
    end_date: str | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    no_cache: bool = False,
) -> dict[str, Any]:
    """Fetch NAV series, snapshots, or profiles for OTC fund codes.

    Args:
        codes: Fund codes, bare (``"000051"``) or suffixed (``"000051.OF"``).
        view: ``nav`` (series), ``snapshot`` (latest row + statuses), or
            ``profile`` (static fund facts).
        start_date: Optional inclusive ``YYYY-MM-DD`` window start (nav view).
        end_date: Optional inclusive ``YYYY-MM-DD`` window end (nav view).
        max_rows: Per-code row cap; 0 serves every row the page bound allows.
        no_cache: Skip the process memo's fresh window and refetch.

    Returns:
        JSON-ready envelope ``{"<code>.OF": {"data": [...], "_provenance":
        {...}}, "_session": {...}, "_unresolved": [...]}``. An unusable code
        is listed under ``_unresolved`` instead of producing an entry. The
        ``_session`` block carries calendar facts (trading day, latest
        plausibly-disclosed NAV date) or explicit ``None`` fields when no
        calendar is available.

    Raises:
        ValueError: ``view`` is not one of :data:`VIEWS`.
    """
    if view not in VIEWS:
        raise ValueError(f"view must be one of {VIEWS}")
    result: dict[str, Any] = {}
    unresolved: list[str] = []
    for raw in codes:
        symbol = canonical_fund_symbol(raw)
        if symbol is None:
            unresolved.append(str(raw))
            continue
        if view == "identity":
            from src.fund_identity import FundIdentityStore
            identity = FundIdentityStore().lookup(symbol)
            if identity is None:
                unresolved.append(symbol)
            else:
                result[symbol] = {"data": identity, "_provenance": {
                    "source": "fund_identity", "as_of": identity.get("as_of"),
                    "caliber": "identity", "quality": identity.get("source_kind", "unknown"),
                    "verified": False,
                }}
            continue
        entry = _code_entry(
            symbol,
            view=view,
            start_date=start_date,
            end_date=end_date,
            max_rows=max_rows,
            include_status=(view == "snapshot"),
            no_cache=no_cache,
        )
        if entry is None or not entry.get("data"):
            unresolved.append(symbol)
            continue
        result[symbol] = entry
    result["_session"] = _session_context()
    if unresolved:
        result["_unresolved"] = unresolved
    return result


def fetch_fund_data_json(**kwargs: Any) -> str:
    """Fetch fund data and return strict JSON."""
    return json.dumps(
        fetch_fund_data(**kwargs), ensure_ascii=False, indent=2, allow_nan=False
    )
