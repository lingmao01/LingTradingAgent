from tests.test_advisor_comparative_reasoning import parse
import pytest


def forecast():
    return {'id': 'fc1', 'subject_ids': ['outside'], 'proposed_at': '2026-10-19',
        'proposition': '官方四季度公告是否确认政策实施', 'horizon_start': '2026-10-20', 'horizon_end': '2026-12-31',
        'resolution_rule': '期限结束前官方公告明确说明已经实施则为1，否则为0；来源不可取得则未解析',
        'resolution_source': 'https://official.example/policy', 'conditions': ['政策未撤回'],
        'invalidation': '官方宣布撤回该政策', 'evidence_refs': ['support'], 'counter_evidence_refs': ['counter'],
        'probability': None, 'probability_basis': None, 'base_rate': None}


def test_optional_probability_and_fixed_future_resolution_contract():
    result = parse({'forecasts': [forecast()]})
    assert len(result.forecasts) == 1
    assert result.forecasts[0].probability is None
    assert result.forecasts[0].resolution_source == 'https://official.example/policy'


@pytest.mark.parametrize('change', ['missing_source', 'missing_rule', 'no_basis', 'quality_is_probability', 'after_horizon'])
def test_forecast_rejects_undefined_resolution_or_invented_probability(change):
    row = forecast()
    if change == 'missing_source': row['resolution_source'] = ''
    if change == 'missing_rule': row['resolution_rule'] = ''
    if change == 'no_basis': row['probability'] = 0.7
    if change == 'quality_is_probability':
        row['probability'] = 0.7
        row['probability_basis'] = {'kind': 'evidence_quality', 'description': 'quality 70%', 'evidence_refs': ['support']}
    if change == 'after_horizon': row['horizon_end'] = '2026-10-18'
    assert not parse({'forecasts': [row]}).forecasts


def test_subjective_probability_explicitly_uncalibrated_and_grounded():
    row = forecast()
    row['probability'] = .7
    row['probability_basis'] = {'kind': 'SUBJECTIVE_UNCALIBRATED', 'description': '根据支持和反证提出主观估计，未验证校准', 'evidence_refs': ['support', 'counter']}
    result = parse({'forecasts': [row]})
    assert result.forecasts[0].probability == .7


@pytest.mark.parametrize('source', ['https://made-up.example/answer', 'some invented future source'])
def test_resolution_source_must_already_be_a_verified_source(source):
    row = forecast()
    row['resolution_source'] = source
    assert not parse({'forecasts': [row]}).forecasts


def test_generation_uses_real_provider_interface_with_same_budget_and_reserves_review():
    import json
    from types import SimpleNamespace
    from src.advisor.budget_scope import debit_model
    from src.advisor.comparisons import build_research_candidates
    from src.advisor.research_budget import ResearchBudget
    from tests.test_advisor_comparative_reasoning import research_fixture
    inputs, bundle, subjects, _ = research_fixture()
    class Provider:
        def stream_chat(self, messages, **kwargs):
            debit_model()
            assert kwargs['timeout'] <= 55
            return SimpleNamespace(content=json.dumps({'forecasts':[forecast()]}))
    budget = ResearchBudget.from_limits()
    result = build_research_candidates(inputs, {}, {}, bundle, SimpleNamespace(subjects=subjects), Provider(), budget=budget)
    assert len(result.forecasts) == 1
    assert budget.receipt()['model_calls'] == 1
    assert budget.acquire_model('review') is True


def test_future_vintage_cannot_hide_in_probability_basis_refs():
    from src.advisor.comparisons import parse_candidates
    from tests.test_advisor_comparative_reasoning import research_fixture
    inputs, bundle, subjects, _ = research_fixture()
    bundle.items['future'] = {'id':'future', 'as_of':'2026-11-01', 'verified':True}
    row = forecast()
    row.update(probability=.7, probability_basis={'kind':'SUBJECTIVE_UNCALIBRATED','description':'未来资料', 'evidence_refs':['future']})
    result = parse_candidates({'forecasts':[row]}, inputs=inputs, bundle=bundle, subjects=subjects)
    assert not result.forecasts
