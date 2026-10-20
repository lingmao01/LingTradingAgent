"""Behavioral contract: concise conclusion, full signal visibility, bound projection."""
import copy
import pytest
from src.advisor.report_view import build_report_view, validate_view
from src.advisor.delivery import render_advice

def sample():
    report={'as_of':'2026-10-19','snapshot_sha256':'frozen','portfolio':{},'holdings':[],
        'research_status':'unavailable','opportunity_scan':{'coverage_status':'INCOMPLETE'},'data_gaps':[]}
    signals=[]
    for symbol in ['000001.SH','000300.SH','399006.SZ','000688.SH']:
        for kind,state in [('BLOCK_ADD','TRUE'),('REDUCE_TRIGGER','FALSE'),('PERMIT_ADD','FALSE')]:
            signals.append({'id':symbol+kind,'signal_id':symbol+kind,'kind':kind,'state':state,'subject_id':symbol,'scope':symbol,'config_status':'CONFIGURED','conditions':[],'as_of':'2026-10-16'})
    return report,signals

def test_conclusion_answers_all_directions_and_does_not_bury_false_signals():
    report,signals=sample(); view=build_report_view(report,signals,[])
    summary=render_advice(view)
    assert '增仓' in summary and '减仓' in summary and '换仓' in summary
    assert '未触发' in summary and '研究' in summary
    assert len(view.to_dict()['signal_cards'])==12
    assert len(summary)<550
    assert '|' not in summary and '快照' not in summary
    assert '000001.SH' in render_advice(view,detail='full')
    assert validate_view(view.to_dict(),report,signals,[])
    altered=copy.deepcopy(view.to_dict());altered['signal_cards'][0]['state']='FALSE'
    assert not validate_view(altered,report,signals,[])

def test_new_reduction_cannot_be_masked_by_no_add_and_unknown_not_false():
    report,signals=sample();signals[1]['state']='TRUE';signals[2]['state']='UNKNOWN'
    text=render_advice(build_report_view(report,signals,[]))
    assert '减仓：已触发' in text
    assert '未知' in text or '不足' in text
