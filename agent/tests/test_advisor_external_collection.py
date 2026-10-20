"""Outside observations are actively collected, dated and kept per market family."""
import json

import pytest

from tests.test_advisor_supplemental import _payload

pytestmark = pytest.mark.unit


def test_external_collection_keeps_healthy_family_when_another_source_fails():
    from src.advisor.external import collect_external
    class Registry:
        def get(self, name):
            return object() if name == 'get_market_data' else None
        def execute(self, name, args):
            if 'SPY.US' not in args['codes']:
                raise TimeoutError('source timed out')
            return json.dumps({'SPY.US': [{'date': '2026-10-14', 'close': 400}, {'date': '2026-10-15', 'close': 410}], '_provenance': {'SPY.US': {'source': 'yahoo', 'quote_currency': 'USD'}}})
    payload = _payload()
    collect_external(Registry(), payload)
    assert payload['data']['external_market'][0]['symbol'] == 'SPY.US'
    assert payload['data']['external_gaps']
    assert len(payload['data']['external_coverage']) == 3


def test_explicit_primary_release_seed_is_verified_even_when_search_returns_only_indexes():
    from src.advisor.news import collect_news
    from tests.test_advisor_news import Registry
    url = 'https://www.bls.gov/news.release/cpi.nr0.htm'
    registry = Registry([], {url: {'status': 'ok', 'url': url, 'title': 'CPI', 'content': f'URL Source: {url}\nPublished Time: 2026-10-14\nMarkdown Content:\nOfficial release'}})
    result = collect_news(registry, as_of='2026-10-16', seeds=[{'url': url, 'title': 'CPI release', 'categories': ['全球宏观与流动性']}])
    assert result['primary'][0]['url'] == url
