"""Independent interpretation stays separate from source facts and obeys hard constraints."""
import copy
import json

import pytest

from tests.test_advisor_decisions import _report

pytestmark = pytest.mark.unit


def _judgment(**changes):
    row = {
        'id': 'trim', 'scope': 'PORTFOLIO', 'action': 'REVIEW_REDUCTION',
        'summary': '科技主题超出已记录上限，优先恢复风险约束。',
        'reasoning': '这项判断来自敞口集中，而非对下一交易日涨跌的预测。',
        'evidence_refs': ['cluster:科技', 'holding:001236.OF'],
        'counterargument': '若在途赎回已经覆盖超限，就不应重复提交。',
        'counter_evidence_refs': [], 'quotes': [],
        'trigger': '最新台账仍超出已记录的主题上限。',
        'invalidation': '在途已覆盖超限，或新台账重算不再超限。',
    }
    from src.advisor.facts import ACTION_CONDITIONS
    row.update(ACTION_CONDITIONS['REVIEW_REDUCTION'])
    row.update(changes)
    action = row.get('action')
    if isinstance(action, str) and action in ACTION_CONDITIONS:
        for key, value in ACTION_CONDITIONS[action].items():
            if key not in changes:
                row[key] = value
    return row


def test_valid_candidate_still_requires_an_independent_review_before_publication():
    from src.advisor.expert import validate_candidates, adjudicate
    from src.advisor.facts import build_catalog
    report = _report()
    catalog = build_catalog(report)
    valid, rejected = validate_candidates({'judgments': [_judgment()]}, catalog)
    assert len(valid) == 1 and rejected == []
    assert adjudicate(valid, {'reviews': []}) == []
    assert adjudicate(valid, {'reviews': [{'id': 'trim', 'supported': True, 'reason': '敞口事实与条件匹配'}]}) == valid
    assert adjudicate(valid, {'reviews': [{'id': 'trim', 'supported': 'true', 'reason': 'bad type'}]}) == []


@pytest.mark.parametrize('change', [
    {'evidence_refs': ['made-up']},
    {'action': 'CONDITIONAL_ADD'},
    {'summary': '目标价999元，立即加仓10000元。'},
    {'summary': '机构正在抢筹，确定牛市即将到来。'},
    {'quotes': [{'ref': 'cluster:科技', 'text': '不存在的原文'}]},
])
def test_invalid_premises_and_policy_conflicts_are_local_rejections(change):
    from src.advisor.expert import validate_candidates
    from src.advisor.facts import build_catalog
    valid, rejected = validate_candidates({'judgments': [_judgment(**change)]}, build_catalog(_report()))
    assert valid == [] and rejected


def test_one_bad_judgment_does_not_erase_an_unaffected_supported_one():
    from src.advisor.expert import validate_candidates, adjudicate
    from src.advisor.facts import build_catalog
    valid, rejected = validate_candidates({'judgments': [_judgment(), _judgment(id='fake', evidence_refs=['missing'])]}, build_catalog(_report()))
    assert [row['id'] for row in valid] == ['trim']
    assert rejected[0]['id'] == 'fake'
    assert adjudicate(valid, {'reviews': [{'id': 'trim', 'supported': False, 'reason': '缺少论证'}]}) == []


def test_catalog_covers_all_holdings_and_a_changed_snapshot_invalidates_judgments():
    from src.advisor.facts import build_catalog
    from src.advisor.expert import attach, approved
    report = _report()
    catalog = build_catalog(report)
    assert 'holding:001236.OF' in catalog['facts']
    attach(report, [_judgment()], catalog, rejected=[], review_receipt={'reviews': [{'id': 'trim', 'supported': True, 'reason': 'supported'}]})
    assert approved(report) == [_judgment()]
    changed = copy.deepcopy(report)
    changed['clusters'][0]['fund_weight_percent'] = 20
    assert approved(changed) == []


def test_bounded_synthesis_is_tool_free_and_reviewer_sees_sources():
    from src.advisor.expert import synthesize
    from tests.test_grounding_release import _ScriptedLLM, _Response
    llm = _ScriptedLLM([
        _Response(content=json.dumps({'judgments': [_judgment()]}, ensure_ascii=False)),
        _Response(content=json.dumps({'reviews': [{'id': 'trim', 'supported': True, 'reason': 'supported'}]})),
    ])
    report = _report()
    result = synthesize(llm, report, request='给出操作建议', call_budget=3)
    assert len(result['accepted']) == 1
    assert llm.calls == 2 and all(tools is None for tools in llm.tools_history)
    reviewer = str(llm.messages_history[-1])
    assert 'cluster:科技' in reviewer and 'fund_weight_percent' in reviewer


def test_provider_failure_and_bad_json_keep_deterministic_assessment():
    from src.advisor.expert import synthesize
    from tests.test_grounding_release import _ScriptedLLM, _Response
    from tests.test_advisor_loop_delivery import OutageLLM
    report = _report()
    result = synthesize(OutageLLM([_Response()]), report, request='分析', call_budget=3)
    assert result['accepted'] == [] and result['provider_error']
    assert report['holdings'][0]['value'] == 180
    llm = _ScriptedLLM([_Response(content='not json')])
    result = synthesize(llm, report, request='分析', call_budget=3)
    assert result['accepted'] == [] and llm.calls <= 3

