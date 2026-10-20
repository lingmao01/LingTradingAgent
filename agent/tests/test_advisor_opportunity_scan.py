import pytest
pytestmark = pytest.mark.unit


def bundle_rows(rows, coverage=True):
    from src.advisor.research_evidence import EvidenceBundle
    return EvidenceBundle.build(rows, as_of='2026-10-19', coverage=[{'scope':'candidate','status':'complete'}] if coverage else [])


def candidate(**changes):
    return {'subject':'NEW', 'symbol':'NEW.US', 'name':'New sector', 'kind':'fact', 'metric':'candidate',
        'verified':True, 'source':'official directory', 'published_at':'2026-10-19',
        'value':{'candidate':True, 'identity_verified':True, 'market':'US', 'price_as_of':'2026-10-19',
                 'transaction_conditions_verified':True, 'investment_logic':'cash flows improving', 'counter_evidence_checked':True, 'comparison':'cash', **changes}}


def test_nonheld_candidate_is_discovered_unknown_access_is_watch_and_cash_preserved():
    from src.advisor.research_context import build_personal_context, CapabilityMap
    from src.advisor.research_budget import ResearchBudget
    from src.advisor.opportunities import screen_opportunities
    context = build_personal_context({'positions':[{'symbol':'OLD.US'}]}, {}, [])
    scan = screen_opportunities(context, bundle_rows([candidate()]), CapabilityMap(), budget=ResearchBudget.from_limits())
    assert scan.candidates[0].subject.canonical_symbol == 'NEW.US'
    assert scan.candidates[0].maturity == 'WATCH'
    assert 'market_access_unknown' in scan.candidates[0].key_gaps
    assert scan.cash_baseline['subject_id'] == 'cash'
    assert not scan.candidates[0].diversification_verified


def test_rankings_do_not_qualify_and_deep_dive_never_exceeds_two():
    from src.advisor.research_context import build_personal_context, CapabilityMap
    from src.advisor.research_budget import ResearchBudget
    from src.advisor.opportunities import screen_opportunities
    context = build_personal_context({'personal_context':{'market_access':['US']}}, {}, [])
    rows = [dict(candidate(), subject=f'N{i}', symbol=f'N{i}.US') for i in range(4)]
    scan = screen_opportunities(context, bundle_rows(rows), CapabilityMap(), budget=ResearchBudget.from_limits())
    assert sum(c.maturity == 'COMPARISON_READY' for c in scan.candidates) == 2
    assert scan.deep_dive_count == 2
    scan = screen_opportunities(context, bundle_rows([candidate(investment_logic='', signal_basis='price_ranking')]), CapabilityMap(), budget=ResearchBudget.from_limits())
    assert scan.candidates[0].maturity == 'WATCH'


def test_unattempted_scan_is_incomplete_and_completed_empty_scan_is_no_qualified():
    from src.advisor.research_context import build_personal_context, CapabilityMap
    from src.advisor.research_budget import ResearchBudget
    from src.advisor.opportunities import screen_opportunities
    args = (build_personal_context({}, {}, []), CapabilityMap())
    assert screen_opportunities(args[0], bundle_rows([], False), args[1], budget=ResearchBudget.from_limits()).coverage_status == 'INCOMPLETE'
    assert screen_opportunities(args[0], bundle_rows([]), args[1], budget=ResearchBudget.from_limits()).coverage_status == 'NO_QUALIFIED_CANDIDATE'


def test_conflicting_identity_and_missing_transaction_feasibility_prevent_comparison_ready():
    from src.advisor.research_context import build_personal_context, CapabilityMap
    from src.advisor.research_budget import ResearchBudget
    from src.advisor.opportunities import screen_opportunities
    context=build_personal_context({'personal_context':{'market_access':['US']}}, {}, [])
    scan=screen_opportunities(context, bundle_rows([candidate(identity_conflict=True)]), CapabilityMap(), budget=ResearchBudget.from_limits())
    assert scan.candidates[0].maturity == 'WATCH'
    scan=screen_opportunities(context, bundle_rows([candidate(transaction_conditions_verified=False)]), CapabilityMap(), budget=ResearchBudget.from_limits())
    assert scan.candidates[0].maturity == 'WATCH'
    assert 'transaction_conditions_unknown' in scan.candidates[0].key_gaps


def test_expired_budget_is_not_a_market_no_opportunity_conclusion():
    from src.advisor.research_context import build_personal_context, CapabilityMap
    from src.advisor.research_budget import ResearchBudget
    from src.advisor.opportunities import screen_opportunities
    now=[0]
    budget=ResearchBudget.from_limits(clock=lambda:now[0])
    now[0]=180
    context=build_personal_context({'personal_context':{'market_access':['US']}}, {}, [])
    result=screen_opportunities(context,bundle_rows([candidate()]),CapabilityMap(),budget=budget)
    assert result.coverage_status=='INCOMPLETE'
    assert 'budget_exhausted' in result.candidates[0].key_gaps
