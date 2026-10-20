"""A busy discovery loop must stop visibly when it cannot gain information."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent.grounding import GroundingLedger
from src.agent.loop import AgentLoop
from src.agent.tools import BaseTool, ToolRegistry
from src.agent.trace import TraceWriter

#: A distinctive digest the ledger returns in these tests, so injection into a
#: withheld-tool turn is proven by that turn's own message list.
DIGEST_LINE = "close=3811.9 (prices::close) [as of 2026-10-08]"


class DiscoveryTool(BaseTool):
    name = "read_document"
    description = "Read an artifact (offline test double)."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    is_readonly = True
    repeatable = True

    def __init__(self, results: list[str]) -> None:
        self.results = results
        self.calls = 0

    def execute(self, **kwargs: object) -> str:
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]


class DiscoveryLLM:
    model_name = "offline"

    def __init__(self, *, vary_arguments: bool = False, finish_after: int = 50) -> None:
        self.calls = 0
        self.vary_arguments = vary_arguments
        self.finish_after = finish_after

    def stream_chat(self, messages: list[dict], **kwargs: object) -> SimpleNamespace:
        self.calls += 1
        done = self.calls > self.finish_after
        return SimpleNamespace(
            content="Found the artifact." if done else "",
            reasoning_content=None,
            has_tool_calls=not done,
            tool_calls=(
                []
                if done
                else [
                    SimpleNamespace(
                        id=f"read-{self.calls}",
                        name="read_document",
                        arguments={
                            "path": (
                                f"guess-{self.calls}"
                                if self.vary_arguments
                                else "missing.csv"
                            )
                        },
                    )
                ]
            ),
        )


class RecordingDiscoveryLLM(DiscoveryLLM):
    """DiscoveryLLM that snapshots the message list handed to every turn."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.snapshots: list[list[dict]] = []

    def stream_chat(self, messages: list[dict], **kwargs: object) -> SimpleNamespace:
        self.snapshots.append([dict(message) for message in messages])
        return super().stream_chat(messages, **kwargs)


