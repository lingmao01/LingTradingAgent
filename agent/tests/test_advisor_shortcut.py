"""K-ETF invokes account research, rather than resolving a tradable ticker."""
import json

import pytest

from src.advisor.decisions import advice_requested
from src.advisor.service import AssessmentService
from src.agent.loop import AgentLoop
from src.agent.tools import BaseTool, ToolRegistry
from src.position_book import PositionBookStore
from src.risk_rules import RiskRulesStore
from src.tools.portfolio_assessment_tool import PortfolioAssessmentTool
from tests.test_grounding_release import _Response, _ScriptedLLM

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('message', [
    'K-ETF', 'k-etf', '  K-ETF！  ', 'Ｋ－ＥＴＦ', 'K–ETF', 'K-ETF，今天只分析001236.OF',
    'K-ETF，重点分析美国取消关税对我的持仓的影响', 'K-ETF，重点关注美联储停止加息的影响',
])
def test_shortcut_requests_personal_advice_without_needing_history(message):
    assert advice_requested(message)


@pytest.mark.parametrize('message', ['为什么现在不能加仓？', '那我应该卖多少？', '具体解释一下。'])
def test_followup_after_shortcut_keeps_the_advisor_contract(message):
    assert advice_requested(message, [{'role': 'user', 'content': 'K-ETF'}])


@pytest.mark.parametrize('message', [
    'KWEB', 'K-ETF.US', 'K-ETFX', 'K-ETF是什么？', '请搜索名为K-ETF的标的',
    '不要执行K-ETF', '他说“K-ETF”', '如果输入K-ETF会怎样？',
    'K-ETF，帮我修复Python函数', 'K-ETF，是什么意思？',
    'K-ETF，请不要执行', 'K-ETF，今天不要执行', 'K-ETF，不要再分析',
    'K-ETF，先不要分析我的持仓', 'K-ETF，暂时不执行', 'K-ETF，取消本轮研究',
])
def test_mentions_symbols_and_unrelated_work_do_not_invoke_the_shortcut(message):
    assert not advice_requested(message)


class _OutsideTool(BaseTool):
    def __init__(self, name):
        self.name = name

    def execute(self, **kwargs):
        if self.name == 'get_market_data':
            return json.dumps({
                **{code: [{'date': '2026-10-14', 'close': 100}, {'date': '2026-10-15', 'close': 101}] for code in kwargs['codes']},
                '_provenance': {code: {'source': 'fixture', 'quote_currency': 'USD'} for code in kwargs['codes']},
            })
        if self.name == 'web_search':
            return json.dumps({'status': 'ok', 'results': []})
        url = kwargs['url']
        return json.dumps({'status': 'ok', 'title': 'Official release',
            'content': f'URL Source: {url}\nPublished Time: 2026-10-15\nMarkdown Content:\nOfficial macro release'})


def _run_shortcut(tmp_path, monkeypatch, *, request='K-ETF', history=None, outage=False, missing_tool=False):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    monkeypatch.setattr('src.agent.loop.market_now', lambda: datetime(2026, 10, 16, 10, tzinfo=ZoneInfo('Asia/Shanghai')))
    book = PositionBookStore(tmp_path / 'account' / 'position_book.json')
    book.set_position('001236.OF', quantity=100)
    book.set_position('001237.OF', quantity=200)
    book.set_account(cash={'amount': 100, 'currency': 'CNY', 'as_of': '2026-10-15'})
    RiskRulesStore(book.path.parent / 'risk_rules.json').set_rule('001236.OF', stop_loss=1.5)
    def funds(codes, **kwargs):
        return {code: {'data': [{'date': '2026-10-15', 'unit_nav': 1}],
            '_provenance': {'source': 'fixture-nav', 'currency': 'CNY', 'caliber': 'nav'}} for code in codes}
    def market(**kwargs):
        return {code: [{'date': '2026-10-15', 'close': 100}] for code in kwargs['codes']}
    registry = ToolRegistry()
    from src.tools.position_book_tool import PositionBookTool
    registry.register(PositionBookTool(book))
    if not missing_tool:
        registry.register(PortfolioAssessmentTool(AssessmentService(book=book, fund_fetcher=funds, market_fetcher=market)))
    for name in ('get_market_data', 'web_search', 'read_url'):
        registry.register(_OutsideTool(name))
    class LLM(_ScriptedLLM):
        request_payload = None

        def stream_chat(self, messages, *args, **kwargs):
            if outage:
                raise RuntimeError('provider unavailable')
            payload = json.loads(messages[-1]['content'])
            if 'request' in payload: self.request_payload = payload
            return super().stream_chat(messages, *args, **kwargs)
    llm = LLM([_Response(content='{}')])
    events = []
    agent = AgentLoop(registry, llm, max_iterations=3, event_callback=lambda event, data: events.append((event, data)))
    run = tmp_path / 'run'
    run.mkdir()
    agent.memory.run_dir = str(run)
    result = agent.run(request, history=history)
    return result, events, run, llm


