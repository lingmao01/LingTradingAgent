"""Bounded discovery and original-page verification; no snippet-as-fact promotion."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

from src.advisor.collection import bounded_collect

PRIMARY_HOSTS = frozenset({
    'pbc.gov.cn', 'stats.gov.cn', 'csrc.gov.cn', 'mof.gov.cn', 'nfra.gov.cn',
    'gov.cn', 'sse.com.cn', 'szse.cn', 'chinabond.com.cn', 'safe.gov.cn',
    'federalreserve.gov', 'bls.gov', 'bea.gov', 'treasury.gov', 'ecb.europa.eu',
    'bis.org', 'commerce.gov', 'boj.or.jp', 'opec.org',
})


def primary_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower().rstrip('.')
        return parsed.scheme == 'https' and not parsed.username and not parsed.password and parsed.port in {None, 443} and any(host == domain or host.endswith('.' + domain) for domain in PRIMARY_HOSTS)
    except ValueError:
        return False


def _origin_key(url: str):
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or '').lower().rstrip('.'), parsed.path.rstrip('/'), parsed.query


def _verify(candidate, page, cutoff):
    url = candidate['url']
    if page.get('status') != 'ok' or page.get('cached'):
        raise ValueError('original page unavailable or explicitly cached')
    content = str(page.get('content') or '')
    source = re.search(r'^URL Source:\s*(\S+)', content, re.M)
    if not source or _origin_key(source.group(1)) != _origin_key(url):
        raise ValueError('original URL could not be confirmed by the reader')
    published = re.search(r'(?:^Published Time:|发布时间\s*[:：]|发布日期\s*[:：])\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})', content, re.M)
    stamp = None
    if published:
        stamp = date(*map(int, re.split('[-/]', published.group(1))))
    elif urlsplit(url).hostname in {'www.bls.gov', 'bls.gov'}:
        release = re.search(r'Transmission of material in this release is embargoed until.{0,180}?((?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4})', content, re.S)
        if release:
            stamp = datetime.strptime(release.group(1), '%B %d, %Y').date()
    if stamp is None:
        raise ValueError('original publication date unavailable; search date is not substituted')
    if stamp > cutoff or stamp < cutoff - timedelta(days=7):
        raise ValueError('publication is future-dated or outside the seven-day event window')
    body = content.split('Markdown Content:', 1)[-1].strip()
    if not body:
        raise ValueError('original page has no readable content')
    facts, period = _primary_facts(url, body)
    return {
        'facts': facts, 'reference_period': period,
        'title': page.get('title') or candidate.get('title') or url,
        'url': url, 'source_domain': urlsplit(url).hostname,
        'published_at': stamp.isoformat(), 'event_time': None,
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'evidence_quality': 'primary_page_via_reader', 'verified': True,
        'verification_scope': 'original URL, publication date and retrieved content; not a causal market forecast',
        'content_sha256': hashlib.sha256(content.encode()).hexdigest(),
        'content': content, 'categories': candidate['categories'],
    }


def _primary_facts(url, body):
    """Only registered original-release templates emit structured macro facts."""
    if urlsplit(url).hostname not in {'www.bls.gov', 'bls.gov'} or 'CONSUMER PRICE INDEX -' not in body:
        return [], None
    period = re.search(r'CONSUMER PRICE INDEX - ([A-Z]+) (\d{4})', body)
    reference = datetime.strptime(f'{period[1]} {period[2]}', '%B %Y').strftime('%Y-%m') if period else None
    patterns = {
        'cpi_yoy': r'Over the last 12\s+months, the all items index increased\s+([\d.]+) percent',
        'cpi_mom': r'CPI-U\) increased\s+([\d.]+) percent on a seasonally adjusted basis',
        'cpi_previous_yoy': r'a larger increase than the\s+([\d.]+)-percent increase reported',
    }
    facts = []
    for metric, pattern in patterns.items():
        match = re.search(pattern, body, re.I)
        if match:
            facts.append({'metric': metric, 'value': float(match[1]), 'unit': 'percent',
                          'reference_period': reference if metric != 'cpi_previous_yoy' else 'previous_month_year_on_year',
                          'source_url': url, 'extraction_method': 'bls-cpi-template-v1'})
    return facts, reference


def read_original(registry, candidate, cutoff):
    """A directory is discovery. Follow at most one observed article link."""
    page = json.loads(registry.execute('read_url', {'url': candidate['url']}))
    try:
        return _verify(candidate, page, cutoff)
    except ValueError as exc:
        if 'publication date unavailable' not in str(exc):
            raise
    source = re.search(r'^URL Source:\s*(\S+)', str(page.get('content') or ''), re.M)
    if not source or _origin_key(source[1]) != _origin_key(candidate['url']):
        raise ValueError('directory origin could not be confirmed')
    for label, href in re.findall(r'\[([^]\n]{1,160})\]\(([^\s)]+)\)', str(page.get('content') or '')):
        if not re.search(r'Summary|News Release|新闻稿|公告|全文|公开市场', label, re.I):
            continue
        url = urljoin(candidate['url'], href)
        if not primary_url(url) or urlsplit(url).hostname != urlsplit(candidate['url']).hostname or _origin_key(url) == _origin_key(candidate['url']):
            continue
        article = json.loads(registry.execute('read_url', {'url': url}))
        result = _verify({**candidate, 'url': url, 'title': label}, article, cutoff)
        result['discovered_via'] = candidate['url']
        return result
    raise ValueError('directory has no eligible original article link')


def _theme_terms(themes):
    text = ' '.join(map(str, themes))
    return [term for term in ('人工智能', '半导体', '芯片', '机器人', '云计算', '创新药', '科创', '红利', '黄金') if term in text][:6]


def collect_news(registry, *, as_of: str, themes=(), seeds=()) -> dict[str, Any]:
    cutoff = date.fromisoformat(as_of)
    themes = _theme_terms(themes)
    month = as_of[:7]
    topic_query = '(' + ' OR '.join(themes) + ')' if themes else '中国 ETF 行业'
    queries = [
        ('中国政策与市场', f'{month} 中国 货币政策 经济数据 公告 (site:pbc.gov.cn OR site:stats.gov.cn OR site:csrc.gov.cn OR site:sse.com.cn)'),
        ('全球宏观与流动性', f'{month} monetary policy inflation release (site:federalreserve.gov OR site:bls.gov OR site:ecb.europa.eu)'),
        ('持仓相关主题', f'{month} {topic_query} 政策 公告 (site:gov.cn OR site:sse.com.cn OR site:szse.cn)'),
    ]
    result = {'as_of': as_of, 'window_calendar_days': 7, 'coverage': [], 'discovery': [], 'primary': [], 'gaps': [],
              'limitations': ['Finite targeted research cannot cover all outside information.', 'A publication is not proof of a future market reaction.']}
    if not registry.get('web_search') or not registry.get('read_url'):
        result['gaps'].append({'scope': 'news', 'reason': 'registered search/reader unavailable'})
        return result
    def search(category, query):
        payload = json.loads(registry.execute('web_search', {'query': query, 'max_results': 4}))
        if payload.get('status') != 'ok':
            raise ValueError(payload.get('error') or 'search unavailable')
        return payload.get('results', [])
    candidates = {row["url"]: dict(row) for row in seeds if isinstance(row, dict) and primary_url(str(row.get("url") or ""))}
    queries_by_category = dict(queries)
    completed, failures = bounded_collect({category: (lambda category=category, query=query: search(category, query)) for category, query in queries}, timeout_seconds=20, max_workers=3)
    for category, rows in completed.items():
        query = queries_by_category[category]
        result['coverage'].append({'category': category, 'query': query, 'discovered_count': len(rows)})
        for row in rows:
            if not isinstance(row, dict) or not row.get('url'):
                continue
            row = {**row, 'verified': False, 'evidence_quality': 'search_snippet', 'category': category}
            result['discovery'].append(row)
            parsed_url = urlsplit(row['url'])
            if primary_url(row['url']) and not (parsed_url.path in {'', '/'} and parsed_url.query):
                candidate = candidates.setdefault(row['url'], {**row, 'categories': []})
                candidate['categories'].append(category)
    for category, reason in failures.items():
        result['coverage'].append({'category': category, 'query': queries_by_category[category], 'discovered_count': None})
        result['gaps'].append({'scope': category, 'reason': reason})
    # Choose one source per research category first, so one noisy search cannot
    # consume every original-page slot. Repeated URLs are fetched only once.
    selected = []
    for category, _ in queries:
        candidate = next((row for row in candidates.values() if category in row['categories'] and row not in selected), None)
        if candidate:
            selected.append(candidate)
    selected += [row for row in candidates.values() if row not in selected][:max(0, 4-len(selected))]
    def read(candidate):
        return read_original(registry, candidate, cutoff)
    completed, failures = bounded_collect({row['url']: (lambda row=row: read(row)) for row in selected}, timeout_seconds=25, max_workers=4)
    result['primary'].extend(completed.values())
    result['gaps'].extend({'url': url, 'reason': reason} for url, reason in failures.items())
    result['primary'].sort(key=lambda row: (row['published_at'], row['url']), reverse=True)
    result['coverage'].sort(key=lambda row: row['category'])
    for category, _ in queries:
        if not any(category in row['categories'] for row in result['primary']):
            result['gaps'].append({'scope': category, 'reason': 'no timely original-page evidence verified in this bounded research'})
    result['research_sha256'] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result
