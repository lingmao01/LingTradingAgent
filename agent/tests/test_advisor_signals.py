"""Pure frozen evidence evaluation, including false/unknown condition trees."""
import json
from pathlib import Path

import pandas as pd
import pytest

pytestmark = pytest.mark.unit


def sample(values=None, condition=None):
    dates = [day.date().isoformat() for day in pd.bdate_range('2026-01-05', periods=60)]
    values = values or list(range(100, 160))
    discipline = {'regime': {'indices': ['000300.SH'], 'sma_short_days': 20, 'sma_long_days': 50, 'macd_confirm_days': 2}, 'tiers': [
        {'action': 'NO ADD', 'when': '收盘 < 短均线（SMA{short}）', 'label': '禁新增'},
        {'action': 'REDUCE', 'when': condition or '收盘 ≥ 短均线（SMA{short}）且动能确认', 'label': '减仓'},
        {'action': 'ADD ALLOWED', 'when': '收盘 ≥ 短均线（SMA{short}）且 MACD柱连续 {confirm} 日转正', 'label': '增仓'},
    ]}
    return {'inputs': {'account': {'positions': []}, 'discipline': discipline, 'as_of': dates[-1], 'trading_sessions': dates, 'calendar_version': 'fixture-v1'}, 'evidence': {'000300.SH': {'symbol': '000300.SH', 'field': 'close', 'value': values[-1], 'as_of': dates[-1], 'source': 'fixture', 'quality': 'current', 'finality': 'closed_session', 'history': [{'date': day, 'value': value} for day, value in zip(dates, values)]}}}


def evaluate(payload):
    from src.advisor.models import AssessmentInputs
    from src.advisor.signals import RuleSet, assess_signals
    return assess_signals(AssessmentInputs.from_payload(payload), rules=RuleSet.from_discipline(payload['inputs']['discipline'], account_rules=payload['inputs'].get('rules', ())))


def test_all_twelve_actual_snapshot_gates_and_unresolved_children_are_visible():
    rows = evaluate(json.loads((Path(__file__).parent / 'fixtures/advisor_signal_snapshot.json').read_text()))
    rows = [row for row in rows if row.subject['kind'] == 'index']
    assert len(rows) == 12
    for effect, expected in [('BLOCK_ADD', 'TRUE'), ('REDUCE_TRIGGER', 'FALSE'), ('PERMIT_ADD', 'FALSE')]:
        selected = [row for row in rows if row.effect == effect]
        assert len(selected) == 4 and {row.state for row in selected} == {expected}
    reduce = next(row for row in rows if row.effect == 'REDUCE_TRIGGER')
    assert reduce.config_status == 'DEFINITION_INCOMPLETE'
    assert [child.state for child in reduce.conditions[0].children] == ['FALSE', 'UNKNOWN']
    assert '{short}' not in reduce.conditions[0].display


def test_true_plus_unknown_is_unknown_and_equal_boundary_uses_recorded_operator():
    rows = evaluate(sample([100] * 60))
    assert next(row for row in rows if row.effect == 'REDUCE_TRIGGER').state == 'UNKNOWN'
    assert next(row for row in rows if row.effect == 'BLOCK_ADD').state == 'FALSE'


def test_immutable_payload_and_metadata_cannot_supply_history():
    from src.advisor.models import AssessmentInputs
    from src.advisor.signals import RuleSet, assess_signals
    payload = sample()
    frozen = AssessmentInputs.from_payload(payload)
    rules = RuleSet.from_discipline(payload['inputs']['discipline'])
    before = tuple(row.to_dict() for row in assess_signals(frozen, rules=rules))
    payload['evidence']['000300.SH']['history'][-1]['value'] = 1
    payload['inputs']['account']['positions'].append({'symbol': 'fake'})
    assert tuple(row.to_dict() for row in assess_signals(frozen, rules=rules)) == before
    with pytest.raises(TypeError):
        frozen.account['positions'] = ()
    payload.pop('evidence')
    payload['data'] = {'observations': {'000300.SH': {'quality': 'current'}}}
    assert all(row.state == 'UNKNOWN' for row in assess_signals(AssessmentInputs.from_payload(payload), rules=rules))


