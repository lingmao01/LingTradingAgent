"""The discipline's two seams: turn context and the grounding boundary.

The discipline is only useful if it reaches the model (``<portfolio-discipline>``
in the turn's message) — and only safe if it is handled as the user's own words:
the ceilings and windows are the user's standing statement, so a ``held`` figure
quoting one reconciles as theirs, while a figure no rule states is still
rejected. The same block rides the scheduled playbooks, so the two after-close
and pre-open templates are pinned here too: the discipline review sits after the
rule check and before the day's tape, and the machine-readable Verdict stays the
last section.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.advice_journal import AdviceJournalStore
from src.agent.context import _SYSTEM_PROMPT, ContextBuilder
from src.agent.grounding import GroundingLedger, ValidationResult
from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry
from src.portfolio_discipline import PortfolioDisciplineStore, render_discipline_block
from src.position_book import PositionBookStore
from src.risk_rules import RiskRulesStore
from src.scheduled_research.playbooks import get_playbook

pytestmark = pytest.mark.unit


def _block(*rows: str) -> str:
    """Render a figures block, which every draft with a role needs."""
    return "\n\n```figures\n" + "\n".join(rows) + "\n```"


def _codes(result: ValidationResult) -> list[str]:
    return [str(issue.get("code")) for issue in result.issues]


def _discipline(tmp_path: Path) -> PortfolioDisciplineStore:
    """A store holding a small discipline: one ceiling, one breaker, guardrail."""
    store = PortfolioDisciplineStore(tmp_path / "portfolio_discipline.json")
    store.update(
        cluster_caps=[
            {"label": "科技簇", "max_percent": 70, "keywords": ["人工智能"]}
        ],
        max_drawdown_percent=20,
        guardrails=["不得在下跌趋势中逆势补仓"],
    )
    return store


# ---------------------------------------------------------------------------
# Turn context
# ---------------------------------------------------------------------------


def test_build_messages_orders_the_book_the_discipline_the_rules_and_the_journal(
    tmp_path: Path,
) -> None:
    """The discipline rides below the book and above the per-instrument rules."""
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
        portfolio_discipline=_discipline(tmp_path),
    )

    content = builder.build_messages(
        "我的纪律还守着吗", recall_block="- **风险偏好** (user): 保守"
    )[-1]["content"]

    assert "Cluster cap 「科技簇」: max 70% of the book" in content
    assert (
        content.index("<user-book>")
        < content.index("<portfolio-discipline>")
        < content.index("<risk-rules>")
        < content.index("<advice-journal>")
        < content.index("<recalled-memories>")
    )
    assert content.endswith("我的纪律还守着吗")


def test_an_empty_discipline_adds_no_tag(tmp_path: Path) -> None:
    """No discipline is no block, not an empty tag."""
    builder = ContextBuilder(
        ToolRegistry(),
        WorkspaceMemory(),
        portfolio_discipline=PortfolioDisciplineStore(
            tmp_path / "portfolio_discipline.json"
        ),
    )

    content = builder.build_messages("你好", recall_block="")[-1]["content"]

    assert content == "你好"


class _BrokenStore:
    """A store whose read fails."""

    def load(self) -> None:
        raise OSError("boom")


def test_an_unreadable_discipline_never_breaks_the_turn() -> None:
    """A broken store degrades to no block instead of failing the run."""
    builder = ContextBuilder(
        ToolRegistry(), WorkspaceMemory(), portfolio_discipline=_BrokenStore()
    )

    assert builder.build_discipline_block() == ""


def test_the_default_discipline_is_resolved_when_none_is_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop injects no store: the default resolves at call time."""
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    PortfolioDisciplineStore().update(max_drawdown_percent=20)
    builder = ContextBuilder(ToolRegistry(), WorkspaceMemory())

    block = builder.build_discipline_block()

    assert "Drawdown circuit breaker: 20% from the peak" in block


