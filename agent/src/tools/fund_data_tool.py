"""Off-exchange fund data tool backed by :mod:`src.fund_data`."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from src.agent.tools import BaseTool
from src.fund_data import (
    DEFAULT_MAX_ROWS,
    VIEWS,
    canonical_fund_symbol,
    fetch_fund_data_json,
)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: A six-digit code carrying any exchange-style suffix that is *not* the
#: off-exchange ``.OF`` marker — the caller reached for this tool with a
#: listed instrument, which belongs to the market-data tool.
_EXCHANGE_SUFFIX_RE = re.compile(r"^\d{6}\.[A-Za-z]{2,4}$")


def _error(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _valid_iso_date(value: str) -> bool:
    """True only for strict ``YYYY-MM-DD`` calendar dates.

    ``date.fromisoformat`` alone is not enough: on Python 3.11+ it also
    accepts the compact ``YYYYMMDD`` form, which the NAV endpoint misreads.
    """
    if not _ISO_DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _optional_date(value: Any, name: str) -> tuple[str | None, str | None]:
    """Validate an optional ``YYYY-MM-DD`` argument.

    Returns:
        ``(normalized, None)`` when the value is absent or acceptable, else
        ``(None, message)`` for the error envelope.
    """
    if value is None or value == "":
        return None, None
    if not isinstance(value, str) or not _valid_iso_date(value.strip()):
        return None, f"{name} must be a valid YYYY-MM-DD date"
    return value.strip(), None


class GetFundDataTool(BaseTool):
    """Fetch NAV series, snapshots, or profiles for off-exchange funds."""

    name = "get_fund_data"
    description = (
        "Fetch off-exchange mutual fund data by fund code — daily NAV series, "
        "latest NAV snapshot, static profile, or dated feeder identity/terms "
        "(view=identity, declared relationships are not verified price facts). This covers OTC "
        "funds including ETF feeder funds: a feeder fund does not trade on an "
        "exchange, so its 'price' is the NAV its manager publishes once per "
        "trading day. NAV-quoted funds report unit_nav, cum_nav and "
        "daily_growth (percent); money-market funds report income_per_10k and "
        "seven_day_yield instead — read _provenance.caliber ('nav' / "
        "'money_market' / 'profile') before interpreting any number. Symbols "
        "are canonicalized to <code>.OF (e.g. 000051.OF); cite that exact "
        "form when using a value in an answer. Exchange-traded instruments "
        "(stocks/ETFs/indexes such as 600519.SH or 510300.SH) are rejected "
        "here — fetch their bars with get_market_data. The _session block "
        "reports the trading-day calendar and the latest plausibly-disclosed "
        "NAV date (null fields mean the calendar is unavailable, not that the "
        "day is closed)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    'Fund codes, bare or suffixed: ["000051", "110022.OF"]. '
                    "Six-digit codes only; exchange suffixes such as .SH/.SZ "
                    "belong to get_market_data."
                ),
            },
            "view": {
                "type": "string",
                "enum": list(VIEWS),
                "description": (
                    "'nav': dated NAV series (newest first). 'snapshot': the "
                    "same series plus each row's purchase/redemption status. "
                    "'profile': static fund facts (full name, inception, "
                    "company, manager, size, benchmark) — no dated rows."
                ),
                "default": "nav",
            },
            "start_date": {
                "type": "string",
                "description": (
                    "Optional inclusive window start in YYYY-MM-DD format "
                    "(nav/snapshot). Omit to let the tool serve the most "
                    "recent rows."
                ),
            },
            "end_date": {
                "type": "string",
                "description": (
                    "Optional inclusive window end in YYYY-MM-DD format "
                    "(nav/snapshot)."
                ),
            },
            "max_rows": {
                "type": "integer",
                "description": (
                    "Per-code row cap. Use 0 only when the full series is "
                    "required."
                ),
                "default": DEFAULT_MAX_ROWS,
            },
            "no_cache": {
                "type": "boolean",
                "description": (
                    "Set true to force a fresh fetch. Bypasses the process "
                    "memo and the loop-level restore of an earlier identical "
                    "result after context compaction; use when a newly "
                    "disclosed NAV is required instead of the payload fetched "
                    "earlier in this run."
                ),
                "default": False,
            },
        },
        "required": ["codes"],
    }
    repeatable = True
    # A lost exact result can be restored from this run's memory after
    # compaction removed the visible payload: NAV rows for a fixed
    # (codes, view, window) are the same evidence. A caller needing the day's
    # newly disclosed NAV passes no_cache=True.
    replay_after_compaction = True

    def execute(self, **kwargs: Any) -> str:
        """Validate inputs, then fetch and return strict JSON.

        Args:
            **kwargs: ``codes``, optional ``view``, ``start_date``,
                ``end_date``, ``max_rows``, and ``no_cache``.

        Returns:
            Strict JSON envelope keyed by canonical ``<code>.OF`` symbol with
            per-fund ``_provenance``, a ``_session`` calendar block, and
            ``_unresolved`` for codes that returned nothing — or an error
            envelope on invalid inputs.
        """
        codes = kwargs.get("codes")
        if not isinstance(codes, list) or not codes:
            return _error("codes must be a non-empty list of strings")
        if any(not isinstance(code, str) or not code.strip() for code in codes):
            return _error("every code must be a non-empty string")
        codes = [code.strip() for code in codes]
        for code in codes:
            if canonical_fund_symbol(code) is not None:
                continue
            if _EXCHANGE_SUFFIX_RE.match(code):
                return _error(
                    f"{code} is an exchange-listed instrument; call "
                    f"get_market_data for its bars, or pass the six-digit OTC "
                    f"fund code (.OF, e.g. 000051.OF) for NAV data"
                )
            return _error(
                f"{code} is not a fund code; expected a six-digit code such "
                f"as 000051 or 000051.OF"
            )

        view = kwargs.get("view", "nav")
        if view not in VIEWS:
            return _error(f"view must be one of {list(VIEWS)}")

        start_date, error = _optional_date(kwargs.get("start_date"), "start_date")
        if error:
            return _error(error)
        end_date, error = _optional_date(kwargs.get("end_date"), "end_date")
        if error:
            return _error(error)
        if start_date and end_date and start_date > end_date:
            return _error(
                f"start_date ({start_date}) must not be after end_date ({end_date})"
            )

        max_rows = kwargs.get("max_rows", DEFAULT_MAX_ROWS)
        if not isinstance(max_rows, int) or isinstance(max_rows, bool):
            return _error("max_rows must be a non-negative integer (0 = all rows)")
        if max_rows < 0:
            max_rows = DEFAULT_MAX_ROWS

        return fetch_fund_data_json(
            codes=codes,
            view=view,
            start_date=start_date,
            end_date=end_date,
            max_rows=max_rows,
            no_cache=bool(kwargs.get("no_cache", False)),
        )
