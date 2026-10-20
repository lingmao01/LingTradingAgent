from dataclasses import replace
import pytest
from tests.test_advisor_forecast_journal import reviewed


def record():
    from src.advisor.forecasts import publish_forecasts
    accepted = reviewed()
    origin = replace(accepted.candidates.forecasts[0], probability=.7, probability_basis={'kind':'SUBJECTIVE_UNCALIBRATED','description':'根据证据的主观未校准估计','evidence_refs':['support']})
    accepted = replace(accepted, candidates=replace(accepted.candidates, forecasts=(origin,)))
    return publish_forecasts('pub1', accepted, published_at='2026-10-19')[0]


def outcome(**kwargs):
    return {'value': 1, 'source': 'https://official.example/policy', 'observed_at':'2026-12-31',
        'resolved_at': '2027-01-01', 'available_at':'2027-01-01', **kwargs}


def test_brier_point_zero_nine_descriptive_groups_and_no_claim_of_skill():
    from src.advisor.forecasts import evaluate_forecasts
    row = record()
    result = evaluate_forecasts([row, row], {'fc1': outcome()}, as_of='2027-01-02')
    assert result.results[0]['brier'] == pytest.approx(.09)
    assert result.sample_count == 1
    assert result.calibration_status == 'DESCRIPTIVE_ONLY'
    assert result.groups[0]['count'] == 1 and result.groups[0]['base_rate'] == 1
    assert result.to_dict()['predictive_advantage'] is None


@pytest.mark.parametrize('change', ['early', 'source', 'window', 'future', 'lookahead', 'survivorship', 'selection', 'revised_data'])
def test_no_score_for_unresolved_or_shifted_or_biased_outcomes(change):
    from src.advisor.forecasts import evaluate_forecasts
    row, resolved, as_of = record(), outcome(), '2027-01-02'
    if change == 'early': as_of = '2026-11-01'
    if change == 'source': resolved['source'] = 'https://different.example'
    if change == 'window': resolved['observed_at'] = '2027-01-01'
    if change == 'future': resolved['available_at'] = '2027-01-03'
    if change == 'lookahead': resolved['lookahead_bias'] = True
    if change == 'survivorship': resolved['survivorship_bias'] = True
    if change == 'selection': resolved['parameter_selection_bias'] = True
    if change == 'revised_data': resolved['original_vintage_available'] = False
    result = evaluate_forecasts([row], {'fc1': resolved}, as_of=as_of)
    assert result.sample_count == 0
    assert result.results[0]['brier'] is None


def test_missing_outcome_remains_unresolved_and_qualitative_is_not_brier_scored():
    from src.advisor.forecasts import evaluate_forecasts, publish_forecasts
    row = publish_forecasts('pub1', reviewed(), published_at='2026-10-19')[0]
    assert evaluate_forecasts([row], {}, as_of='2027-01-02').results[0]['status'] == 'UNRESOLVED'
    result = evaluate_forecasts([row], {'fc1':outcome()}, as_of='2027-01-02')
    assert result.results[0]['status'] == 'RESOLVED' and result.sample_count == 0


def test_republication_of_same_original_event_does_not_inflate_sample_count():
    from src.advisor.forecasts import evaluate_forecasts
    first = record()
    repeated = replace(first, id='another_record', publication_id='pub2', published_at='2026-10-20')
    result = evaluate_forecasts([first, repeated], {'fc1':outcome()}, as_of='2027-01-02')
    assert result.sample_count == 1
    assert any(row['status'] == 'NOT_SCORABLE' and 'duplicate' in row['reason'] for row in result.results)
