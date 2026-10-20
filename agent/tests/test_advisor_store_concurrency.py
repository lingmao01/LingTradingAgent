"""Concurrent book edits serialize the whole read-modify-write transaction."""
import threading

import pytest

from src.position_book import PositionBookStore

pytestmark = pytest.mark.unit


def test_second_book_edit_cannot_overtake_a_paused_first_save(tmp_path, monkeypatch):
    path = tmp_path / 'book.json'
    first, second = PositionBookStore(path), PositionBookStore(path)
    entered, release, overtook = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original = PositionBookStore._save
    def save(store, *args, **kwargs):
        if threading.current_thread().name == 'first-write':
            entered.set()
            release.wait(2)
        else:
            overtook.set()
        return original(store, *args, **kwargs)
    monkeypatch.setattr(PositionBookStore, '_save', save)
    def write(store, symbol):
        try: store.set_position(symbol, quantity=100)
        except Exception as exc: errors.append(exc)
    a = threading.Thread(target=write, args=(first, '001236.OF'), name='first-write')
    b = threading.Thread(target=write, args=(second, '001237.OF'), name='second-write')
    a.start()
    assert entered.wait(1)
    b.start()
    try:
        assert not overtook.wait(.05)
    finally:
        release.set()
        a.join(2)
        b.join(2)
    assert not errors
    assert {row.symbol for row in first.load()} == {'001236.OF', '001237.OF'}
