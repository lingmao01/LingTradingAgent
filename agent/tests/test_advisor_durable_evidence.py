"""Compaction and public evidence reads cannot lose holdings or cross runs."""
import json

import pytest

from src.agent.tools import ToolRegistry

pytestmark = pytest.mark.unit


def test_all_holdings_are_page_readable_and_model_path_cannot_select_another_run(tmp_path):
    from src.advisor.snapshots import evidence_scope
    from src.advisor.workflow import persist_json
    from src.tools.advisor_evidence_tool import AdvisorEvidenceTool
    first, other = tmp_path / 'first', tmp_path / 'other'
    persist_json(first / 'artifacts/advisor_snapshot.json', {'data': {'holdings': [{'symbol': f'{n:06}.OF', 'value': n} for n in range(13)]}})
    persist_json(other / 'artifacts/advisor_snapshot.json', {'data': {'holdings': [{'symbol': 'SECRET'}]}})
    registry = ToolRegistry()
    registry.register(AdvisorEvidenceTool())
    with evidence_scope(lambda: first):
        rows = []
        for offset in (0, 5, 10):
            result = json.loads(registry.execute('get_advisor_evidence', {'section': 'holdings', 'offset': offset, 'limit': 5, 'run_dir': str(other)}))
            assert result['ok'] is True
            rows.extend(result['data'])
        assert len(rows) == 13
        assert all(row['symbol'] != 'SECRET' for row in rows)
        assert json.loads(registry.execute('get_advisor_evidence', {'section': 'holdings', 'symbol': '000012.OF'}))['data'][0]['value'] == 12
    assert json.loads(registry.execute('get_advisor_evidence', {'section': 'holdings'}))['ok'] is False


def test_discovery_text_is_never_rendered_as_verified_news():
    from src.advisor.decisions import decide
    from src.advisor.delivery import render_advice
    from tests.test_advisor_decisions import _report
    report = _report()
    report['news'] = {'primary': [], 'discovery': [{'title': '机构正在抢筹', 'verified': False}], 'coverage': [], 'gaps': [{'reason': 'original unavailable'}]}
    text = render_advice(report, decide(report))
    assert '机构正在抢筹' not in text
    assert '外围' in text and '原文' in text


def test_loop_worker_keeps_trusted_evidence_scope(tmp_path):
    from types import SimpleNamespace
    from src.advisor.snapshots import evidence_scope
    from src.advisor.workflow import persist_json
    from src.agent.loop import AgentLoop
    from src.tools.advisor_evidence_tool import AdvisorEvidenceTool
    registry = ToolRegistry()
    registry.register(AdvisorEvidenceTool())
    agent = AgentLoop(registry, SimpleNamespace())
    persist_json(tmp_path / 'artifacts/advisor_snapshot.json', {'data': {'holdings': [{'symbol': '001236.OF'}]}})
    with evidence_scope(lambda: tmp_path):
        result, _ = agent._invoke_tool('get_advisor_evidence', {'section': 'holdings'}, call_id='one')
    assert json.loads(result)['ok'] is True


def test_advisor_research_rejects_unadvertised_write_calls():
    from types import SimpleNamespace
    from src.agent.loop import AgentLoop
    from src.agent.tools import BaseTool
    class MutateTool(BaseTool):
        name = 'mutate'
        is_readonly = False
        def __init__(self, marker):
            self.marker = marker
        def execute(self, **kwargs):
            raise AssertionError('must not execute')
    registry = ToolRegistry()
    registry.register(MutateTool("fixture"))
    agent = AgentLoop(registry, SimpleNamespace())
    agent._advisor_mode = True
    result, _ = agent._invoke_tool('mutate', {}, call_id='bad')
    assert json.loads(result)['status'] == 'error'
