from dataclasses import replace
import pytest
from tests.test_advisor_comparative_reasoning import parse
from tests.test_advisor_forecast_contract import forecast


def reviewed(*, binding=True):
    from src.advisor.comparisons import review_candidates
    candidate = parse({'forecasts': [forecast()]})
    result = review_candidates(candidate, {'reviews': [{'id': 'fc1', 'supported': True, 'reason': '规则、来源和证据明确'}]}, review_id='review1')
    return replace(result, publication_binding={'publication_id': 'pub1', 'input_sha256': 'inputs1', 'evidence_sha256': 'evidence1',
        'body_sha256': 'body1', 'view_sha256': 'view1', 'committed': True}) if binding else result


def test_only_committed_hash_bound_reviewed_forecasts_publish_idempotently():
    from src.advisor.forecasts import publish_forecasts
    with pytest.raises(ValueError): publish_forecasts('pub1', parse({'forecasts': [forecast()]}), published_at='2026-10-19')
    with pytest.raises(ValueError): publish_forecasts('pub1', reviewed(binding=False), published_at='2026-10-19')
    result = publish_forecasts('pub1', reviewed(), published_at='2026-10-19')
    assert result == publish_forecasts('pub1', reviewed(), published_at='2026-10-19')
    assert result[0].execution_state == 'not_reported'
    assert result[0].forecast_id == 'fc1'
    with pytest.raises(ValueError): publish_forecasts('other', reviewed(), published_at='2026-10-19')


def test_review_denials_and_duplicate_votes_fail_closed():
    from src.advisor.comparisons import review_candidates
    from src.advisor.forecasts import publish_forecasts
    candidate = parse({'forecasts': [forecast()]})
    result = review_candidates(candidate, {'reviews': [{'id':'fc1','supported':True,'reason':'yes'}, {'id':'fc1','supported':True,'reason':'again'}]})
    assert not result.accepted_forecast_ids


def test_revision_append_keeps_original_resolution_and_failure_case():
    from src.advisor.forecasts import publish_forecasts, revise_forecast
    original = publish_forecasts('pub1', reviewed(), published_at='2026-10-19')[0]
    revised = revise_forecast(original, revised_at='2026-10-21', reason='出现重要反证', probability=None, evidence_refs=('counter',))
    assert original.version == 1 and revised.version == 2
    assert revised.original_terms_sha256 == original.original_terms_sha256
    assert revised.origin.to_dict() == original.origin.to_dict()
    assert revised.revisions[0]['reason'] == '出现重要反证'
    with pytest.raises(TypeError): revise_forecast(original, revised_at='2026-10-21', reason='换窗口', horizon_end='2027-01-01')


def test_reviewed_roundtrip_preserves_only_accepted_objects_and_detects_tampering():
    import copy
    from src.advisor.comparisons import ReviewedResearch
    from src.advisor.forecasts import publish_forecasts
    payload = reviewed().to_dict()
    restored = ReviewedResearch.from_dict(payload)
    assert publish_forecasts('pub1', restored, published_at='2026-10-19') == publish_forecasts('pub1', reviewed(), published_at='2026-10-19')
    tampered = copy.deepcopy(payload)
    tampered['forecasts'][0]['horizon_end'] = '2027-01-31'
    with pytest.raises(ValueError): ReviewedResearch.from_dict(tampered)
    missing = copy.deepcopy(payload)
    missing['accepted_forecast_ids'] = ['invented']
    with pytest.raises(ValueError): ReviewedResearch.from_dict(missing)
    with pytest.raises(ValueError): ReviewedResearch.from_dict({'forecasts': [forecast()]})
