"""A-share trading calendar and fund NAV disclosure timing (minimal).

Two operational questions the fund workflows keep asking, answered from one
source of truth instead of from model guesswork:

* Is a given day a trading day (and which day was the previous session)?
* Which NAV date can a fund plausibly have disclosed by now?

Deliberately minimal: the calendar comes from the public A-share calendar
endpoint, is cached process-wide and on disk (that endpoint is rate-limited to
roughly one call per hour, so a fetched year-wide window must survive restarts
and sibling processes), and every answer degrades to ``None`` — explicit
unknown — when no calendar is available. Nothing here guesses.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.market_observation import SHANGHAI, market_now

logger = logging.getLogger(__name__)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Hour of the local day after which a fund's NAV for that trading day is
#: commonly disclosed. It is a floor, not a guarantee: some funds (QDII and
#: Hong Kong feeders) publish later in the night or on the next business day.
_NAV_DISCLOSURE_HOUR = 20

# Days fetched on each side of the query day. One successful call covers more
# than two years of questions, which matters because the public calendar
# endpoint allows only about one call per hour.
_WINDOW_BACK_DAYS = 400
_WINDOW_FORWARD_DAYS = 400

_CALENDAR_SOURCE = "tushare"
_TOKEN_PLACEHOLDERS = {"", "your-tushare-token"}
_CACHE_DIR_ENV = "VIBE_TRADING_CALENDAR_CACHE_DIR"
_CACHE_FILE_NAME = "market_calendar.json"

#: "YYYY-MM-DD" -> {"is_open": int, "pretrade_date": "YYYY-MM-DD" | None}
_ROWS: dict[str, dict[str, Any]] = {}
_WINDOW: tuple[date, date] | None = None
_DISK_LOADED = False
_LOCK = threading.Lock()


def _cache_file() -> Path:
    """Return the calendar cache file path (env-overridable for tests)."""
    override = os.environ.get(_CACHE_DIR_ENV, "").strip()
    base = (
        Path(override).expanduser()
        if override
        else Path.home() / ".vibe-trading" / "cache"
    )
    return base / _CACHE_FILE_NAME


def _compact(value: date) -> str:
    """Return a tushare-style ``YYYYMMDD`` date string."""
    return value.strftime("%Y%m%d")


def _iso_from_compact(value: Any) -> str | None:
    """Return ``YYYY-MM-DD`` from a tushare ``YYYYMMDD`` cell, or None."""
    text = str(value or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"


def _coerce_day(value: date | datetime | str) -> date:
    """Coerce a date-like value to a :class:`date`, or raise ValueError."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    if _ISO_DATE_RE.match(text):
        return date.fromisoformat(text)
    raise ValueError(f"not a calendar date: {value!r}")


def _load_disk_locked() -> None:
    """Load the disk cache once. Caller holds ``_LOCK``."""
    global _DISK_LOADED, _WINDOW
    if _DISK_LOADED:
        return
    _DISK_LOADED = True
    try:
        payload = json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    window = payload.get("window")
    if isinstance(window, list) and len(window) == 2:
        try:
            _WINDOW = (date.fromisoformat(str(window[0])), date.fromisoformat(str(window[1])))
        except ValueError:
            _WINDOW = None
    rows = payload.get("rows")
    if not isinstance(rows, dict):
        return
    for key, row in rows.items():
        if isinstance(row, dict) and _ISO_DATE_RE.match(str(key)):
            _ROWS[str(key)] = {
                "is_open": 1 if row.get("is_open") else 0,
                "pretrade_date": row.get("pretrade_date"),
            }


def _write_disk(rows: dict[str, dict[str, Any]], window: tuple[date, date]) -> None:
    """Persist a fetched window atomically; a failed write is not fatal."""
    path = _cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "window": [window[0].isoformat(), window[1].isoformat()],
                    "rows": rows,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("trading calendar cache write failed: %s", exc)


def _tushare_token() -> str:
    """Return the configured calendar-API token, or '' when unconfigured."""
    try:
        from src.config.accessor import get_env_config

        token = str(get_env_config().data.tushare_token or "").strip()
    except Exception:  # noqa: BLE001 - an unreadable config is an absent token
        return ""
    return "" if token in _TOKEN_PLACEHOLDERS else token


def _fetch_calendar_rows(
    day: date,
) -> tuple[dict[str, dict[str, Any]], tuple[date, date]] | None:
    """Fetch one calendar window around ``day``.

    Returns:
        ``(rows, window)`` on success, or ``None`` when no token is
        configured, the provider refuses (the endpoint is rate-limited to
        about one call per hour), or the answer carries no rows.
    """
    token = _tushare_token()
    if not token:
        return None
    window = (
        day - timedelta(days=_WINDOW_BACK_DAYS),
        day + timedelta(days=_WINDOW_FORWARD_DAYS),
    )
    try:
        import tushare as ts

        pro = ts.pro_api(token)
        frame = pro.trade_cal(
            exchange="SSE",
            start_date=_compact(window[0]),
            end_date=_compact(window[1]),
            fields="cal_date,is_open,pretrade_date",
        )
    except Exception as exc:  # noqa: BLE001 - an absent calendar is not fatal
        logger.warning("trading calendar fetch failed: %s", exc)
        return None
    records = frame.to_dict("records") if hasattr(frame, "to_dict") else []
    rows: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        day_iso = _iso_from_compact(record.get("cal_date"))
        if day_iso is None:
            continue
        rows[day_iso] = {
            "is_open": 0 if str(record.get("is_open")).strip() in ("0", "", "None") else 1,
            "pretrade_date": _iso_from_compact(record.get("pretrade_date")),
        }
    if not rows:
        return None
    return rows, window


