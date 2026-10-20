"""The user position book (``src.position_book``) and its tool.

Every test reads and writes a book file under ``tmp_path``; nothing touches
the real runtime root or the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.position_book import (
    PositionBookStore,
    canonical_book_symbol,
    render_book_block,
)
from src.tools.position_book_tool import PositionBookTool

pytestmark = pytest.mark.unit


def _store(tmp_path: Path) -> PositionBookStore:
    return PositionBookStore(tmp_path / "position_book.json")


def _tool(tmp_path: Path) -> PositionBookTool:
    return PositionBookTool(store=_store(tmp_path))


# ---------------------------------------------------------------------------
# Symbol canonicalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600519", "600519.SH"),
        ("510300", "510300.SH"),
        ("900901", "900901.SH"),
        ("000001", "000001.SZ"),
        ("159915", "159915.SZ"),
        ("300750", "300750.SZ"),
        ("430139", "430139.BJ"),
        ("835174", "835174.BJ"),
        ("600519.sh", "600519.SH"),
        (" 000001 .SZ ", "000001.SZ"),
        ("000051.of", "000051.OF"),
        ("aapl.us", "AAPL.US"),
        ("BRK-B.us", "BRK-B.US"),
        ("700.hk", "00700.HK"),
        ("9988.HK", "09988.HK"),
    ],
)
def test_canonical_book_symbols(raw: str, expected: str) -> None:
    """A bare A-share code is qualified by its first digit; suffixed forms keep
    their venue with the venue's own body shape."""
    assert canonical_book_symbol(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "AAPL",  # no venue spelled out: never guessed
        "60051.SH",  # five digits under an A-share suffix
        "600519.XX",  # unknown suffix
        "600519.SH.X",
        "770000",  # leading digit with no exchange mapping
        "000051.OF.X",
    ],
)
def test_unqualifiable_symbols_are_rejected(raw: object) -> None:
    """Anything without an unambiguous canonical form is refused, not guessed."""
    assert canonical_book_symbol(raw) is None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_set_position_round_trips_through_the_file(tmp_path: Path) -> None:
    """A recorded position survives a reload, in the documented file shape."""
    store = _store(tmp_path)

    position = store.set_position(
        "600519", quantity=100, cost=1500.0, name="贵州茅台", note="长期底仓"
    )

    assert position.symbol == "600519.SH"
    payload = json.loads((tmp_path / "position_book.json").read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["positions"][0]["symbol"] == "600519.SH"
    reloaded = store.load()
    assert [p.symbol for p in reloaded] == ["600519.SH"]
    assert reloaded[0].quantity == 100
    assert reloaded[0].cost == 1500.0
    assert reloaded[0].name == "贵州茅台"
    assert reloaded[0].note == "长期底仓"


def test_update_merges_and_keeps_unstated_fields(tmp_path: Path) -> None:
    """A quantity-only update is a merge: cost and name already recorded stay."""
    store = _store(tmp_path)
    store.set_position(
        "600519.SH", quantity=100, cost=1500.0, name="贵州茅台", note="长期底仓"
    )

    store.set_position("600519.SH", quantity=200)

    [position] = store.load()
    assert position.quantity == 200
    assert position.cost == 1500.0
    assert position.name == "贵州茅台"
    assert position.note == "长期底仓"
    assert position.updated_at


def test_negative_quantities_are_short_positions(tmp_path: Path) -> None:
    """A short is a signed quantity, not an error."""
    store = _store(tmp_path)

    store.set_position("AAPL.US", quantity=-50, cost=190.25)

    [position] = store.load()
    assert position.symbol == "AAPL.US"
    assert position.quantity == -50


def test_remove_and_clear(tmp_path: Path) -> None:
    """Removing is per-symbol; clearing empties the whole book once."""
    store = _store(tmp_path)
    store.set_position("600519.SH", quantity=100)
    store.set_position("000001.SZ", quantity=1000)

    assert store.remove_position("600519") is True
    assert store.remove_position("600519.SH") is False
    assert [p.symbol for p in store.load()] == ["000001.SZ"]

    assert store.clear() == 1
    assert store.load() == []
    assert store.clear() == 0


def test_invalid_writes_are_refused(tmp_path: Path) -> None:
    """Bad symbols and numbers raise instead of being recorded."""
    store = _store(tmp_path)

    with pytest.raises(ValueError):
        store.set_position("AAPL", quantity=1)
    with pytest.raises(ValueError):
        store.set_position("600519.SH", quantity="many")
    with pytest.raises(ValueError):
        store.set_position("600519.SH", quantity=1, cost=-1)
    with pytest.raises(ValueError):
        store.remove_position("nope")
    assert store.load() == []


def test_a_broken_book_file_is_reported_unavailable_without_overwriting_it(
    tmp_path: Path,
) -> None:
    """Reading remains bounded; a write must not silently erase damaged user state."""
    path = tmp_path / "position_book.json"
    path.write_text("{not json", encoding="utf-8")
    store = PositionBookStore(path)

    assert store.load() == []

    with pytest.raises(ValueError, match="unreadable"):
        store.set_position("000001.SZ", quantity=1)
    assert path.read_text() == "{not json"
    assert store.load_account()["quality"] == "unavailable"


def test_rows_that_cannot_be_parsed_are_dropped_not_guessed(
    tmp_path: Path,
) -> None:
    """A persisted row missing its symbol or quantity is dropped, never fixed."""
    path = tmp_path / "position_book.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "positions": [
                    {"symbol": "600519.SH"},  # no quantity
                    {"symbol": "not-a-symbol", "quantity": 1},
                    {"symbol": "000001.SZ", "quantity": "x"},
                    {"symbol": "000051.OF", "quantity": 10, "cost": "x"},
                ],
            }
        ),
        encoding="utf-8",
    )

    positions = PositionBookStore(path).load()

    assert [p.symbol for p in positions] == ["000051.OF"]
    assert positions[0].cost is None


