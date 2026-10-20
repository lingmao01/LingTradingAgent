import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
pytestmark = pytest.mark.unit


def test_one_absolute_clock_and_review_reserve_survive_retry():
    from src.advisor.research_budget import ResearchBudget
    now = [100.0]
    budget = ResearchBudget.from_limits(clock=lambda: now[0])
    assert budget.deadline == 280
    assert budget.remaining_seconds() == 125
    assert budget.acquire_model() and budget.acquire_model()
    assert not budget.acquire_model()
    now[0] = 226
    assert not budget.acquire_source()
    assert budget.acquire_model(stage='review')
    assert not budget.acquire_model(stage='review')
    assert budget.receipt()['model_calls'] == 3
    assert budget.deadline == 280


def test_parallel_dispatch_failure_does_not_refund_source_quota():
    from src.advisor.research_budget import ResearchBudget
    budget = ResearchBudget.from_limits(max_source_calls=4)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: budget.acquire_source(), range(50)))
    assert sum(results) == 4
    assert budget.receipt()['source_calls'] == 4


def test_lower_limits_cancel_and_nonfinite_values():
    from src.advisor.research_budget import ResearchBudget
    cancel = threading.Event()
    budget = ResearchBudget.from_limits(max_wall_seconds=90, max_model_calls=2, max_source_calls=1,
        review_reserve_seconds=20, clock=lambda: 0, cancel=cancel)
    assert budget.remaining_seconds() == 70
    cancel.set()
    assert not budget.acquire_source() and not budget.acquire_model(stage='review')
    assert budget.receipt()['cancelled']
    with pytest.raises(ValueError): ResearchBudget.from_limits(max_wall_seconds=float('inf'))
