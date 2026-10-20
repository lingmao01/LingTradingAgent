"""Published advice is auditable; paper baselines never become realized trades."""
import copy

import pandas as pd
import pytest

from tests.test_advisor_decisions import _report

pytestmark = pytest.mark.unit


def test_publication_requires_valid_final_contract_and_is_idempotent(tmp_path):
    from src.advisor.decisions import decide
    from src.advisor.journal import PublishedAdviceStore
    store = PublishedAdviceStore(tmp_path / 'published.json')
    report = _report()
    decision = decide(report)
    first = store.publish(run_id='one', report=report, decision=decision, content='reviewed content', delivery={'release_path': 'structured_assessment', 'completion_status': 'partial'}, published_at='2026-10-15T10:00:00+08:00')
    assert store.publish(run_id='one', report=report, decision=decision, content='reviewed content', delivery={'release_path': 'structured_assessment', 'completion_status': 'partial'})['id'] == first['id']
    assert len(store.list()) == 1
    assert first['execution_state'] == 'not_reported'
    assert first['review_protocol']['observation_horizons'] == [5, 20]
    fake = copy.deepcopy(decision)
    fake['cards'][0]['action'] = 'ADD'
    with pytest.raises(ValueError):
        store.publish(run_id='bad', report=report, decision=fake, content='bad', delivery={'release_path': 'structured_assessment'})
    with pytest.raises(ValueError):
        store.publish(run_id='failed', report=report, decision=decision, content='', delivery={'release_path': 'fallback'})
    assert len(store.list()) == 1


def test_more_than_eight_due_entries_are_page_readable(tmp_path):
    from src.advisor.decisions import decide
    from src.advisor.journal import PublishedAdviceStore
    store = PublishedAdviceStore(tmp_path / 'published.json')
    report = _report()
    for index in range(13):
        store.publish(run_id=str(index), report=report, decision=decide(report), content='valid', delivery={'release_path': 'structured_assessment'}, published_at='2026-10-01T10:00:00+08:00')
    assert len(store.list(due_as_of='2026-10-16')) == 13
    assert len(store.list(due_as_of='2026-10-02')) == 0
    assert len(store.page(offset=10, limit=5, due_as_of='2026-10-16')['entries']) == 3


def test_paper_comparison_is_time_ordered_cost_adjusted_and_not_realized():
    from src.advisor.evaluation import compare_baselines
    days = pd.bdate_range('2026-09-01', periods=31)
    histories = {'AAA': [{'date': str(day.date()), 'value': 1 + i / 100} for i, day in enumerate(days)]}
    result = compare_baselines(histories, {'AAA': 1}, published_as_of='2026-09-01', cutoff='2026-10-16', cost_bps=100, benchmark=histories['AAA'])
    assert result['start_date'] == '2026-09-02'  # next observation after publication
    assert result['realized_execution_performance'] is None
    window = result['horizons']['20']
    assert window['hold']['ending_wealth'] < 1.21 / 1.01  # entry/terminal cost included
    assert set(window) == {'hold', 'periodic_invest', 'rebalance', 'benchmark'}
    assert result['assumptions']['cost_bps'] == 100
    assert result['assumptions']['cash_yield'] == 0
    assert result['probability_of_success'] is None


def test_evaluation_rejects_unknown_costs_and_incomplete_weights():
    from src.advisor.evaluation import compare_baselines
    with pytest.raises(ValueError):
        compare_baselines({}, {'AAA': None}, published_as_of='2026-10-01', cutoff='2026-10-16', cost_bps=None)


def test_draft_journal_note_cannot_masquerade_as_published_advice(tmp_path):
    import json
    from src.advisor.snapshots import evidence_scope
    from src.advisor.workflow import persist_json
    from src.advice_journal import AdviceJournalStore
    from src.tools.advice_journal_tool import AdviceJournalTool
    store = AdviceJournalStore(tmp_path / 'advice_journal.json')
    persist_json(tmp_path / 'artifacts/advisor_snapshot.json', {'data': {}})
    with evidence_scope(lambda: tmp_path):
        result = json.loads(AdviceJournalTool(store).execute(action='record', symbol='001236.OF', advice='add'))
        assert result['status'] == 'deferred'
    assert not store.path.exists()


