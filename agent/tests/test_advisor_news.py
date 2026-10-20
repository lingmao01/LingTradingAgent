"""News discovery cannot certify a factual premise or a publication date."""
import json

import pytest

pytestmark = pytest.mark.unit


class Registry:
    def __init__(self, results, pages):
        self.results, self.pages, self.calls = results, pages, []
    def get(self, name):
        return object()
    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        if name == 'web_search':
            return json.dumps({'status': 'ok', 'results': self.results})
        return json.dumps(self.pages[arguments['url']])


def test_primary_news_keeps_original_times_and_snippets_stay_unverified():
    from src.advisor.news import collect_news
    url = 'https://www.pbc.gov.cn/example.html'
    fake = 'https://www.pbc.gov.cn.evil.example/fake'
    registry = Registry([{'title': '央行公告', 'url': url, 'published_at': '2026-10-15', 'snippet': 'search-only claim'}, {'title': '伪装来源', 'url': fake}], {
        url: {'status': 'ok', 'url': url, 'content': f'Title: 央行公告\nURL Source: {url}\nPublished Time: 2026-10-15\nMarkdown Content:\n公开市场业务交易公告。', 'title': '央行公告'},
    })
    result = collect_news(registry, as_of='2026-10-16', themes=['AI'])
    assert len(result['primary']) == 1
    assert result['primary'][0]['published_at'] == '2026-10-15'
    assert result['primary'][0]['event_time'] is None
    assert result['primary'][0]['content_sha256']
    assert all(row['verified'] is False for row in result['discovery'])
    assert all(arguments.get('url') != fake for name, arguments in registry.calls if name == 'read_url')
    assert len([1 for name, args in registry.calls if name == 'read_url']) == 1


@pytest.mark.parametrize('change', ['cached', 'future', 'wrong-origin', 'missing-date'])
def test_cached_future_wrong_origin_and_undated_pages_cannot_be_current_news(change):
    from src.advisor.news import collect_news
    url = 'https://www.pbc.gov.cn/example.html'
    page = {'status': 'ok', 'url': url, 'content': f'Title: 公告\nURL Source: {url}\nPublished Time: 2026-10-15\nMarkdown Content:\n公告内容', 'title': '公告'}
    if change == 'cached': page['cached'] = True
    if change == 'future': page['content'] = page['content'].replace('2026-10-15', '2027-01-01')
    if change == 'wrong-origin': page['content'] = page['content'].replace('URL Source: ' + url, 'URL Source: https://evil.example/')
    if change == 'missing-date': page['content'] = page['content'].replace('Published Time: 2026-10-15\n', '')
    result = collect_news(Registry([{'url': url, 'title': '公告', 'published_at': '2026-10-15'}], {url: page}), as_of='2026-10-16')
    assert result['primary'] == []
    assert result['gaps']


def test_one_news_source_failure_does_not_discard_other_primary_evidence():
    from src.advisor.news import collect_news
    registry = Registry([], {})
    original = registry.execute
    def execute(name, args):
        if name == 'web_search' and '中国' in args['query']:
            raise TimeoutError('source timeout')
        return original(name, args)
    registry.execute = execute
    result = collect_news(registry, as_of='2026-10-16')
    assert result['coverage'] and result['gaps']
    assert any('timeout' in str(gap) for gap in result['gaps'])


def test_bls_embargo_date_is_publication_time_and_reference_month_is_separate():
    from src.advisor.news import collect_news
    url = 'https://www.bls.gov/news.release/cpi.nr0.htm'
    page = {'status': 'ok', 'url': url, 'title': 'Consumer Price Index Summary', 'content': f'''Title: Consumer Price Index Summary
URL Source: {url}
Markdown Content:
Transmission of material in this release is embargoed until
8:30 a.m. (ET) Wednesday, October 14, 2026 USDL-26-1574
CONSUMER PRICE INDEX - SEPTEMBER 2026
The Consumer Price Index for All Urban Consumers (CPI-U) increased 0.6 percent on a seasonally adjusted basis in September after rising 0.4 percent in August.
Over the last 12 months, the all items index increased 3.7 percent before seasonal adjustment.
The all items index rose 3.7 percent for the 12 months ending September, a larger increase than the 3.4-percent increase reported for the 12 months ending in August.
'''}
    result = collect_news(Registry([{'url': url, 'title': page['title']}], {url: page}), as_of='2026-10-16')
    assert result['primary'][0]['published_at'] == '2026-10-14'
    assert result['primary'][0]['reference_period'] == '2026-09'
    facts = {fact['metric']: fact for fact in result['primary'][0]['facts']}
    assert facts['cpi_yoy']['value'] == 3.7
    assert facts['cpi_previous_yoy']['value'] == 3.4
    assert all(fact['unit'] == 'percent' for fact in facts.values())


def test_directory_follows_one_observed_same_origin_summary_link():
    from src.advisor.news import collect_news
    toc = 'https://www.bls.gov/news.release/cpi.toc.htm'
    article = 'https://www.bls.gov/news.release/cpi.nr0.htm'
    registry = Registry([{'url': toc, 'title': 'CPI contents'}], {
        toc: {'status': 'ok', 'url': toc, 'content': f'URL Source: {toc}\nMarkdown Content:\n[Summary](cpi.nr0.htm)\n[Other](https://evil.example/payload)'},
        article: {'status': 'ok', 'url': article, 'title': 'CPI summary', 'content': f'URL Source: {article}\nPublished Time: 2026-10-14\nMarkdown Content:\nOfficial release'},
    })
    result = collect_news(registry, as_of='2026-10-16', themes=['易方达人工智能ETF联接C', '华夏半导体芯片ETF联接A'])
    assert result['primary'][0]['url'] == article
    assert result['primary'][0]['discovered_via'] == toc
    read_urls = [args['url'] for name, args in registry.calls if name == 'read_url']
    assert read_urls == [toc, article]
    queries = [args['query'] for name, args in registry.calls if name == 'web_search']
    assert all('易方达' not in query for query in queries)
    assert any('人工智能' in query and '半导体' in query for query in queries)


def test_directory_does_not_follow_cross_origin_or_forged_origin_links():
    from src.advisor.news import collect_news
    toc = 'https://www.bls.gov/news.release/cpi.toc.htm'
    registry = Registry([{'url': toc}], {toc: {'status': 'ok', 'url': toc, 'content': f'URL Source: {toc}\nMarkdown Content:\n[Summary](https://evil.example/article)'}})
    assert collect_news(registry, as_of='2026-10-16')['primary'] == []
    assert len([1 for name, _ in registry.calls if name == 'read_url']) == 1
