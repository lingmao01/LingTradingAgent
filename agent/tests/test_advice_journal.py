"""The advice journal (``src.advice_journal``) and its tool.

Every test reads and writes a journal file under ``tmp_path``; nothing touches
the real runtime root or the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.advice_journal import AdviceJournalStore, render_journal_block
from src.tools.advice_journal_tool import AdviceJournalTool

pytestmark = pytest.mark.unit


def _store(tmp_path: Path) -> AdviceJournalStore:
    return AdviceJournalStore(tmp_path / "advice_journal.json")


def _tool(tmp_path: Path) -> AdviceJournalTool:
    return AdviceJournalTool(store=_store(tmp_path))


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def test_a_recorded_call_persists_and_reloads(tmp_path: Path) -> None:
    """A call survives the process that recorded it."""
    store = _store(tmp_path)

    entry = store.record(
        "600519.SH", action="reduce", reference_price=1600.0, rationale="区间上沿"
    )

    assert entry.id == 1
    assert entry.status == "open"
    reloaded = AdviceJournalStore(tmp_path / "advice_journal.json").load()
    assert [row.to_dict() for row in reloaded] == [entry.to_dict()]


def test_bare_a_share_codes_and_hk_codes_canonicalize(tmp_path: Path) -> None:
    """The journal shares the book's symbol grammar."""
    store = _store(tmp_path)

    assert store.record("600519", action="hold").symbol == "600519.SH"
    assert store.record("000051.OF", action="watch").symbol == "000051.OF"
    assert store.record("700.HK", action="watch").symbol == "00700.HK"


def test_a_journal_is_a_log_not_a_snapshot(tmp_path: Path) -> None:
    """A second call for the same symbol is a new entry, not an update."""
    store = _store(tmp_path)

    first = store.record("600519.SH", action="hold")
    second = store.record("600519.SH", action="reduce", reference_price=1650)

    assert [first.id, second.id] == [1, 2]
    assert [row.action for row in store.load()] == ["hold", "reduce"]


def test_ids_are_never_reused_across_writes(tmp_path: Path) -> None:
    """A removed id stays retired: close/remove references never drift."""
    store = _store(tmp_path)

    first = store.record("600519.SH", action="hold")
    assert store.remove(first.id) is True

    assert store.record("000001.SZ", action="watch").id == 2


def test_record_rejects_unusable_input(tmp_path: Path) -> None:
    """Bad symbols, actions and prices raise instead of being stored."""
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="unsupported symbol"):
        store.record("AAPL", action="hold")
    with pytest.raises(ValueError, match="unsupported action"):
        store.record("600519.SH", action="buy")
    with pytest.raises(ValueError, match="finite"):
        store.record("600519.SH", action="hold", reference_price=float("nan"))
    with pytest.raises(ValueError, match="negative"):
        store.record("600519.SH", action="hold", reference_price=-1)
    with pytest.raises(ValueError, match="number"):
        store.record("600519.SH", action="hold", reference_price=True)
    assert store.load() == []


def test_the_rationale_is_kept_to_one_bounded_line(tmp_path: Path) -> None:
    """A pasted rationale can neither break the block's shape nor run away."""
    entry = _store(tmp_path).record(
        "600519.SH", action="hold", rationale="first line\nsecond line" + "x" * 400
    )

    assert "\n" not in entry.rationale
    assert entry.rationale.startswith("first line second line")
    assert len(entry.rationale) == 300


# ---------------------------------------------------------------------------
# Reading, closing, removing
# ---------------------------------------------------------------------------


def test_a_corrupt_file_reads_as_empty_and_stays_writable(tmp_path: Path) -> None:
    """A half-written journal degrades to empty instead of failing reads."""
    (tmp_path / "advice_journal.json").write_text("{not json", encoding="utf-8")

    assert _store(tmp_path).load() == []
    assert _store(tmp_path).record("600519.SH", action="hold").id == 1


def test_next_id_falls_back_past_the_highest_stored_id(tmp_path: Path) -> None:
    """A corrupt ``next_id`` cannot mint an id a stored entry already holds."""
    path = tmp_path / "advice_journal.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "next_id": "three",
                "entries": [{"id": 7, "symbol": "600519.SH", "action": "hold"}],
            }
        ),
        encoding="utf-8",
    )

    assert _store(tmp_path).record("000001.SZ", action="watch").id == 8


def test_close_records_the_outcome_and_keeps_the_history(tmp_path: Path) -> None:
    """Reviewing an entry stamps it closed and keeps everything it recorded."""
    store = _store(tmp_path)
    entry = store.record(
        "600519.SH", action="reduce", reference_price=1600.0, rationale="upper band"
    )

    closed = store.close(f"#{entry.id}", note="价格随后回落 6%")

    assert closed is not None
    assert closed.status == "closed"
    assert closed.close_note == "价格随后回落 6%"
    assert (closed.symbol, closed.action, closed.reference_price) == (
        "600519.SH",
        "reduce",
        1600.0,
    )
    assert closed.recorded_at == entry.recorded_at
    assert closed.closed_at != ""