def test_missing_trading_session_and_intraday_cross_do_not_create_close_trigger():
    payload = sample()
    del payload['evidence']['000300.SH']['history'][-5]
    assert all(row.state == 'UNKNOWN' for row in evaluate(payload))
    payload = sample(list(range(160, 100, -1)))
    obs = payload['evidence']['000300.SH']
    obs['expected_closed_session'] = obs['history'][-2]['date']
    obs['finality'] = 'in_progress'
    obs['value'] = obs['history'][-1]['value'] = 10000
    assert next(row for row in evaluate(payload) if row.effect == 'BLOCK_ADD').state == 'TRUE'


def test_macd_means_two_positive_closed_values_not_only_new_cross():
    rows = evaluate(sample())
    permit = next(row for row in rows if row.effect == 'PERMIT_ADD')
    assert permit.state == 'TRUE'
    assert permit.conditions[0].children[-1].operator == 'RECENT_POSITIVE'


def test_drawdown_requires_cashflow_peak_and_comparable_history_and_no_rules_are_distinct():
    payload = sample()
    payload['inputs']['discipline']['max_drawdown_percent'] = 20
    payload['inputs']['account']['positions'] = [{'symbol': '000051.OF'}]
    payload['data'] = {'stress_scenarios': [{'account_loss_percent_low': 2}]}
    rows = evaluate(payload)
    assert next(row for row in rows if row.rule_id == 'account:max_drawdown').state == 'UNKNOWN'
    assert next(row for row in rows if row.subject.get('id') == '000051.OF').config_status == 'NOT_CONFIGURED'


def test_exact_cluster_weight_can_breach_but_historical_amount_cannot():
    payload = sample()
    payload['inputs']['discipline']['cluster_caps'] = [{'label': '科技', 'members': ['000051.OF'], 'max_percent': 70}]
    payload['inputs']['account']['positions'] = [{'symbol': '000051.OF', 'quantity': 80}, {'symbol': '000052.OF', 'quantity': 20}]
    for symbol in ['000051.OF', '000052.OF']:
        payload['evidence'][symbol] = {'symbol': symbol, 'field': 'unit_nav', 'value': 1, 'as_of': payload['inputs']['as_of'], 'quality': 'latest_disclosed', 'source': 'fixture', 'currency': 'CNY'}
    cluster = next(row for row in evaluate(payload) if row.rule_id == 'cluster:科技:max_percent')
    assert cluster.state == 'TRUE' and cluster.conditions[0].actual == 80
    payload['inputs']['account']['positions'][0].update(quantity=None, reported_value=80)
    assert next(row for row in evaluate(payload) if row.rule_id == 'cluster:科技:max_percent').state == 'UNKNOWN'


def test_cash_unreconciled_events_cannot_pass_recorded_cash_floor():
    payload = sample()
    payload['inputs']['account'].update(cash={'amount': 100, 'currency': 'CNY', 'as_of': payload['inputs']['as_of']}, minimum_cash=50, unreconciled_events=[{'symbol': '__CASH__'}])
    cash = next(row for row in evaluate(payload) if row.rule_id == 'account:minimum_cash')
    assert cash.state == 'UNKNOWN'


def test_maximum_loss_not_compared_to_hypothetical_stress_and_original_policy_gap_visible():
    payload = sample()
    payload['inputs']['account']['max_loss_amount'] = 1000
    payload['inputs']['discipline'].update(cluster_caps=[{'label': '科技', 'max_percent': 90}], note='常规上限70%；确认后允许上浮至90%，不设归位')
    payload['data'] = {'stress_scenarios': [{'fund_loss_high': 100}]}
    rows = evaluate(payload)
    assert next(row for row in rows if row.rule_id == 'account:max_loss_amount').state == 'UNKNOWN'
    assert next(row for row in rows if row.rule_id == 'cluster:科技:policy_definition').config_status == 'DEFINITION_INCOMPLETE'
