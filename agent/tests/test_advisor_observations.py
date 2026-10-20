"""Snapshot time, source and unit integrity across the public data tools."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.market_data import fetch_market_data
from src.agent.grounding import GroundingLedger

pytestmark = pytest.mark.unit


def _frame(day, close=4.427):
    return pd.DataFrame(
        {"open": [close], "high": [close], "low": [close], "close": [close], "volume": [100]},
        index=pd.DatetimeIndex([day], name="trade_date"),
    )


def test_nonempty_stale_source_falls_through_to_current_source():
    class Old:
        name = "tencent"
        def fetch(self, codes, start, end, interval):
            return {code: _frame("2026-10-14") for code in codes}
    class Current:
        name = "akshare"
        def fetch(self, codes, start, end, interval):
            return {code: _frame("2026-10-15") for code in codes}
    data = fetch_market_data(
        codes=["510330.SH"], start_date="2026-10-01", end_date="2026-10-15",
        expected_session="2026-10-15", include_provenance=True,
        loader_resolver=lambda name: Old if name == "tencent" else Current,
        fallback_chain_provider=lambda _: ["tencent", "akshare"],
    )
    assert data["510330.SH"][-1]["trade_date"].startswith("2026-10-15")
    provenance = data["_provenance"]["510330.SH"]
    assert provenance["source"] == "akshare"
    assert provenance["quality"] == "current"
    assert provenance["expected_session"] == "2026-10-15"
    assert provenance["as_of"].startswith("2026-10-15")
    assert provenance["fetched_at"]


def test_all_sources_stale_keep_newest_observation_and_expose_gap():
    class Old:
        name = "tencent"
        def fetch(self, codes, start, end, interval):
            return {code: _frame("2026-10-14") for code in codes}
    class Older:
        name = "akshare"
        def fetch(self, codes, start, end, interval):
            return {code: _frame("2026-10-12") for code in codes}
    data = fetch_market_data(
        codes=["510330.SH"], start_date="2026-10-01", end_date="2026-10-15",
        expected_session="2026-10-15", include_provenance=True,
        loader_resolver=lambda name: Old if name == "tencent" else Older,
        fallback_chain_provider=lambda _: ["tencent", "akshare"],
    )
    assert data["510330.SH"][-1]["trade_date"].startswith("2026-10-14")
    assert data["_provenance"]["510330.SH"]["quality"] == "stale"
    assert data["_provenance"]["510330.SH"]["source"] == "tencent"


def test_a_provider_bar_beyond_the_requested_cutoff_cannot_become_latest():
    class Future:
        name = "tencent"
        def fetch(self, codes, start, end, interval):
            return {code: _frame("2026-10-16", 99) for code in codes}
    class Current:
        name = "akshare"
        def fetch(self, codes, start, end, interval):
            return {code: _frame("2026-10-15") for code in codes}
    data = fetch_market_data(
        codes=["510330.SH"], start_date="2026-10-01", end_date="2026-10-15",
        expected_session="2026-10-15", include_provenance=True,
        loader_resolver=lambda name: Future if name == "tencent" else Current,
        fallback_chain_provider=lambda _: ["tencent", "akshare"],
    )
    assert data["510330.SH"][-1]["close"] == 4.427
    assert data["_provenance"]["510330.SH"]["as_of"] == "2026-10-15"


def test_expected_session_uses_china_market_clock_not_host_zone(monkeypatch):
    from src.market_observation import expected_cn_session
    monkeypatch.setattr("src.market_calendar.is_trading_day", lambda day: True)
    monkeypatch.setattr("src.market_calendar.previous_trading_day", lambda day: pd.Timestamp("2026-10-14").date())
    # 11 UTC is 19 Shanghai, so the October 15 daily close is expected.
    assert expected_cn_session("510330.SH", "2026-10-15", now=datetime(2026, 10, 15, 11, tzinfo=ZoneInfo("UTC"))) == "2026-10-15"
    assert expected_cn_session("510330.SH", "2026-10-15", now=datetime(2026, 10, 15, 0, tzinfo=ZoneInfo("UTC"))) == "2026-10-14"
    assert expected_cn_session("SPY.US", "2026-10-15") is None


def test_indicator_preserves_the_source_and_date_of_its_actual_series(monkeypatch):
    from src.tools.technical_indicator_tool import TechnicalIndicatorTool
    rows = pd.DataFrame({"close": range(1, 51), "volume": [100] * 50}, index=pd.date_range("2026-08-27", periods=50, name="trade_date"))
    monkeypatch.setattr("src.tools.technical_indicator_tool.fetch_market_data", lambda **kw: {
        "510330.SH": rows, "_provenance": {"510330.SH": {
            "source": "tencent", "as_of": "2026-10-15", "quality": "current",
            "volume_unit": "lots", "adjustment": "none", "fetched_at": "2026-10-15T11:00:00Z",
        }},
    })
    result = json.loads(TechnicalIndicatorTool().execute(symbol="510330.SH"))
    assert result["source"] == "tencent"
    assert result["provenance"]["as_of"] == result["latest_date"] == "2026-10-15"
    assert result["provenance"]["adjustment"] == "none"


def test_chinese_index_resolution_and_report_use_points(tmp_path):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="上证指数行情")
    ledger.ingest_tool_result(tool_name="search_symbol", arguments={"query": "000001.SH"}, call_id="id", success=True, result=json.dumps({
        "ok": True, "data": {"query": "000001.SH", "candidates": [
            {"symbol": "000001.SH", "name": "上证指数", "type": "指数", "market": "cn", "source": "eastmoney"},
        ]},
    }))
    identity = next(iter(ledger._identities.values()))
    assert identity.instrument_type == "index"
    assert identity.quote_unit == "points"
    ledger.ingest_tool_result(tool_name="get_market_data", arguments={"codes": ["000001.SH"]}, call_id="bars", success=True, result=json.dumps({
        "000001.SH": [{"trade_date": "2026-10-15", "close": 3707.48}],
        "_provenance": {"000001.SH": {"source": "tencent", "quote_unit": "points"}},
    }))
    assert ledger.revalidate("000001.SH（tencent）2026-10-15 收盘3707.48点。").valid
    summary = ledger._latest_close_summary(True)
    assert "点" in summary and "3707.48 CNY" not in summary


def test_empty_northbound_is_unavailable_instead_of_success(monkeypatch):
    from src.tools import northbound_tool as nb
    monkeypatch.setattr(nb, "get_json", lambda *a, **kw: {"data": None})
    def no_fallback(**kw):
        raise RuntimeError("not configured")
    monkeypatch.setattr(nb.tushare_fallbacks, "fetch_northbound_flow", no_fallback)
    payload = json.loads(nb.NorthboundFlowTool().execute())
    assert payload["ok"] is False
    assert payload["availability"] == "unavailable"
    assert payload["retryable"] is False
    assert "not_disclosed" not in payload["availability"]


def test_search_snippets_keep_dates_but_are_never_marked_verified(monkeypatch):
    from src.tools.web_search_tool import WebSearchTool
    monkeypatch.setenv("ALIYUN_IQS_API_KEY", "unit-key")
    monkeypatch.setattr("src.tools.web_search_tool._aliyun_iqs_search", lambda *a, **kw: [
        {"title": "政策发布", "href": "https://www.pbc.gov.cn/example", "body": "摘要", "published_at": "2026-10-15 09:00"},
        {"title": "无日期转载", "href": "https://example.com/story", "body": "摘要"},
    ])
    payload = json.loads(WebSearchTool().execute(query="今日政策"))
    first, second = payload["results"]
    assert first["published_at"] == "2026-10-15 09:00"
    assert first["source_domain"] == "www.pbc.gov.cn"
    assert first["evidence_quality"] == "search_snippet"
    assert first["verified"] is False
    assert first["fetched_at"]
    assert second["published_at"] is None
    assert second["event_time"] is None


def test_carried_index_observation_keeps_points_and_original_capture_time():
    records = GroundingLedger._carried_records("prior", {"evidence": [{
        "call_id": "q", "tool": "get_market_data", "symbol": "000001.SH", "source": "tencent",
        "timestamp": "2026-10-15", "field": "close", "value": 3707.48, "status": "observed",
        "currency": "CNY", "quote_unit": "points", "quality": "current", "fetched_at": "2026-10-15T11:00:00Z",
    }]})
    assert records[0].quote_unit == "points"
    assert records[0].fetched_at == "2026-10-15T11:00:00Z"


def test_nav_disclosure_clock_converts_aware_timestamps_to_shanghai(monkeypatch):
    from src import market_calendar
    monkeypatch.setattr(market_calendar, "is_trading_day", lambda day: True)
    monkeypatch.setattr(market_calendar, "previous_trading_day", lambda day: pd.Timestamp("2026-10-14").date())
    result = market_calendar.latest_nav_date(datetime(2026, 10, 15, 14, tzinfo=ZoneInfo("UTC")))
    assert result.isoformat() == "2026-10-15"


def test_fund_fetched_at_is_origin_fetch_time_not_a_cache_read_time(monkeypatch):
    from src import fund_data
    fund_data._CACHE.clear()
    monkeypatch.setattr(fund_data, '_fetch_nav_entry', lambda *a, **k: {'data': [{'date': '2026-10-15', 'unit_nav': 2}], '_provenance': {'caliber': 'nav', 'source': 'eastmoney'}})
    monkeypatch.setattr(fund_data, '_session_context', lambda: {})
    first = fund_data.fetch_fund_data(['001236.OF'])['001236.OF']
    assert first['_provenance']['fetched_at']
    second = fund_data.fetch_fund_data(['001236.OF'])['001236.OF']
    assert second['_provenance']['fetched_at'] == first['_provenance']['fetched_at']


def test_current_fund_observation_older_than_disclosure_reference_is_not_fresh(tmp_path, monkeypatch):
    from datetime import datetime
    from src.advisor.service import AssessmentService
    from src.position_book import PositionBookStore
    monkeypatch.setattr('src.advisor.service.market_now', lambda: datetime.fromisoformat('2026-10-16T10:00:00+08:00'))
    book = PositionBookStore(tmp_path / 'book.json')
    book.set_position('001236.OF', quantity=100)
    def funds(codes, **kwargs):
        return {codes[0]: {'data': [{'date': '2026-10-14', 'unit_nav': 2}], '_provenance': {'source': 'test', 'currency': 'CNY', 'caliber': 'nav'}}, '_session': {'latest_nav_date': '2026-10-15'}}
    result = AssessmentService(book=book, fund_fetcher=funds, market_fetcher=lambda **kw: {}).collect(as_of='2026-10-16')
    assert result['evidence']['001236.OF']['quality'] == 'stale'
    assert result['evidence']['001236.OF']['expected_nav_date'] == '2026-10-15'