def test_shortcut_collects_nav_outside_evidence_and_delivers_triggered_reduction(tmp_path, monkeypatch):
    result, events, run, llm = _run_shortcut(tmp_path, monkeypatch)
    assert result['release_path'] == 'structured_assessment'
    snapshot = json.loads((run / 'artifacts/advisor_snapshot.json').read_text())['data']
    delivery = json.loads((run / 'artifacts/advisor_delivery.json').read_text())
    assert snapshot['holdings'][0]['price'] == 1
    assert {row['symbol'] for row in snapshot['holdings']} == {'001236.OF', '001237.OF'}
    assert snapshot['holdings'][0]['rules']['stop_loss']['state'] == 'BREACHED'
    assert len(snapshot['external_coverage']) == 3
    assert snapshot['news']['primary'][0]['verified'] is True
    assert delivery['decision']['stance'] == 'REDUCE_RISK'
    assert any(card['action'] == 'REVIEW_REDUCTION' for card in delivery['decision']['cards'])
    assert '001236.OF' in result['content'] and '减仓' in result['content']
    assert '减仓' in llm.request_payload['request'] and '行情' in llm.request_payload['request']
    assert not any(event == 'tool_call' and data['tool'] == 'search_symbol' for event, data in events)


def test_shortcut_supplement_keeps_explicit_subportfolio_scope(tmp_path, monkeypatch):
    result, _, run, llm = _run_shortcut(tmp_path, monkeypatch, request='K-ETF，今天只分析001237.OF')
    report = json.loads((run / 'artifacts/advisor_snapshot.json').read_text())['data']
    assert [row['symbol'] for row in report['holdings']] == ['001237.OF']
    assert report['portfolio']['analysis_scope'] == 'requested_subportfolio'
    assert '子组合' in result['content'] and '001236.OF' not in result['content']
    assert '只分析001237.OF' in llm.request_payload['request']


def test_shortcut_provider_failure_still_delivers_facts_and_reduction_conditions(tmp_path, monkeypatch):
    result, _, _, _ = _run_shortcut(tmp_path, monkeypatch, outage=True)
    assert result['release_path'] == 'structured_assessment'
    assert result['execution_status'] == 'failed'
    assert result['completion_status'] == 'partial'
    assert '模型分析未完成' in result['content'] and '001236.OF' in result['content']
    assert '减仓' in result['content']


@pytest.mark.parametrize(('message', 'history'), [
    ('K-ETF', None),
    ('为什么现在不能加仓？', [{'role': 'user', 'content': 'K-ETF'}]),
])
def test_shortcut_with_missing_assessment_tool_reports_gap_instead_of_symbol_lookup(tmp_path, monkeypatch, message, history):
    result, _, run, _ = _run_shortcut(tmp_path, monkeypatch, request=message, history=history, missing_tool=True)
    assert result['release_path'] == 'structured_assessment'
    assert result['completion_status'] == 'partial'
    snapshot = json.loads((run / 'artifacts/advisor_snapshot.json').read_text())
    assert 'assessment' in {row.get('scope') for row in snapshot['data']['data_gaps']}
