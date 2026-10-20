"""The portfolio checkup's rule discipline: playbook contract and run shape.

The checkup prompt is frozen when the job is created, but every fire runs
through the same agent loop as a typed message, so the *current* book and the
*current* standing rules reach the model as freshly built ``<user-book>`` /
``<risk-rules>`` blocks. These tests pin both halves: the playbook text must
demand the rule check and the ``Risk rules`` section, and a scheduled-shaped
turn must actually carry the current blocks — not the ones frozen in the
prompt.

No network: playbooks are local files and message construction is pure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.agent.context import ContextBuilder
from src.agent.grounding import GroundingLedger
from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry
from src.position_book import PositionBookStore, render_book_block
from src.risk_rules import RiskRulesStore, render_risk_block
from src.scheduled_research.playbooks import get_playbook
from src.scheduled_research.service import build_job_from_draft

pytestmark = pytest.mark.unit

SLUG = "portfolio-checkup"


def _draft(**variables: str) -> dict[str, Any]:
    """A portfolio-checkup job draft, with optional variable overrides."""
    return {
        "title": "Weekly checkup",
        "source": {
            "kind": "playbook",
            "playbook_slug": SLUG,
            "variables": variables,
        },
        "schedule": {"expression": "60000", "timezone": "Asia/Shanghai"},
        "delivery": {"mode": "in_app"},
    }


def _block(*rows: str) -> str:
    """Render a figures block, which every draft with a role needs."""
    return "\n\n```figures\n" + "\n".join(rows) + "\n```"


# ---------------------------------------------------------------------------
# Playbook contract: the checkup must demand the rule check
# ---------------------------------------------------------------------------


def test_the_body_demands_the_rule_check() -> None:
    """The checkup reads the fresh blocks and checks stops, targets and caps."""
    body = get_playbook(SLUG).body

    assert "<user-book>" in body
    assert "<risk-rules>" in body
    assert "stop-loss" in body
    assert "take-profit" in body
    assert "maximum quantity" in body


def test_the_risk_rules_section_follows_the_book_summary() -> None:
    """The rule check is the second section: breaches before any statistic."""
    body = get_playbook(SLUG).body
    output = body[body.index("## Output") : body.index("## Boundaries")]

    assert "## Risk rules" in body
    assert "no risk rules recorded" in body
    assert output.index("`## Book summary`") < output.index("`## Risk rules`")
    assert output.index("`## Risk rules`") < output.index("`## Exposure`")


def test_the_verdict_tail_is_unchanged() -> None:
    """The machine-readable tail keeps its states, its format and its place."""
    body = get_playbook(SLUG).body
    output = body[body.index("## Output") : body.index("## Boundaries")]
    unwrapped = " ".join(body.split())

    assert "STATE one of `DRIFT`, `FLAT`" in unwrapped
    # Verdict is the last listed report section: nothing may follow it.
    assert output.rindex("`## ") == output.index("`## Verdict`")


# ---------------------------------------------------------------------------
# The scheduled run's shape: fresh blocks ride above the frozen prompt
# ---------------------------------------------------------------------------


def test_a_scheduled_checkup_runs_on_the_current_book_and_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rules recorded after the job was saved still reach the fire."""
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    PositionBookStore().set_position(
        "600519.SH", quantity=100, cost=1500.0, name="贵州茅台"
    )
    job = build_job_from_draft(_draft(), now_ms=1_000)
    assert "quantity 100" in job.prompt  # the holdings line frozen at creation

    # The book moves and rules are recorded after the prompt was frozen.
    PositionBookStore().set_position("600519.SH", quantity=300)
    RiskRulesStore().set_rule("600519.SH", stop_loss=1400.0, max_quantity=200)
    builder = ContextBuilder(ToolRegistry(), WorkspaceMemory())

    content = builder.build_messages(job.prompt)[-1]["content"]

    assert "quantity 100" in content  # the frozen prompt itself, intact
    assert content.endswith(job.prompt)
    assert "</user-book>" in content and "quantity 300" in content
    assert "</risk-rules>" in content
    assert "stop-loss 1400" in content
    assert "position 300 is over the cap" in content
    assert "## Risk rules" in content  # the contract travelled with the prompt
    assert content.index("<user-book>") < content.index("<risk-rules>")


def test_the_scheduled_turn_reconciles_a_rule_level_as_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop level quoted against the scheduled turn is the user's own."""
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    book = PositionBookStore()
    book.set_position("600519.SH", quantity=300, cost=1500.0)
    rules = RiskRulesStore()
    rules.set_rule("600519.SH", stop_loss=1400.0, max_quantity=200)
    job = build_job_from_draft(_draft(), now_ms=1_000)
    ledger = GroundingLedger(
        run_dir=tmp_path,
        user_message=job.prompt,
        recalled_memory="\n\n".join(
            part
            for part in (
                render_book_block(book),
                render_risk_block(rules, book=book),
            )
            if part
        ),
    )

    draft = "止损线 1400 元未触发，现价高于它；300 股仍超过 200 股上限。" + _block(
        "1400 | held | the stop-loss the user recorded",
        "300 | held | the quantity the user recorded",
        "200 | held | the position cap the user recorded",
    )

    result = ledger.validate_final_answer(draft)

    assert result.valid is True, result.issues