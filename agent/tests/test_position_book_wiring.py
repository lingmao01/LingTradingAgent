"""The position book's three seams: context, grounding corpus, playbook jobs.

The book is only useful if it reaches the places the user's own numbers are
read from: the turn's message context (``<user-book>``), the grounding
ledger's user corpus (a ``held`` figure the user recorded must reconcile), and
the scheduled-research playbook that cannot run without holdings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.agent.context import ContextBuilder
from src.agent.grounding import GroundingLedger, ValidationResult
from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry
from src.position_book import PositionBookStore, render_book_block
from src.scheduled_research.playbooks import ResearchPlaybook
from src.scheduled_research.service import _fill_holdings_from_book, build_job_from_draft

pytestmark = pytest.mark.unit


def _block(*rows: str) -> str:
    """Render a figures block, which every draft with a role needs."""
    return "\n\n```figures\n" + "\n".join(rows) + "\n```"


def _codes(result: ValidationResult) -> list[str]:
    return [str(issue.get("code")) for issue in result.issues]


# ---------------------------------------------------------------------------
# Turn context
# ---------------------------------------------------------------------------


def test_build_messages_injects_the_book_before_the_memories(
    tmp_path: Path,
) -> None:
    """The book rides in the user message, ahead of recalled memories."""
    store = PositionBookStore(tmp_path / "position_book.json")
    store.set_position("600519.SH", quantity=100, cost=1500.0, name="贵州茅台")
    builder = ContextBuilder(ToolRegistry(), WorkspaceMemory(), position_book=store)

    messages = builder.build_messages(
        "看看我的持仓", recall_block="- **风险偏好** (user): 保守"
    )

    content = messages[-1]["content"]
    assert "<user-book>" in content
    assert "600519.SH 贵州茅台" in content
    assert "quantity 100" in content
    assert content.index("<user-book>") < content.index("<recalled-memories>")
    assert content.endswith("看看我的持仓")


def test_no_book_block_when_nothing_is_recorded(tmp_path: Path) -> None:
    """An empty book adds no tag and no whitespace to the message."""
    builder = ContextBuilder(
        ToolRegistry(),
        WorkspaceMemory(),
        position_book=PositionBookStore(tmp_path / "position_book.json"),
    )

    content = builder.build_messages("你好", recall_block="")[-1]["content"]

    assert content == "你好"


class _BrokenStore:
    """A store whose read fails."""

    def load(self) -> list[Any]:
        raise OSError("boom")


def test_an_unreadable_book_never_breaks_the_turn() -> None:
    """A broken store degrades to no block instead of failing the run."""
    builder = ContextBuilder(
        ToolRegistry(), WorkspaceMemory(), position_book=_BrokenStore()
    )

    assert builder.build_book_block() == ""


# ---------------------------------------------------------------------------
# Grounding: the book is the user's own statement
# ---------------------------------------------------------------------------


def test_a_held_figure_recorded_in_the_book_reconciles(tmp_path: Path) -> None:
    """A quantity and cost the user recorded can be restated as theirs."""
    store = PositionBookStore(tmp_path / "position_book.json")
    store.set_position("600519.SH", quantity=100, cost=1500.0)
    ledger = GroundingLedger(
        run_dir=tmp_path,
        user_message="我接下来怎么操作？",
        recalled_memory=render_book_block(store),
    )

    draft = "你账上的 100 股继续持有，成本 1500 元，暂不加仓。" + _block(
        "100 | held | the quantity the user recorded",
        "1500 | held | the cost the user recorded",
    )

    result = ledger.validate_final_answer(draft)

    assert result.valid is True, result.issues


def test_a_misquoted_book_figure_is_still_rejected(tmp_path: Path) -> None:
    """The book does not authorize numbers the user never stated."""
    store = PositionBookStore(tmp_path / "position_book.json")
    store.set_position("600519.SH", quantity=100, cost=1500.0)
    ledger = GroundingLedger(
        run_dir=tmp_path,
        user_message="我接下来怎么操作？",
        recalled_memory=render_book_block(store),
    )

    draft = "你的成本是 1600 元。" + _block(
        "1600 | held | the cost the user recorded"
    )

    result = ledger.validate_final_answer(draft)

    assert _codes(result) == ["held_value_not_provided"]


# ---------------------------------------------------------------------------
# Scheduled playbook: holdings come from the book
# ---------------------------------------------------------------------------


def _draft(**variables: str) -> dict[str, Any]:
    """A portfolio-checkup job draft, with optional variable overrides."""
    return {
        "title": "Weekly checkup",
        "source": {
            "kind": "playbook",
            "playbook_slug": "portfolio-checkup",
            "variables": variables,
        },
        "schedule": {"expression": "60000", "timezone": "Asia/Shanghai"},
        "delivery": {"mode": "in_app"},
    }


def test_a_playbook_job_takes_holdings_from_the_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkup scheduled without holdings reads them from the book."""
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    PositionBookStore().set_position(
        "600519.SH", quantity=100, cost=1500.0, name="贵州茅台"
    )

    job = build_job_from_draft(_draft(), now_ms=1_000)

    assert "600519.SH 贵州茅台" in job.prompt
    assert "quantity 100" in job.prompt
    assert "(no holdings provided" not in job.prompt
    assert job.config["holdings_source"] == "position_book"


def test_explicit_holdings_win_over_the_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller-supplied value is never overwritten by the book."""
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    PositionBookStore().set_position("600519.SH", quantity=100)

    job = build_job_from_draft(_draft(holdings="600519.SH x 100"), now_ms=1_000)

    assert "600519.SH x 100" in job.prompt
    assert "holdings_source" not in job.config


def test_an_empty_book_leaves_the_playbook_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No book, no substitution: the template's own 'holdings missing' line runs."""
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))

    job = build_job_from_draft(_draft(), now_ms=1_000)

    assert "(no holdings provided" in job.prompt
    assert "holdings_source" not in job.config


def test_a_book_too_large_for_the_variable_cap_is_refused_out_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oversized book fails job creation instead of running a partial one."""
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    store = PositionBookStore()
    for index in range(12):
        store.set_position(f"6005{index:02d}.SH", quantity=1, note="n" * 480)
    assert len(render_book_block()) > 4000

    with pytest.raises(ValueError, match="cap is 4000"):
        build_job_from_draft(_draft(), now_ms=1_000)


def test_a_playbook_without_a_holdings_variable_is_left_alone() -> None:
    """Playbooks that declare no holdings are never touched by the book."""
    playbook = ResearchPlaybook(
        slug="stub",
        name="Stub",
        description="stub",
        suggested_schedule="60000",
        suggested_timezone=None,
        markets=("global",),
        data_capabilities=("nothing",),
        variables={},
        body="do it",
    )
    config: dict[str, Any] = {}

    assert _fill_holdings_from_book(playbook, {}, config) == {}
    assert "holdings_source" not in config