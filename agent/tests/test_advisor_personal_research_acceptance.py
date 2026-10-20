"""Controlled integrated research acceptance; live-source/model proof is separate."""
import json
import pytest
from tests.test_advisor_signal_acceptance import run_controlled, judgment, assert_snapshot_gates
from tests.test_advisor_comparative_reasoning import research_fixture

pytestmark = pytest.mark.unit


def joint_document(catalog, *, prefer='cash', invalid=None):
    outside = next(key for key,row in catalog['subjects'].items() if row.get('name')=='黄金')
    holding = next(key for key,row in catalog['subjects'].items() if row.get('canonical_symbol')=='001236.OF')
    refs = [key for key,row in catalog['facts'].items() if row['kind']=='retrieved_evidence' and '黄金' in str(row)]
    assert refs, 'fixture original must survive acquisition and enter the actual model catalog'
    _,_,_,comparison = research_fixture()
    comparison.update(subject_ids=[holding,outside], horizon_start='2026-10-20',horizon_end='2026-12-31',
        evidence_refs=refs[:1],counter_evidence_refs=[],preferred_alternative_id=prefer,
        reasoning='政策兑现仍待核验，现金保留调整选择权；黄金可作为持仓外观察方向。')
    for alternative in comparison['alternatives']:
        alternative['subject_ids']=[holding,outside] if alternative['action']=='ROTATE' else [holding]
    comparison['risk_analysis']=[]
    proposal={'id':'rotation1','action':'ROTATE','subject_ids':[holding,outside],
        'origin':'RESEARCH_JUDGMENT','evidence_refs':refs[:1], 'condition_refs':[],
        'comparison_id':'cmp1','parameters':{}}
    if invalid=='subject': proposal['subject_ids']=[holding,'invented_outside']
    if invalid=='evidence': comparison['evidence_refs']=['invented_evidence']
    document={'judgments':[judgment()], 'comparisons':[comparison],'proposals':[proposal]}
    return document


def test_public_loop_discovers_unheld_direction_without_promoting_unknown_access(tmp_path,monkeypatch):
    run = run_controlled(tmp_path,monkeypatch)
    assert_snapshot_gates(run.report)
    scan = run.report['opportunity_scan']
    gold = next(row for row in scan['candidates'] if row['subject']['name']=='黄金')
    assert gold['subject']['canonical_symbol']=='' and gold['subject']['availability']=='UNKNOWN'
    assert gold['maturity']=='WATCH'
    assert 'market_access_unknown' in gold['key_gaps']
    assert gold['subject']['id'] in run.llm.payloads[0]['catalog']['subjects']
    assert gold['subject']['id'] not in {row['id'] for row in run.report['holdings']}
    assert run.report['personal_context']['known'].get('investment_horizon') is None
    assert run.book.load_account()['positions'][0]['symbol']=='001236.OF'
    assert len(run.book.load_account()['positions'])==1
    assert all(name!='position_book' for name,_ in run.calls)
    queries=json.dumps([args for name,args in run.calls if name=='web_search'],ensure_ascii=False)
    assert 'cash' not in queries and 'quantity' not in queries


def test_joint_comparison_proposal_is_reviewed_together_then_requalified_with_same_inputs(tmp_path,monkeypatch):
    run = run_controlled(tmp_path,monkeypatch,document=joint_document)
    assert_snapshot_gates(run.report)
    reviewed=run.report['reviewed_research']
    assert reviewed['accepted_comparison_ids']==['cmp1']
    assert reviewed['accepted_proposal_ids']==['rotation1']
    assert len(run.llm.payloads)==2
    reviewed_payload=run.llm.payloads[-1]['research_candidates']
    assert reviewed_payload['comparisons'][0]['id']=='cmp1'
    assert reviewed_payload['proposals'][0]['id']=='rotation1'
    assert reviewed['input_sha256']==run.report['assessment_input_sha256']
    assert reviewed['evidence_sha256']==run.report['research_evidence']['bundle_hash']
    rotation=next(row for row in run.report['action_assessments'] if row.get('proposal_id')=='rotation1')
    assert rotation['origin']=='RESEARCH_JUDGMENT'
    assert rotation['eligibility'] not in {'ELIGIBLE','SUPPORTED'}, 'unknown identity/access must block executable rotation'
    assert run.result['advisor_report']['evidence_groups']['comparisons'][0]['id']=='cmp1'


