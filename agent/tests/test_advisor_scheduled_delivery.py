"""Scheduled feeder checks share the same assessment and deterministic Verdict."""
import pytest
from src.scheduled_research.verdict import parse_verdict_section
from tests.test_advisor_decisions import _report
pytestmark = pytest.mark.unit


def test_verdict_preserves_unknown_gates_and_never_invents_cashflow_drawdown():
    from src.advisor.scheduled import verdict_tail
    report = _report()
    report['holdings'][0]['rules']['stop_loss'] = {'state': 'UNKNOWN', 'reason': 'stale mark'}
    report['regime'][0]['gates'] = [{'action': 'REDUCE', 'state': 'UNKNOWN', 'reason': 'momentum not defined'}]
    tail = verdict_tail(report)
    state, items = parse_verdict_section(tail)
    assert state == 'ok'
    assert any(item.symbol == '001236.OF' and item.state == 'UNKNOWN' for item in items)
    assert any(item.symbol == 'PORTFOLIO' and item.state == 'UNKNOWN' for item in items)
    assert not any(item.state == 'TRIPPED' for item in items)


def test_scheduled_route_only_uses_the_book_when_the_job_did_not_supply_other_holdings():
    from src.advisor.scheduled import assessment_playbook
    assert assessment_playbook({'playbook': 'daily-risk-watch'}) == 'daily-risk-watch'
    assert assessment_playbook({'playbook': 'portfolio-checkup', 'holdings_source': 'position_book'}) == 'portfolio-checkup'
    assert assessment_playbook({'playbook': 'portfolio-checkup'}) is None
    assert assessment_playbook({'playbook': 'earnings-season-tracker'}) is None


def test_rule_only_symbol_and_at_cap_theme_are_visible_without_an_invented_trade():
    from src.advisor.scheduled import verdict_tail
    from tests.test_advisor_assessment import _assess, _obs
    report = _assess({'positions': [{'symbol': '001236.OF', 'quantity': 100}]},
        {'001236.OF': _obs('001236.OF', [2]), '001237.OF': _obs('001237.OF', [1])},
        rules=[{'symbol': '001237.OF', 'stop_loss': 2}])
    assert report['rule_assessments'][0]['symbol'] == '001237.OF'
    assert report['rule_assessments'][0]['rules']['stop_loss']['state'] == 'UNKNOWN'
    report['clusters'] = [{'state': 'AT CAP'}]
    state, items = parse_verdict_section(verdict_tail(report))
    assert state == 'ok'
    assert any(item.symbol == '001237.OF' and item.state == 'UNKNOWN' for item in items)
    assert any(item.state == 'AT CAP' for item in items)


def test_actual_scheduled_loop_uses_the_registered_assessment_and_unknown_verdict(tmp_path, monkeypatch):
    import json
    from src.agent.loop import AgentLoop
    from src.agent.tools import ToolRegistry
    from src.position_book import PositionBookStore
    from src.advisor.service import AssessmentService
    from src.tools.portfolio_assessment_tool import PortfolioAssessmentTool
    from tests.test_grounding_release import _ScriptedLLM, _Response
    from src.risk_rules import RiskRulesStore
    monkeypatch.setenv('VIBE_TRADING_HOME',str(tmp_path))
    book=PositionBookStore(tmp_path/'position_book.json');book.set_position('001236.OF',quantity=100)
    RiskRulesStore(tmp_path/'risk_rules.json').set_rule('001237.OF',stop_loss=2)
    def fund(codes,**kwargs):
        return {codes[0]:{'data':[{'date':'2026-10-15','unit_nav':1}], '_provenance':{'caliber':'nav','currency':'CNY','source':'test'}}}
    registry=ToolRegistry();registry.register(PortfolioAssessmentTool(AssessmentService(book=book,fund_fetcher=fund,market_fetcher=lambda **k:{})))
    agent=AgentLoop(registry,_ScriptedLLM([_Response(content='not json')]),max_iterations=1,research_playbook='daily-risk-watch')
    run=tmp_path/'runs/scheduled';run.mkdir(parents=True);agent.memory.run_dir=str(run)
    result=agent.run('Inspect the current book and standing rules, then write the final Verdict.')
    assert result['release_path']=='structured_assessment'
    state,items=parse_verdict_section(result['content'])
    assert state=='ok' and any(row.symbol=='001237.OF' and row.state=='UNKNOWN' for row in items)
    assert (run/'artifacts/advisor_publication.json').exists()
    report=json.loads((run/'artifacts/advisor_snapshot.json').read_text())['data']
    assert report['holdings'][0]['symbol']=='001236.OF'
