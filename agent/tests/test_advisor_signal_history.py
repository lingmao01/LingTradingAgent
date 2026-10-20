from src.advisor.signal_history import compare_signals

def s(state='FALSE',day='2026-10-16',version='v1',value=100):
    return {'signal_id':'s','rule_id':'r','rule_version':version,'effect':'REDUCE_TRIGGER','subject':{'id':'000300.SH'},'state':state,'evidence_as_of':day,'finality':'closed_session','conditions':[{'actual':value}]}

def test_repeated_close_and_intraday_fetch_changes_never_make_new_trigger():
    prior=s('TRUE'); current={**prior,'fetched_at':'later'}
    assert compare_signals([current],[prior])[0]['transition']=='STILL_TRIGGERED'
    assert compare_signals([s('TRUE','2026-10-19')],[s()])[0]['transition']=='NEW_TRIGGER'
    assert compare_signals([s('FALSE','2026-10-19')],[s('TRUE')])[0]['transition']=='CLEARED'

def test_unknown_versions_and_revision_do_not_manufacture_new_signal():
    assert compare_signals([s('TRUE')],None)[0]['transition']=='NO_BASELINE'
    assert compare_signals([s('TRUE','2026-10-19')],[s('UNKNOWN')])[0]['transition']=='NOT_COMPARABLE'
    assert compare_signals([s('TRUE',version='v2')],[s()])[0]['transition']=='NOT_COMPARABLE'
    assert compare_signals([s('TRUE',value=101)],[s()])[0]['transition']=='NOT_COMPARABLE'
