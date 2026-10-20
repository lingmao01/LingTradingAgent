import copy
import json
import threading
import time

import pytest

from src.advisor.research_budget import ResearchBudget
from src.agent.tools import BaseTool, ToolRegistry

pytestmark = pytest.mark.unit


def payload():
    return {'inputs': {'account': {'positions': [
        {'id': 'holding-a', 'symbol': '012734.OF'},
        {'id': 'holding-b', 'symbol': '008887.OF'},
        {'id': 'holding-c', 'symbol': '018344.OF'}]}, 'identities': {}},
        'data': {'as_of': '2026-10-19'}}


def profile(value='中证人工智能主题指数收益率×95%+银行活期存款利率×5%', **provenance):
    return {'data': [{'item': '基金全称', 'value': '某人工智能ETF联接C'},
                     {'item': '业绩比较基准', 'value': value}],
            '_provenance': {'source': 'xueqiu', 'caliber': 'profile', 'as_of': None, **provenance}}


class ProfileTool(BaseTool):
    name = 'get_fund_data'

    def __init__(self, answer, *, readonly=True):
        self.answer, self.calls, self.is_readonly = answer, [], readonly

    def execute(self, **arguments):
        self.calls.append(arguments)
        return self.answer(arguments) if callable(self.answer) else json.dumps(self.answer)


def collect(answer=None, *, data=None, budget=None, readonly=True, **kwargs):
    from src.advisor.identity_collection import collect_identity_candidates
    registry = ToolRegistry()
    tool = ProfileTool(answer if answer is not None else {'012734.OF': profile()}, readonly=readonly)
    registry.register(tool)
    budget = budget or ResearchBudget.from_limits()
    result = collect_identity_candidates(registry, data or payload(), budget, **kwargs)
    return result, tool, budget


def test_registered_profile_leads_keep_provenance_without_promoting_or_mutating_inputs():
    data = payload()
    before = copy.deepcopy(data)
    result, tool, budget = collect(data=data)
    assert data == before
    assert len(tool.calls) == 1
    assert tool.calls[0] == {'codes': ['012734.OF', '008887.OF'], 'view': 'profile', 'no_cache': True}
    assert budget.receipt()['source_calls'] == 1
    candidate = result['candidates'][0]
    assert candidate['symbol'] == '012734.OF' and candidate['holding_ids'] == ['holding-a']
    assert candidate['verification_status'] == 'DECLARED_CANDIDATE'
    assert candidate['verified'] is False and candidate['execution_usable'] is False
    assert candidate['relationship_kind'] == 'performance_benchmark'
    assert candidate['tracking_index'] is None
    assert candidate['source'] == 'xueqiu' and candidate['fetched_at']
    assert candidate['published_at'] is None
    assert 'publication_time_unknown' in candidate['gaps']
    assert any(row['reason'] == 'holding_limit_reached' for row in result['gaps'])


def test_explicit_index_code_is_only_a_declared_candidate_and_conflict_is_preserved():
    data = payload()
    data['inputs']['identities']['012734.OF'] = {'tracking_index': '000300.SH', 'verified': False}
    answer = {'012734.OF': {'data': [{'item': '跟踪指数', 'value': '中证人工智能主题指数 930713.CSI'}],
                             '_provenance': {'source': 'xueqiu', 'as_of': '2026-10-18'}}}
    result, _, _ = collect(answer, data=data)
    candidate = result['candidates'][0]
    assert candidate['tracking_index'] == '930713.CSI'
    assert candidate['conflicts'] and candidate['verified'] is False
    assert candidate['conflicts'][0]['declared_tracking_index'] == '000300.SH'


@pytest.mark.parametrize('provenance,reason', [
    ({'as_of': '2026-10-20'}, 'future_publication'),
    ({'as_of': 'invalid'}, 'invalid_publication_date'),
    ({'stale': True}, 'stale_profile'),
    ({'truncated': True}, 'truncated_profile'),
])
def test_future_invalid_stale_and_truncated_profiles_remain_incomplete(provenance, reason):
    result, _, _ = collect({'012734.OF': profile(**provenance)})
    assert reason in result['candidates'][0]['gaps']
    assert result['candidates'][0]['execution_usable'] is False
    assert result['coverage'][0]['status'] == 'incomplete'


def test_ambiguous_code_and_name_only_do_not_invent_mapping():
    answer = {'012734.OF': {'data': [{'item': '跟踪指数', 'value': '000300.SH / 000905.SH'}],
                            '_provenance': {'source': 'xueqiu'}},
              '008887.OF': {'data': [{'item': '基金全称', 'value': '华夏国证半导体芯片ETF联接A'}],
                            '_provenance': {'source': 'xueqiu'}}}
    result, _, _ = collect(answer)
    assert len(result['candidates']) == 1
    assert result['candidates'][0]['tracking_index'] is None
    assert 'ambiguous_target' in result['candidates'][0]['gaps']
    assert any(row['reason'] == 'relationship_not_in_profile' for row in result['gaps'])


def test_resolved_dated_mapping_skipped_but_future_verified_mapping_is_unresolved():
    data = payload()
    data['inputs']['identities'] = {
        '012734.OF': {'tracking_index': '930713.CSI', 'verified': True, 'source': 'manager', 'as_of': '2026-10-18'},
        '008887.OF': {'tracking_index': '980017.CNI', 'verified': True, 'source': 'manager', 'as_of': '2026-10-20'}}
    _, tool, _ = collect(data=data)
    assert tool.calls[0]['codes'] == ['008887.OF', '018344.OF']


