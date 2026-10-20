"""Tests for the scheduled-research verdict record and its parser."""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.scheduled_research.executor import ScheduledResearchExecutor
from src.scheduled_research.models import (
    DeliveryRecord,
    DeliveryStatus,
    JobStatus,
    ScheduledResearchJob,
)
from src.scheduled_research.store import ScheduledResearchJobStore
from src.scheduled_research.verdict import (
    PARSE_CONTRACT_VIOLATION,
    PARSE_NO_SECTION,
    PARSE_OK,
    VerdictRecord,
    outcome_of,
    parse_verdict_section,
)


def test_no_verdict_section() -> None:
    parse, items = parse_verdict_section("## Book summary\nall quiet\n## Data gaps\nnone\n")
    assert parse == PARSE_NO_SECTION
    assert items == []


def test_verdict_section_with_items() -> None:
    text = (
        "## Overnight tape\nstuff\n"
        "## Verdict\n"
        "- 600519.SH: FLAT - weight stayed inside the band\n"
        "- 0700.HK: DRIFT - weight crossed the stated band\n"
        "\n"
        "## Data gaps\n"
        "none\n"
    )
    parse, items = parse_verdict_section(text)
    assert parse == PARSE_OK
    assert [(i.symbol, i.state, i.reason) for i in items] == [
        ("600519.SH", "FLAT", "weight stayed inside the band"),
        ("0700.HK", "DRIFT", "weight crossed the stated band"),
    ]


def test_verdict_section_empty_is_a_real_answer() -> None:
    parse, items = parse_verdict_section("## Book summary\n...\n## Verdict\n\n## Data gaps\nnone\n")
    assert parse == PARSE_OK
    assert items == []
    assert outcome_of(items) == "no_calls"


def test_verdict_section_stops_at_next_heading() -> None:
    text = "## Verdict\n- AAPL: QUIET - nothing moved\n## Data gaps\n- not: an item\n"
    parse, items = parse_verdict_section(text)
    assert parse == PARSE_OK
    assert len(items) == 1


def test_malformed_line_fails_closed() -> None:
    parse, items = parse_verdict_section("## Verdict\nthis line is not a contract line\n")
    assert parse == PARSE_CONTRACT_VIOLATION
    assert items == []


def test_reason_is_optional() -> None:
    parse, items = parse_verdict_section("## Verdict\n- TSLA: HOT\n")
    assert parse == PARSE_OK
    assert items[0].reason == ""


def test_a_multi_word_state_parses() -> None:
    """A risk watch writes `OVER CAP`; a portfolio gate writes `NO ADD`."""
    text = (
        "## Verdict\n"
        "- 600519.SH: OVER CAP - size above the recorded maximum\n"
        "- PORTFOLIO: NO ADD - the block's no-add gate is open\n"
        "- PORTFOLIO: ADD ALLOWED - every gate is open\n"
    )
    parse, items = parse_verdict_section(text)
    assert parse == PARSE_OK
    assert [(i.symbol, i.state, i.reason) for i in items] == [
        ("600519.SH", "OVER CAP", "size above the recorded maximum"),
        ("PORTFOLIO", "NO ADD", "the block's no-add gate is open"),
        ("PORTFOLIO", "ADD ALLOWED", "every gate is open"),
    ]


def test_outcome_mixed_and_uniform() -> None:
    _, items = parse_verdict_section("## Verdict\n- A: FLAT - x\n- B: FLAT - y\n")
    assert outcome_of(items) == "FLAT"
    _, mixed = parse_verdict_section("## Verdict\n- A: FLAT - x\n- B: DRIFT - y\n")
    assert outcome_of(mixed) == "mixed"


def _record(session: str, at: int, symbol: str = "AAPL") -> VerdictRecord:
    return VerdictRecord(
        session_id=session,
        recorded_at=at,
        parse=PARSE_OK,
        outcome="FLAT",
        items=[],
    )


def test_verdict_record_serde_roundtrip() -> None:
    record = _record("s2", 2000)
    record.previous = _record("s1", 1000)
    restored = VerdictRecord.from_dict(record.to_dict())
    assert restored.session_id == "s2"
    assert restored.previous is not None and restored.previous.session_id == "s1"
    assert restored.previous.previous is None


