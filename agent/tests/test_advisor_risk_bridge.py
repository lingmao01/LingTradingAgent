"""Fund risk uses NAV and discloses excluded original exposure."""
import json

import pandas as pd
import pytest

from src.tools.portfolio_risk_tool import PortfolioRiskXrayTool

pytestmark = pytest.mark.unit


def test_nav_bridge_keeps_original_weights_and_partial_scope():
    dates = pd.bdate_range('2026-07-01', periods=45)
    calls = []
    def market(**kwargs):
        calls.append(kwargs['codes'])
        return {'510330.SH': [{'date': str(day.date()), 'close': 4 + i / 100} for i, day in enumerate(dates)]}
    def fund(codes, **kwargs):
        return {codes[0]: {'data': [{'date': str(day.date()), 'unit_nav': 2 + i / 100} for i, day in enumerate(dates)], '_provenance': {'caliber': 'nav', 'source': 'test-nav'}}}
    weights = {'001236.OF': .25, '510330.SH': .75}
    tool = PortfolioRiskXrayTool(data_fetcher=market, fund_fetcher=fund)
    result = json.loads(tool.execute(symbols=list(weights), weights=weights, start_date='2026-07-01', end_date='2026-10-15'))
    assert result['status'] == 'ok'
    assert calls == [['510330.SH']]
    assert result['meta']['original_weights'] == weights
    assert result['meta']['risk_scope'] == 'requested_basket'
    assert result['meta']['series_basis']['001236.OF'] == 'unit_nav'
    tool = PortfolioRiskXrayTool(data_fetcher=market, fund_fetcher=lambda *a, **k: {})
    partial = json.loads(tool.execute(symbols=list(weights), weights=weights))
    assert partial['status'] == 'ok'
    assert partial['meta']['risk_scope'] == 'available_subportfolio'
    assert partial['meta']['covered_original_weight'] == .75
    assert partial['meta']['original_weights'] == weights
    assert partial['meta']['unresolved_symbols'] == ['001236.OF']


def test_invalid_excluded_weight_cannot_evade_validation():
    tool = PortfolioRiskXrayTool(data_fetcher=lambda **kw: {'AAA': [{'date': str(day.date()), 'close': 10 + i} for i, day in enumerate(pd.bdate_range('2026-01-01', periods=40))]})
    result = json.loads(tool.execute(symbols=['AAA', 'MISSING'], weights={'AAA': 1, 'MISSING': -1}))
    assert result['status'] == 'error'


def test_risk_tool_ignores_future_rows_and_duplicate_dates_cannot_pad_history():
    good = [{'date': str(day.date()), 'close': 10 + i / 100} for i, day in enumerate(pd.bdate_range('2026-08-01', periods=40))]
    tool = PortfolioRiskXrayTool(data_fetcher=lambda **kw: {'AAA': good + [{'date': '2027-01-01', 'close': 99999}]})
    result = json.loads(tool.execute(symbols=['AAA'], start_date='2026-08-01', end_date='2026-10-15'))
    assert result['status'] == 'ok'
    assert '2027' not in result['data']['inputs']['last_date']
    assert result['data']['inputs']['return_observations'] == 39
    tool = PortfolioRiskXrayTool(data_fetcher=lambda **kw: {'AAA': good[:2] * 20})
    assert json.loads(tool.execute(symbols=['AAA']))['status'] == 'error'


def test_risk_tool_requires_thirty_common_dates_not_two_long_disjoint_series():
    first = [{'date': str(day.date()), 'close': 10 + i / 100} for i, day in enumerate(pd.bdate_range('2026-08-01', periods=40))]
    second = [{'date': str(day.date()), 'close': 20 + i / 100} for i, day in enumerate(pd.bdate_range('2026-06-01', periods=40))] + first[-2:]
    tool = PortfolioRiskXrayTool(data_fetcher=lambda **kw: {'AAA': first, 'BBB': second})
    result = json.loads(tool.execute(symbols=['AAA', 'BBB'], start_date='2026-06-01', end_date='2026-10-15'))
    assert result['status'] == 'error' and '30' in result['error']
