"""The risk rules' two seams: turn context and the grounding boundary.

The rules are only useful if they reach the model (``<risk-rules>`` in the
turn's message) — and only safe if they are handled as the user's own words:
the limits are the user's standing statement, so a ``held`` figure quoting one
reconciles as theirs, while a figure no rule states is still rejected.
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
from src.risk_rules import RiskRulesStore, render_risk_block

pytestmark = pytest.mark.unit


def _block(*rows: str) -> str:
    """Render a figures block, which every draft with a role needs."""
    return "\n\n```figures\n" + "\n".join(rows) + "\n```"


def _codes(result: ValidationResult) -> list[str]:
    return [str(issue.get("code")) for issue in result.issues]


# ---------------------------------------------------------------------------
# Turn context
# ---------------------------------------------------------------------------


def test_build_messages_orders_the_book_the_rules_the_journal_and_the_memories(
    tmp_path: Path,
) -> None:
    """The rules ride in the user message, below the book, above the journal."""
    book = PositionBookStore(tmp_path / "position_book.json")
    book.set_position("600519.SH", quantity=300, cost=1500.0, name="贵州茅台")
    rules = RiskRulesStore(tmp_path / "risk_rules.json")
    rules.set_rule("600519.SH", stop_loss=1400.0, max_quantity=200)
    journal = AdviceJournalStore(tmp_path / "advice_journal.json")
    journal.record("600519.SH", action="hold", reference_price=1600.0)
    builder = ContextBuilder(
        ToolRegistry(),
        WorkspaceMemory(),
        position_book=book,
        advice_journal=journal,
        risk_rules=rules,
    )

    messages = builder.build_messages(
        "我的止损还守着吗", recall_block="- **风险偏好** (user): 保守"
    )

    content = messages[-1]["content"]
    assert "stop-loss 1400" in content
    assert "position 300 is over the cap" in content
    assert (
        content.index("<user-book>")
        < content.index("<risk-rules>")
        < content.index("<advice-journal>")
        < content.index("<recalled-memories>")
    )
    assert content.endswith("我的止损还守着吗")


def test_an_empty_store_adds_no_tag(tmp_path: Path) -> None:
    """No rules is no block, not an empty tag."""
    builder = ContextBuilder(
        ToolRegistry(),
        WorkspaceMemory(),
        risk_rules=RiskRulesStore(tmp_path / "risk_rules.json"),
    )

    content = builder.build_messages("你好", recall_block="")[-1]["content"]

    assert content == "你好"


class _BrokenStore:
    """A store whose read fails."""

    def load(self) -> list[Any]:
        raise OSError("boom")


def test_an_unreadable_store_never_breaks_the_turn() -> None:
    """A broken store degrades to no block instead of failing the run."""
    builder = ContextBuilder(
        ToolRegistry(), WorkspaceMemory(), risk_rules=_BrokenStore()
    )

    assert builder.build_risk_block() == ""


def test_the_default_stores_are_resolved_when_none_are_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop injects no stores: defaults resolve at call time, cap included."""
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    PositionBookStore().set_position("600519.SH", quantity=300)
    RiskRulesStore().set_rule("600519.SH", max_quantity=200)
    builder = ContextBuilder(ToolRegistry(), WorkspaceMemory())

    block = builder.build_risk_block()

    assert "max 200 units" in block
    assert "position 300 is over the cap" in block


# ---------------------------------------------------------------------------
# Grounding: the rules are the user's own statement
# ---------------------------------------------------------------------------


def test_a_limit_stated_in_the_rules_reconciles_as_held(tmp_path: Path) -> None:
    """A stop or cap the user recorded can be restated as theirs."""
    rules = RiskRulesStore(tmp_path / "risk_rules.json")
    rules.set_rule("600519.SH", stop_loss=1400.0, max_quantity=200)
    ledger = GroundingLedger(
        run_dir=tmp_path,
        user_message="我的止损线还在吗？",
        recalled_memory=render_risk_block(rules),
    )

    draft = "你设的止损线 1400 元、单标的 200 股上限仍在。" + _block(
        "1400 | held | the stop-loss the user recorded",
        "200 | held | the position cap the user recorded",
    )

    result = ledger.validate_final_answer(draft)

    assert result.valid is True, result.issues


def test_a_limit_no_rule_states_is_still_rejected(tmp_path: Path) -> None:
    """The rules do not authorize numbers the user never stated."""
    rules = RiskRulesStore(tmp_path / "risk_rules.json")
    rules.set_rule("600519.SH", stop_loss=1400.0)
    ledger = GroundingLedger(
        run_dir=tmp_path,
        user_message="我的止损线还在吗？",
        recalled_memory=render_risk_block(rules),
    )

    draft = "你的止损线是 1500 元。" + _block(
        "1500 | held | the stop-loss the user recorded"
    )

    result = ledger.validate_final_answer(draft)

    assert _codes(result) == ["held_value_not_provided"]