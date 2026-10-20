"""The pre-market brief's rule discipline: playbook contract and run shape.

The brief prompt is frozen when the job is created, but every fire runs
through the same agent loop as a typed message, so the *current* book and the
*current* standing rules reach the model as freshly built ``<user-book>`` /
``<risk-rules>`` blocks. These tests pin both halves: the brief must open on
the rule check, keep the tail it already had, bind its levels to a retrieved
close rather than an assumed open, and a scheduled-shaped turn must carry the
current blocks — not the ones frozen in the prompt.

No network: playbooks are local files and message construction is pure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.agent.context import ContextBuilder
from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry
from src.position_book import PositionBookStore
from src.risk_rules import RiskRulesStore
from src.scheduled_research.playbooks import get_playbook
from src.scheduled_research.service import build_job_from_draft

pytestmark = pytest.mark.unit

SLUG = "premarket-brief"


def _draft(**variables: str) -> dict[str, Any]:
    """A pre-market brief job draft, with optional variable overrides."""
    return {
        "title": "Morning brief",
        "source": {"kind": "playbook", "playbook_slug": SLUG, "variables": variables},
        "schedule": {"expression": "60000", "timezone": "Asia/Shanghai"},
        "delivery": {"mode": "in_app"},
    }


# ---------------------------------------------------------------------------
# Playbook contract: the brief opens on the rule check
# ---------------------------------------------------------------------------


def test_the_body_demands_the_rule_check() -> None:
    """The brief reads the fresh blocks and checks stops, targets and caps."""
    body = get_playbook(SLUG).body

    assert "<user-book>" in body
    assert "<risk-rules>" in body
    assert "stop-loss" in body
    assert "take-profit" in body
    assert "maximum quantity" in body


def test_the_rule_check_opens_the_brief() -> None:
    """The check is the first section: breaches before any tape narration."""
    body = get_playbook(SLUG).body
    output = body[body.index("## Output") : body.index("## Boundaries")]

    assert "## Risk rules" in body
    assert "no risk rules recorded" in body
    assert output.index("`## Risk rules`") < output.index("`## Overnight tape`")
    assert output.index("`## Overnight tape`") < output.index("`## Watch list`")


def test_the_verdict_tail_is_unchanged() -> None:
    """The machine-readable tail keeps its states, its format and its place."""
    body = get_playbook(SLUG).body
    output = body[body.index("## Output") : body.index("## Boundaries")]
    unwrapped = " ".join(body.split())

    assert "STATE one of `HOT`, `MIXED`, `QUIET`" in unwrapped
    assert "- SYMBOL: STATE - one short reason" in unwrapped
    # Verdict is the last listed report section: nothing may follow it.
    assert output.rindex("`## ") == output.index("`## Verdict`")


def test_the_brief_does_not_pretend_to_know_the_open() -> None:
    """Pre-open, the check binds to a retrieved close, never an assumed print."""
    unwrapped = " ".join(get_playbook(SLUG).body.split())

    assert "The brief does not know the open" in unwrapped
    assert "the check used the latest retrieved close" in unwrapped


# ---------------------------------------------------------------------------
# The scheduled run's shape: fresh blocks ride above the frozen prompt
# ---------------------------------------------------------------------------


def test_a_scheduled_brief_runs_on_the_current_book_and_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rules recorded after the job was saved still reach the fire."""
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    PositionBookStore().set_position(
        "600519.SH", quantity=100, cost=1500.0, name="贵州茅台"
    )
    job = build_job_from_draft(_draft(), now_ms=1_000)
    assert "(no watch list configured)" in job.prompt  # frozen at creation

    # The book moves and rules are recorded after the prompt was frozen.
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