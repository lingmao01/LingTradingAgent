import json
from dataclasses import replace
from src.advisor.forecasts import resolve_forecast_outcomes, evaluate_forecasts
from tests.test_advisor_forecast_evaluation import record

def test_declared_numeric_outcome_uses_exact_source_date_and_does_not_resolve_free_text():
    from src.advisor.comparisons import digest
    row=record()
    origin=replace(row.origin,resolution_source='000300.SH',resolution_rule=json.dumps({'field':'close','operator':'>','threshold':100,'at':'horizon_end'}))
    row=replace(row,origin=origin,original_terms_sha256=digest(origin))
    calls=[]
    def market(**args):
        calls.append(args);return {'000300.SH':[{'date':origin.horizon_end,'close':110}], '_provenance':{'000300.SH':{'source':'fixture','quality':'historical_requested','finality':'closed_session','fetched_at':'2027-01-02'}}}
    result=resolve_forecast_outcomes([row],as_of='2027-01-02',market_fetcher=market)
    assert result[row.id]['value']==1
    evaluated=evaluate_forecasts([row],result,as_of='2027-01-02')
    assert evaluated.results[0]['status']=='RESOLVED'
    assert len(calls)==1
    assert resolve_forecast_outcomes([row],as_of='2026-10-19',market_fetcher=market)=={}
    free=replace(row,origin=replace(origin,resolution_rule='政策是否落地'))
    assert resolve_forecast_outcomes([free],as_of='2027-01-02',market_fetcher=market)=={}
