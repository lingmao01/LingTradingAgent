"""Public loop fault injection cannot rewrite independently computed signals."""
import pytest
from tests.test_advisor_signal_acceptance import (run_controlled, judgment, assert_snapshot_gates)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['generation_timeout','review_timeout','bad_json','reject_all'])
def test_model_failures_keep_all_frozen_gates_without_unreviewed_draft_leakage(tmp_path,monkeypatch,mode):
    run = run_controlled(tmp_path/'fault',monkeypatch,model_mode=mode)
    healthy = run_controlled(tmp_path/'healthy',monkeypatch)
    assert run.report['signal_assessments']==healthy.report['signal_assessments']
    assert_snapshot_gates(run.report)
    assert run.report['expert_analysis']['accepted']==[]
    assert '保留现金并核验新方向' not in run.result['content']
    assert run.result['release_path']=='structured_assessment'
    assert '减仓：未触发' in run.result['content']
    assert run.llm.calls<=3
    assert run.report['research_budget']['model_calls']==run.llm.calls
    assert run.report['reviewed_research']['comparisons']==[]
    if mode in {'generation_timeout','review_timeout'}:
        assert run.result['execution_status']=='failed'
        assert run.result['completion_status']=='partial'


@pytest.mark.parametrize('mutation', ['made_up_condition','cross_subject_condition','new_threshold'])
def test_rule_judgments_cannot_invent_or_borrow_another_subject_gate(tmp_path,monkeypatch,mutation):
    def document(catalog):
        condition = next(iter(catalog['conditions']))
        scope = catalog['conditions'][condition]['subject_id']
        changes = {'id':'forged_rule','scope':scope,'origin':'RULE','condition_refs':[condition],
            'evidence_refs':['signal:'+catalog['conditions'][condition]['signal_id']]}
        if mutation=='made_up_condition': changes['condition_refs']=['made_up_condition']
        elif mutation=='cross_subject_condition': changes['scope']='000688.SH' if scope!='000688.SH' else '000300.SH'
        else:
            changes['trigger']='收盘大于99999时才能买入。'
        return {'judgments':[judgment(),judgment(**changes)]}
    run = run_controlled(tmp_path,monkeypatch,document=document)
    assert_snapshot_gates(run.report)
    assert [r['id'] for r in run.report['expert_analysis']['accepted']]==['controlled_hold']
    assert any(r['id']=='forged_rule' for r in run.report['expert_analysis']['rejected'])
    assert '收盘大于99999时才能买入' not in run.result['advisor_report']['full_text']


@pytest.mark.parametrize('source_mode', ['outage','missing_original','stale'])
def test_source_failures_preserve_rules_and_distinguish_incomplete_from_no_opportunity(tmp_path,monkeypatch,source_mode):
    run = run_controlled(tmp_path,monkeypatch,source_mode=source_mode)
    assert_snapshot_gates(run.report)
    assert run.report['opportunity_scan']['coverage_status']=='INCOMPLETE'
    assert '不能据此认定没有机会' in run.result['content']
    assert run.report['holdings'][0]['value']==100
    assert run.report['research_budget']['source_calls']==len(run.calls)<=24
    assert any(row['scope']=='candidate' for row in run.report['research_evidence']['coverage'])


def test_lower_loop_budget_cannot_publish_unreviewed_model_opinion(tmp_path,monkeypatch):
    run = run_controlled(tmp_path,monkeypatch,max_iterations=1)
    assert_snapshot_gates(run.report)
    assert run.llm.calls==0
    assert not run.report['expert_analysis']['accepted']
    assert run.report['research_budget']['max_model_calls']==1
    assert '保留现金并核验新方向' not in run.result['content']


def test_shared_source_budget_stops_dispatch_and_preserves_honest_scan_gap(tmp_path,monkeypatch):
    from src.advisor.research_budget import ResearchBudget
    original=ResearchBudget.from_limits
    monkeypatch.setattr(ResearchBudget,'from_limits',lambda **kwargs:original(max_source_calls=1,**kwargs))
    run=run_controlled(tmp_path,monkeypatch)
    assert_snapshot_gates(run.report)
    assert len(run.calls)==run.report['research_budget']['source_calls']==1
    assert run.report['opportunity_scan']['coverage_status']=='INCOMPLETE'
    assert run.llm.calls<=3
    assert '不能据此认定没有机会' in run.result['content']
