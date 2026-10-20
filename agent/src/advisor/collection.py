"""Bounded read-only collection with retained successes and capped late workers."""
from __future__ import annotations

import contextvars
import queue
import threading
import time
from math import isfinite

_SLOTS = threading.BoundedSemaphore(16)


def bounded_collect(jobs, *, timeout_seconds: float, max_workers=4):
    if not isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError('collection deadline must be finite and positive')
    if not jobs:
        return {}, {}
    deadline = time.monotonic() + timeout_seconds
    pending, output = queue.Queue(), queue.Queue()
    done = threading.Event()
    for key, job in jobs.items():
        pending.put((key, job))
    def worker():
        try:
            while not done.is_set() and time.monotonic() < deadline:
                try:
                    key, job = pending.get_nowait()
                except queue.Empty:
                    return
                try:
                    value = job()
                    if not done.is_set():
                        output.put((key, value, None))
                except Exception as exc:
                    if not done.is_set():
                        output.put((key, None, str(exc)))
        finally:
            _SLOTS.release()
    started = 0
    for _ in range(min(max_workers, len(jobs))):
        if not _SLOTS.acquire(blocking=False):
            break
        context = contextvars.copy_context()
        threading.Thread(target=lambda context=context: context.run(worker), daemon=True, name='advisor-collection').start()
        started += 1
    results, errors = {}, {}
    try:
        while started and len(results) + len(errors) < len(jobs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                key, value, error = output.get(timeout=remaining)
            except queue.Empty:
                break
            if error is None:
                results[key] = value
            else:
                errors[key] = error
    finally:
        done.set()
    for key in jobs.keys() - results.keys() - errors.keys():
        errors[key] = 'collection deadline exceeded' if started else 'collection worker capacity unavailable'
    return results, errors