def test_staged_publication_commits_only_the_matching_delivered_text(tmp_path):
    from src.advisor.decisions import decide
    from src.advisor.journal import PublishedAdviceStore
    from src.advisor.publication import stage_publication, finalize_publication
    root = tmp_path / 'run'
    journal = tmp_path / 'account/published.json'
    report = _report()
    stage_publication(root, journal, report, decide(report), 'delivered content', {'release_path': 'structured_assessment'})
    assert not journal.exists()
    with pytest.raises(ValueError):
        finalize_publication(root, 'a rejected draft')
    assert not journal.exists()
    finalize_publication(root, 'delivered content')
    finalize_publication(root, 'delivered content')
    assert len(PublishedAdviceStore(journal).list()) == 1


def test_public_journal_lists_all_due_publications_with_cursor(tmp_path):
    import json
    from src.advice_journal import AdviceJournalStore
    from src.advisor.decisions import decide
    from src.advisor.journal import PublishedAdviceStore
    from src.agent.tools import ToolRegistry
    from src.tools.advice_journal_tool import AdviceJournalTool
    root = tmp_path / 'account'
    store = PublishedAdviceStore(root / 'published_advice.json')
    report = _report()
    for index in range(11):
        store.publish(run_id=str(index), report=report, decision=decide(report), content='valid', delivery={'release_path': 'structured_assessment'}, published_at='2026-10-01T10:00:00+08:00')
    registry = ToolRegistry()
    registry.register(AdviceJournalTool(AdviceJournalStore(root / 'advice_journal.json')))
    first = json.loads(registry.execute('advice_journal', {'action': 'due', 'as_of': '2026-10-16', 'limit': 8}))
    assert first['status'] == 'ok' and first['total'] == 11
    assert first['next_offset'] == 8
    second = json.loads(registry.execute('advice_journal', {'action': 'due', 'as_of': '2026-10-16', 'offset': 8}))
    assert len(second['entries']) == 3


def test_partial_review_remains_due_for_the_later_predeclared_horizon(tmp_path):
    from src.advisor.decisions import decide
    from src.advisor.journal import PublishedAdviceStore
    store = PublishedAdviceStore(tmp_path / 'published.json')
    report = _report()
    entry = store.publish(run_id='partial', report=report, decision=decide(report), content='valid', delivery={'release_path': 'structured_assessment'}, published_at='2026-10-01T10:00:00+08:00')
    store.add_evaluation(entry['id'], {'as_of': '2026-10-09', 'unavailable_horizons': [20], 'review_status': 'partial'})
    assert not store.list(due_as_of='2026-10-10')
    assert len(store.list(due_as_of='2026-10-16')) == 1
    store.add_evaluation(entry['id'], {'as_of': '2026-10-16', 'unavailable_horizons': [], 'review_status': 'complete'})
    assert not store.list(due_as_of='2026-10-24')


def test_session_recovery_finishes_publication_after_transcript_commit(tmp_path, monkeypatch):
    from src.advisor.decisions import decide
    from src.advisor.journal import PublishedAdviceStore
    from src.advisor.publication import stage_publication
    from src.session.models import Attempt, Message, AttemptStatus
    from tests.test_session_service_lifecycle import _service
    service = _service(tmp_path, monkeypatch)
    session = service.create_session(title='crash recovery')
    attempt = Attempt(session_id=session.session_id, prompt='advice')
    attempt.mark_running()
    service.store.create_attempt(attempt)
    run = tmp_path / 'runs/crash-run'
    report = _report()
    path = tmp_path / 'published.json'
    stage_publication(run, path, report, decide(report), 'committed advice', {'release_path': 'structured_assessment'})
    service.store.append_message(Message(session_id=session.session_id, role='assistant', content='committed advice', linked_attempt_id=attempt.attempt_id,
        metadata={'status': 'completed', 'run_id': 'crash-run', 'completion_status': 'partial', 'release_path': 'structured_assessment'}))
    restored = _service(tmp_path, monkeypatch)
    assert len(PublishedAdviceStore(path).list()) == 1
    recovered = restored.store.get_attempt(session.session_id, attempt.attempt_id)
    assert recovered.status == AttemptStatus.COMPLETED
    assert recovered.delivery['completion_status'] == 'partial'
    _service(tmp_path, monkeypatch)
    assert len(PublishedAdviceStore(path).list()) == 1


