"""A useful report explains constraints without asking users to fix source outages."""
import pytest

from src.advisor.decisions import decide
from src.advisor.delivery import render_advice
from tests.test_advisor_assessment import _assess, _obs

pytestmark = pytest.mark.unit


def test_nav_source_failure_requests_source_recovery_not_a_full_holdings_resubmission():
    report = _assess({'positions': [{'symbol': '001236.OF', 'quantity': 100}]}, {})
    cards = decide(report)['cards']
    card = next(card for card in cards if card['scope'] == '001236.OF')
    assert card['action'] == 'RETRY_SOURCE'
    assert '数据源' in card['next_check'] and '当前份额' not in card['next_check']
    text = render_advice(report, decide(report))
    assert 'missing fund NAV' not in text


def test_report_consolidates_known_quantity_uncertainty_and_localizes_source_gaps():
    account = {'positions': [{'symbol': f'{i:06}.OF', 'quantity': None, 'reported_value': 100, 'value_as_of': '2026-10-14'} for i in range(1, 9)]}
    observations = {row['symbol']: _obs(row['symbol'], [1, 1.1]) for row in account['positions']}
    report = _assess(account, observations)
    report['news'] = {'primary': [], 'coverage': [], 'gaps': [{'url': 'https://www.bls.gov/example', 'reason': 'original publication date unavailable; search date is not substituted'}]}
    text = render_advice(report, decide(report))
    assert text.count('不要求重报整份持仓') <= 1
    assert 'original publication date unavailable' not in text
    assert 'example；' not in text
    assert '份额' in text and '历史金额' in text


def test_conditional_cluster_wholly_below_cap_reports_that_bound_without_promoting_it_to_exact():
    report = _assess({'positions': [{'symbol': '001236.OF', 'name': 'AI联接', 'quantity': None, 'reported_value': 40, 'value_as_of': '2026-10-14'}, {'symbol': '001237.OF', 'name': '宽基联接', 'quantity': None, 'reported_value': 60, 'value_as_of': '2026-10-14'}]}, {'001236.OF': _obs('001236.OF', [1, 1.1]), '001237.OF': _obs('001237.OF', [1, 1.1])}, discipline={'cluster_caps': [{'label': '科技', 'keywords': ['AI'], 'max_percent': 90}]})
    assert report['clusters'][0]['state'] == 'UNKNOWN'
    assert report['clusters'][0]['conditional_state'] == 'WITHIN_CAP'
    assert '假设成立时未超限' in render_advice(report, decide(report))


def test_outside_table_has_a_blank_line_before_notes_and_failed_domains_cannot_become_links():
    from tests.test_advisor_decisions import _report
    report = _report()
    report['external_market'] = [{'symbol': 'SPY.US', 'value': 410, 'currency': 'USD', 'source': 'sina', 'as_of': '2026-10-15', 'price_change_percent': 1}]
    report['news'] = {'primary': [], 'gaps': [{'url': 'https://www.pbc.gov.cn/example', 'reason': 'original publication date unavailable; search date is not substituted'}]}
    text = render_advice(report, decide(report))
    assert '\n\n涨跌按' in text
    assert 'pbc.gov.cn；' not in text


def test_primary_source_title_carriage_return_cannot_break_markdown_link():
    from tests.test_advisor_decisions import _report
    report = _report()
    report['news'] = {'primary': [{'title': 'Consumer Price Index Summary \r - 2026 M09 Results', 'url': 'https://www.bls.gov/news.release/cpi.nr0.htm', 'published_at': '2026-10-14'}]}
    text = render_advice(report, decide(report))
    assert '[Consumer Price Index Summary - 2026 M09 Results](https://www.bls.gov/news.release/cpi.nr0.htm)' in text
    assert '\r' not in text