def test_verdict_record_from_missing_fields_is_lenient() -> None:
    restored = VerdictRecord.from_dict({})
    assert restored.parse == PARSE_NO_SECTION
    assert restored.items == []


def _job_with_session(job_id: str, session_id: str) -> ScheduledResearchJob:
    return ScheduledResearchJob(
        id=job_id,
        prompt=f"prompt for {job_id}",
        schedule="60000",
        status=JobStatus.COMPLETED,
        delivery=DeliveryRecord(status=DeliveryStatus.NONE, session_id=session_id),
    )


def test_sweep_records_verdict_for_channel_less_job(tmp_path: Path) -> None:
    store = ScheduledResearchJobStore(path=tmp_path / "jobs.json")
    store.upsert(_job_with_session("job-1", "sess-1"), validate=False)
    briefing = "## Book summary\n...\n## Verdict\n- 600519.SH: DRIFT - band crossed\n"

    def read_briefing(session_id: str):
        assert session_id == "sess-1"
        return ("completed", briefing)

    async def scenario() -> int:
        executor = ScheduledResearchExecutor(store, _noop_dispatch, briefing_reader=read_briefing)
        return await executor.sweep_deliveries()

    changed = asyncio.run(scenario())
    assert changed == 1
    saved = store.get("job-1")
    assert saved is not None and saved.last_verdict is not None
    assert saved.last_verdict.session_id == "sess-1"
    assert saved.last_verdict.parse == PARSE_OK
    assert saved.last_verdict.outcome == "DRIFT"
    assert saved.last_verdict.items[0].symbol == "600519.SH"
    assert saved.last_verdict.previous is None


def test_sweep_verdict_is_written_once_per_firing(tmp_path: Path) -> None:
    store = ScheduledResearchJobStore(path=tmp_path / "jobs.json")
    store.upsert(_job_with_session("job-1", "sess-1"), validate=False)

    def read_briefing(session_id: str):
        return ("completed", "## Verdict\n- A: QUIET - x\n")

    async def scenario() -> tuple[int, int]:
        executor = ScheduledResearchExecutor(store, _noop_dispatch, briefing_reader=read_briefing)
        first = await executor.sweep_deliveries()
        second = await executor.sweep_deliveries()
        return first, second

    first, second = asyncio.run(scenario())
    assert (first, second) == (1, 0)


def test_sweep_shifts_previous_verdict_one_level(tmp_path: Path) -> None:
    store = ScheduledResearchJobStore(path=tmp_path / "jobs.json")
    store.upsert(_job_with_session("job-1", "sess-1"), validate=False)
    briefings = {
        "sess-1": ("completed", "## Verdict\n- A: QUIET - first\n"),
        "sess-2": ("completed", "## Verdict\n- A: DRIFT - second\n"),
    }

    def read_briefing(session_id: str):
        return briefings[session_id]

    async def scenario() -> None:
        executor = ScheduledResearchExecutor(store, _noop_dispatch, briefing_reader=read_briefing)
        await executor.sweep_deliveries()
        job = store.get("job-1")
        assert job is not None
        job.delivery = DeliveryRecord(status=DeliveryStatus.NONE, session_id="sess-2")
        store.upsert(job, validate=False)
        await executor.sweep_deliveries()

    asyncio.run(scenario())
    saved = store.get("job-1")
    assert saved is not None and saved.last_verdict is not None
    assert saved.last_verdict.session_id == "sess-2"
    previous = saved.last_verdict.previous
    assert previous is not None and previous.session_id == "sess-1"
    assert previous.previous is None  # the chain stays one level deep


def test_sweep_leaves_verdict_alone_while_in_flight(tmp_path: Path) -> None:
    store = ScheduledResearchJobStore(path=tmp_path / "jobs.json")
    store.upsert(_job_with_session("job-1", "sess-1"), validate=False)

    def read_briefing(session_id: str):
        return None  # still running

    async def scenario() -> int:
        executor = ScheduledResearchExecutor(store, _noop_dispatch, briefing_reader=read_briefing)
        return await executor.sweep_deliveries()

    assert asyncio.run(scenario()) == 0
    saved = store.get("job-1")
    assert saved is not None and saved.last_verdict is None


