"""Useful conditional risk arithmetic does not require fictional current shares."""
import pytest

from tests.test_advisor_assessment import _assess, _obs

pytestmark = pytest.mark.unit


def test_stress_uses_fund_value_and_cash_without_inventing_a_probability():
    report = _assess({'positions': [{'symbol': '001236.OF', 'quantity': 100}], 'cash': {'amount': 100, 'currency': 'CNY', 'as_of': '2026-10-15'}}, {'001236.OF': _obs('001236.OF', [2])})
    stress = report['stress_scenarios']
    row = next(row for row in stress if row['fund_change_percent'] == -20)
    assert row['fund_loss_low'] == 40 and row['fund_loss_high'] == 40
    assert row['account_loss_percent_low'] == pytest.approx(40 / 300 * 100)
    assert row['probability'] is None and row['not_forecast'] is True
    assert row['assumptions']


def test_conditional_amount_scenario_preserves_ranges_and_unknown_shares():
    from src.advisor.delivery import render_advice
    from src.advisor.decisions import decide
    report = _assess({'positions': [{'symbol': '001236.OF', 'reported_value': 1000, 'quantity': None}], 'valuation_date_range': ['2026-10-14', '2026-10-15'], 'cash': {'amount': 1000, 'currency': 'CNY'}}, {'001236.OF': _obs('001236.OF', [1, 1.1])})
    row = next(row for row in report['stress_scenarios'] if row['fund_change_percent'] == -20)
    assert row['fund_loss_low'] == pytest.approx(200)
    assert row['fund_loss_high'] == pytest.approx(220)
    assert row['account_loss_percent_low'] == pytest.approx(10)
    assert row['account_loss_percent_high'] == pytest.approx(220 / 2100 * 100)
    assert report['holdings'][0]['quantity'] is None
    text = render_advice(report, decide(report))
    assert '压力情景' in text and '200.00' in text and '预测' in text


def test_missing_amounts_do_not_create_a_full_account_stress_number():
    report = _assess({'positions': [{'symbol': '001236.OF', 'quantity': None}]}, {})
    assert report['stress_scenarios'] == []