def test_completed_attempt_retries_missing_publication_receipt_with_actual_delivery_time(tmp_path, monkeypatch):
    from src.advisor.decisions import decide
    from src.advisor.journal import PublishedAdviceStore
    from src.advisor.publication import stage_publication
    from src.session.models import Attempt, Message
    from tests.test_session_service_lifecycle import _service
    service = _service(tmp_path, monkeypatch)
    session = service.create_session(title='publication retry')
    run = tmp_path / 'runs/completed-run'
    attempt = Attempt(session_id=session.session_id, prompt='advice', run_dir=str(run))
    attempt.mark_completed(summary='committed advice')
    service.store.create_attempt(attempt)
    report, path = _report(), tmp_path / 'published.json'
    stage_publication(run, path, report, decide(report), 'committed advice', {'release_path': 'structured_assessment'})
    stamp = '2026-10-15T07:00:00+00:00'
    service.store.append_message(Message(session_id=session.session_id, role='assistant', content='committed advice', linked_attempt_id=attempt.attempt_id, created_at=stamp, metadata={'status': 'completed'}))
    _service(tmp_path, monkeypatch)
    rows = PublishedAdviceStore(path).list()
    assert len(rows) == 1 and rows[0]['published_at'] == stamp
    _service(tmp_path, monkeypatch)
    assert len(PublishedAdviceStore(path).list()) == 1


def test_paper_advice_scenario_is_frozen_at_publication_and_has_cost_drawdown_turnover(tmp_path):
    from src.advisor.decisions import decide
    from src.advisor.evaluation import compare_baselines
    from src.advisor.journal import PublishedAdviceStore
    report=_report()
    entry=PublishedAdviceStore(tmp_path/'journal.json').publish(run_id='trim',report=report,decision=decide(report),content='valid',delivery={'release_path':'structured_assessment'})
    scenario=entry['review_protocol']['advice_scenario']
    assert scenario['target_weights']['001236.OF']==pytest.approx(.15)
    assert scenario['target_weights']['001237.OF']==pytest.approx(.1)
    days=['2026-10-01','2026-10-02','2026-10-05']
    histories={symbol:[{'date':day,'value':price} for day,price in zip(days,[1,.8,.9])] for symbol in scenario['target_weights']}
    result=compare_baselines(histories,{'001236.OF':.9,'001237.OF':.1},published_as_of='2026-09-30',cutoff='2026-10-15',cost_bps=100,horizons=[2],advice_weights=scenario['target_weights'])
    observed=result['horizons']['2']
    assert 'advice_scenario' in observed
    assert observed['advice_scenario']['max_drawdown'] < observed['hold']['max_drawdown']
    assert observed['advice_scenario']['turnover'] > 0
    assert observed['advice_scenario']['scenario_cost'] > 0
    assert result['realized_execution_performance'] is None


def test_missing_intermediate_benchmark_mark_cannot_claim_zero_full_window_drawdown():
    from src.advisor.evaluation import compare_baselines
    history=[{'date':day,'value':value} for day,value in zip(['2026-10-01','2026-10-02','2026-10-05'],[1,.8,1])]
    result=compare_baselines({'AAA':history},{'AAA':1},published_as_of='2026-09-30',cutoff='2026-10-15',cost_bps=0,horizons=[2],benchmark=[history[0],history[2]],advice_weights={'AAA':1})
    row=result['horizons']['2']['benchmark']
    assert row['net_return']==0
    assert row['max_drawdown'] is None
    assert result['gaps']
