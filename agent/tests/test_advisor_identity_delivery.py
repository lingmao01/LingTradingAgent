"""Identity uncertainty survives the human report, not only hidden JSON metadata."""
from src.advisor.report_view import build_report_view

def test_identity_conflict_future_source_and_omitted_coverage_are_readable():
    collection={'candidates':[{'symbol':'001236.OF','claim_text':'跟踪关系线索','source_ref':'official profile',
        'published_at':'2030-01-01','fetched_at':'2026-10-19T10:00:00Z',
        'conflicts':[{'declared_tracking_index':'000300.SH','candidate_tracking_index':'000688.SH'}],
        'gaps':['future_publication','relationship_conflict']}],
        'gaps':[{'symbol':'018344.OF','reason':'holding_limit_reached'}],
        'coverage':[{'symbol':'001236.OF','status':'incomplete'}]}
    report={'as_of':'2026-10-19','snapshot_sha256':'snapshot','portfolio':{},'identity_candidate_collection':collection}
    view=build_report_view(report,[],[]).to_dict();full=view['full_text']
    for text in ('晚于本轮截止日','关系存在冲突','000300.SH','000688.SH','018344.OF','覆盖上限','incomplete','抓取时间不替代原文日期'):
        assert text in full
    assert view['evidence_groups']['identity_candidates']==collection
    assert '2030' not in view['summary_text']
