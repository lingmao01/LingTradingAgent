"""One thread-safe dispatch budget, shared by collection and actual model requests."""
from __future__ import annotations

import math
import threading
import time
from typing import Any


class BudgetExhausted(RuntimeError):
    """A research dispatch could not acquire the shared budget."""


class ResearchBudget:
    def __init__(self, *, max_wall_seconds, max_source_calls, max_model_calls,
                 review_reserve_seconds, clock, cancel):
        self.clock = clock
        self.cancel = cancel
        self.started_at = clock()
        self.deadline = self.started_at + max_wall_seconds
        self.max_wall_seconds = max_wall_seconds
        self.max_source_calls = max_source_calls
        self.max_model_calls = max_model_calls
        self.review_reserve_seconds = min(review_reserve_seconds, max_wall_seconds)
        self._lock = threading.RLock()
        self._source_calls = 0
        self._model_calls = 0
        self._review_calls = 0
        self._requests: list[dict[str, Any]] = []

    @classmethod
    def from_limits(cls, *, max_wall_seconds=180, max_source_calls=24,
                    max_model_calls=3, review_reserve_seconds=55,
                    clock=time.monotonic, cancel=None):
        for name, value in [('max_wall_seconds', max_wall_seconds), ('max_source_calls', max_source_calls),
                            ('max_model_calls', max_model_calls), ('review_reserve_seconds', review_reserve_seconds)]:
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f'{name} must be finite and nonnegative')
        if int(max_source_calls) != max_source_calls or int(max_model_calls) != max_model_calls:
            raise ValueError('call limits must be integers')
        # Callers may lower limits; this API never expands the approved ceiling.
        return cls(max_wall_seconds=min(float(max_wall_seconds), 180),
                   max_source_calls=min(int(max_source_calls), 24),
                   max_model_calls=min(int(max_model_calls), 3),
                   review_reserve_seconds=float(review_reserve_seconds), clock=clock, cancel=cancel)

    @property
    def cancelled(self):
        return bool(self.cancel and (self.cancel.is_set() if hasattr(self.cancel, 'is_set') else self.cancel()))

    def remaining_seconds(self, stage='generate'):
        if stage not in {'generate', 'review', 'source'}:
            raise ValueError('unknown research stage')
        if self.cancelled:
            return 0.0
        reserve = 0 if stage == 'review' else self.review_reserve_seconds
        return max(0.0, self.deadline - self.clock() - reserve)

    def acquire_source(self):
        with self._lock:
            if self.remaining_seconds('source') <= 0 or self._source_calls >= self.max_source_calls:
                return False
            self._source_calls += 1
            return True

    def acquire_model(self, stage='generate'):
        if stage not in {'generate', 'review'}:
            raise ValueError('model stage must be generate or review')
        with self._lock:
            limit = self.max_model_calls if stage == 'review' else max(0, self.max_model_calls - 1)
            if self.remaining_seconds(stage) <= 0 or self._model_calls >= limit:
                return False
            self._model_calls += 1
            self._review_calls += stage == 'review'
            self._requests.append({'request_number': self._model_calls, 'stage': stage})
            return True

    def record_model_usage(self, usage=None, **metadata):
        """Attach scalar usage only; never retain prompts, credentials or SDK objects."""
        allowed = {'input_tokens', 'output_tokens', 'total_tokens', 'prompt_tokens', 'completion_tokens', 'cost', 'model', 'status'}
        values = {**(usage if isinstance(usage, dict) else {}), **metadata}
        safe = {key: value for key, value in values.items() if key in allowed and isinstance(value, (str, int, float, type(None)))}
        with self._lock:
            if self._requests:
                self._requests[-1].update(safe)

    def receipt(self):
        with self._lock:
            return {'max_wall_seconds': self.max_wall_seconds, 'max_source_calls': self.max_source_calls,
                    'max_model_calls': self.max_model_calls, 'review_reserve_seconds': self.review_reserve_seconds,
                    'started_at': self.started_at, 'deadline': self.deadline,
                    'elapsed_seconds': max(0, self.clock() - self.started_at),
                    'remaining_seconds': self.remaining_seconds('review'),
                    'source_calls': self._source_calls, 'model_calls': self._model_calls,
                    'review_calls': self._review_calls, 'cancelled': self.cancelled,
                    'requests': [dict(row) for row in self._requests]}

    def to_dict(self):
        return self.receipt()
