import json
import pytest
from src.agent.tools import BaseTool, ToolRegistry
pytestmark = pytest.mark.unit


def test_dual_lane_is_required_without_holdings_and_queries_never_include_private_book():
    from src.advisor.research_context import build_personal_context, build_capability_map
    from src.advisor.research_budget import ResearchBudget
    from src.advisor.research_planner import plan_research
    registry = ToolRegistry()
    context = build_personal_context({'cash': {'amount': 987654}, 'positions': []}, {}, [])
    plan = plan_research(context, {'as_of': '2026-10-19'}, build_capability_map(registry), ResearchBudget.from_limits())
    assert {q.scope for q in plan.questions} == {'holding', 'candidate'}
    assert '987654' not in str(plan.to_dict())
    assert plan.max_supplement_rounds == 1


def test_bounded_readonly_dispatch_retains_good_evidence_and_exploration_coverage():
    from src.advisor.research_context import build_personal_context, build_capability_map
    from src.advisor.research_budget import ResearchBudget
    from src.advisor.research_evidence import EvidenceBundle
    from src.advisor.research_planner import plan_research, collect_research_evidence
    class Search(BaseTool):
        name = 'web_search'
        def execute(self, **kwargs): return json.dumps({'status': 'ok', 'results': []})
    registry = ToolRegistry(); registry.register(Search())
    budget = ResearchBudget.from_limits()
    plan = plan_research(build_personal_context({}, {}, []), {'as_of':'2026-10-19'}, build_capability_map(registry), budget)
    initial = EvidenceBundle.build([], as_of='2026-10-19')
    result = collect_research_evidence(plan, registry, initial=initial, budget=budget)
    assert any(row['scope'] == 'candidate' and row['status'] == 'complete' for row in result.coverage)
    assert budget.receipt()['source_calls'] > 0
    assert result.parent_hash == initial.bundle_hash


def test_write_tool_cannot_be_selected_even_if_registered_under_search_name():
    from src.advisor.research_context import build_personal_context, build_capability_map
    from src.advisor.research_budget import ResearchBudget
    from src.advisor.research_evidence import EvidenceBundle
    from src.advisor.research_planner import plan_research, collect_research_evidence
    class Write(BaseTool):
        name = 'web_search'; is_readonly = False
        def execute(self, **kwargs): raise AssertionError('write dispatched')
    registry = ToolRegistry(); registry.register(Write())
    budget = ResearchBudget.from_limits()
    plan = plan_research(build_personal_context({}, {}, []), {}, build_capability_map(registry), budget)
    result = collect_research_evidence(plan, registry, initial=EvidenceBundle.build([]), budget=budget)
    assert result.gaps and budget.receipt()['source_calls'] == 0


def test_unverified_search_hits_cannot_claim_completed_opportunity_scan():
    from src.advisor.research_context import build_personal_context, build_capability_map
    from src.advisor.research_budget import ResearchBudget
    from src.advisor.research_evidence import EvidenceBundle
    from src.advisor.research_planner import plan_research, collect_research_evidence
    from src.advisor.opportunities import screen_opportunities
    class Search(BaseTool):
        name = 'web_search'
        def execute(self, **kwargs):
            return json.dumps({'status':'ok','results':[{'title':'hot theme', 'url':'https://unverified.example/a', 'snippet':'buy now'}]})
    registry=ToolRegistry(); registry.register(Search())
    context=build_personal_context({}, {}, []); budget=ResearchBudget.from_limits(); caps=build_capability_map(registry)
    plan=plan_research(context, {'as_of':'2026-10-19'}, caps, budget)
    result=collect_research_evidence(plan, registry, initial=EvidenceBundle.build([], as_of='2026-10-19'), budget=budget)
    assert screen_opportunities(context, result, caps, budget=budget).coverage_status == 'INCOMPLETE'
