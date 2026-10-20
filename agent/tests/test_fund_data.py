"""Off-exchange fund data (``src.fund_data``): NAV series, memo, envelope.

All HTTP is mocked at the client function the module imports
(``src.fund_data.get_json``), and the market-session block is pinned via the
module-level ``_session_context`` indirection, so no test reaches a live
endpoint or the calendar provider.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

import pytest

from src import fund_data as fd

pytestmark = pytest.mark.unit

SYMBOL = "000051.OF"
SESSION = {
    "is_trading_day": True,
    "latest_nav_date": "2026-06-24",
    "source": "tushare",
}


@pytest.fixture(autouse=True)
def _clear_fund_cache():
    """The memo is process-level; every test starts and ends with a cold one."""
    fd._CACHE.clear()
    yield
    fd._CACHE.clear()


def _row(
    day: str,
    *,
    unit: str = "1.7340",
    cum: str = "2.3450",
    growth: str = "0.52",
    purchase: str = "开放申购",
    redemption: str = "开放赎回",
) -> dict[str, str]:
    """One raw NAV row in the endpoint's field layout."""
    return {
        "FSRQ": day,
        "DWJZ": unit,
        "LJJZ": cum,
        "JZZZL": growth,
        "SGZT": purchase,
        "SHZT": redemption,
    }


def _page(
    rows: list[Any],
    *,
    total: Any = None,
    sy_type: str | None = None,
    fund_type: str | None = None,
) -> dict[str, Any]:
    """One raw paginated response for the NAV endpoint.

    ``total=None`` (the default) omits the ``TotalCount`` cell entirely.
    """
    data: dict[str, Any] = {"LSJZList": rows}
    if sy_type is not None:
        data["SYType"] = sy_type
    if fund_type is not None:
        data["FundType"] = fund_type
    payload: dict[str, Any] = {"Data": data}
    if total is not None:
        payload["TotalCount"] = total
    return payload