def test_merged_holding_does_not_expand_more_than_two_affected_holdings_or_split_assets():
    data = payload()
    data['inputs']['account'] = {'positions': [], 'holding_groups': [
        {'id': 'ambiguous', 'candidate_symbols': ['008585.OF', '012733.OF']},
        {'id': 'other', 'candidate_symbols': ['012734.OF', '008887.OF']} ]}
    result, tool, _ = collect(data=data)
    assert len(tool.calls[0]['codes']) <= 2
    assert result['coverage'][0]['holding_ids'] == ['ambiguous']
    assert all(row.get('symbol') != 'ambiguous' for row in result['candidates'])
    assert any(row['reason'] == 'symbol_limit_reached' for row in result['gaps'])


@pytest.mark.parametrize('readonly', [False, None])
def test_non_readonly_tool_never_dispatches(readonly):
    result, tool, budget = collect(readonly=readonly)
    assert tool.calls == [] and budget.receipt()['source_calls'] == 0
    assert any(row['reason'] == 'readonly_profile_unavailable' for row in result['gaps'])


def test_no_registered_tool_is_truthful():
    from src.advisor.identity_collection import collect_identity_candidates
    result = collect_identity_candidates(ToolRegistry(), payload(), ResearchBudget.from_limits())
    assert not result['candidates']
    assert any(row['reason'] == 'readonly_profile_unavailable' for row in result['gaps'])


@pytest.mark.parametrize('mode', ['cancel', 'quota', 'reserve'])
def test_cancel_source_quota_and_review_reserve_prevent_dispatch(mode):
    cancel = threading.Event()
    now = [0.0]
    budget = ResearchBudget.from_limits(clock=lambda: now[0], cancel=cancel, max_source_calls=0 if mode == 'quota' else 24)
    if mode == 'cancel': cancel.set()
    if mode == 'reserve': now[0] = 126
    result, tool, _ = collect(budget=budget)
    assert tool.calls == [] and not result['candidates']
    assert any(row['reason'] in {'cancelled', 'shared_budget_exhausted'} for row in result['gaps'])


def test_slow_tool_is_bounded_and_late_result_cannot_mutate_returned_collection():
    def slow(_):
        time.sleep(.1)
        return json.dumps({'012734.OF': profile()})
    started = time.monotonic()
    result, _, budget = collect(slow, max_seconds=.02)
    assert time.monotonic() - started < .09
    before = copy.deepcopy(result)
    time.sleep(.11)
    assert result == before and not result['candidates']
    assert budget.receipt()['source_calls'] == 1


def test_actual_registered_fund_tool_profile_shape_and_exact_one_debit(monkeypatch):
    from src.advisor.identity_collection import collect_identity_candidates
    from src.advisor.budget_scope import budget_scope
    from src.tools.fund_data_tool import GetFundDataTool
    calls = []

    def fetch(**arguments):
        calls.append(arguments)
        return json.dumps({'012734.OF': profile(), '_session': {'latest_nav_date': '2026-10-19'}})

    monkeypatch.setattr('src.tools.fund_data_tool.fetch_fund_data_json', fetch)
    registry = ToolRegistry()
    registry.register(GetFundDataTool())
    budget = ResearchBudget.from_limits()
    with budget_scope(budget):
        result = collect_identity_candidates(registry, payload(), budget)
    assert calls[0]['view'] == 'profile' and calls[0]['no_cache'] is True
    assert result['candidates'][0]['claim_field'] == '业绩比较基准'
    assert budget.receipt()['source_calls'] == 1
    assert budget.receipt()['model_calls'] == 0


def test_cancellation_while_profile_is_in_flight_discards_returned_candidates():
    cancel = threading.Event()
    budget = ResearchBudget.from_limits(cancel=cancel)

    def cancel_during_read(_):
        cancel.set()
        return json.dumps({'012734.OF': profile()})

    result, _, _ = collect(cancel_during_read, budget=budget)
    assert not result['candidates']
    assert result['gaps'][-1]['reason'] == 'cancelled'
    assert budget.receipt()['source_calls'] == 1


def test_multiple_explicit_tracking_claims_preserve_conflict_instead_of_selecting_one():
    answer = {'012734.OF': {'data': [
        {'item': '跟踪指数', 'value': '000300.SH'},
        {'item': '标的指数', 'value': '000905.SH'}], '_provenance': {'source': 'xueqiu'}}}
    result, _, _ = collect(answer)
    assert len(result['candidates']) == 2
    assert all('relationship_conflict' in row['gaps'] and row['conflicts'] for row in result['candidates'])


@pytest.mark.parametrize('limit', [0, -1, float('nan'), float('inf')])
def test_invalid_timeout_rejected_before_any_tool_dispatch(limit):
    with pytest.raises(ValueError): collect(max_seconds=limit)


@pytest.mark.parametrize('answer', [{'status': 'error', 'error': 'secret'}, 'broken-json', {'012734.OF': {'data': 'broken'}}])
def test_failed_or_malformed_profiles_do_not_leak_error_or_make_candidates(answer):
    if isinstance(answer, str): answer = lambda _, value=answer: value
    result, _, _ = collect(answer)
    assert not result['candidates'] and result['gaps']
    assert 'secret' not in json.dumps(result)