def _patch_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the ledger a digest, as if it held observed quotes."""
    monkeypatch.setattr(
        GroundingLedger,
        "verified_evidence_digest",
        lambda self: f"- 000001.SH: {DIGEST_LINE}",
    )


def _digest_rides_with(llm: RecordingDiscoveryLLM, instruction: str) -> bool:
    """Whether one message carries both the instruction and the digest."""
    return any(
        instruction in (message.get("content") or "")
        and DIGEST_LINE in (message.get("content") or "")
        for snapshot in llm.snapshots
        for message in snapshot
    )


def build_loop(tmp_path: Path, tool: DiscoveryTool, llm: DiscoveryLLM):
    registry = ToolRegistry()
    registry.register(tool)
    events = []
    loop = AgentLoop(
        registry=registry,
        llm=llm,
        max_iterations=24,
        event_callback=lambda name, data: events.append((name, data)),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    loop.memory.run_dir = str(run_dir)
    return loop, events, run_dir


@pytest.mark.parametrize("vary_arguments", [False, True])
def test_failed_discovery_stops_with_visible_recovery(tmp_path, vary_arguments):
    tool = DiscoveryTool(['{"status":"error","message":"File not found"}'])
    loop, events, run_dir = build_loop(
        tmp_path, tool, DiscoveryLLM(vary_arguments=vary_arguments)
    )
    result = loop.run("Find the artifacts from my previous backtest.")

    assert result["status"] == "failed"
    # Stall-out eight times -> one text-only convergence turn (the model calls
    # tools anyway, so it is refused) -> the next judgment fails the run.
    assert result["iterations"] == 10
    # A repeated identity is refused on the SECOND failure, not the first:
    # one honest retry runs, the third attempt is blocked. Distinct identities
    # keep running until the convergence turn blocks the unoffered call.
    assert tool.calls == (9 if vary_arguments else 2)
    assert "no_progress" in result["reason"]
    assert "path" in result["content"]
    assert "rerun" in result["content"]
    assert any(
        name == "text_delta" and data["delta"] == result["content"]
        for name, data in events
    )
    assert json.loads((run_dir / "state.json").read_text())["status"] == "failed"
    records = list(TraceWriter.read(run_dir))
    assert any(r["type"] == "no_progress_convergence" for r in records)
    assert any(r["type"] == "no_progress" for r in records)
    assert records[-1]["type"] == "end" and records[-1]["status"] == "failed"


@pytest.mark.parametrize("readonly", [True, False])
def test_new_observations_or_distinct_writes_keep_running(tmp_path, readonly):
    results = [json.dumps({"status": "ok", "value": i}) for i in range(12)]
    tool = DiscoveryTool(results if readonly else ['{"status":"ok"}'])
    tool.is_readonly = readonly
    loop, _, _ = build_loop(
        tmp_path, tool, DiscoveryLLM(vary_arguments=True, finish_after=12)
    )
    result = loop.run("Read each artifact.")
    assert result["status"] == "success"
    assert result["iterations"] == 13
    assert tool.calls == 12


def test_rephrased_reads_of_the_same_result_are_not_progress(tmp_path):
    tool = DiscoveryTool(['{"status":"ok","snippets":[]}'])
    loop, _, _ = build_loop(
        tmp_path, tool, DiscoveryLLM(vary_arguments=True)
    )
    result = loop.run("Find the prior artifact.")
    assert result["status"] == "failed"
    # First result is new, later ones are not: stall-out at nine, the refused
    # convergence turn at ten, and the failing judgment after the model's last
    # real tool round at eleven.
    assert result["iterations"] == 11
    assert "no_progress" in result["reason"]


def test_failed_call_ledger_resets_for_a_new_run(tmp_path):
    tool = DiscoveryTool(['{"status":"error"}'])
    loop, _, _ = build_loop(tmp_path, tool, DiscoveryLLM())
    first = loop.run("Read the artifact.")
    second = loop.run("Try again after I repaired the artifact.")
    assert first["iterations"] == second["iterations"] == 10
    # Two executions per run (fail, retry, then blocked), and the second run
    # starts from an empty ledger rather than inheriting the first run's.
    assert tool.calls == 4


def test_old_metrics_do_not_turn_no_progress_into_success(tmp_path):
    tool = DiscoveryTool(['{"status":"error"}'])
    loop, _, run_dir = build_loop(tmp_path, tool, DiscoveryLLM())
    (run_dir / "artifacts").mkdir()
    (run_dir / "artifacts" / "metrics.csv").write_text("return\n0.1\n")
    result = loop.run("Explain this previous run using its original trades.")
    assert result["status"] == "failed"
    assert "no_progress" in result["reason"]


def test_successful_repair_allows_a_previously_failed_read(tmp_path):
    from src.agent.context import ContextBuilder

    tool = DiscoveryTool(['{"status":"error"}', '{"status":"ok","data":"repaired"}'])
    loop, _, run_dir = build_loop(tmp_path, tool, DiscoveryLLM())
    repair = DiscoveryTool(['{"status":"ok"}'])
    repair.name = "repair_file"
    repair.is_readonly = False
    loop.registry.register(repair)
    trace = TraceWriter(run_dir)
    messages = []
    for i, name in enumerate([tool.name, repair.name, tool.name]):
        loop._process_tool_calls(
            [SimpleNamespace(id=str(i), name=name, arguments={"path": "missing.csv"})],
            ContextBuilder,
            messages,
            trace,
            [],
            i,
        )
    trace.close()
    assert tool.calls == 2
    assert json.loads(messages[-1]["content"])["data"] == "repaired"


def test_authorization_denial_does_not_poison_an_unexecuted_call(tmp_path):
    from src.agent.context import ContextBuilder

    tool = DiscoveryTool(['{"status":"ok"}'])
    loop, _, run_dir = build_loop(tmp_path, tool, DiscoveryLLM())
    trace = TraceWriter(run_dir)
    messages = []
    call = SimpleNamespace(
        id="denied", name=tool.name, arguments={"path": "artifact.csv"}
    )
    loop._record_blocked_tool_call(
        call,
        '{"status":"error","reason":"identity not yet resolved"}',
        ContextBuilder,
        messages,
        trace,
        [],
        1,
    )
    call = SimpleNamespace(id="authorized", name=tool.name, arguments=call.arguments)
    loop._process_tool_calls([call], ContextBuilder, messages, trace, [], 2)
    trace.close()
    assert tool.calls == 1


def test_a_transient_failure_survives_one_identical_retry(tmp_path):
    """The case the block is NOT for: a read that fails once for a reason that
    has nothing to do with its arguments.

    Only a successful *mutating* call clears the failed ledger, and a research
    run may have none, so refusing on the first failure made a rate limit /
    network blip / tool timeout permanent for the whole run.
    """
    tool = DiscoveryTool(
        [
            '{"status":"error","error":"rate limited, retry later"}',
            '{"status":"ok","data":"artifact"}',
        ]
    )
    loop, _, _ = build_loop(tmp_path, tool, DiscoveryLLM(finish_after=2))
    result = loop.run("Read the artifact.")

    assert tool.calls == 2, "the identical retry after a transient failure must run"
    assert result["status"] == "success"


def test_failure_block_threshold_is_two_sided():
    """Both sides of the gate, so a future change cannot quietly move it."""
    from src.agent.tool_progress import ToolProgress

    progress = ToolProgress()
    key = ("read_document", '{"path":"missing.csv"}')

    progress.record("read_document", key, '{"status":"error"}', success=False)
    assert not progress.is_blocked(key), "one failure must not block a retry"

    progress.record("read_document", key, '{"status":"error"}', success=False)
    assert progress.is_blocked(key), "the second identical failure must block"

    progress.record(
        "repair_file", ("repair_file", "{}"), '{"status":"ok"}',
        success=True, is_readonly=False,
    )
    assert not progress.is_blocked(key), "a successful mutation clears the ledger"


@pytest.mark.parametrize("vary_arguments", [False, True])
def test_withheld_tool_turns_carry_the_ledger_digest(
    tmp_path, monkeypatch, vary_arguments
):
    """The convergence and refusal turns quote the ledger's observations.

    Layer-1 compaction can empty the working context; without the digest a
    tools-withheld turn reported data the session really observed as "not
    retrieved" (2026-10-08 run).
    """
    _patch_digest(monkeypatch)
    tool = DiscoveryTool(['{"status":"error","message":"File not found"}'])
    llm = RecordingDiscoveryLLM(vary_arguments=vary_arguments)
    loop, _, _ = build_loop(tmp_path, tool, llm)
    result = loop.run("Find the artifacts from my previous backtest.")

    assert result["status"] == "failed"
    assert _digest_rides_with(llm, "Tool calling is closed for this turn")
    assert _digest_rides_with(llm, "No further tool data will change this answer")


def test_the_final_text_only_turn_carries_the_ledger_digest(tmp_path, monkeypatch):
    _patch_digest(monkeypatch)
    tool = DiscoveryTool(['{"status":"ok"}'])
    tool.is_readonly = False
    llm = RecordingDiscoveryLLM(vary_arguments=True)
    loop, _, _ = build_loop(tmp_path, tool, llm)
    result = loop.run("Refresh each dataset.")

    assert result["iterations"] == 24, "the run must reach the forced text-only cap"
    assert len(llm.snapshots) == 24
    final = [
        message
        for message in llm.snapshots[-1]
        if "[SYSTEM] Verified evidence this session already retrieved"
        in (message.get("content") or "")
    ]
    assert final and DIGEST_LINE in final[-1]["content"], (
        "the last-iteration turn must quote the ledger"
    )
    assert all(
        DIGEST_LINE not in (message.get("content") or "")
        for snapshot in llm.snapshots[:-1]
        for message in snapshot
    ), "no earlier turn may carry the digest"
