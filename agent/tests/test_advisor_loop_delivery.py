"""Public loop delivery survives a bad draft, provider outage and compaction."""
import json

import pytest

from src.advisor.service import AssessmentService
from src.agent.loop import AgentLoop
from src.agent.tools import ToolRegistry
from src.position_book import PositionBookStore
from src.tools.portfolio_assessment_tool import PortfolioAssessmentTool
from tests.test_grounding_release import _ScriptedLLM, _Response

pytestmark = pytest.mark.unit


class OutageLLM(_ScriptedLLM):
    def stream_chat(self, *args, **kwargs):
        raise RuntimeError('provider unavailable')


def _run(tmp_path, llm, missing=False, max_iterations=1):
    book = PositionBookStore(tmp_path / 'account' / 'position_book.json')
    book.set_position('001236.OF', quantity=100)
    book.set_account(cash={'amount': 100, 'currency': 'CNY', 'as_of': '2026-10-15'})
    def funds(codes, **kwargs):
        return {} if missing else {codes[0]: {'data': [{'date': '2026-10-15', 'unit_nav': 2}], '_provenance': {'source': 'test-nav', 'currency': 'CNY', 'caliber': 'nav'}}}
    registry = ToolRegistry()
    registry.register(PortfolioAssessmentTool(AssessmentService(book=book, fund_fetcher=funds, market_fetcher=lambda **k: {})))
    from src.tools.advisor_evidence_tool import AdvisorEvidenceTool
    registry.register(AdvisorEvidenceTool())
    events = []
    agent = AgentLoop(registry, llm, max_iterations=max_iterations, event_callback=lambda e, d: events.append((e, d)))
    agent.memory.run_dir = str(tmp_path / 'run')
    (tmp_path / 'run').mkdir()
    result = agent.run('搜集整理今日最新行情数据以及外围信息，给出持仓操作建议。')
    return result, events


def test_bad_numeric_and_nonnumeric_model_premises_cannot_replace_the_assessment(tmp_path):
    llm = _ScriptedLLM([_Response(content='机构正在大规模抢筹，确定牛市即将到来，立即全仓买入，目标价999元。')])
    result, events = _run(tmp_path, llm)
    assert result['status'] == 'success'
    assert '抢筹' not in result['content'] and '999' not in result['content']
    assert '001236.OF' in result['content'] and '下一次' in result['content']
    assert result['release_path'] == 'structured_assessment'
    assert result['actionability_status'] in {'conditional', 'limited'}
    snapshot = json.loads((tmp_path / 'run/artifacts/advisor_snapshot.json').read_text())
    assert snapshot['data']['holdings'][0]['value'] == 200
    final = next(data['delta'] for event, data in reversed(events) if event == 'text_delta')
    assert final == result['content']


def test_provider_failure_keeps_useful_facts_and_honest_execution_status(tmp_path):
    result, events = _run(tmp_path, OutageLLM([_Response()]))
    assert result['status'] == 'success'  # delivered report, compatible transport
    assert result['execution_status'] == 'failed'
    assert result['completion_status'] == 'partial'
    assert '单位净值' in result['content'] and '模型分析未完成' in result['content']
    assert result['reason']
    state = json.loads((tmp_path / 'run/state.json').read_text())
    assert state['execution_status'] == 'failed'


def test_missing_fund_still_delivers_conditions_and_keeps_identity(tmp_path):
    result, _events = _run(tmp_path, _ScriptedLLM([_Response(content='')]), missing=True)
    assert result['status'] == 'success'
    assert result['completion_status'] == 'partial'
    assert '001236.OF' in result['content'] and '缺口' in result['content']
    assert '本轮其余结论尚未' not in result['content']


def test_initial_assessment_has_real_tool_events_before_the_model_turn(tmp_path):
    result, events = _run(tmp_path, _ScriptedLLM([_Response(content='finish')]))
    assert any(event == 'tool_call' and data['tool'] == 'get_portfolio_assessment' for event, data in events)
    assert any(event == 'tool_result' and data['tool'] == 'get_portfolio_assessment' for event, data in events)
    assert result['status'] == 'success'


def test_personal_advice_uses_bounded_independent_synthesis_instead_of_repeated_research(tmp_path):
    from tests.test_advisor_expert import _judgment
    judgment = _judgment(scope='PORTFOLIO', action='LIMITED_JUDGMENT', evidence_refs=['account'],
        summary='当前证据尚未给出新增风险的优势，保留现金的选择权。',
        reasoning='现金是缓冲，持仓仍承担损失；技术许可不能替代收益风险比较。',
        **__import__('src.advisor.facts',fromlist=['ACTION_CONDITIONS']).ACTION_CONDITIONS['LIMITED_JUDGMENT'])
    llm = _ScriptedLLM([
        _Response(content=json.dumps({'judgments': [judgment]}, ensure_ascii=False)),
        _Response(content=json.dumps({'reviews': [{'id': 'trim', 'supported': True, 'reason': '条件判断与账户资料相容'}]})),
    ])
    result, _ = _run(tmp_path, llm, max_iterations=40)
    assert llm.calls == 2 and all(tools is None for tools in llm.tools_history)
    assert judgment['summary'] in result['content']
    assert '独立研判' in result['content'] and '反证' in result['content']
    artifact = json.loads((tmp_path / 'run/artifacts/advisor_snapshot.json').read_text())
    assert artifact['data']['expert_analysis']['accepted'] == [judgment]
    assert result['execution_status'] == 'success'


def test_current_user_updates_and_scope_are_used_by_the_actual_loop_before_freezing(tmp_path):
    from src.tools.position_book_tool import PositionBookTool
    book = PositionBookStore(tmp_path/'position_book.json')
    book.set_position('001236.OF', quantity=100)
    book.set_position('001237.OF', quantity=100)
    registry = ToolRegistry();registry.register(PositionBookTool(book))
    def funds(codes, **kwargs):
        return {codes[0]: {'data': [{'date': '2026-10-15', 'unit_nav': 2}], '_provenance': {'source': 'test', 'currency': 'CNY', 'caliber': 'nav'}}}
    registry.register(PortfolioAssessmentTool(AssessmentService(book=book, fund_fetcher=funds, market_fetcher=lambda **k: {})))
    agent = AgentLoop(registry, _ScriptedLLM([_Response(content='bad json')]), max_iterations=1)
    run = tmp_path/'run';run.mkdir();agent.memory.run_dir=str(run)
    result = agent.run('我已经全部赎回001236.OF，现金更新为500元。今天只分析001237.OF，给出持仓操作建议。')
    report = json.loads((run/'artifacts/advisor_snapshot.json').read_text())['data']
    assert [row['symbol'] for row in report['holdings']] == ['001237.OF']
    assert report['portfolio']['available_cash'] == 500
    assert report['portfolio']['analysis_scope'] == 'requested_subportfolio'
    assert report['portfolio']['account_value'] is None
    assert '001236.OF' not in result['content']
    assert '子组合' in result['content']
