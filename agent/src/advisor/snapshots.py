"""Run-scoped evidence access, independent of the model's compacted context."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_ROOT = ContextVar('advisor_evidence_root', default=None)


@contextmanager
def evidence_scope(root_getter):
    token = _ROOT.set(root_getter)
    try:
        yield
    finally:
        _ROOT.reset(token)


def evidence_root() -> Path:
    getter = _ROOT.get()
    if getter is None or not getter():
        raise ValueError('no trusted active-run evidence context')
    return Path(getter()).resolve()
