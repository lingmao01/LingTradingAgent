"""Independent integration regression probes; all runtime writes use test sandbox."""
from copy import deepcopy
import pytest

from src.advisor.decisions import decide
from src.advisor.delivery import validate_delivery
from src.advisor.report_view import build_report_view
from tests.test_advisor_signal_acceptance import run_controlled, SOURCE_URL
from tests.test_advisor_personal_research_acceptance import joint_document

pytestmark = pytest.mark.unit


def reduction_document(catalog):
    doc = joint_document(catalog)
    holding = doc['comparisons'][0]['subject_ids'][0]
    doc['proposals'][0].update(id='reduce_reviewed', action='REDUCE', subject_ids=[holding])
    return doc


def rich_document(catalog):
    doc = reduction_document(catalog)
    ref = doc['comparisons'][0]['evidence_refs'][0]
    subjects = doc['comparisons'][0]['subject_ids']
    doc['forecasts'] = [{'id':'future_policy', 'subject_ids':subjects,
        'proposed_at':'2026-10-19', 'proposition':'官方在期限内确认新的黄金投资政策实施',
        'horizon_start':'2026-10-20', 'horizon_end':'2026-12-31',
        'resolution_rule':'官方原文明确确认实施则成立，原文缺失则保持未解析',
        'resolution_source':SOURCE_URL, 'conditions':['政策未撤回'],
        'invalidation':'官方明确撤回政策', 'evidence_refs':[ref],
        'counter_evidence_refs':[], 'probability':None, 'probability_basis':None, 'base_rate':None}]
    doc['scenarios'] = [{'id':'policy_branch', 'subject_ids':subjects,
        'horizon_start':'2026-10-20', 'horizon_end':'2026-12-31',
        'assumptions':['政策执行延迟'], 'path':'政策延迟导致需求兑现推后',
        'impact':'持仓外方向的机会成本仍待比较', 'invalidation':'原文证实政策已开始实施',
        'evidence_refs':[ref], 'counter_evidence_refs':[]}]
    return doc


def with_view(run):
    report = deepcopy(run.report)
    report['advisor_report'] = build_report_view(report,report['signal_assessments'],report['action_assessments']).to_dict()
    return report


def test_accepted_independent_reduction_is_not_hidden_by_false_technical_trigger(tmp_path,monkeypatch):
    run = run_controlled(tmp_path,monkeypatch,document=reduction_document)
    action = next(a for a in run.report['action_assessments'] if a.get('proposal_id')=='reduce_reviewed')
    assert action['eligibility']=='ELIGIBLE'
    text = run.result['advisor_report']['summary_text']
    assert any(phrase in text for phrase in ('研究建议减仓','研究减仓','研究：减仓','独立减仓','独立研究减仓')), text


def test_complete_export_contains_reviewed_forecast_scenario_and_execution_details(tmp_path,monkeypatch):
    run = run_controlled(tmp_path,monkeypatch,document=rich_document)
    reviewed = run.report['reviewed_research']
    assert len(reviewed['forecasts'])==1 and len(reviewed['scenarios'])==1
    full = run.result['advisor_report']['full_text']
    forecast, scenario = reviewed['forecasts'][0], reviewed['scenarios'][0]
    for field in ('proposition','resolution_rule','resolution_source','horizon_end'):
        assert forecast[field] in full, (field, full)
    assert scenario['path'] in full
    assert 'reduce_reviewed' in full or '已审查研究' in full
    assert '费率' in full or '费用' in full


@pytest.mark.parametrize('mutation',['action_eligibility','action_input','research_input','research_evidence','signal_subject','signal_applicability'])
def test_delivery_rejects_cross_snapshot_or_requalified_action_tampering(tmp_path,monkeypatch,mutation):
    run = run_controlled(tmp_path,monkeypatch,document=reduction_document)
    report = with_view(run)
    assert validate_delivery(report,decide(report))
    if mutation=='action_eligibility':
        report['action_assessments'][0]['eligibility']='ELIGIBLE'
        report['action_assessments'][0]['blocked_by']=[]
    elif mutation=='action_input':
        report['action_assessments'][0]['input_sha256']='other-input-snapshot'
    elif mutation=='signal_subject':
        report['signal_assessments'][0]['subject']={'id':'entire-account','kind':'account','name':'全账户'}
        report['signal_assessments'][0]['scope']='account'
    elif mutation=='signal_applicability':
        report['signal_assessments'][0].update(applicability='RESOLVED',holding_ids=['unrelated-holding'],mapping_refs=['invented-mapping'])
    else:
        from src.advisor.comparisons import digest
        key='input_sha256' if mutation=='research_input' else 'evidence_sha256'
        report['reviewed_research'][key]='other-snapshot'
        reviewed=report['reviewed_research']
        reviewed['reviewed_sha256']=digest({k:v for k,v in reviewed.items() if k not in {'publication_binding','reviewed_sha256'}})
    # A self-consistent view/hash is insufficient: trusted inputs must rederive actions.
    report['advisor_report']=build_report_view(report,report['signal_assessments'],report['action_assessments']).to_dict()
    assert not validate_delivery(report,decide(report)), mutation


def test_unapproved_raw_judgment_cannot_leak_through_structured_view(tmp_path,monkeypatch):
    from src.advisor.expert import approved
    run = run_controlled(tmp_path,monkeypatch)
    report = deepcopy(run.report)
    report['expert_analysis']['accepted'][0]['summary']='未经引用的新判断：确定目标价99999999'
    assert approved(report)==[]
    view=build_report_view(report,report['signal_assessments'],report['action_assessments']).to_dict()
    assert '未经引用的新判断：确定目标价99999999' not in view['summary_text']+view['full_text']


def test_judgment_review_cannot_approve_comparison_with_colliding_id(tmp_path,monkeypatch):
    import json
    from tests.test_advisor_signal_acceptance import FixtureModel, _Response
    original=FixtureModel.stream_chat
    def only_review_judgment(self,messages,*args,**kwargs):
        payload=json.loads(messages[-1]['content'])
        if 'judgments' not in payload:
            return original(self,messages,*args,**kwargs)
        self.payloads.append(payload);self.calls+=1;self.tools_history.append(kwargs.get('tools'))
        # The one explicit review applies to the prose judgment, not a comparison.
        return _Response(content=json.dumps({'reviews':[{'id':'controlled_hold','supported':True,
            'reason':'The judgment on preserving cash is supported; comparative objects were not reviewed.'}]}))
    monkeypatch.setattr(FixtureModel,'stream_chat',only_review_judgment)
    def colliding_document(catalog):
        doc=joint_document(catalog)
        doc['comparisons'][0]['id']='controlled_hold'
        doc['proposals']=[]
        return doc
    run=run_controlled(tmp_path,monkeypatch,document=colliding_document)
    assert not run.report['reviewed_research']['accepted_comparison_ids']

def test_summary_surfaces_actual_reviewed_invalidation_instead_of_generic_next_check(tmp_path,monkeypatch):
    run=run_controlled(tmp_path,monkeypatch,document=reduction_document)
    opinion=run.report['expert_analysis']['accepted'][0]
    assert opinion['invalidation'] in run.result['advisor_report']['summary_text']
