"""Cross-process, per-path locks for small read-modify-write JSON stores."""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

_GUARD = threading.Lock()
_LOCKS = {}


@contextmanager
def file_transaction(path):
    path = Path(path).resolve()
    with _GUARD:
        local = _LOCKS.setdefault(str(path), threading.RLock())
    with local:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(path.suffix + '.lock').open('a+b') as handle:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def store_transaction(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with file_transaction(self.path):
            return method(self, *args, **kwargs)
    return locked
