"""The registered assessment tool joins actual account and dated NAV clients."""
from __future__ import annotations

import json

import pytest

from src.agent.tools import ToolRegistry
from src.position_book import PositionBookStore

pytestmark = pytest.mark.unit


def test_registered_assessment_values_feeders_through_nav_and_fetches_each_index_once(tmp_path):
    from src.advisor.service import AssessmentService
    from src.tools.portfolio_assessment_tool import PortfolioAssessmentTool
    book = PositionBookStore(tmp_path / "position_book.json")
    book.set_position("001236.OF", quantity=100)
    book.set_account(cash={"amount": 100, "currency": "CNY", "as_of": "2026-10-15"})
    calls = []
    def funds(codes, **kwargs):
        calls.append(("fund", tuple(codes)))
        return {codes[0]: {"data": [{"date": "2026-10-15", "unit_nav": 2}], "_provenance": {
            "source": "eastmoney", "currency": "CNY", "caliber": "nav", "as_of": "2026-10-15",
        }}}
    def market(**kwargs):
        calls.append(("market", tuple(kwargs["codes"])))
        assert all(not code.endswith(".OF") for code in kwargs["codes"])
        return {}
    registry = ToolRegistry()
    registry.register(PortfolioAssessmentTool(AssessmentService(book=book, fund_fetcher=funds, market_fetcher=market)))
    payload = json.loads(registry.execute("get_portfolio_assessment", {"as_of": "2026-10-15"}))
    assert payload["ok"] is True
    assert payload["data"]["portfolio"]["account_value"] == 300
    assert payload["evidence"]["001236.OF"]["source"] == "eastmoney"
    assert calls.count(("fund", ("001236.OF",))) == 1
    assert len([call for call in calls if call[0] == "market"]) == 1


def test_a_failed_fund_source_keeps_market_assessment_and_holding_identity(tmp_path):
    from src.advisor.service import AssessmentService
    from src.tools.portfolio_assessment_tool import PortfolioAssessmentTool
    book = PositionBookStore(tmp_path / "position_book.json")
    book.set_position("001236.OF", quantity=100)
    def unavailable(codes, **kwargs):
        raise TimeoutError("source timed out")
    def market(**kwargs):
        return {"000300.SH": [{"trade_date": "2026-10-15", "close": 4166.01}], "_provenance": {
            "000300.SH": {"source": "tencent", "quality": "current"},
        }}
    registry = ToolRegistry()
    registry.register(PortfolioAssessmentTool(AssessmentService(book=book, fund_fetcher=unavailable, market_fetcher=market)))
    payload = json.loads(registry.execute("get_portfolio_assessment", {"as_of": "2026-10-15"}))
    assert payload["ok"] is True
    assert payload["data"]["holdings"][0]["symbol"] == "001236.OF"
    assert payload["data"]["holdings"][0]["value"] is None
    assert any(row["symbol"] == "000300.SH" and row["close"] == 4166.01 for row in payload["data"]["regime"])
    assert any("timed out" in gap["reason"] for gap in payload["data"]["data_gaps"])


def test_invalid_assessment_inputs_fail_before_any_network_call():
    from src.tools.portfolio_assessment_tool import PortfolioAssessmentTool
    registry = ToolRegistry()
    registry.register(PortfolioAssessmentTool())
    for params in ({"as_of": "not-a-date"}, {"lookback_days": True}, {"lookback_days": -1}, {"observations": {"fake": 999}}):
        payload = json.loads(registry.execute("get_portfolio_assessment", params))
        assert payload["ok"] is False
        assert payload["error"]


def test_amount_only_account_does_not_fetch_unusable_six_month_nav_histories(tmp_path):
    from src.advisor.service import AssessmentService
    book = PositionBookStore(tmp_path / 'position_book.json')
    book.set_position('001236.OF', reported_value=100, value_as_of='2026-09-29')
    calls = []
    def fund(codes, **kwargs):
        calls.append(kwargs)
        return {codes[0]: {'data': [{'date': '2026-10-15', 'unit_nav': 2}, {'date': '2026-09-29', 'unit_nav': 1}], '_provenance': {'caliber': 'nav', 'source': 'test', 'currency': 'CNY'}}}
    result = AssessmentService(book=book, fund_fetcher=fund, market_fetcher=lambda **k: {}).collect(as_of='2026-10-16')
    assert all(call['start_date'] >= '2026-09-29' for call in calls)
    assert result['data']['holdings'][0]['value_low'] == 200


def test_bad_identity_sidecar_does_not_discard_the_healthy_account_and_nav(tmp_path):
    from src.advisor.service import AssessmentService
    book = PositionBookStore(tmp_path / 'position_book.json')
    book.set_position('001236.OF', quantity=100)
    (tmp_path / 'fund_identities.json').write_text('{bad')
    def fund(codes, **kw):
        return {codes[0]: {'data': [{'date': '2026-10-15', 'unit_nav': 2}], '_provenance': {'source': 'test', 'currency': 'CNY', 'caliber': 'nav'}}}
    result = AssessmentService(book=book, fund_fetcher=fund, market_fetcher=lambda **kw: {}).collect(as_of='2026-10-15')
    assert result['data']['holdings'][0]['value'] == 200
    assert any(gap.get('scope') == 'fund_identity' for gap in result['data']['data_gaps'])
    assert (tmp_path / 'fund_identities.json').read_text() == '{bad'


def test_explicit_redemption_confirmation_date_reaches_public_assessment(tmp_path):
    from src.advisor.service import AssessmentService
    from src.tools.portfolio_assessment_tool import PortfolioAssessmentTool
    from src.fund_identity import FundIdentityStore
    book = PositionBookStore(tmp_path / 'position_book.json')
    book.set_position('001236.OF', quantity=10, lots=[{'lot_id': 'x', 'quantity': 10, 'confirmed_at': '2026-10-14'}])
    FundIdentityStore(tmp_path / 'fund_identities.json').record('001236.OF', terms={'effective_from': '2026-01-01', 'redemption_tiers': [{'holding_days_below': 7, 'fee_percent': 1.5}], 'otherwise_fee_percent': 0})
    def fund(codes, **kw):
        return {codes[0]: {'data': [{'date': '2026-10-15', 'unit_nav': 2}], '_provenance': {'source': 'test', 'currency': 'CNY', 'caliber': 'nav'}}}
    registry = ToolRegistry()
    registry.register(PortfolioAssessmentTool(AssessmentService(book=book, fund_fetcher=fund, market_fetcher=lambda **kw: {})))
    def call(**kw):
        return json.loads(registry.execute('get_portfolio_assessment', {'as_of': '2026-10-15', **kw}))
    assert call()['data']['holdings'][0]['redemption_fee']['estimate'] is None
    fee = call(redemption_confirmation_date='2026-10-16')['data']['holdings'][0]['redemption_fee']
    assert fee['estimate'] == pytest.approx(.3)
    assert fee['confirmation_date'] == '2026-10-16'
    assert call(redemption_confirmation_date='bad')['ok'] is False
