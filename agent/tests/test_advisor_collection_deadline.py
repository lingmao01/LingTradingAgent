"""A hanging source cannot consume the whole report or drop completed evidence."""
import threading
import time

import pytest

from src.position_book import PositionBookStore

pytestmark = pytest.mark.unit


def test_collection_deadline_keeps_market_and_other_fund_marks(tmp_path):
    from src.advisor.service import AssessmentService
    book = PositionBookStore(tmp_path / 'position_book.json')
    book.set_position('001236.OF', quantity=100)
    book.set_position('001237.OF', quantity=100)
    release = threading.Event()
    def fund(codes, **kwargs):
        if codes[0] == '001236.OF':
            release.wait(.5)
        return {codes[0]: {'data': [{'date': '2026-10-15', 'unit_nav': 2}], '_provenance': {'caliber': 'nav', 'source': 'test', 'currency': 'CNY'}}}
    def market(**kwargs):
        return {'000300.SH': [{'date': '2026-10-15', 'close': 4000}], '_provenance': {'000300.SH': {'source': 'test', 'quality': 'current'}}}
    start = time.monotonic()
    try:
        result = AssessmentService(book=book, fund_fetcher=fund, market_fetcher=market, collection_timeout_seconds=.03).collect(as_of='2026-10-15')
        assert time.monotonic() - start < .2
        assert '001237.OF' in result['evidence'] and '000300.SH' in result['evidence']
        assert '001236.OF' not in result['evidence']
        assert any('deadline' in gap['reason'] for gap in result['data']['data_gaps'] if gap.get('symbol') == '001236.OF')
    finally:
        release.set()