def _serve(pages: list[dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """A ``get_json`` stand-in answering one canned page per ``pageIndex``."""

    def _get_json(_url, params=None, headers=None, **_kwargs):
        index = int((params or {}).get("pageIndex") or 1)
        if index > len(pages):
            return {"Data": None}
        return pages[index - 1]

    return _get_json


def _entry(**kwargs: Any) -> dict[str, Any]:
    """Call ``_code_entry`` with the view defaults the tool passes."""
    defaults = dict(
        view="nav",
        start_date=None,
        end_date=None,
        max_rows=fd.DEFAULT_MAX_ROWS,
        include_status=False,
        no_cache=False,
    )
    defaults.update(kwargs)
    return fd._code_entry(SYMBOL, **defaults)


class TestCanonicalFundSymbol:
    def test_bare_and_suffixed_spellings_fold_to_one_symbol(self):
        assert fd.canonical_fund_symbol("000051") == SYMBOL
        assert fd.canonical_fund_symbol("000051.OF") == SYMBOL
        assert fd.canonical_fund_symbol("000051.of") == SYMBOL
        assert fd.canonical_fund_symbol(" 110022.OF ") == "110022.OF"

    def test_non_fund_spellings_are_rejected(self):
        for raw in (None, "", "00051", "0000511", "600519.SH", "abcdef", "000051.HK"):
            assert fd.canonical_fund_symbol(raw) is None


class TestScalarHelpers:
    def test_to_float_rejects_placeholders_and_non_finite(self):
        assert fd._to_float(None) is None
        assert fd._to_float("") is None
        assert fd._to_float("--") is None
        assert fd._to_float("nan") is None
        assert fd._to_float("abc") is None
        assert fd._to_float(float("inf")) is None
        assert fd._to_float(float("nan")) is None
        assert fd._to_float("1,234.5") == 1234.5

    def test_clean_date_requires_an_iso_day(self):
        assert fd._clean_date("2026-06-24") == "2026-06-24"
        # Longer cells are truncated to the day; non-ISO shapes are rejected.
        assert fd._clean_date("2026-06-24 00:00:00") == "2026-06-24"
        assert fd._clean_date("24/06/2026") is None
        assert fd._clean_date("<NA>") is None
        assert fd._clean_date(None) is None


class TestParseNavRow:
    def test_nav_row_labels_unit_and_cumulative(self):
        parsed = fd._parse_nav_row(_row("2026-06-24"), money_market=False, include_status=False)
        assert parsed == {
            "date": "2026-06-24",
            "unit_nav": 1.734,
            "cum_nav": 2.345,
            "daily_growth": 0.52,
        }

    def test_money_market_row_labels_income_and_yield(self):
        """The same columns carry different calibers for a money-market fund."""
        parsed = fd._parse_nav_row(
            _row("2026-06-24", unit="0.6543", cum="1.8120"),
            money_market=True,
            include_status=False,
        )
        assert parsed == {
            "date": "2026-06-24",
            "income_per_10k": 0.6543,
            "seven_day_yield": 1.812,
        }
        assert "unit_nav" not in parsed

    def test_status_cells_attach_only_when_requested(self):
        without = fd._parse_nav_row(_row("2026-06-24"), money_market=False, include_status=False)
        assert "purchase_status" not in without
        with_status = fd._parse_nav_row(_row("2026-06-24"), money_market=False, include_status=True)
        assert with_status["purchase_status"] == "开放申购"
        assert with_status["redemption_status"] == "开放赎回"

    def test_undated_row_is_dropped(self):
        assert fd._parse_nav_row({"DWJZ": "1.0"}, money_market=False, include_status=False) is None
        assert (
            fd._parse_nav_row(_row("not-a-date"), money_market=False, include_status=False)
            is None
        )

    def test_placeholder_cells_become_none(self):
        parsed = fd._parse_nav_row(
            _row("2026-06-24", unit="--", cum="", growth="--"),
            money_market=False,
            include_status=False,
        )
        assert parsed is not None
        assert parsed["unit_nav"] is None
        assert parsed["cum_nav"] is None
        assert parsed["daily_growth"] is None


class TestFetchNavEntry:
    def test_pagination_stops_on_a_short_page(self):
        page_one = [_row(f"2026-06-{day:02d}") for day in range(1, 21)]
        page_two = [_row(f"2026-05-{day:02d}") for day in range(1, 6)]
        with patch.object(
            fd,
            "get_json",
            side_effect=_serve([_page(page_one, total=25), _page(page_two, total=25)]),
        ) as calls:
            entry = fd._fetch_nav_entry(
                "000051",
                start_date=None,
                end_date=None,
                max_rows=0,
                include_status=False,
            )

        assert len(entry["data"]) == 25
        assert [call.kwargs["params"]["pageIndex"] for call in calls.call_args_list] == [1, 2]
        provenance = entry["_provenance"]
        assert provenance["source"] == "eastmoney"
        assert provenance["caliber"] == "nav"
        assert provenance["currency"] == "CNY"
        assert provenance["rows_available"] == 25
        # Newest first: the first row is also the series' as-of day.
        assert provenance["as_of"] == "2026-06-01"

    def test_max_rows_caps_the_series_without_a_total_count(self):
        page_one = [_row(f"2026-06-{day:02d}") for day in range(1, 21)]
        with patch.object(fd, "get_json", side_effect=_serve([_page(page_one)])) as calls:
            entry = fd._fetch_nav_entry(
                "000051",
                start_date=None,
                end_date=None,
                max_rows=3,
                include_status=False,
            )

        assert [row["date"] for row in entry["data"]] == [
            "2026-06-01",
            "2026-06-02",
            "2026-06-03",
        ]
        assert calls.call_count == 1
        assert entry["_provenance"]["rows_available"] is None

    def test_window_filter_keeps_only_requested_days(self):
        rows = [_row("2026-06-20"), _row("2026-06-24"), _row("2026-06-25")]
        with patch.object(fd, "get_json", side_effect=_serve([_page(rows, total=3)])):
            entry = fd._fetch_nav_entry(
                "000051",
                start_date="2026-06-23",
                end_date="2026-06-24",
                max_rows=0,
                include_status=False,
            )

        assert [row["date"] for row in entry["data"]] == ["2026-06-24"]

    def test_sy_type_marks_the_money_market_caliber(self):
        rows = [{"FSRQ": "2026-06-24", "DWJZ": "0.6543", "LJJZ": "1.8120", "JZZZL": ""}]
        with patch.object(fd, "get_json", side_effect=_serve([_page(rows, sy_type="货币型")])):
            entry = fd._fetch_nav_entry(
                "000051",
                start_date=None,
                end_date=None,
                max_rows=0,
                include_status=False,
            )

        assert entry["_provenance"]["caliber"] == "money_market"
        assert entry["data"][0] == {
            "date": "2026-06-24",
            "income_per_10k": 0.6543,
            "seven_day_yield": 1.812,
        }

    def test_fund_type_005_marks_the_money_market_caliber_without_sy_type(self):
        rows = [{"FSRQ": "2026-06-24", "DWJZ": "0.6543", "LJJZ": "1.8120"}]
        with patch.object(
            fd, "get_json", side_effect=_serve([_page(rows, fund_type="005")])
        ):
            entry = fd._fetch_nav_entry(
                "000051",
                start_date=None,
                end_date=None,
                max_rows=0,
                include_status=False,
            )

        assert entry["_provenance"]["caliber"] == "money_market"
        assert "income_per_10k" in entry["data"][0]

    def test_unknown_code_answers_empty_data(self):
        # An empty listing ends pagination before any count cell is read, so
        # the entry reports an unknown count next to empty data. The caller
        # turns that empty entry into an ``_unresolved`` code.
        with patch.object(fd, "get_json", side_effect=_serve([_page([])])):
            entry = fd._fetch_nav_entry(
                "000051",
                start_date=None,
                end_date=None,
                max_rows=0,
                include_status=False,
            )

        assert entry["data"] == []
        assert entry["_provenance"]["as_of"] is None
        assert entry["_provenance"]["rows_available"] is None

    def test_undated_rows_are_dropped_and_as_of_follows_the_newest_kept_row(self):
        rows = [{"FSRQ": "--", "DWJZ": "1.0"}, _row("2026-06-24"), _row("2026-06-23")]
        with patch.object(fd, "get_json", side_effect=_serve([_page(rows, total=2)])):
            entry = fd._fetch_nav_entry(
                "000051",
                start_date=None,
                end_date=None,
                max_rows=0,
                include_status=False,
            )

        assert [row["date"] for row in entry["data"]] == ["2026-06-24", "2026-06-23"]
        assert entry["_provenance"]["as_of"] == "2026-06-24"


class TestCodeEntryCache:
    def test_second_call_is_served_from_the_memo(self):
        with patch.object(
            fd, "get_json", side_effect=_serve([_page([_row("2026-06-24")], total=1)])
        ) as calls:
            first = _entry()
            second = _entry()

        assert calls.call_count == 1
        assert first == second

    def test_a_different_view_key_refetches(self):
        with patch.object(
            fd, "get_json", side_effect=_serve([_page([_row("2026-06-24")], total=1)])
        ) as calls:
            _entry(include_status=False)
            _entry(include_status=True)

        assert calls.call_count == 2

    def test_no_cache_bypasses_a_fresh_memo(self):
        with patch.object(
            fd, "get_json", side_effect=_serve([_page([_row("2026-06-24")], total=1)])
        ) as calls:
            _entry()
            _entry(no_cache=True)

        assert calls.call_count == 2

    def test_failed_refresh_serves_the_memoized_copy_as_stale(self):
        with patch.object(
            fd, "get_json", side_effect=_serve([_page([_row("2026-06-24")], total=1)])
        ):
            fresh = _entry()

        with patch.object(fd, "get_json", side_effect=RuntimeError("provider down")):
            stale = _entry(no_cache=True)

        assert stale is not None
        assert stale["_provenance"]["stale"] is True
        assert stale["data"] == fresh["data"]
        assert "stale" not in fresh["_provenance"]

    def test_failed_refresh_without_a_memo_answers_none(self):
        with patch.object(fd, "get_json", side_effect=RuntimeError("provider down")):
            assert _entry() is None

    def test_a_memo_beyond_the_stale_window_is_dropped(self):
        with patch.object(
            fd, "get_json", side_effect=_serve([_page([_row("2026-06-24")], total=1)])
        ):
            assert _entry() is not None

        key = next(iter(fd._CACHE))
        timestamp, memo = fd._CACHE[key]
        fd._CACHE[key] = (timestamp - fd._STALE_MAX_AGE_S - 1.0, memo)

        with patch.object(fd, "get_json", side_effect=RuntimeError("provider down")):
            assert _entry() is None


class TestFetchFundDataEnvelope:
    def test_envelope_keys_canonical_symbol_and_session_block(self):
        with patch.object(
            fd, "get_json", side_effect=_serve([_page([_row("2026-06-24")], total=1)])
        ), patch.object(fd, "_session_context", return_value=SESSION):
            result = fd.fetch_fund_data(["000051"])

        assert set(result) == {SYMBOL, "_session"}
        assert result["_session"] == SESSION
        assert result[SYMBOL]["_provenance"]["source"] == "eastmoney"
        assert "_unresolved" not in result

    def test_unusable_codes_are_reported_without_a_fetch(self):
        with patch.object(
            fd, "get_json", side_effect=AssertionError("must not fetch")
        ) as calls, patch.object(fd, "_session_context", return_value=SESSION):
            result = fd.fetch_fund_data(["600519.SH", "abc", "12345"])

        assert calls.call_count == 0
        assert result["_unresolved"] == ["600519.SH", "abc", "12345"]
        assert result["_session"] == SESSION

    def test_an_empty_series_is_reported_unresolved(self):
        with patch.object(fd, "get_json", side_effect=_serve([{"Data": None}])), patch.object(
            fd, "_session_context", return_value=SESSION
        ):
            result = fd.fetch_fund_data(["000051"])

        assert result["_unresolved"] == [SYMBOL]
        assert SYMBOL not in result

    def test_snapshot_view_attaches_the_trading_status_cells(self):
        with patch.object(
            fd, "get_json", side_effect=_serve([_page([_row("2026-06-24")], total=1)])
        ), patch.object(fd, "_session_context", return_value=SESSION):
            result = fd.fetch_fund_data(["000051"], view="snapshot")

        row = result[SYMBOL]["data"][0]
        assert row["purchase_status"] == "开放申购"
        assert row["redemption_status"] == "开放赎回"

    def test_unknown_view_raises_value_error(self):
        with pytest.raises(ValueError):
            fd.fetch_fund_data(["000051"], view="bars")


class TestSessionContextBlock:
    def test_calendar_facts_pass_through(self):
        with patch("src.market_calendar.session_context", return_value=SESSION):
            assert fd._session_context() == SESSION

    def test_an_unavailable_calendar_degrades_to_explicit_unknowns(self):
        with patch(
            "src.market_calendar.session_context", side_effect=RuntimeError("no token")
        ):
            assert fd._session_context() == {
                "is_trading_day": None,
                "latest_nav_date": None,
                "source": None,
            }


class TestProfileView:
    def test_profile_rows_are_text_with_their_own_provenance(self, monkeypatch):
        class _Frame:
            empty = False

            @staticmethod
            def to_dict(orient):
                assert orient == "records"
                return [
                    {"item": "基金全称", "value": "华夏沪深300ETF联接A"},
                    {"item": "成立日期", "value": "2009-07-10"},
                ]

        fake_akshare = SimpleNamespace(
            fund_individual_basic_info_xq=lambda symbol: _Frame()
        )
        monkeypatch.setitem(sys.modules, "akshare", fake_akshare)

        with patch.object(fd, "_session_context", return_value=SESSION):
            result = fd.fetch_fund_data(["000051"], view="profile")

        entry = result[SYMBOL]
        assert entry["data"][0] == {"item": "基金全称", "value": "华夏沪深300ETF联接A"}
        assert entry["_provenance"]["caliber"] == "profile"
        assert entry["_provenance"]["source"] == "xueqiu"

    def test_a_missing_profile_package_answers_unresolved(self, monkeypatch):
        # ``sys.modules[name] = None`` makes ``import akshare`` raise
        # ImportError, the optional-dependency state this path must survive.
        monkeypatch.setitem(sys.modules, "akshare", None)

        with patch.object(fd, "_session_context", return_value=SESSION):
            result = fd.fetch_fund_data(["000051"], view="profile")

        assert result["_unresolved"] == [SYMBOL]

    def test_profile_rows_tolerate_empty_or_foreign_frames(self):
        assert fd._profile_rows(None) == []
        assert fd._profile_rows(SimpleNamespace(empty=True)) == []
        assert fd._profile_rows(object()) == []


class TestStrictJson:
    def test_json_output_round_trips(self):
        with patch.object(
            fd, "get_json", side_effect=_serve([_page([_row("2026-06-24")], total=1)])
        ), patch.object(fd, "_session_context", return_value=SESSION):
            out = fd.fetch_fund_data_json(codes=["000051"])

        assert json.loads(out)[SYMBOL]["data"][0]["unit_nav"] == 1.734

    def test_non_finite_values_are_rejected_not_serialized(self):
        def _nan_envelope(**_kwargs: Any) -> dict[str, Any]:
            return {
                SYMBOL: {
                    "data": [{"date": "2026-06-24", "unit_nav": float("nan")}],
                    "_provenance": {},
                }
            }

        with patch.object(fd, "fetch_fund_data", side_effect=_nan_envelope):
            with pytest.raises(ValueError):
                fd.fetch_fund_data_json(codes=["000051"])