def test_writes_are_atomic_with_no_temp_file_left_behind(tmp_path: Path) -> None:
    """The temp file is renamed into place, so nothing else survives a write."""
    store = _store(tmp_path)

    store.set_position("600519.SH", quantity=100)
    store.set_position("000001.SZ", quantity=1)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["position_book.json", "position_book.json.lock"]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_book_block_lines(tmp_path: Path) -> None:
    """The block quotes symbol, name, quantity and cost per position."""
    store = _store(tmp_path)
    store.set_position("600519.SH", quantity=100, cost=1500.0, name="贵州茅台")
    store.set_position("000051.OF", quantity=1000.5, name="易方达")

    lines = render_book_block(store).splitlines()

    assert lines[0].startswith(
        "- 600519.SH 贵州茅台: quantity 100, average cost 1500 per unit"
    )
    assert lines[1].startswith("- 000051.OF 易方达: quantity 1000.5")
    assert "average cost" not in lines[1]


def test_render_book_block_is_empty_for_an_empty_book(tmp_path: Path) -> None:
    """Nothing held is an empty block, which callers skip entirely."""
    assert render_book_block(_store(tmp_path)) == ""
    assert render_book_block(PositionBookStore(tmp_path / "missing.json")) == ""


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


def test_tool_set_list_remove_clear(tmp_path: Path) -> None:
    """The four actions read and write the same book through one tool."""
    tool = _tool(tmp_path)

    recorded = json.loads(
        tool.execute(action="set", symbol="600519", quantity=100, cost=1500.0)
    )
    assert recorded["status"] == "ok"
    assert recorded["position"]["symbol"] == "600519.SH"

    listed = json.loads(tool.execute(action="list"))
    assert listed["status"] == "ok"
    assert listed["count"] == 1
    assert listed["positions"][0]["quantity"] == 100

    removed = json.loads(tool.execute(action="remove", symbol="600519.SH"))
    assert removed["status"] == "ok"
    assert json.loads(tool.execute(action="remove", symbol="600519.SH"))["status"] == (
        "not_found"
    )

    json.loads(tool.execute(action="set", symbol="000001.SZ", quantity=1))
    cleared = json.loads(tool.execute(action="clear"))
    assert cleared["status"] == "ok"
    assert json.loads(tool.execute(action="list"))["count"] == 0


def test_tool_reports_input_errors_as_payloads(tmp_path: Path) -> None:
    """Invalid input comes back as an error payload, not an exception."""
    tool = _tool(tmp_path)

    missing_quantity = json.loads(tool.execute(action="set", symbol="600519.SH"))
    assert missing_quantity["status"] == "error"
    bad_symbol = json.loads(
        tool.execute(action="set", symbol="AAPL", quantity=1)
    )
    assert bad_symbol["status"] == "error"
    unknown = json.loads(tool.execute(action="teleport"))
    assert unknown["status"] == "error"


def test_tool_name_matches_its_module_for_discovery() -> None:
    """The registry maps ``position_book_tool`` to the tool named
    ``position_book``, so a discovery failure is reportable under one name."""
    assert PositionBookTool.name == "position_book"
    assert PositionBookTool.__module__ == "src.tools.position_book_tool"


def test_malformed_row_is_not_silently_lost_by_later_account_write(tmp_path):
    import json
    from src.position_book import PositionBookStore
    path = tmp_path / 'position_book.json'
    original = json.dumps({'positions': [{'symbol': '001236.OF', 'quantity': 100}, {'symbol': '001237.OF', 'quantity': 'broken'}]})
    path.write_text(original)
    book = PositionBookStore(path)
    assert book.load_account().get('errors')
    with pytest.raises(ValueError):
        book.set_account(cash={'amount': 10, 'currency': 'CNY'})
    with pytest.raises(ValueError):
        book.remove_position('001236.OF')
    assert path.read_text() == original


def test_bad_pending_rows_preserve_file_and_report_unavailable_cash(tmp_path):
    import json
    from src.position_book import PositionBookStore
    path=tmp_path/'position_book.json'
    raw=json.dumps({'positions':[{'symbol':'001236.OF','quantity':100}], 'cash':{'amount':100,'currency':'CNY','as_of':'2026-10-16'},'pending_orders':['bad']})
    path.write_text(raw)
    account=PositionBookStore(path).load_account()
    assert account['errors'] and account['available_cash'] is None
    assert path.read_text()==raw
