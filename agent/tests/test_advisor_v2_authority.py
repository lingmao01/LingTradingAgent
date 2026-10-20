"""Legacy descriptive gates cannot override the frozen v2 signal authority."""
from src.advisor.decisions import decide
from src.advisor.facts import build_catalog

def test_unknown_v2_signal_cannot_be_upgraded_by_legacy_open_gate():
    report={'assessment_schema_version':2,'as_of':'2026-10-19','snapshot_sha256':'frozen','portfolio':{},
        'regime':[{'symbol':'000300.SH','gates':[{'action':'NO ADD','state':'OPEN'}]}],
        'signal_assessments':[{'signal_id':'signal:strict','rule_id':'r','effect':'BLOCK_ADD','state':'UNKNOWN',
            'config_status':'CONFIGURED','subject':{'id':'000300.SH','kind':'index','symbol':'000300.SH'},'scope':'market_reference','conditions':[]}],
        'action_assessments':[],'research_subjects':{}}
    decision=decide(report)
    assert decision['stance']=='LIMITED_JUDGMENT'
    assert not any(c['action']=='NO_ADD' for c in decision['cards'])
    market=build_catalog(report)['facts']['market:000300.SH']['data']
    assert 'gates' not in market
