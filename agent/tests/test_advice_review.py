"""The advice review's contract: playbook text and scheduled-run shape.

The advice review is the weekly companion of the advice journal: it grades the
open calls the agent recorded against prices retrieved on the run, and closes
the ones whose thesis has played out or failed. Its prompt is frozen when the
job is created, but every fire runs through the same agent loop as a typed
message, so the *current* journal reaches the model as a freshly built
``<advice-journal>`` block. These tests pin both halves: the playbook text must
open on the journal review, keep the machine-readable tail and the close
discipline, and a scheduled-shaped turn must carry the current open entries --
and nothing already closed.

No network: playbooks are local files and message construction is pure.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from src.advice_journal import AdviceJournalStore
from src.agent.context import ContextBuilder
from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry
from src.scheduled_research.executor import next_due
from src.scheduled_research.playbooks import build_job, get_playbook
from src.scheduled_research.service import build_job_from_draft

pytestmark = pytest.mark.unit

SLUG = "advice-review"

# 2026-08-04T12:00:00Z, a Tuesday: the next authored Sunday 18:00 fire is 08-09.
NOW_MS = 1785585600000


def _draft() -> dict[str, Any]:
    """An advice-review job draft; the template declares no variables."""
    return {
        "title": "Weekly advice review",
        "source": {"kind": "playbook", "playbook_slug": SLUG, "variables": {}},
        "schedule": {"expression": "0 18 * * 0", "timezone": "Asia/Shanghai"},
        "delivery": {"mode": "in_app"},
    }


# ---------------------------------------------------------------------------
# Playbook contract: the review grades the journal, and only closes it
# ---------------------------------------------------------------------------


def test_the_review_reads_the_journal_block_only() -> None:
    """No entry is frozen into the template; the block is the only source."""
    body = get_playbook(SLUG).body

    assert "<advice-journal>" in body
    assert "no open entries" in body
    assert "never fall back to an earlier run's journal" in body


def test_close_is_the_only_write_and_it_never_beautifies() -> None:
    """The journal may be closed, never rewritten; tidying is forbidden."""
    unwrapped = " ".join(get_playbook(SLUG).body.split())

    assert "Closing is the only legacy-entry write this review performs" in unwrapped
    assert "close note" in unwrapped
    assert "Never rewrite, reword, or remove a recorded entry" in unwrapped
    assert "never close an entry just because its review is due" in unwrapped


def test_the_report_opens_on_the_journal_review_and_holds_the_tail() -> None:
    """Grading first, then the closes, the standing calls, and the tail."""
    body = get_playbook(SLUG).body
    output = body[body.index("## Output") : body.index("## Boundaries")]

    assert output.index("`## Journal review`") < output.index("`## Closed this run`")
    assert output.index("`## Closed this run`") < output.index("`## Next check`")
    assert output.index("`## Next check`") < output.index("`## Data gaps`")
    # Verdict is the last listed report section: nothing may follow it.
    assert output.rindex("`## ") == output.index("`## Verdict`")


def test_the_outcome_words_stay_machine_readable() -> None:
    """The four outcome words, their order, and the one-line tail format."""
    unwrapped = " ".join(get_playbook(SLUG).body.split())

    assert (
        "OUTCOME one of `PLAYED OUT`, `RUNNING`, `INVALIDATED`, `UNGRADED`"
        in unwrapped
    )
    assert "- #ID SYMBOL: OUTCOME - one short reason" in unwrapped
    assert "List `INVALIDATED` lines first and `UNGRADED` lines last" in unwrapped


def test_a_single_session_is_not_an_outcome() -> None:
    """A move smaller than the recent daily range is noise, not a result."""
    unwrapped = " ".join(get_playbook(SLUG).body.split())

    assert "A move smaller than the recent daily range is noise, not an outcome" in unwrapped
    assert "graded `UNGRADED` on its line" in unwrapped
    assert "named in `Data gaps`" in unwrapped


def test_the_first_fire_lands_on_a_sunday_evening() -> None:
    """A Tuesday-noon UTC creation first fires Sunday 18:00 in Shanghai."""
    playbook = get_playbook(SLUG)
    job = build_job(SLUG, now_ms=NOW_MS)

    assert playbook.suggested_schedule == "0 18 * * 0"
    assert playbook.suggested_timezone == "Asia/Shanghai"
    assert job.next_run_at == next_due("0 18 * * 0", NOW_MS, "Asia/Shanghai")
    local = datetime.fromtimestamp(job.next_run_at / 1000, ZoneInfo("Asia/Shanghai"))
    assert local.weekday() == 6
    assert (local.hour, local.minute) == (18, 0)
    assert "{{" not in job.prompt


# ---------------------------------------------------------------------------
# The scheduled run's shape: the open journal rides above the frozen prompt
# ---------------------------------------------------------------------------


def test_a_scheduled_review_runs_on_the_open_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entries recorded after the job was saved still reach the fire, and a
    closed entry no longer rides along."""
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    journal = AdviceJournalStore()
    journal.record(
        "600519.SH", action="reduce", reference_price=1600.0, rationale="trim into strength"
    )
    second = journal.record("000001.SZ", action="watch", reference_price=12.5)
    journal.close(second.id, note="the turn did not come")
    job = build_job_from_draft(_draft(), now_ms=1_000)

    builder = ContextBuilder(ToolRegistry(), WorkspaceMemory())
    content = builder.build_messages(job.prompt)[-1]["content"]

    assert content.endswith(job.prompt)  # the frozen prompt itself, intact
    assert "<advice-journal>" in content and "</advice-journal>" in content
    block = content[content.index("<advice-journal>") : content.index("</advice-journal>")]
    assert "- #1 600519.SH: reduce, reference 1600" in block
    assert "#2" not in block  # closed entries have already done their turn