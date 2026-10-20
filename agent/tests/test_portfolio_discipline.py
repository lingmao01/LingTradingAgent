"""The portfolio discipline store (``src.portfolio_discipline``) and its tool.

The discipline is the user's own portfolio-level framework — theme ceilings,
regime windows, a drawdown breaker, a redemption-fee ladder and guardrails. It
is data, not code: every ceiling and window lives in the user's file and is
re-read on every turn, so changing a number never touches the agent's source.
These tests pin that the module ships no default document, that a write merges
rather than replaces, and that a malformed section is dropped instead of
silently repaired.

Every test reads and writes a discipline file under ``tmp_path``; nothing
touches the real runtime root or the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.portfolio_discipline import (
    PortfolioDisciplineStore,
    render_discipline_block,
)
from src.tools.portfolio_discipline_tool import PortfolioDisciplineTool

pytestmark = pytest.mark.unit


def _store(tmp_path: Path) -> PortfolioDisciplineStore:
    return PortfolioDisciplineStore(tmp_path / "portfolio_discipline.json")


def _tool(tmp_path: Path) -> PortfolioDisciplineTool:
    return PortfolioDisciplineTool(store=_store(tmp_path))


# ---------------------------------------------------------------------------
# A fresh install ships nothing
# ---------------------------------------------------------------------------


def test_a_fresh_store_holds_no_document(tmp_path: Path) -> None:
    """The module ships no default discipline; absent is absent."""
    store = _store(tmp_path)

    assert store.load() is None
    assert render_discipline_block(store) == ""


def test_an_unreadable_document_is_treated_as_absent(tmp_path: Path) -> None:
    """A corrupt file never breaks the turn that reads it."""
    path = tmp_path / "portfolio_discipline.json"
    path.write_text("{not json", encoding="utf-8")

    assert PortfolioDisciplineStore(path).load() is None


# ---------------------------------------------------------------------------
# Writing and reading
# ---------------------------------------------------------------------------


def test_a_discipline_persists_and_reloads(tmp_path: Path) -> None:
    """A ceiling survives the process that recorded it."""
    store = _store(tmp_path)
    store.update(
        cluster_caps=[
            {"label": "科技簇", "max_percent": 70, "keywords": ["人工智能", "芯片"]}
        ],
        max_drawdown_percent=20,
    )

    reloaded = PortfolioDisciplineStore(tmp_path / "portfolio_discipline.json").load()

    assert reloaded is not None
    assert reloaded.max_drawdown_percent == 20
    assert reloaded.cluster_caps[0].label == "科技簇"
    assert reloaded.cluster_caps[0].keywords == ("人工智能", "芯片")


def test_an_update_merges_instead_of_replacing(tmp_path: Path) -> None:
    """Changing one section keeps the rest of the discipline."""
    store = _store(tmp_path)
    store.update(cluster_caps=[{"label": "科技簇", "max_percent": 70}])

    updated = store.update(max_drawdown_percent=25)

    assert updated.max_drawdown_percent == 25
    assert [cap.label for cap in updated.cluster_caps] == ["科技簇"]


def test_an_empty_list_clears_that_section_only(tmp_path: Path) -> None:
    """An explicit empty list drops one section and nothing else."""
    store = _store(tmp_path)
    store.update(
        cluster_caps=[{"label": "科技簇", "max_percent": 70}],
        guardrails=["不得逆势补仓"],
    )

    updated = store.update(guardrails=[])

    assert updated.guardrails == ()
    assert [cap.label for cap in updated.cluster_caps] == ["科技簇"]


def test_an_unknown_section_is_refused(tmp_path: Path) -> None:
    """A section nothing reads is refused, never stored silently."""
    with pytest.raises(ValueError, match="unknown section"):
        _store(tmp_path).update(nonsense=1)


def test_an_update_that_empties_the_document_is_refused(tmp_path: Path) -> None:
    """Clearing the last section leaves no document to store."""
    store = _store(tmp_path)
    store.update(guardrails=["不得逆势补仓"])

    with pytest.raises(ValueError, match="empty"):
        store.update(guardrails=[])


def test_clear_removes_the_document(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.update(max_drawdown_percent=20)

    assert store.clear() is True
    assert store.load() is None
    assert store.clear() is False


# ---------------------------------------------------------------------------
# Lenient parsing: a bad section is dropped, never repaired by guessing
# ---------------------------------------------------------------------------


def test_a_malformed_cluster_is_dropped_and_the_rest_survives(tmp_path: Path) -> None:
    """One unusable ceiling never takes the whole discipline down."""
    path = tmp_path / "portfolio_discipline.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "discipline": {
                    "cluster_caps": [
                        {"label": "", "max_percent": 70},
                        {"label": "科技簇", "max_percent": 190},
                        {"label": "宽基簇", "max_percent": 30},
                    ],
                    "max_drawdown_percent": 20,
                },
            }
        ),
        encoding="utf-8",
    )

    discipline = PortfolioDisciplineStore(path).load()

    assert discipline is not None
    assert [cap.label for cap in discipline.cluster_caps] == ["宽基簇"]
    assert discipline.max_drawdown_percent == 20


def test_a_document_left_with_nothing_readable_is_absent(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_discipline.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "discipline": {"cluster_caps": "nope", "max_drawdown_percent": "abc"},
            }
        ),
        encoding="utf-8",
    )

    assert PortfolioDisciplineStore(path).load() is None


def test_redemption_tiers_are_sorted_by_holding_days(tmp_path: Path) -> None:
    """The ladder reads shortest window first, whatever order it was stored in."""
    store = _store(tmp_path)
    store.update(
        redemption_fees=[
            {"holding_days_below": 30, "fee_percent": 0.5},
            {"holding_days_below": 7, "fee_percent": 1.5},
        ]
    )

    tiers = store.load().redemption_fees

    assert [tier.holding_days_below for tier in tiers] == [7, 30]


# ---------------------------------------------------------------------------
# The rendered block: rules only, no holdings, no computed tier
# ---------------------------------------------------------------------------


def test_the_block_states_the_recorded_rules(tmp_path: Path) -> None:
    """Every recorded section reaches the injected block."""
    store = _store(tmp_path)
    store.update(
        cluster_caps=[
            {"label": "科技簇", "max_percent": 70, "keywords": ["人工智能", "芯片"]}
        ],
        regime={
            "indices": ["000001.SH"],
            "sma_short_days": 20,
            "sma_long_days": 50,
            "macd_confirm_days": 2,
        },
        max_drawdown_percent=20,
        add_conditions=["预设信号触发"],
        redemption_fees=[{"holding_days_below": 7, "fee_percent": 1.5}],
        guardrails=["不得在下跌趋势中逆势补仓"],
    )

    block = render_discipline_block(store)

    assert "Cluster cap 「科技簇」: max 70% of the book" in block
    assert "keywords 人工智能, 芯片" in block
    assert "short average 20 sessions, long average 50 sessions" in block
    assert "Drawdown circuit breaker: 20% from the peak" in block
    assert "Adding requires: 预设信号触发" in block
    assert "Redemption fee: held fewer than 7 days charges 1.5%" in block
    assert "Standing guardrails: 不得在下跌趋势中逆势补仓" in block


def test_a_window_change_is_data_not_code(tmp_path: Path) -> None:
    """A different window renders differently with no code change."""
    store = _store(tmp_path)
    store.update(regime={"indices": ["000001.SH"], "sma_short_days": 10})

    assert "short average 10 sessions" in render_discipline_block(store)


def test_the_block_names_the_cluster_not_a_holding(tmp_path: Path) -> None:
    """Membership is a keyword rule: no held symbol is copied into the store."""
    store = _store(tmp_path)
    store.update(
        cluster_caps=[
            {"label": "科技簇", "max_percent": 70, "keywords": ["人工智能"]}
        ]
    )

    block = render_discipline_block(store)
    document = (tmp_path / "portfolio_discipline.json").read_text(encoding="utf-8")

    assert "人工智能" in block
    assert "600519" not in document  # a holding is never copied into the store


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def test_the_tool_shows_an_empty_discipline(tmp_path: Path) -> None:
    payload = json.loads(_tool(tmp_path).execute(action="show"))

    assert payload["status"] == "ok"
    assert payload["recorded"] is False
    assert payload["block"] == ""


def test_the_tool_update_then_show(tmp_path: Path) -> None:
    tool = _tool(tmp_path)

    updated = json.loads(
        tool.execute(action="update", max_drawdown_percent=20, guardrails=["不得逆势补仓"])
    )
    shown = json.loads(tool.execute(action="show"))

    assert updated["status"] == "ok"
    assert "max_drawdown_percent" in updated["message"]
    assert shown["recorded"] is True
    assert "Drawdown circuit breaker: 20% from the peak" in shown["block"]


def test_the_tool_update_without_a_section_errors(tmp_path: Path) -> None:
    payload = json.loads(_tool(tmp_path).execute(action="update"))

    assert payload["status"] == "error"
    assert "at least one section" in payload["error"]


def test_the_tool_clear_reports_when_nothing_was_stored(tmp_path: Path) -> None:
    payload = json.loads(_tool(tmp_path).execute(action="clear"))

    assert payload["status"] == "not_found"


def test_the_tool_rejects_an_unknown_action(tmp_path: Path) -> None:
    payload = json.loads(_tool(tmp_path).execute(action="frobnicate"))

    assert payload["status"] == "error"


# ---------------------------------------------------------------------------
# The write path is strict: a rule that would be dropped is refused
# ---------------------------------------------------------------------------


def test_a_write_that_would_drop_a_cluster_is_refused(tmp_path: Path) -> None:
    """Reading is lenient; writing must not report a dropped ceiling as saved."""
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="at most"):
        store.update(
            cluster_caps=[
                {"label": "科技簇", "max_percent": 70, "keywords": [f"k{i}" for i in range(41)]}
            ],
            max_drawdown_percent=20,
        )


def test_a_write_that_would_truncate_a_keyword_is_refused(tmp_path: Path) -> None:
    """A keyword cut to the cap could stop matching the holding it names."""
    with pytest.raises(ValueError, match="exceeds"):
        _store(tmp_path).update(
            cluster_caps=[{"label": "X", "max_percent": 70, "keywords": ["甲" * 45]}]
        )


def test_a_write_that_would_clear_guardrails_is_refused(tmp_path: Path) -> None:
    """An over-long guardrail list must not silently empty the section."""
    with pytest.raises(ValueError, match="at most"):
        _store(tmp_path).update(
            guardrails=[f"g{i}" for i in range(41)], max_drawdown_percent=20
        )


def test_a_write_with_an_out_of_range_ceiling_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_percent"):
        _store(tmp_path).update(
            cluster_caps=[{"label": "科技簇", "max_percent": 190}],
            max_drawdown_percent=20,
        )


def test_the_tool_reports_a_refused_write_instead_of_ok(tmp_path: Path) -> None:
    """The model must see an error, never a false success."""
    payload = json.loads(
        _tool(tmp_path).execute(
            action="update",
            cluster_caps=[{"label": "科技簇", "max_percent": 190}],
            max_drawdown_percent=20,
        )
    )

    assert payload["status"] == "error"
    assert "max_percent" in payload["error"]


def test_the_read_path_stays_lenient_for_a_legacy_file(tmp_path: Path) -> None:
    """A file already on disk degrades instead of failing the turn."""
    path = tmp_path / "portfolio_discipline.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "discipline": {
                    "cluster_caps": [
                        {"label": "X", "max_percent": 70, "keywords": ["甲" * 45]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    discipline = PortfolioDisciplineStore(path).load()

    assert discipline is not None
    assert len(discipline.cluster_caps[0].keywords[0]) == 40  # truncated on read


# ---------------------------------------------------------------------------
# The permission gates (tiers): data, evaluated independently
# ---------------------------------------------------------------------------


def _tiers() -> list[dict[str, str]]:
    return [
        {"action": "NO ADD", "label": "禁加仓线", "when": "收盘 < 短均线（SMA{short}）"},
        {"action": "REDUCE", "label": "减仓触发器", "when": "收盘 ≥ 短均线（SMA{short}）且动能确认"},
        {
            "action": "ADD ALLOWED",
            "label": "恢复加仓",
            "when": "收盘 ≥ 短均线（SMA{short}）且 MACD柱连续 {confirm} 日转正",
        },
    ]


def test_the_gates_round_trip_and_fill_the_recorded_windows(tmp_path: Path) -> None:
    """A gate is stored as data and its placeholders resolve to the regime windows."""
    store = _store(tmp_path)
    store.update(
        regime={
            "indices": ["000001.SH"],
            "sma_short_days": 20,
            "sma_long_days": 50,
            "macd_confirm_days": 2,
        },
        tiers=_tiers(),
    )

    block = render_discipline_block(store)

    assert "- Gate 「禁加仓线」 → NO ADD: 收盘 < 短均线（SMA20）" in block
    assert "MACD柱连续 2 日转正" in block
    assert "{short}" not in block and "{confirm}" not in block
    assert [
        tier.action for tier in PortfolioDisciplineStore(store.path).load().tiers
    ] == ["NO ADD", "REDUCE", "ADD ALLOWED"]


def test_a_gate_note_is_filled_like_its_condition(tmp_path: Path) -> None:
    """A placeholder in the note resolves too; no literal brace reaches the prompt."""
    store = _store(tmp_path)
    store.update(
        regime={"sma_short_days": 20, "sma_long_days": 50},
        tiers=[
            {
                "action": "NO ADD",
                "label": "禁加仓线",
                "when": "收盘 < 短均线（SMA{short}）",
                "note": "中间带（SMA{long} ≤ 收盘 < SMA{short}）也禁止补仓",
            }
        ],
    )

    block = render_discipline_block(store)

    assert "中间带（SMA50 ≤ 收盘 < SMA20）也禁止补仓" in block
    assert "{" not in block and "}" not in block


def test_a_window_change_changes_the_gate_condition(tmp_path: Path) -> None:
    """The condition follows the data: a new window renders a new condition."""
    store = _store(tmp_path)
    store.update(regime={"sma_short_days": 10}, tiers=_tiers())

    assert "短均线（SMA10）" in render_discipline_block(store)


def test_a_gate_with_an_unknown_action_is_refused(tmp_path: Path) -> None:
    """The action vocabulary is frozen so a machine can read the tail."""
    with pytest.raises(ValueError, match="action"):
        _store(tmp_path).update(
            tiers=[{"action": "BUY", "label": "x", "when": "y"}]
        )


def test_a_write_that_would_overflow_the_gates_is_refused(tmp_path: Path) -> None:
    """The injected block is bounded like every other rule list."""
    with pytest.raises(ValueError, match="at most"):
        _store(tmp_path).update(
            tiers=[
                {"action": "NO ADD", "label": f"g{i}", "when": "x"} for i in range(41)
            ]
        )


def test_a_gate_without_a_condition_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="condition"):
        _store(tmp_path).update(tiers=[{"action": "NO ADD", "label": "x"}])


def test_a_bad_gate_row_is_dropped_on_read(tmp_path: Path) -> None:
    """A legacy file degrades: a row that is not a rule is dropped, not repaired."""
    path = tmp_path / "portfolio_discipline.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "discipline": {
                    "tiers": [
                        {"action": "BUY", "label": "bad", "when": "x"},
                        {"action": "NO ADD", "label": "禁加仓线", "when": "收盘 < SMA{short}"},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    discipline = PortfolioDisciplineStore(path).load()

    assert discipline is not None
    assert [tier.action for tier in discipline.tiers] == ["NO ADD"]


def test_the_tool_updates_the_gates(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    updated = json.loads(tool.execute(action="update", tiers=_tiers()))

    assert updated["status"] == "ok"
    assert "Gate 「禁加仓线」" in updated["block"]


# ---------------------------------------------------------------------------
# Clear must work even on a document too damaged to parse
# ---------------------------------------------------------------------------


def test_clear_removes_a_corrupted_document(tmp_path: Path) -> None:
    """A file that loads as absent must still be clearable, or the store wedges."""
    path = tmp_path / "portfolio_discipline.json"
    path.write_text("{not json", encoding="utf-8")

    assert PortfolioDisciplineStore(path).clear() is True
    assert not path.exists()
