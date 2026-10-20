"""The daily risk watch's contract: playbook text and scheduled-run shape.

Daily risk watch is the after-close companion of the checkup: a compact
weekday rule inspection. Its prompt is frozen when the job is created, but
every fire runs through the same agent loop as a typed message, so the
*current* book and the *current* standing rules reach the model as freshly
built ``<user-book>`` / ``<risk-rules>`` blocks. These tests pin both halves:
the playbook text must open on the rule check and keep the machine-readable
tail, and a scheduled-shaped turn must carry the current blocks and levels.

No network: playbooks are local files and message construction is pure.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from src.agent.context import ContextBuilder
from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry
from src.position_book import PositionBookStore
from src.risk_rules import RiskRulesStore
from src.scheduled_research.executor import next_due
from src.scheduled_research.playbooks import build_job, get_playbook
from src.scheduled_research.service import build_job_from_draft

pytestmark = pytest.mark.unit

SLUG = "daily-risk-watch"

# 2026-08-04T12:00:00Z, a Tuesday: the next authored 16:00 fire is Wednesday.
NOW_MS = 1785585600000


def _draft() -> dict[str, Any]:
    """A daily-risk-watch job draft; the template declares no variables."""
    return {
        "title": "After-close watch",
        "source": {"kind": "playbook", "playbook_slug": SLUG, "variables": {}},
        "schedule": {"expression": "60000", "timezone": "Asia/Shanghai"},
        "delivery": {"mode": "in_app"},
    }


# ---------------------------------------------------------------------------
# Playbook contract: the watch opens on the rule check
# ---------------------------------------------------------------------------


def test_the_watch_opens_on_the_rule_check() -> None:
    """The rule inspection is the first report section, before any move."""
    body = get_playbook(SLUG).body
    output = body[body.index("## Output") : body.index("## Boundaries")]

    assert "## Risk rules" in body
    assert "no risk rules recorded" in body
    assert output.index("`## Risk rules`") < output.index("`## Moves`")
    assert output.index("`## Moves`") < output.index("`## Stance`")


def test_the_watch_reads_the_fresh_blocks_only() -> None:
    """No level is frozen into the template; the blocks are the only source."""
    body = get_playbook(SLUG).body

    assert "<user-book>" in body
    assert "<risk-rules>" in body
    assert "stop-loss" in body
    assert "take-profit" in body
    assert "maximum quantity" in body


def test_the_verdict_tail_stays_machine_readable_and_last() -> None:
    """The tail keeps its headline states, its format and its place."""
    body = get_playbook(SLUG).body
    output = body[body.index("## Output") : body.index("## Boundaries")]
    unwrapped = " ".join(body.split())

    assert "STATE one of `BREACHED`, `REACHED`, `OVER CAP`, `CLEAR`" in unwrapped
    assert "- SYMBOL: STATE - one short reason" in unwrapped
    # Verdict is the last listed report section: nothing may follow it.
    assert output.rindex("`## ") == output.index("`## Verdict`")


def test_the_watch_never_trades() -> None:
    """Stance stays conditional and the report never touches an order."""
    unwrapped = " ".join(get_playbook(SLUG).body.split())

    assert "conditional action" in unwrapped
    assert "trigger" in unwrapped
    assert "invalidation" in unwrapped
    assert "Do not place, modify, or cancel any order" in unwrapped


def test_the_suggested_cadence_is_after_the_close_on_weekdays() -> None:
    playbook = get_playbook(SLUG)

    assert playbook.suggested_schedule == "0 16 * * 1-5"
    assert playbook.suggested_timezone == "Asia/Shanghai"


def test_the_first_fire_lands_on_a_weekday_afternoon() -> None:
    """A Tuesday-noon UTC creation first fires Wednesday 16:00 in Shanghai."""
    job = build_job(SLUG, now_ms=NOW_MS)

    assert job.next_run_at == next_due("0 16 * * 1-5", NOW_MS, "Asia/Shanghai")
    local = datetime.fromtimestamp(job.next_run_at / 1000, ZoneInfo("Asia/Shanghai"))
    assert local.weekday() < 5
    assert (local.hour, local.minute) == (16, 0)
    assert "{{" not in job.prompt


# ---------------------------------------------------------------------------
# The scheduled run's shape: fresh blocks ride above the frozen prompt
# ---------------------------------------------------------------------------


def test_a_scheduled_watch_runs_on_the_current_book_and_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Levels recorded after the job was saved still reach the fire."""
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    PositionBookStore().set_position("600519.SH", quantity=100, cost=1500.0, name="贵州茅台")
    job = build_job_from_draft(_draft(), now_ms=1_000)

    # The book and the rules move after the prompt was frozen.
    PositionBookStore().set_position("600519.SH", quantity=300)
    RiskRulesStore().set_rule("600519.SH", stop_loss=1400.0, max_quantity=200)
    builder = ContextBuilder(ToolRegistry(), WorkspaceMemory())

    content = builder.build_messages(job.prompt)[-1]["content"]

    assert content.endswith(job.prompt)  # the frozen prompt itself, intact
    assert "## Risk rules" in content  # the contract travelled with the prompt
    assert "</user-book>" in content and "quantity 300" in content
    assert "</risk-rules>" in content
    assert "stop-loss 1400" in content
    assert "position 300 is over the cap" in content
    assert content.index("<user-book>") < content.index("<risk-rules>")