"""The advice journal's two seams: turn context and the grounding boundary.

The journal is only useful if it reaches the model (``<advice-journal>`` in
the turn's message) — and only safe if it stays out of the grounding ledger's
user corpus: the journal is the agent's own record, so one of its numbers must
never reconcile as the user's, and quoting one goes through the ``cited`` role
instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.advice_journal import AdviceJournalStore
from src.agent.context import ContextBuilder
from src.agent.grounding import GroundingLedger, ValidationResult
from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry
from src.position_book import PositionBookStore

pytestmark = pytest.mark.unit


def _block(*rows: str) -> str:
    """Render a figures block, which every draft with a role needs."""
    return "\n\n```figures\n" + "\n".join(rows) + "\n```"


def _codes(result: ValidationResult) -> list[str]:
    return [str(issue.get("code")) for issue in result.issues]


# ---------------------------------------------------------------------------
# Turn context
# ---------------------------------------------------------------------------


def test_build_messages_orders_the_book_the_journal_and_the_memories(
    tmp_path: Path,
) -> None:
    """The journal rides in the user message, below the book, above recall."""
    book = PositionBookStore(tmp_path / "position_book.json")
    book.set_position("600519.SH", quantity=100, cost=1500.0, name="贵州茅台")
    journal = AdviceJournalStore(tmp_path / "advice_journal.json")
    journal.record("600519.SH", action="hold", reference_price=1600.0)
    builder = ContextBuilder(
        ToolRegistry(), WorkspaceMemory(), position_book=book, advice_journal=journal
    )

    messages = builder.build_messages(
        "看看我的持仓和上次建议", recall_block="- **风险偏好** (user): 保守"
    )

    content = messages[-1]["content"]
    assert "<user-book>" in content
    assert "#1 600519.SH: hold, reference 1600" in content
    assert (
        content.index("<user-book>")
        < content.index("<advice-journal>")
        < content.index("<recalled-memories>")
    )
    assert content.endswith("看看我的持仓和上次建议")


def test_closed_entries_leave_the_turn(tmp_path: Path) -> None:
    """An entry that has been graded stops being injected."""
    journal = AdviceJournalStore(tmp_path / "advice_journal.json")
    entry = journal.record("600519.SH", action="hold", rationale="wait for the print")
    builder = ContextBuilder(
        ToolRegistry(), WorkspaceMemory(), advice_journal=journal
    )

    assert "<advice-journal>" in builder.build_messages("x", recall_block="")[-1][
        "content"
    ]

    journal.close(entry.id, note="held flat")

    content = builder.build_messages("x", recall_block="")[-1]["content"]
    assert "<advice-journal>" not in content
    assert content == "x"


def test_an_empty_journal_adds_no_tag(tmp_path: Path) -> None:
    """No entries is no block, not an empty tag."""
    builder = ContextBuilder(
        ToolRegistry(),
        WorkspaceMemory(),
        advice_journal=AdviceJournalStore(tmp_path / "advice_journal.json"),
    )

    content = builder.build_messages("你好", recall_block="")[-1]["content"]

    assert content == "你好"


class _BrokenStore:
    """A store whose read fails."""

    def load(self) -> list[Any]:
        raise OSError("boom")


def test_an_unreadable_journal_never_breaks_the_turn() -> None:
    """A broken store degrades to no block instead of failing the run."""
    builder = ContextBuilder(
        ToolRegistry(), WorkspaceMemory(), advice_journal=_BrokenStore()
    )

    assert builder.build_advice_block() == ""


# ---------------------------------------------------------------------------
# Grounding: the journal is the agent's own record
# ---------------------------------------------------------------------------


def test_a_journal_figure_never_reconciles_as_the_users(tmp_path: Path) -> None:
    """The journal is deliberately outside the user corpus.

    A reference price the model itself recorded must not become a ``held``
    value — that would let the agent's own earlier number be laundered as the
    user's authority, the self-certification hole this boundary closes.
    """
    ledger = GroundingLedger(
        run_dir=tmp_path,
        user_message="上次建议之后怎么样了？",
        recalled_memory="",
    )

    draft = "上次建议的记录参考价是 1600.5 元。" + _block(
        "1600.5 | held | the advice journal reference price"
    )

    result = ledger.validate_final_answer(draft)

    assert _codes(result) == ["held_value_not_provided"]


def test_a_journal_figure_quoted_as_cited_passes(tmp_path: Path) -> None:
    """The intended path: a record is quoted as cited, naming the journal."""
    ledger = GroundingLedger(
        run_dir=tmp_path,
        user_message="帮我回顾一下上次的减仓建议",
        recalled_memory="",
    )

    draft = "上次建议记录参考价 1600.5 元（建议台账 2026-09-25）。" + _block(
        "1600.5 | cited | 建议台账 2026-09-25"
    )

    result = ledger.validate_final_answer(draft)

    assert result.valid is True, result.issues