def test_closing_twice_amends_the_note_instead_of_failing(tmp_path: Path) -> None:
    """A review can be corrected without re-recording the call."""
    store = _store(tmp_path)
    entry = store.record("600519.SH", action="hold")

    store.close(entry.id, note="looked flat")
    amended = store.close(entry.id, note="ended up 4% higher")

    assert amended is not None
    assert amended.close_note == "ended up 4% higher"
    assert len(store.load()) == 1


def test_close_and_remove_report_a_missing_entry(tmp_path: Path) -> None:
    """A wrong id is answered, not stored."""
    store = _store(tmp_path)

    assert store.close(9) is None
    assert store.remove(9) is False
    with pytest.raises(ValueError, match="id must be an integer"):
        store.close("abc")

    entry = store.record("600519.SH", action="watch")
    assert store.remove(entry.id) is True
    assert store.load() == []


# ---------------------------------------------------------------------------
# The injected block
# ---------------------------------------------------------------------------


def test_the_block_carries_open_entries_newest_first(tmp_path: Path) -> None:
    """The turn sees the latest open calls, with price and record date."""
    store = _store(tmp_path)
    store.record("600519.SH", action="hold", reference_price=1600.0)
    store.record("000001.SZ", action="watch")

    lines = render_journal_block(store).splitlines()

    assert lines[0].startswith("- #2 000001.SZ: watch")
    assert lines[1].startswith("- #1 600519.SH: hold, reference 1600")
    assert "(recorded " in lines[1]


def test_closed_entries_leave_the_block(tmp_path: Path) -> None:
    """An entry that has been graded stops riding in every turn."""
    store = _store(tmp_path)
    entry = store.record("600519.SH", action="hold")
    assert render_journal_block(store) != ""

    store.close(entry.id)

    assert render_journal_block(store) == ""


def test_an_empty_journal_renders_nothing(tmp_path: Path) -> None:
    """No file is no block, not an empty tag."""
    assert render_journal_block(_store(tmp_path)) == ""


def test_the_block_is_bounded_to_the_most_recent_entries(tmp_path: Path) -> None:
    """The block rides in every turn, so its size is capped."""
    store = _store(tmp_path)
    for index in range(10):
        store.record(f"6005{index:02d}.SH", action="hold")

    lines = render_journal_block(store).splitlines()

    assert len(lines) == 8
    assert lines[0].startswith("- #10 ")
    assert lines[-1].startswith("- #3 ")


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def test_tool_records_lists_closes_and_removes(tmp_path: Path) -> None:
    """Every journal action round-trips through a JSON payload."""
    tool = _tool(tmp_path)

    recorded = json.loads(
        tool.execute(
            action="record",
            symbol="600519.SH",
            advice="reduce",
            reference_price=1600,
            rationale="区间上沿",
        )
    )
    assert recorded["status"] == "ok"
    assert recorded["entry"]["id"] == 1

    listed = json.loads(tool.execute(action="list"))
    assert listed["count"] == 1
    assert listed["open_count"] == 1

    closed = json.loads(tool.execute(action="close", id=1, note="worked"))
    assert closed["status"] == "ok"
    assert closed["entry"]["status"] == "closed"

    assert json.loads(tool.execute(action="close", id=99))["status"] == "not_found"
    assert json.loads(tool.execute(action="remove", id=1))["status"] == "ok"
    assert json.loads(tool.execute(action="remove", id=1))["status"] == "not_found"


def test_tool_reports_input_errors_as_payloads(tmp_path: Path) -> None:
    """Invalid input comes back as an error payload, not an exception."""
    tool = _tool(tmp_path)

    missing_symbol = json.loads(tool.execute(action="record", advice="hold"))
    assert missing_symbol["status"] == "error"
    bad_action = json.loads(
        tool.execute(action="record", symbol="600519.SH", advice="buy")
    )
    assert bad_action["status"] == "error"
    bad_id = json.loads(tool.execute(action="close", id="abc"))
    assert bad_id["status"] == "error"
    unknown = json.loads(tool.execute(action="teleport"))
    assert unknown["status"] == "error"


def test_tool_name_matches_its_module_for_discovery() -> None:
    """The registry maps ``advice_journal_tool`` to the tool named
    ``advice_journal``, so a discovery failure is reportable under one name."""
    assert AdviceJournalTool.name == "advice_journal"
    assert AdviceJournalTool.__module__ == "src.tools.advice_journal_tool"