def test_a_scheduled_run_carries_the_current_discipline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ceiling recorded after the job was saved still reaches the fire."""
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    PortfolioDisciplineStore().update(
        cluster_caps=[{"label": "科技簇", "max_percent": 70}]
    )
    builder = ContextBuilder(ToolRegistry(), WorkspaceMemory())

    content = builder.build_messages("盘后检查")[-1]["content"]

    assert "</portfolio-discipline>" in content
    assert "Cluster cap 「科技簇」: max 70% of the book" in content


# ---------------------------------------------------------------------------
# Grounding: the discipline is the user's own statement
# ---------------------------------------------------------------------------


def test_a_ceiling_stated_in_the_discipline_reconciles_as_held(tmp_path: Path) -> None:
    """A ceiling the user recorded can be restated as theirs."""
    store = _discipline(tmp_path)
    ledger = GroundingLedger(
        run_dir=tmp_path,
        user_message="我的科技簇上限是多少？",
        recalled_memory=render_discipline_block(store),
    )

    draft = "你设的科技簇上限是 70%。" + _block(
        "70 | held | the cluster ceiling the user recorded"
    )

    assert ledger.validate_final_answer(draft).valid is True


def test_a_limit_no_discipline_states_is_still_rejected(tmp_path: Path) -> None:
    """The discipline does not authorize numbers the user never stated."""
    store = _discipline(tmp_path)
    ledger = GroundingLedger(
        run_dir=tmp_path,
        user_message="我的回撤熔断是多少？",
        recalled_memory=render_discipline_block(store),
    )

    draft = "你的回撤熔断是 25%。" + _block(
        "25% | held | the breaker the user recorded"
    )

    assert _codes(ledger.validate_final_answer(draft)) == ["held_value_not_provided"]


# ---------------------------------------------------------------------------
# The autonomy guideline (objectivity over agreement)
# ---------------------------------------------------------------------------


def test_the_autonomy_guideline_is_in_the_prompt_outside_the_principles() -> None:
    """The guideline exists, and the six frozen principles are untouched."""
    prompt = ContextBuilder(ToolRegistry(), WorkspaceMemory()).build_system_prompt(
        "anything"
    )

    assert (
        "Run the reader's discipline from this session's data, and hold the line."
        in prompt
    )
    assert "Agreement is not help" in " ".join(prompt.split())
    # It lives in Guidelines, never inside the frozen Output Principles block.
    principles = _SYSTEM_PROMPT.split("## Output Principles", 1)[1].split(
        "## Tools", 1
    )[0]
    assert "Run the reader's discipline" not in principles


# ---------------------------------------------------------------------------
# The two scheduled playbooks carry the discipline review
# ---------------------------------------------------------------------------


def test_the_watch_reviews_the_discipline_after_the_rules() -> None:
    """The discipline section sits after the rule check and before the moves."""
    body = get_playbook("daily-risk-watch").body
    output = body[body.index("## Output") : body.index("## Boundaries")]

    assert "## Portfolio discipline" in body
    assert "no portfolio discipline recorded" in body
    assert output.index("`## Risk rules`") < output.index("`## Portfolio discipline`")
    assert output.index("`## Portfolio discipline`") < output.index("`## Moves`")
    # Verdict stays the last listed section.
    assert output.rindex("`## ") == output.index("`## Verdict`")


def test_the_watch_evaluates_the_recorded_gates_independently() -> None:
    """The gates are read from the block and judged on their own, not cascaded."""
    unwrapped = " ".join(get_playbook("daily-risk-watch").body.split())

    assert "Each `Gate` line the block records is a permission on an action" in unwrapped
    assert "the gates are independent" in unwrapped.lower()
    assert "first-match-wins" in unwrapped
    assert "redemption" in unwrapped
    assert "guardrail" in unwrapped
    # The block's own no-add gate names the refusal, not a hardcoded threshold.
    assert "the block's `NO ADD` gate is open" in unwrapped


def test_the_watch_verdict_carries_portfolio_lines() -> None:
    """The machine-readable tail names the gates, the caps and the breaker."""
    body = get_playbook("daily-risk-watch").body
    output = body[body.index("## Output") : body.index("## Boundaries")]
    unwrapped = " ".join(output.split())

    assert "- PORTFOLIO: GATE - one short reason" in unwrapped
    assert "`- PORTFOLIO: OVER CAP - one short reason`" in unwrapped
    assert "`- PORTFOLIO: TRIPPED - one short reason`" in unwrapped
    # The per-symbol rule contract is untouched.
    assert "- SYMBOL: STATE - one short reason" in unwrapped
    assert output.rindex("`## ") == output.index("`## Verdict`")


def test_the_brief_reviews_the_discipline_before_the_tape() -> None:
    """The pre-open pre-check sits after the rule check and before the tape."""
    body = get_playbook("premarket-brief").body
    output = body[body.index("## Output") : body.index("## Boundaries")]

    assert output.index("`## Risk rules`") < output.index("`## Portfolio discipline`")
    assert output.index("`## Portfolio discipline`") < output.index("`## Overnight tape`")
    assert output.rindex("`## ") == output.index("`## Verdict`")


def test_the_brief_evaluates_the_gates_and_tails_the_verdict() -> None:
    """The brief judges each recorded gate and names it in the Verdict tail."""
    body = get_playbook("premarket-brief").body
    output = body[body.index("## Output") : body.index("## Boundaries")]
    unwrapped = " ".join(body.split())
    tail = " ".join(output.split())

    # The gate evaluation is stated in Method, before the Output section.
    assert "each `Gate` line the block records is a permission on an action" in unwrapped
    assert "the gates are independent" in unwrapped.lower()
    assert "first-match-wins" in unwrapped
    assert "- PORTFOLIO: GATE - one short reason" in tail
    assert "`- PORTFOLIO: OVER CAP - one short reason`" in unwrapped
    assert "`- PORTFOLIO: TRIPPED - one short reason`" in unwrapped
    assert "- SYMBOL: STATE - one short reason" in unwrapped
    assert output.rindex("`## ") == output.index("`## Verdict`")
