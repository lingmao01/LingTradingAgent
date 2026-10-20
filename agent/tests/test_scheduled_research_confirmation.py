from __future__ import annotations

import asyncio
import json
from pathlib import Path

from src.channels.bus.events import DeliveryReceipt
from src.channels.targets import DeliveryTarget
from src.scheduled_research.executor import ScheduledResearchExecutor
from src.scheduled_research.models import (
    DeliveryStatus,
    JobStatus,
    ScheduledResearchJob,
)
from src.scheduled_research.store import ScheduledResearchJobStore
from src.scheduled_research.service import build_job_from_draft, public_job
from src.tools.scheduled_research_tool import ScheduledResearchTool


def _draft() -> dict:
    return {
        "title": "Morning scan",
        "source": {"kind": "prompt", "prompt": "Summarize the market."},
        "schedule": {"expression": "60000", "timezone": "Asia/Shanghai"},
        "end_at": 2_000_000,
        "delivery": {"mode": "in_app"},
    }


def test_proposal_does_not_create_until_surface_commit_and_commit_is_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.scheduled_research import proposals

    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    store = ScheduledResearchJobStore(tmp_path / "jobs.json")
    monkeypatch.setattr(proposals, "default_store", lambda: store)
    monkeypatch.setattr(
        proposals,
        "scheduler_status",
        lambda: {"enabled": True, "running": True, "executable": True},
    )
    monkeypatch.setattr("src.scheduled_research.service.time.time", lambda: 1_000)
    monkeypatch.setattr("src.scheduled_research.proposals.time.time", lambda: 1_000)

    result = json.loads(
        ScheduledResearchTool().execute(action="propose_create", draft=_draft())
    )
    assert result["type"] == "scheduled_research.proposal"
    assert result["status"] == "pending"
    assert store.load() == {}

    first = proposals.commit_proposal(result["proposal_id"])
    second = proposals.commit_proposal(result["proposal_id"])
    assert first == second
    assert len(store.load()) == 1
    job = next(iter(store.load().values()))
    assert job.title == "Morning scan"
    assert job.end_at == 2_000_000


def test_elapsed_end_at_expires_without_dispatch(tmp_path: Path) -> None:
    store = ScheduledResearchJobStore(tmp_path / "jobs.json")
    store.upsert(
        ScheduledResearchJob(
            id="ended",
            title="Ended",
            prompt="never run",
            schedule="60000",
            next_run_at=1_000,
            end_at=900,
        )
    )
    dispatched: list[str] = []

    async def dispatch(job: ScheduledResearchJob) -> str:
        dispatched.append(job.id)
        return "session"

    executor = ScheduledResearchExecutor(store, dispatch, enabled=False)
    asyncio.run(executor.tick(now_ms=1_001))
    assert dispatched == []
    assert store.get("ended").status is JobStatus.EXPIRED


def test_configured_delivery_keeps_raw_provider_target_out_of_public_payload(
    monkeypatch,
) -> None:
    from src.scheduled_research import service

    target = DeliveryTarget(
        ref="research-team",
        label="Research Team",
        channel="slack",
        target="raw-provider-destination",
    )
    monkeypatch.setattr(service, "resolve_delivery_target", lambda _ref: target)
    draft = _draft()
    draft["delivery"] = {"mode": "configured", "target_ref": target.ref}

    job = build_job_from_draft(draft, now_ms=1_000)
    payload = public_job(job)

    assert job.delivery_target == "raw-provider-destination"
    assert payload["delivery"]["target_ref"] == "research-team"
    assert payload["delivery"]["target_label"] == "Research Team"
    assert "raw-provider-destination" not in json.dumps(payload)