def test_sweep_records_how_the_run_ended_for_channel_less_job(tmp_path: Path) -> None:
    """The job's own status says "accepted"; this is what the run produced."""
    store = ScheduledResearchJobStore(path=tmp_path / "jobs.json")
    store.upsert(_job_with_session("job-1", "sess-1"), validate=False)
    briefing = "## Verdict\n- 600519.SH: DRIFT - band crossed\n"

    def read_run(session_id: str):
        assert session_id == "sess-1"
        return ("completed", briefing, False)

    async def scenario() -> int:
        executor = ScheduledResearchExecutor(store, _noop_dispatch, run_reader=read_run)
        return await executor.sweep_deliveries()

    assert asyncio.run(scenario()) == 1
    saved = store.get("job-1")
    assert saved is not None and saved.last_run is not None
    assert saved.last_run.session_id == "sess-1"
    assert saved.last_run.outcome == "completed"
    assert saved.last_run.degraded is False
    assert saved.last_run.excerpt == "## Verdict - 600519.SH: DRIFT - band crossed"
    assert saved.last_run.recorded_at is not None


def test_sweep_marks_a_deterministic_fallback_as_degraded(tmp_path: Path) -> None:
    """A fallback reports "completed" on the wire but is not a real result."""
    store = ScheduledResearchJobStore(path=tmp_path / "jobs.json")
    store.upsert(_job_with_session("job-1", "sess-1"), validate=False)

    def read_run(session_id: str):
        return ("completed", "verified prices only", True)

    async def scenario() -> int:
        executor = ScheduledResearchExecutor(store, _noop_dispatch, run_reader=read_run)
        return await executor.sweep_deliveries()

    assert asyncio.run(scenario()) == 1
    saved = store.get("job-1")
    assert saved is not None and saved.last_run is not None
    assert saved.last_run.outcome == "completed"
    assert saved.last_run.degraded is True


def test_sweep_records_a_failed_run_without_an_excerpt(tmp_path: Path) -> None:
    """A run that ended failed is recorded as such — not left reading "accepted"."""
    store = ScheduledResearchJobStore(path=tmp_path / "jobs.json")
    store.upsert(_job_with_session("job-1", "sess-1"), validate=False)

    def read_run(session_id: str):
        return ("failed", "", False)

    async def scenario() -> int:
        executor = ScheduledResearchExecutor(store, _noop_dispatch, run_reader=read_run)
        return await executor.sweep_deliveries()

    assert asyncio.run(scenario()) == 1
    saved = store.get("job-1")
    assert saved is not None and saved.last_run is not None
    assert saved.last_run.outcome == "failed"
    assert saved.last_run.excerpt is None
    assert saved.last_run.degraded is False
    assert saved.last_verdict is None  # nothing to parse without a briefing


def test_sweep_records_the_run_once_per_firing(tmp_path: Path) -> None:
    store = ScheduledResearchJobStore(path=tmp_path / "jobs.json")
    store.upsert(_job_with_session("job-1", "sess-1"), validate=False)

    def read_run(session_id: str):
        return ("completed", "briefing", False)

    async def scenario() -> tuple[int, int]:
        executor = ScheduledResearchExecutor(store, _noop_dispatch, run_reader=read_run)
        first = await executor.sweep_deliveries()
        second = await executor.sweep_deliveries()
        return first, second

    assert asyncio.run(scenario()) == (1, 0)


def test_run_excerpt_is_bounded_and_collapses_whitespace() -> None:
    from src.scheduled_research.executor import _run_excerpt

    long_text = "line one\nline two\n" + ("x" * 500)
    excerpt = _run_excerpt(long_text)
    assert excerpt is not None
    assert "\n" not in excerpt
    assert len(excerpt) <= 280
    assert excerpt.startswith("line one line two")
    # A blank answer yields no excerpt, so the row does not show an empty gap.
    assert _run_excerpt("   \n  ") is None


async def _noop_dispatch(job: ScheduledResearchJob) -> None:
    return None
