"""Behavioral contracts for reviewed comparative research, never a holdings whitelist."""
import copy
from types import SimpleNamespace

import pytest


def research_fixture():
    inputs = SimpleNamespace(input_sha256='inputs1', as_of='2026-10-19')
    bundle = SimpleNamespace(sha256='evidence1', items={
        'support': {'id': 'support', 'category': 'earnings', 'as_of': '2026-10-18', 'text': '盈利增长，但价格需要独立验证'},
        'counter': {'id': 'counter', 'category': 'policy', 'verified': True, 'usable': True, 'url': 'https://official.example/policy', 'as_of': '2026-10-18', 'text': '政策尚待兑现'},
    })
    subjects = {'holding': {'id': 'holding'}, 'outside': {'id': 'outside', 'availability': 'UNKNOWN'}}
    comparison = {'id': 'cmp1', 'subject_ids': ['holding', 'outside'],
        'horizon_start': '2026-10-20', 'horizon_end': '2026-12-31', 'objective': '比较共同期限内的风险与机会成本',
        'alternatives': [{'id': key, 'action': action, 'subject_ids': ids, 'tradeoff': text}
            for key, action, ids, text in [('keep', 'HOLD', ['holding'], '保留原敞口并承担机会成本'),
                ('add', 'ADD', ['holding'], '增加共同风险'), ('cash', 'REDUCE_CASH', ['holding'], '放弃部分反弹并产生费用'),
                ('switch', 'ROTATE', ['holding', 'outside'], '新方向准入及双边费用待核对')]],
        'evidence_refs': ['support'], 'counter_evidence_refs': ['counter'],
        'assumptions': ['政策仍待执行'], 'invalidation': '后续兑现证据改变收益路径',
        'conclusion': 'conditional', 'preferred_alternative_id': 'cash',
        'reasoning': '研究性减仓可以降低共同风险，独立于技术规则是否触发',
        'fragility': {'small_change_reverses': True, 'explanation': '执行改善会使维持原仓更有吸引力'},
        'risk_analysis': [{'kind': 'MECHANISM', 'evidence_refs': ['support'], 'explanation': '共同风险是机制推论，并非实测相关性'}]}
    return inputs, bundle, subjects, comparison


def parse(document):
    from src.advisor.comparisons import parse_candidates
    inputs, bundle, subjects, _ = research_fixture()
    return parse_candidates(document, inputs=inputs, bundle=bundle, subjects=subjects)


def test_four_alternatives_allow_independent_reduce_and_outside_research():
    _, _, _, row = research_fixture()
    result = parse({'comparisons': [row]})
    assert result.rejected == ()
    assert result.comparisons[0].conclusion == 'conditional'
    assert result.comparisons[0].to_dict()['fragility']['small_change_reverses'] is True
    assert result.subjects['outside']['availability'] == 'UNKNOWN'
    row['reasoning'] = 'mutated'
    assert result.comparisons[0].reasoning != 'mutated'


@pytest.mark.parametrize('change', ['horizon', 'missing_cash', 'unknown_ref', 'future_vintage', 'fake_return', 'unmarked_fragility'])
def test_comparisons_reject_noncomparable_or_unsupported_claims(change):
    from src.advisor.comparisons import parse_candidates
    inputs, bundle, subjects, row = research_fixture()
    if change == 'horizon': row['alternatives'][0]['horizon_end'] = '2027-01-01'
    if change == 'missing_cash': row['alternatives'].pop(2)
    if change == 'unknown_ref': row['evidence_refs'] = ['invented']
    if change == 'future_vintage': bundle.items['support']['as_of'] = '2026-10-21'
    if change == 'fake_return': row['alternatives'][0]['expected_return'] = 0.2
    if change == 'unmarked_fragility': row['fragility']['explanation'] = ''
    result = parse_candidates({'comparisons': [row]}, inputs=inputs, bundle=bundle, subjects=subjects)
    assert not result.comparisons
    assert result.rejected


@pytest.mark.parametrize('text', ['盈利增长意味着价格低估', '政策宣布说明政策已经兑现', '市场共识未知但业绩超预期', '均线上穿意味着独立买入优势'])
def test_no_evidence_category_cannot_support_stronger_market_claim(text):
    _, _, _, row = research_fixture()
    row['reasoning'] = text
    assert not parse({'comparisons': [row]}).comparisons


def test_empty_structured_arrays_preserve_no_forecast_and_do_not_reject_legacy_judgments():
    result = parse({'judgments': [{'id': 'legacy'}], 'comparisons': [], 'forecasts': []})
    assert result.forecasts == () and result.rejected == ()


def test_common_risk_separates_disclosed_overlap_from_mechanism():
    _, _, _, row = research_fixture()
    row['risk_analysis'] = [{'kind': 'DISCLOSED_OVERLAP', 'evidence_refs': ['support'], 'explanation': '底层重叠'}]
    assert not parse({'comparisons': [row]}).comparisons


def test_precise_future_return_hidden_in_prose_is_rejected():
    _, _, _, row = research_fixture()
    row['reasoning'] = '预计收益20%，换仓可确定获得更高回报'
    assert not parse({'comparisons': [row]}).comparisons


def test_cross_bundle_vintage_and_unverified_sources_cannot_back_research():
    from src.advisor.comparisons import parse_candidates
    inputs, bundle, subjects, row = research_fixture()
    bundle.items['support']['usable'] = False
    assert not parse_candidates({'comparisons':[row]}, inputs=inputs, bundle=bundle, subjects=subjects).comparisons


def test_proposal_requires_a_reviewed_comparison_and_remains_draft():
    from src.advisor.comparisons import review_candidates
    _, _, _, row = research_fixture()
    proposal = {'id':'prop1', 'action':'REDUCE', 'subject_ids':['holding'], 'origin':'RESEARCH_JUDGMENT',
        'evidence_refs':['support'], 'condition_refs':[], 'comparison_id':'cmp1', 'parameters':{}}
    candidates = parse({'comparisons':[row], 'proposals':[proposal]})
    assert len(candidates.proposals) == 1
    assert not hasattr(candidates.proposals[0], 'eligibility')
    rejected_comparison = review_candidates(candidates, {'reviews':[{'id':'prop1','supported':True,'reason':'supports reduced risk'}]})
    assert rejected_comparison.accepted_proposal_ids == ()
    accepted = review_candidates(candidates, {'reviews':[{'id':'prop1','supported':True,'reason':'supported research'},
        {'id':'cmp1','supported':True,'reason':'comparison valid'}]})
    assert accepted.accepted_proposal_ids == ('prop1',)