def test_provider_receipt_is_persisted_as_sent(tmp_path: Path) -> None:
    store = ScheduledResearchJobStore(tmp_path / "jobs.json")
    store.upsert(
        ScheduledResearchJob(
            id="receipt",
            prompt="run",
            schedule="60000",
            next_run_at=1_000,
            delivery_channel="feishu",
            delivery_target="opaque-provider-target",
        )
    )

    async def dispatch(_job: ScheduledResearchJob) -> str:
        return "session-1"

    async def sender(_channel: str, _target: str | None, _text: str) -> DeliveryReceipt:
        return DeliveryReceipt(status="sent", provider_message_id="om_provider_receipt")

    executor = ScheduledResearchExecutor(
        store,
        dispatch,
        enabled=False,
        now_fn=lambda: 1_000,
        briefing_reader=lambda _session: ("completed", "briefing"),
        channel_sender=sender,
    )
    asyncio.run(executor.tick(now_ms=1_000))
    delivery = store.get("receipt").delivery
    assert delivery.status is DeliveryStatus.SENT
    assert delivery.provider_message_id == "om_provider_receipt"


def test_generic_adapter_acknowledgement_is_persisted_as_accepted(
    tmp_path: Path,
) -> None:
    store = ScheduledResearchJobStore(tmp_path / "jobs.json")
    store.upsert(
        ScheduledResearchJob(
            id="accepted",
            prompt="run",
            schedule="60000",
            next_run_at=1_000,
            delivery_channel="slack",
            delivery_target="opaque-provider-target",
        )
    )

    async def dispatch(_job: ScheduledResearchJob) -> str:
        return "session-2"

    async def sender(_channel: str, _target: str | None, _text: str) -> DeliveryReceipt:
        return DeliveryReceipt(status="accepted")

    executor = ScheduledResearchExecutor(
        store,
        dispatch,
        enabled=False,
        now_fn=lambda: 1_000,
        briefing_reader=lambda _session: ("completed", "briefing"),
        channel_sender=sender,
    )
    asyncio.run(executor.tick(now_ms=1_000))
    delivery = store.get("accepted").delivery
    assert delivery.status is DeliveryStatus.ACCEPTED
    assert delivery.provider_message_id is None


def test_im_confirmation_accepts_english_and_chinese_tokens(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """"confirm"/"确认" commit and "cancel"/"取消" discard; free text passes through.

    The IM runtime's exact-match tokens must cover both spellings: the 16
    adapters serve both audiences, and a token set that only matches the
    Chinese words locks English-speaking users out of confirming.
    """
    from src.channels.bus.events import InboundMessage
    from src.channels.bus.queue import MessageBus
    from src.channels.runtime import ChannelRuntime
    from src.scheduled_research import proposals

    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    store = ScheduledResearchJobStore(tmp_path / "jobs.json")
    monkeypatch.setattr(proposals, "default_store", lambda: store)
    monkeypatch.setattr(
        proposals,
        "scheduler_status",
        lambda: {"enabled": True, "running": True, "executable": True},
    )
    monkeypatch.setattr("src.scheduled_research.service.time.time", lambda: 1_000)
    monkeypatch.setattr("src.scheduled_research.proposals.time.time", lambda: 1_000)

    runtime = ChannelRuntime(
        bus=MessageBus(),
        session_service=None,
        manager=None,
        session_map_path=tmp_path / "channel_sessions.json",
    )

    def _msg(content: str) -> InboundMessage:
        return InboundMessage(
            channel="websocket", sender_id="u", chat_id="c", content=content
        )

    async def scenario() -> None:
        # Free text is never treated as a confirmation.
        assert await runtime._handle_scheduled_confirmation(_msg("sounds good"), "s-im") is False

        proposals.propose_create(_draft(), session_id="s-im")
        assert await runtime._handle_scheduled_confirmation(_msg("Confirm"), "s-im") is True
        assert len(store.load()) == 1

        proposals.propose_create(_draft(), session_id="s-im")
        assert await runtime._handle_scheduled_confirmation(_msg("cancel"), "s-im") is True
        assert len(store.load()) == 1  # discarded, not committed

        proposals.propose_create(_draft(), session_id="s-im")
        assert await runtime._handle_scheduled_confirmation(_msg("确认"), "s-im") is True
        assert len(store.load()) == 2

        proposals.propose_create(_draft(), session_id="s-im")
        assert await runtime._handle_scheduled_confirmation(_msg("取消"), "s-im") is True
        assert len(store.load()) == 2

    asyncio.run(scenario())