@pytest.mark.parametrize('change', [
    {'scope': []}, {'action': {}}, {'id': []},
    {'quotes': [{'ref': [], 'text': 'x'}]},
    {'summary': '预计上涨987.654%，因此无需控制风险。'},
    {'summary': '截至2026-12-31这些数据仍有效。'},
])
def test_malformed_or_unanchored_items_fail_closed_without_crashing(change):
    from src.advisor.expert import validate_candidates
    from src.advisor.facts import build_catalog
    valid, rejected = validate_candidates({'judgments': [_judgment(**change)]}, build_catalog(_report()))
    assert not valid and rejected


def test_percent_intervals_use_explicit_derived_weights_without_losing_range_signs():
    from src.advisor.expert import validate_candidates
    from src.advisor.facts import build_catalog
    report = _report()
    row = report['holdings'][0]
    row['fund_weight_low'], row['fund_weight_high'] = .6663, .6710
    judgment = _judgment(summary='条件基金权重区间66.63%-67.10%，仍须核对条件假设。')
    valid, rejected = validate_candidates({'judgments': [judgment]}, build_catalog(report))
    assert valid and not rejected

@pytest.mark.parametrize('document', [{'judgments': None}, {'judgments': [_judgment(id=[])]}])
def test_malformed_model_document_can_use_the_bounded_repair_without_throwing(document):
    from src.advisor.expert import synthesize
    from tests.test_grounding_release import _ScriptedLLM, _Response
    llm = _ScriptedLLM([_Response(content=json.dumps(document))])
    result = synthesize(llm, _report(), request='分析', call_budget=3)
    assert result['status'] == 'unavailable' and result['calls'] <= 3


def test_supported_subset_is_reviewed_before_optional_repairs_and_review_can_retry_once():
    from src.advisor.expert import synthesize
    from tests.test_grounding_release import _Response
    class TransientReview:
        calls=0
        def stream_chat(self,*args,**kwargs):
            self.calls+=1
            if self.calls==1:return _Response(content=json.dumps({'judgments':[_judgment(),_judgment(id='bad',evidence_refs=['missing'])]}))
            if self.calls==2:raise ConnectionError('temporary provider connection failure')
            return _Response(content=json.dumps({'reviews':[{'id':'trim','supported':True,'reason':'independent source check passed'}]}))
    llm=TransientReview()
    outcome=synthesize(llm,_report(),request='给出建议',call_budget=3)
    assert llm.calls==3 and [row['id'] for row in outcome['accepted']]==['trim']
    assert outcome['provider_error'] is None

@pytest.mark.parametrize('change', [
    {'reasoning':'压力情景中的账户损失处于用户最大回撤阈值之内，因此账户风险仍可控。'},
    {'action':'NO_ADD','invalidation':'用户更新现金余额与持仓金额口径，使禁加仓前提不再成立。'},
])
def test_stress_loss_is_not_existing_drawdown_and_cash_cannot_reverse_a_price_gate(change):
    from src.advisor.expert import validate_candidates
    from src.advisor.facts import build_catalog
    valid,rejected=validate_candidates({'judgments':[_judgment(**change)]},build_catalog(_report()))
    assert not valid and rejected


def test_referenced_index_name_is_an_identifier_not_a_three_hundred_point_claim():
    from src.advisor.expert import validate_candidates
    from src.advisor.facts import build_catalog
    catalog=build_catalog(_report())
    row=_judgment(summary='沪深300的条件需与具体基金信号区分。',evidence_refs=['market:000300.SH','cluster:科技','holding:001236.OF'])
    valid,rejected=validate_candidates({'judgments':[row]},catalog)
    assert valid and not rejected
    row['summary']='预计收益上涨300%，无需复核。'
    assert not validate_candidates({'judgments':[row]},catalog)[0]


def test_temporary_generation_connection_failure_can_retry_with_a_review_still_reserved():
    from src.advisor.expert import synthesize
    from tests.test_grounding_release import _Response
    class TransientGenerate:
        calls=0
        def stream_chat(self,*args,**kwargs):
            self.calls+=1
            if self.calls==1:raise ConnectionError('temporary connection failure')
            if self.calls==2:return _Response(content=json.dumps({'judgments':[_judgment()]}))
            return _Response(content=json.dumps({'reviews':[{'id':'trim','supported':True,'reason':'source and conditions checked'}]}))
    llm=TransientGenerate()
    outcome=synthesize(llm,_report(),request='分析',call_budget=3)
    assert len(outcome['accepted'])==1 and outcome['calls']==3 and outcome['provider_error'] is None


def test_current_constraints_and_price_gate_conditions_are_not_rewritten_from_old_notes():
    from src.advisor.facts import build_catalog
    from src.advisor.expert import validate_candidates
    report=_report()
    report['recorded_discipline']={'cluster_caps':[{'label':'科技','max_percent':60}], 'note':'曾用70%，现已按结构化数值替换', 'redemption_fees':[{'holding_days_below':7,'fee_percent':1.5}]}
    catalog=build_catalog(report)
    assert 'note' not in catalog['facts']['discipline']['data']
    assert 'redemption_fees' not in catalog['facts']['discipline']['data']
    assert catalog['action_conditions']['NO_ADD']['invalidation']
    row=_judgment(action='NO_ADD',trigger='任意条件')
    assert not validate_candidates({'judgments':[row]},catalog)[0]
    row.update(catalog['action_conditions']['NO_ADD'])
    assert validate_candidates({'judgments':[row]},catalog)[0]
