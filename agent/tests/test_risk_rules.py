"""The standing risk rules (``src.risk_rules``) and their tool.

Every test reads and writes a rules file under ``tmp_path``; nothing touches
the real runtime root or the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.position_book import PositionBookStore
from src.risk_rules import RiskRulesStore, render_risk_block
from src.tools.risk_rules_tool import RiskRulesTool

pytestmark = pytest.mark.unit


def _store(tmp_path: Path) -> RiskRulesStore:
    return RiskRulesStore(tmp_path / "risk_rules.json")


def _tool(tmp_path: Path) -> RiskRulesTool:
    return RiskRulesTool(store=_store(tmp_path))


# ---------------------------------------------------------------------------
# Writing and reading
# ---------------------------------------------------------------------------


def test_a_rule_persists_and_reloads(tmp_path: Path) -> None:
    """A rule survives the process that set it."""
    rule = _store(tmp_path).set_rule("600519.SH", stop_loss=1400.0, max_quantity=200)

    reloaded = RiskRulesStore(tmp_path / "risk_rules.json").load()

    assert [row.to_dict() for row in reloaded] == [rule.to_dict()]


def test_bare_a_share_and_hk_codes_canonicalize(tmp_path: Path) -> None:
    """The rules share the book's symbol grammar."""
    store = _store(tmp_path)

    assert store.set_rule("600519", stop_loss=1400).symbol == "600519.SH"
    assert store.set_rule("700.HK", take_profit=500).symbol == "00700.HK"


def test_set_merges_instead_of_replacing(tmp_path: Path) -> None:
    """An omitted limit keeps what was already recorded for the symbol."""
    store = _store(tmp_path)
    store.set_rule("600519.SH", stop_loss=1400.0, max_quantity=200)

    updated = store.set_rule("600519.SH", take_profit=1900.0)

    assert (updated.stop_loss, updated.take_profit, updated.max_quantity) == (
        1400.0,
        1900.0,
        200,
    )


def test_a_fresh_rule_needs_at_least_one_limit(tmp_path: Path) -> None:
    """A rule that says nothing is refused, not stored as a blank line."""
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="at least one"):
        store.set_rule("600519.SH")

    assert store.load() == []


def test_limits_must_be_finite_and_positive(tmp_path: Path) -> None:
    """Bad numbers and bad symbols raise instead of being stored."""
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="finite"):
        store.set_rule("600519.SH", stop_loss=float("nan"))
    with pytest.raises(ValueError, match="positive"):
        store.set_rule("600519.SH", max_quantity=0)
    with pytest.raises(ValueError, match="positive"):
        store.set_rule("600519.SH", take_profit=-1)
    with pytest.raises(ValueError, match="number"):
        store.set_rule("600519.SH", stop_loss=True)
    with pytest.raises(ValueError, match="unsupported symbol"):
        store.set_rule("AAPL", stop_loss=100)

    assert store.load() == []


def test_a_corrupt_file_reads_as_empty_and_stays_writable(tmp_path: Path) -> None:
    """A half-written file degrades to empty instead of failing reads."""
    (tmp_path / "risk_rules.json").write_text("{not json", encoding="utf-8")

    assert _store(tmp_path).load() == []
    assert _store(tmp_path).set_rule("600519.SH", stop_loss=1400).symbol == "600519.SH"


def test_unusable_rows_are_dropped_and_partial_rows_kept(tmp_path: Path) -> None:
    """A bad row never becomes a rule; a bad field drops like an absent one."""
    (tmp_path / "risk_rules.json").write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [
                    {"symbol": "NOT A SYMBOL", "stop_loss": 10},
                    {"symbol": "600519.SH"},
                    {"symbol": "000001.SZ", "stop_loss": -5, "max_quantity": 100},
                ],
            }
        ),
        encoding="utf-8",
    )

    rules = _store(tmp_path).load()

    assert [rule.symbol for rule in rules] == ["000001.SZ"]
    assert rules[0].stop_loss is None
    assert rules[0].max_quantity == 100


def test_remove_and_clear_report_what_they_removed(tmp_path: Path) -> None:
    """Removing answers truthfully; clearing counts what went."""
    store = _store(tmp_path)
    store.set_rule("600519.SH", stop_loss=1400.0)
    store.set_rule("000001.SZ", max_quantity=100)

    assert store.remove("000001.SZ") is True
    assert store.remove("000001.SZ") is False
    assert store.clear() == 1
    assert store.clear() == 0


# ---------------------------------------------------------------------------
# The injected block
# ---------------------------------------------------------------------------


