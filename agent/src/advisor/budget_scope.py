"""Per-research request accounting; unrelated chats retain their provider policy."""
from contextlib import contextmanager
from contextvars import ContextVar

_ACTIVE = ContextVar('advisor_research_budget', default=None)
_STAGE = ContextVar('advisor_model_stage', default='generate')

@contextmanager
def budget_scope(budget, *, stage='generate'):
    token, phase = _ACTIVE.set(budget), _STAGE.set(stage)
    try:
        yield
    finally:
        _STAGE.reset(phase)
        _ACTIVE.reset(token)

def active_budget():
    return _ACTIVE.get()

def debit_model():
    budget = active_budget()
    if budget is not None and not budget.acquire_model(stage=_STAGE.get()):
        raise RuntimeError('shared research model/time budget exhausted')

def source_call(call, *args, **kwargs):
    budget = active_budget()
    if budget is not None and not budget.acquire_source():
        raise RuntimeError('shared research source/time budget exhausted')
    return call(*args, **kwargs)

def scoped_provider(llm):
    """Disable SDK retries only for this request, including nonstream fallback.

    LangChain's max_retries field alone does not rebuild existing SDK clients.
    Copy the runnable and replace both clients with SDK with_options clones.
    Providers without a controllable client fail closed in research scope.
    """
    if active_budget() is None:
        return llm
    from copy import copy
    if getattr(llm, 'bound', None) is not None and isinstance(getattr(llm, 'kwargs', None), dict):
        return scoped_provider(llm.bound).bind(**llm.kwargs)
    scoped = copy(llm)
    for root, leaf in (('root_client', 'client'), ('root_async_client', 'async_client')):
        client = getattr(llm, root, None)
        if client is not None and callable(getattr(client, 'with_options', None)):
            setattr(scoped, root, client.with_options(max_retries=0))
            setattr(scoped, leaf, getattr(scoped, root).chat.completions)
        elif root == 'root_client' and getattr(llm, 'max_retries', 0):
            raise RuntimeError('research provider cannot enforce request retry budget')
    return scoped
