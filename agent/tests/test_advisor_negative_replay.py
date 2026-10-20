import json
from types import SimpleNamespace

import pytest

from src.agent.loop import AgentLoop
from src.agent.tools import BaseTool, ToolRegistry

pytestmark = pytest.mark.unit


class EmptyFlow(BaseTool):
    name = 'get_northbound_flow'
    def __init__(self, fixture_marker):
        self.calls = 0
    def execute(self, **kwargs):
        self.calls += 1
        return json.dumps({'ok': False, 'availability': 'unavailable', 'retryable': False, 'error': 'no observations'})


def test_advisor_reuses_explicit_nonretryable_absence_but_allows_explicit_refresh():
    registry = ToolRegistry()
    tool = EmptyFlow("fixture")
    registry.register(tool)
    agent = AgentLoop(registry, SimpleNamespace())
    agent._advisor_mode = True
    first, _ = agent._invoke_tool(tool.name, {'lookback_days': 10}, call_id='one')
    second, _ = agent._invoke_tool(tool.name, {'lookback_days': 10}, call_id='two')
    assert first == second
    assert tool.calls == 1
    agent._invoke_tool(tool.name, {'lookback_days': 10, 'no_cache': True}, call_id='refresh')
    assert tool.calls == 2
    agent._invoke_tool(tool.name, {'lookback_days': 5}, call_id='different')
    assert tool.calls == 3