def _row_for(day: date) -> dict[str, Any] | None:
    """Return the cached calendar row for ``day``, fetching a window if needed."""
    global _WINDOW
    with _LOCK:
        _load_disk_locked()
        if _WINDOW is not None and _WINDOW[0] <= day <= _WINDOW[1]:
            return _ROWS.get(day.isoformat())
    fetched = _fetch_calendar_rows(day)
    if fetched is None:
        return None
    rows, window = fetched
    with _LOCK:
        _ROWS.clear()
        _ROWS.update(rows)
        _WINDOW = window
        row = _ROWS.get(day.isoformat())
    # Persist outside the lock: the write is file IO, and one fetched window
    # must outlive the process — the endpoint allows about one call per hour.
    _write_disk(rows, window)
    return row


def is_trading_day(day: date | datetime | str) -> bool | None:
    """Whether ``day`` is an exchange trading day.

    Args:
        day: A :class:`date`, :class:`datetime`, or ``YYYY-MM-DD`` string.

    Returns:
        ``True``/``False`` from the calendar, or ``None`` when no calendar is
        available — unknown is reported, never guessed.

    Raises:
        ValueError: ``day`` is not a recognizable date.
    """
    row = _row_for(_coerce_day(day))
    if row is None:
        return None
    return bool(row.get("is_open"))


def previous_trading_day(day: date | datetime | str) -> date | None:
    """Return the most recent trading day strictly before ``day``.

    Returns:
        The previous session's date, or ``None`` when the calendar cannot
        answer (strictly-before is enforced even if the source's own
        previous-session cell disagrees).

    Raises:
        ValueError: ``day`` is not a recognizable date.
    """
    target = _coerce_day(day)
    row = _row_for(target)
    if row is None:
        return None
    previous = row.get("pretrade_date")
    if (
        isinstance(previous, str)
        and _ISO_DATE_RE.match(previous)
        and previous < target.isoformat()
    ):
        return date.fromisoformat(previous)
    with _LOCK:
        earlier = sorted(
            (
                key
                for key, value in _ROWS.items()
                if key < target.isoformat() and value.get("is_open")
            ),
            reverse=True,
        )
    return date.fromisoformat(earlier[0]) if earlier else None


def latest_nav_date(now: datetime | None = None) -> date | None:
    """Return the newest NAV date plausibly disclosed at ``now``.

    The minimal rule: after :data:`_NAV_DISCLOSURE_HOUR` on a trading day the
    day's own NAV counts as disclosed, otherwise the previous trading day's
    does. ``None`` when the calendar cannot answer.
    """
    moment = now or market_now()
    if moment.tzinfo is not None:
        moment = moment.astimezone(SHANGHAI)
    today = moment.date()
    open_today = is_trading_day(today)
    if open_today is None:
        return None
    if open_today and moment.hour >= _NAV_DISCLOSURE_HOUR:
        return today
    return previous_trading_day(today)


def session_context(now: datetime | None = None) -> dict[str, Any]:
    """Return compact session facts for tool payloads.

    The shape is stable and explicit: an answerable calendar reports booleans
    and dates, an unavailable one reports ``None`` fields rather than
    omitting them, so a reader can tell "closed today" from "unknown".
    """
    moment = now or market_now()
    if moment.tzinfo is not None:
        moment = moment.astimezone(SHANGHAI)
    open_today = is_trading_day(moment.date())
    latest = latest_nav_date(moment)
    return {
        "is_trading_day": open_today,
        "latest_nav_date": latest.isoformat() if latest else None,
        "source": _CALENDAR_SOURCE if open_today is not None else None,
    }


def _reset_cache() -> None:
    """Drop the process memo and disk-load flag (tests only)."""
    global _DISK_LOADED, _WINDOW
    with _LOCK:
        _ROWS.clear()
        _WINDOW = None
        _DISK_LOADED = False


def frozen_calendar(start_date: str, end_date: str) -> dict[str, Any]:
    """Freeze a complete known exchange calendar for a single assessment.

    The fetch, if necessary, belongs to collection; calculation consumes only
    these rows. A sparse/unavailable calendar never certifies continuity.
    """
    import hashlib
    first, last = _coerce_day(start_date), _coerce_day(end_date)
    _row_for(last)
    with _LOCK:
        rows = {day: dict(row) for day, row in _ROWS.items() if start_date <= day <= end_date}
    needed = (last - first).days + 1
    if len(rows) != needed:
        return {"trading_sessions": [], "calendar_version": "unavailable"}
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    return {"trading_sessions": sorted(day for day, row in rows.items() if row["is_open"]),
            "calendar_version": "tushare:" + digest}
