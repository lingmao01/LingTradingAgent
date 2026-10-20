from types import SimpleNamespace
import pytest
from src.advisor.budget_scope import budget_scope, debit_model, scoped_provider
from src.advisor.research_budget import ResearchBudget

def test_actual_requests_and_empty_stream_fallback_share_cap_and_scoped_sdk_policy():
    calls=[]
    class SDK:
        chat=SimpleNamespace(completions='original')
        def with_options(self,**kw):
            calls.append(kw);return SimpleNamespace(chat=SimpleNamespace(completions='no-retry'))
    provider=SimpleNamespace(root_client=SDK(),client='original',max_retries=5)
    budget=ResearchBudget.from_limits()
    assert scoped_provider(provider) is provider
    with budget_scope(budget):
        scoped=scoped_provider(provider)
        assert scoped.client=='no-retry' and provider.client=='original'
        debit_model();debit_model()
        with pytest.raises(RuntimeError):debit_model()
    with budget_scope(budget,stage='review'):debit_model()
    assert budget.receipt()['model_calls']==3
    assert calls==[{'max_retries':0}]