@pytest.mark.parametrize('invalid',['subject','evidence'])
def test_forged_candidate_or_evidence_cannot_enter_review_or_public_actions(tmp_path,monkeypatch,invalid):
    run=run_controlled(tmp_path,monkeypatch,document=lambda catalog:joint_document(catalog,invalid=invalid))
    reviewed=run.report['reviewed_research']
    assert not reviewed['accepted_proposal_ids']
    assert run.llm.payloads[-1].get('research_candidates',{}).get('proposals',[])==[]
    if invalid=='evidence': assert not reviewed['accepted_comparison_ids']
    assert 'rotation1' not in [row.get('proposal_id') for row in run.report['action_assessments']]
    assert all('invented_outside' not in json.dumps(row) for row in run.result['advisor_report']['action_cards'])
    assert_snapshot_gates(run.report)


def test_review_denial_of_comparison_cannot_be_bypassed_by_proposal_approval(tmp_path,monkeypatch):
    from tests.test_advisor_signal_acceptance import FixtureModel, _Response
    original=FixtureModel.stream_chat
    def partial_review(self,messages,*args,**kwargs):
        payload=json.loads(messages[-1]['content'])
        if 'judgments' not in payload:return original(self,messages,*args,**kwargs)
        self.payloads.append(payload);self.calls+=1;self.tools_history.append(kwargs.get('tools'))
        return _Response(content=json.dumps({'reviews':[
            {'id':'controlled_hold','supported':True,'reason':'supported account judgment'},
            {'id':'cmp1','supported':False,'reason':'counterevidence incomplete'},
            {'id':'rotation1','supported':True,'reason':'proposal alone cannot authorize comparison'}]}))
    monkeypatch.setattr(FixtureModel,'stream_chat',partial_review)
    run=run_controlled(tmp_path,monkeypatch,document=joint_document)
    assert run.report['reviewed_research']['accepted_comparison_ids']==[]
    assert run.report['reviewed_research']['accepted_proposal_ids']==[]
    assert run.report['expert_analysis']['accepted'][0]['id']=='controlled_hold'
    assert run.result['advisor_report']['evidence_groups']['comparisons']==[]
    assert_snapshot_gates(run.report)


def test_controlled_counterevidence_changes_comparison_but_never_the_rule_snapshot(tmp_path,monkeypatch):
    # The scripted model selects from actually acquired evidence; this checks
    # integration, not an assertion that a real model will reason identically.
    def respond(catalog):
        favorable=any('政策兑现证据得到改善' in str(row) for row in catalog['facts'].values())
        document=joint_document(catalog,prefer='switch' if favorable else 'keep')
        if not favorable:
            document['comparisons'][0]['reasoning']='政策执行仍待验证，保留原仓并等待新证据，避免付出无证据支持的换仓费用。'
            document['proposals'][0].update(action='HOLD_REVIEW',subject_ids=[document['comparisons'][0]['subject_ids'][0]])
        return document
    favorable=run_controlled(tmp_path/'before',monkeypatch,source_mode='favorable',document=respond)
    counter=run_controlled(tmp_path/'after',monkeypatch,source_mode='healthy',document=respond)
    assert favorable.report['reviewed_research']['comparisons'][0]['preferred_alternative_id']=='switch'
    assert counter.report['reviewed_research']['comparisons'][0]['preferred_alternative_id']=='keep'
    assert favorable.report['reviewed_research']['proposals'][0]['action']=='ROTATE'
    assert counter.report['reviewed_research']['proposals'][0]['action']=='HOLD_REVIEW'
    assert_snapshot_gates(favorable.report);assert_snapshot_gates(counter.report)
    assert favorable.report['research_evidence']['bundle_hash']!=counter.report['research_evidence']['bundle_hash']


def test_contradictory_originals_remain_separate_with_an_explicit_conflict(tmp_path,monkeypatch):
    run=run_controlled(tmp_path,monkeypatch,source_mode='contradictory')
    evidence=run.report['research_evidence']
    assert evidence['conflicts']
    assert all(row['status']=='CHECK_OBJECT_TIME_UNIT_AND_VINTAGE' and row['resolution'] is None for row in evidence['conflicts'])
    assert any('政策执行仍待验证' in str(row['value']) for row in evidence['items'])
    assert any('政策兑现证据存在相反结果' in str(row['value']) for row in evidence['items'])
    assert_snapshot_gates(run.report)
