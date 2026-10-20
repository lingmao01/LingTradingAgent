"""Only supported, unconfigured official DeepSeek checks get bounded effort."""
from types import SimpleNamespace
import pytest
from src.providers.chat import ChatLLM, LLMRuntimeSnapshot
pytestmark = pytest.mark.unit

@pytest.mark.parametrize('base,effort,expected', [
    ('https://api.deepseek.com/v1', '', True),
    ('https://api.deepseek.com.evil.test/v1', '', False),
    ('https://api.deepseek.com/v1', 'high', False),
    ('https://relay.test/v1', '', False),
])
def test_review_effort_is_local_and_respects_explicit_configuration(base, effort, expected):
    class Adapter:
        openai_api_base = base
        def bind(self, **kwargs):
            assert kwargs == {'reasoning_effort': 'low'}
            return SimpleNamespace(bound=True)
    client = ChatLLM.__new__(ChatLLM)
    original = client._llm = Adapter()
    client.model_name = 'deepseek-flash'
    client.runtime_snapshot = LLMRuntimeSnapshot(provider='deepseek', configured_model='deepseek-flash', reasoning_effort=effort)
    seen = []
    # Exercise wrapper policy; ordinary stream implementation is independently covered.
    def stream(self, *args, **kwargs):
        seen.append(self._llm)
        return 'response'
    from unittest.mock import patch
    with patch.object(ChatLLM, 'stream_chat', stream):
        assert client.stream_advisor_check([]) == 'response'
    assert bool(getattr(seen[0], 'bound', False)) is expected
    assert client._llm is original