def test_the_block_lists_the_limits_with_the_record_date(tmp_path: Path) -> None:
    """Every stated limit rides in the turn, with the day it was set."""
    store = _store(tmp_path)
    store.set_rule(
        "600519.SH", stop_loss=1400.0, take_profit=1900.0, max_quantity=200
    )

    lines = render_risk_block(store).splitlines()

    assert len(lines) == 1
    assert lines[0].startswith(
        "- 600519.SH: stop-loss 1400, take-profit 1900, max 200 units"
    )
    assert "(recorded " in lines[0]
    assert "over the cap" not in lines[0]


def test_a_holding_over_the_cap_is_flagged(tmp_path: Path) -> None:
    """The one rule measurable from stored state is checked against the book."""
    store = _store(tmp_path)
    store.set_rule("600519.SH", max_quantity=200)
    store.set_rule("000001.SZ", max_quantity=200)
    store.set_rule("300750.SZ", max_quantity=200)
    book = PositionBookStore(tmp_path / "position_book.json")
    book.set_position("600519.SH", quantity=300)
    book.set_position("000001.SZ", quantity=200)

    lines = render_risk_block(store, book=book).splitlines()

    assert "position 300 is over the cap" in lines[0]
    assert "over the cap" not in lines[1]
    # A rule with no book row cannot be over anything.
    assert "over the cap" not in lines[2]


def test_a_short_position_is_capped_by_its_exposure(tmp_path: Path) -> None:
    """A short's cap constrains its exposure, not its signed quantity."""
    store = _store(tmp_path)
    store.set_rule("600519.SH", max_quantity=200)
    book = PositionBookStore(tmp_path / "position_book.json")
    book.set_position("600519.SH", quantity=-250)

    assert "position -250 is over the cap" in render_risk_block(store, book=book)


class _BrokenBook:
    """A book store whose read fails."""

    def load(self) -> list[Any]:
        raise OSError("boom")


def test_a_broken_book_drops_only_the_cap_note(tmp_path: Path) -> None:
    """The rules are the primary content: a failed cross-check keeps them."""
    store = _store(tmp_path)
    store.set_rule("600519.SH", stop_loss=1400.0, max_quantity=200)

    line = render_risk_block(store, book=_BrokenBook())

    assert "stop-loss 1400" in line
    assert "over the cap" not in line


def test_an_empty_store_renders_nothing(tmp_path: Path) -> None:
    """No file is no block, not an empty tag."""
    assert render_risk_block(_store(tmp_path)) == ""


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def test_tool_sets_lists_removes_and_clears(tmp_path: Path) -> None:
    """Every rules action round-trips through a JSON payload."""
    tool = _tool(tmp_path)

    recorded = json.loads(
        tool.execute(
            action="set", symbol="600519.SH", stop_loss=1400, max_quantity=200
        )
    )
    assert recorded["status"] == "ok"
    assert recorded["rule"]["symbol"] == "600519.SH"
    assert recorded["rule"]["stop_loss"] == 1400

    listed = json.loads(tool.execute(action="list"))
    assert listed["count"] == 1

    missing = json.loads(tool.execute(action="remove", symbol="000001.SZ"))
    assert missing["status"] == "not_found"
    assert json.loads(tool.execute(action="remove", symbol="600519.SH"))["status"] == "ok"
    assert json.loads(tool.execute(action="list"))["count"] == 0

    tool.execute(action="set", symbol="600519.SH", stop_loss=1400)
    cleared = json.loads(tool.execute(action="clear"))
    assert cleared["status"] == "ok"
    assert cleared["message"] == "Cleared 1 rule(s)"


def test_tool_reports_input_errors_as_payloads(tmp_path: Path) -> None:
    """Invalid input comes back as an error payload, not an exception."""
    tool = _tool(tmp_path)

    missing_symbol = json.loads(tool.execute(action="set", stop_loss=1400))
    assert missing_symbol["status"] == "error"
    no_limits = json.loads(tool.execute(action="set", symbol="600519.SH"))
    assert no_limits["status"] == "error"
    negative = json.loads(
        tool.execute(action="set", symbol="600519.SH", stop_loss=-1)
    )
    assert negative["status"] == "error"
    unknown = json.loads(tool.execute(action="teleport"))
    assert unknown["status"] == "error"


def test_tool_name_matches_its_module_for_discovery() -> None:
    """The registry maps ``risk_rules_tool`` to the tool named ``risk_rules``,
    so a discovery failure is reportable under one name."""
    assert RiskRulesTool.name == "risk_rules"
    assert RiskRulesTool.__module__ == "src.tools.risk_rules_tool"