"""Useful advice follows observed constraints, never missing-data default hold."""
import copy

import pytest

from tests.test_advisor_assessment import _assess, _obs, _discipline

pytestmark = pytest.mark.unit


def _report():
    return _assess({'positions': [{'symbol': '001236.OF', 'quantity': 90, 'name': 'AI联接'}, {'symbol': '001237.OF', 'quantity': 10, 'name': '红利联接'}], 'cash': {'amount': 100, 'currency': 'CNY', 'as_of': '2026-10-15'}}, {'001236.OF': _obs('001236.OF', [2]), '001237.OF': _obs('001237.OF', [2]), '000300.SH': _obs('000300.SH', [1, 2, 3], field='close')}, discipline={**_discipline([{'action': 'ADD ALLOWED', 'when': '收盘 >= SMA2'}]), 'cluster_caps': [{'label': '科技', 'keywords': ['AI'], 'max_percent': 60}]})


def test_reduce_to_cash_respects_shrinking_fund_denominator():
    from src.advisor.decisions import decide
    result = decide(_report())
    cluster = next(row for row in result['cards'] if row['action'] == 'REDUCE_CLUSTER')
    assert cluster['cash_redemption_estimate'] == pytest.approx(150)  # (180-.6*200)/(1-.6)
    assert cluster['reallocation_estimate'] == pytest.approx(60)
    assert cluster['scope'] == '科技'
    assert cluster['trigger'] and cluster['invalidation'] and cluster['next_check']
    assert not any(row['action'] == 'ADD' for row in result['cards'])


def test_allowed_is_not_buy_and_unknown_is_not_hold():
    from src.advisor.decisions import decide
    report = _report()
    report['clusters'] = []
    result = decide(report)
    assert result['stance'] == 'CONDITIONAL_ADD'
    assert all(card['action'] != 'ADD' for card in result['cards'])
    missing = _assess({'positions': [{'symbol': '001236.OF', 'quantity': 10}]}, {})
    result = decide(missing)
    assert result['stance'] == 'LIMITED_JUDGMENT'
    assert all(card['action'] != 'HOLD' for card in result['cards'])
    assert result['cards'][0]['next_check']


def test_pending_redemption_does_not_issue_duplicate_order():
    from src.advisor.decisions import decide
    report = _report()
    report['holdings'][0]['available_quantity'] = 10  # already reserved 80*2=160, enough for 150 trim
    result = decide(report)
    card = next(row for row in result['cards'] if row['action'] == 'REDUCE_CLUSTER')
    assert card['additional_cash_redemption_estimate'] == 0
    assert card['pending_redemption_estimate'] == 160


def test_render_is_complete_useful_and_cannot_publish_tampered_decisions():
    from src.advisor.decisions import decide
    from src.advisor.delivery import render_advice, validate_delivery
    report = _report()
    decision = decide(report)
    text = render_advice(report, decision)
    assert '减仓' in text and '下一次' in text and '失效' in text
    assert '001236.OF' in text and '单位净值' in text
    assert '本轮其余结论尚未' not in text
    assert validate_delivery(report, decision)
    fake = copy.deepcopy(decision)
    fake['cards'][0]['action'] = 'ADD'
    assert not validate_delivery(report, fake)


def test_stale_marks_do_not_turn_an_old_cap_breach_into_a_current_order():
    from src.advisor.decisions import decide
    report = _report()
    report['holdings'][0]['gaps'] = ['mark freshness is not confirmed for this decision']
    result = decide(report)
    assert not any(card['action'] == 'REDUCE_CLUSTER' for card in result['cards'])
    assert any(card['action'] == 'VERIFY_HOLDING' for card in result['cards'])


def test_max_quantity_trigger_accounts_for_reserved_redemptions():
    from src.advisor.decisions import decide
    report = _report()
    report['clusters'] = []
    report['holdings'][0]['rules'] = {'max_quantity': {'state': 'OVER CAP', 'level': 50, 'observed': 90}}
    report['holdings'][0]['available_quantity'] = 40
    result = decide(report)
    card = next(card for card in result['cards'] if card['action'] == 'REVIEW_REDUCTION')
    assert card['additional_quantity_reduction'] == 0


def test_primary_macro_direction_has_an_explicit_inference_and_no_surprise_claim():
    from src.advisor.decisions import decide
    report = _report()
    report['news'] = {'primary': [{'verified': True, 'url': 'https://www.bls.gov/news.release/cpi.nr0.htm', 'published_at': '2026-10-14', 'facts': [
        {'metric': 'cpi_yoy', 'value': 3.7, 'unit': 'percent'},
        {'metric': 'cpi_previous_yoy', 'value': 3.4, 'unit': 'percent'},
    ]}]}
    result = decide(report)
    card = next(card for card in result['cards'] if card['action'] == 'MACRO_ASSESSMENT')
    assert card['inference_type'] == 'economic_mechanism_not_causal_proof'
    assert card['yoy_change_percentage_points'] == pytest.approx(.3)
    assert '超预期' not in card['rationale']
    assert 'https://www.bls.gov/news.release/cpi.nr0.htm' in card['evidence_refs']
    report['news']['primary'][0]['verified'] = False
    assert not any(card['action'] == 'MACRO_ASSESSMENT' for card in decide(report)['cards'])


def test_combined_holding_keeps_both_known_navs_instead_of_claiming_missing_quotes():
    from src.advisor.decisions import decide
    from src.advisor.delivery import render_advice
    report = _assess({'positions': [], 'holding_groups': [{'group_id': 'combined', 'candidate_symbols': ['001234.OF', '001235.OF'], 'reported_value': 1000}], 'valuation_date_range': ['2026-10-14', '2026-10-14']}, {
        '001234.OF': _obs('001234.OF', [1, 1.2345]), '001235.OF': _obs('001235.OF', [2, 2.3456]),
    })
    text = render_advice(report, decide(report))
    assert '1.2345' in text and '2.3456' in text
    assert report['holdings'][0]['quantity'] is None


@pytest.mark.parametrize('cash', [
    {'amount': 10, 'currency': 'CNY', 'as_of': '2025-01-01'},
    {'amount': 10, 'currency': 'USD', 'as_of': '2026-10-15'},
])
def test_stale_or_other_currency_cash_cannot_trigger_current_cny_liquidity_actions(cash):
    from src.advisor.decisions import decide
    from src.advisor.facts import build_catalog
    report = _report()
    report['clusters'] = []
    report['portfolio'].update(available_cash=cash['amount'], available_cash_currency=cash['currency'], cash_as_of=cash['as_of'])
    report['personal_parameters']['minimum_cash'] = 50
    decision = decide(report)
    assert not any(card['action'] == 'RESTORE_CASH' for card in decision['cards'])
    assert decision['stance'] != 'CONDITIONAL_ADD'
    assert 'CONDITIONAL_ADD' not in build_catalog(report)['allowed_actions']
