"""Personal facts and actual capabilities are not inferred from holdings."""
import pytest
from src.agent.tools import BaseTool, ToolRegistry
pytestmark = pytest.mark.unit


def test_unknown_preferences_and_wealth_are_not_inferred_from_old_holdings():
    from src.advisor.research_context import build_personal_context
    context = build_personal_context({'version': 7, 'positions': [{'symbol': '001236.OF', 'quantity': 2}]}, {}, [])
    assert context.account_version == '7'
    assert context.known.get('investment_horizon') is None
    assert 'investment_horizon' in context.unknown_fields
    assert context.known.get('total_wealth') is None
    assert context.minimum_questions == ()
    assert not context.preferences


def test_current_user_statement_wins_without_promoting_guidance_to_hard_constraint():
    from src.advisor.research_context import build_personal_context
    context = build_personal_context({'personal_context': {'investment_horizon': 'old'}},
        {'guardrails': ['不加杠杆'], 'constraints': [{'field': 'allocation', 'value': 'observe', 'strength': 'USER_GUIDANCE'}]},
        [{'field': 'investment_horizon', 'value': '一年', 'source': 'current user', 'as_of': '2026-10-19', 'current': True},
         {'field': 'market_access', 'value': ['CN'], 'source': 'user', 'as_of': '2026-10-18'}])
    assert context.known['investment_horizon'] == '一年'
    assert any(row['strength'] == 'USER_HARD' and row['value'] == '不加杠杆' for row in context.constraints)
    assert next(row for row in context.constraints if row.get('field') == 'allocation')['strength'] == 'USER_GUIDANCE'
    assert context.market_access == ('CN',)


def test_only_decision_changing_unknown_creates_one_minimum_question():
    from src.advisor.research_context import build_personal_context
    context = build_personal_context({'decision_dependencies': ['investment_horizon', 'liquidity_need']}, {}, [])
    assert len(context.minimum_questions) == 1


def test_registry_presence_is_not_runtime_availability_and_errors_do_not_expose_secrets():
    from src.advisor.research_context import build_capability_map
    class Broken(BaseTool):
        name = 'web_search'
        @classmethod
        def check_available(cls): return False
        def execute(self, **kwargs): return '{}'
    registry = ToolRegistry(); registry.register(Broken())
    registry.record_registration_failure('get_market_data', 'token=super-secret')
    caps = build_capability_map(registry)
    assert caps.tools['web_search'].registered
    assert not caps.tools['web_search'].available
    assert caps.tools['get_market_data'].observed_failure
    assert 'super-secret' not in str(caps.to_dict())


def test_frozen_assessment_account_is_supported_and_context_cannot_mutate_it():
    from src.advisor.models import freeze
    from src.advisor.research_context import build_personal_context
    account = freeze({'positions':[{'symbol':'OLD.US'}], 'personal_context':{'market_access':['US']}})
    context = build_personal_context(account, freeze({'guardrails':['不加杠杆']}), [])
    assert context.market_access == ('US',)
    with pytest.raises(TypeError): context.holdings[0]['symbol'] = 'NEW.US'
    assert context.to_dict()['holdings'][0]['symbol'] == 'OLD.US'


def test_unspecified_recorded_rule_strength_is_preserved_as_unknown_not_promoted():
    from src.advisor.research_context import build_personal_context
    context=build_personal_context({}, {'tiers':[{'action':'NO ADD','when':'收盘低于均线'}], 'max_drawdown_percent':20}, [])
    assert any(row.get('field')=='tiers' and row['strength']=='UNKNOWN' for row in context.constraints)
    assert any(row.get('field')=='max_drawdown_percent' and row['value']==20 for row in context.constraints)
