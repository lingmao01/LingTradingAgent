"""The exposed assessment contract works through a real FastMCP session."""
import asyncio
import json

import pytest

pytestmark = pytest.mark.unit


def test_assessment_round_trip_and_invalid_cutoff_through_mcp(tmp_path, monkeypatch):
    from fastmcp import Client
    import mcp_server
    from src.advisor.service import AssessmentService
    from src.agent.tools import ToolRegistry
    from src.position_book import PositionBookStore
    from src.tools.portfolio_assessment_tool import PortfolioAssessmentTool
    book = PositionBookStore(tmp_path / 'position_book.json')
    book.set_position('001236.OF', quantity=10, lots=[{'lot_id': 'x', 'quantity': 10, 'confirmed_at': '2026-10-14'}])
    from src.fund_identity import FundIdentityStore
    FundIdentityStore(tmp_path / 'fund_identities.json').record('001236.OF', terms={'effective_from': '2026-01-01', 'redemption_tiers': [{'holding_days_below': 7, 'fee_percent': 1.5}], 'otherwise_fee_percent': 0})
    calls = []
    def funds(codes, **kwargs):
        calls.append(codes)
        return {codes[0]: {'data': [{'date': '2026-10-15', 'unit_nav': 2}], '_provenance': {'source': 'test-nav', 'currency': 'CNY', 'caliber': 'nav'}}}
    registry = ToolRegistry()
    registry.register(PortfolioAssessmentTool(AssessmentService(book=book, fund_fetcher=funds, market_fetcher=lambda **kw: {})))
    monkeypatch.setattr(mcp_server, '_get_registry', lambda: registry)
    async def scenario():
        async with Client(mcp_server.mcp) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert 'get_portfolio_assessment' in names
            assert 'get_advisor_evidence' not in names  # no trusted active-run context on MCP
            good = await client.call_tool('get_portfolio_assessment', {'as_of': '2026-10-15'})
            payload = json.loads(good.content[0].text)
            assert payload['ok'] is True
            assert payload['data']['holdings'][0]['value'] == 20
            selected = await client.call_tool('get_portfolio_assessment', {'as_of': '2026-10-15', 'scope_symbols': ['001236.OF'], 'redemption_confirmation_date': '2026-10-16'})
            scoped = json.loads(selected.content[0].text)
            assert scoped['ok'] is True
            assert scoped['data']['portfolio']['analysis_scope'] == 'requested_subportfolio'
            assert scoped['data']['holdings'][0]['redemption_fee']['estimate'] == pytest.approx(.3)
            before = len(calls)
            bad = await client.call_tool('get_portfolio_assessment', {'as_of': 'not-a-date'})
            assert json.loads(bad.content[0].text)['ok'] is False
            assert len(calls) == before
    asyncio.run(scenario())
