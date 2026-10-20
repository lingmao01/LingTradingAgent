"""Registered ex-post review uses dated sources and predeclared paper methods."""
import json

import pandas as pd
import pytest

from src.advisor.assessment import assess_portfolio
from src.advisor.decisions import decide
from src.advisor.journal import PublishedAdviceStore
from src.agent.tools import ToolRegistry
from tests.test_advisor_assessment import _obs

pytestmark = pytest.mark.unit


def test_registered_review_uses_nav_and_never_claims_user_execution(tmp_path):
    from src.advisor.review import ReviewService
    from src.tools.advisor_review_tool import AdvisorReviewTool
    store = PublishedAdviceStore(tmp_path / 'published_advice.json')
    report = assess_portfolio({'positions': [{'symbol': '001236.OF', 'quantity': 100}]}, {'001236.OF': _obs('001236.OF', [2], day='2026-08-31')}, as_of='2026-08-31')
    entry = store.publish(run_id='published', report=report, decision=decide(report), content='valid', delivery={'release_path': 'structured_assessment'}, published_at='2026-09-01T10:00:00+08:00')
    days = pd.bdate_range('2026-09-01', '2026-10-16')
    def funds(codes, **kwargs):
        return {codes[0]: {'data': [{'date': str(day.date()), 'unit_nav': 2 + i / 100} for i, day in enumerate(days)], '_provenance': {'source': 'test-nav', 'caliber': 'nav'}}}
    def market(**kwargs):
        assert kwargs['codes'] == ['000300.SH']
        return {'000300.SH': [{'date': str(day.date()), 'close': 4000 + i} for i, day in enumerate(days)]}
    registry = ToolRegistry()
    registry.register(AdvisorReviewTool(ReviewService(store=store, fund_fetcher=funds, market_fetcher=market)))
    result = json.loads(registry.execute('review_published_advice', {'publication_id': entry['id'], 'as_of': '2026-10-16'}))
    assert result['ok'] is True
    assert result['data']['scope'] == 'fund_sleeve_paper_baselines'
    assert result['data']['execution_state'] == 'not_reported'
    assert result['data']['realized_execution_performance'] is None
    assert len(result['data']['scenarios']) == 3
    assert all(row['result']['start_date'] == '2026-09-02' for row in result['data']['scenarios'])
    assert len(store.list()[0]['evaluations']) == 1


def test_review_does_not_manufacture_weights_from_unknown_current_holdings(tmp_path):
    from src.advisor.review import ReviewService
    store = PublishedAdviceStore(tmp_path / 'published_advice.json')
    report = assess_portfolio({'positions': [{'symbol': '001236.OF', 'reported_value': 100}]}, {}, as_of='2026-08-31')
    entry = store.publish(run_id='unknown', report=report, decision=decide(report), content='valid', delivery={'release_path': 'structured_assessment'})
    result = ReviewService(store=store).review(entry['id'], as_of='2026-10-16')
    assert result['review_status'] == 'unavailable'
    assert result['scenarios'] == []
    assert result['realized_execution_performance'] is None


def test_scheduled_review_uses_full_publication_pages_and_preserves_unknown_execution():
    from src.scheduled_research.playbooks import get_playbook
    body = get_playbook('advice-review').body
    assert 'published-decision review capability' in body
    assert 'next page' in body and 'due publications' in body
    assert 'not reported' in body and 'paper baselines' in body
