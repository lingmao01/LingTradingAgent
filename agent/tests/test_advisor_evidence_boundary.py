import pytest

from src.agent.grounding import GroundingLedger

pytestmark = pytest.mark.unit


def test_assessment_calculations_are_not_observed_market_prices(tmp_path):
    import json
    ledger = GroundingLedger(run_dir=tmp_path, user_message='分析001236.OF持仓')
    ledger.ingest_tool_result(tool_name='get_portfolio_assessment', arguments={}, call_id='snapshot', success=True,
        result=json.dumps({'ok': True, 'data': {'holdings': [{'symbol': '001236.OF', 'value': 999}]}, 'evidence': {
            '001236.OF': {'symbol': '001236.OF', 'field': 'unit_nav', 'currency': 'CNY', 'source': 'eastmoney', 'as_of': '2026-10-15', 'quality': 'latest_disclosed', 'history': [{'date': '2026-10-15', 'value': 2}]},
        }}))
    records = [r for r in ledger._evidence if r.symbol == '001236.OF']
    assert any(r.field == 'unit_nav' and r.value == 2 for r in records)
    assert all(r.value != 999 for r in records)


def test_terms_history_keeps_the_old_terms_source(tmp_path):
    from src.fund_identity import FundIdentityStore
    store = FundIdentityStore(tmp_path / 'identities.json')
    store.record('001236.OF', source_ref='old prospectus', as_of='2026-01-01', terms={'effective_from': '2026-01-01'})
    latest = store.record('001236.OF', source_ref='new prospectus', as_of='2026-10-15', terms={'effective_from': '2026-10-15'})
    assert latest['terms_history'][0]['source_ref'] == 'old prospectus'
    assert latest['terms_history'][0]['as_of'] == '2026-01-01'
