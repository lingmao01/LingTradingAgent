"""Join registered source results to the frozen advisor account, never model prose."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from src.advisor.assessment import assess_portfolio
from src.advisor.models import Observation
from src.advisor.news import _verify, primary_url
from src.advisor.service import _observation


def absorb(payload: dict[str, Any], tool: str, args: dict, result: str) -> bool:
    report = payload['data']
    report.setdefault('external_market', [])
    try:
        raw = json.loads(result)
    except (ValueError, TypeError):
        return False
    if not isinstance(raw, dict) or raw.get('ok') is False or raw.get('status') in {'error', 'failed'}:
        return False
    if tool == 'read_url':
        url = args.get('url')
        if not isinstance(url, str) or not primary_url(url):
            return False
        try:
            item = _verify({'url': url, 'categories': ['补充原文研究']}, raw, date.fromisoformat(report['as_of']))
        except (ValueError, TypeError, KeyError):
            return False
        news = report.setdefault('news', {'primary': [], 'gaps': [], 'coverage': []})
        news['primary'] = [row for row in news.get('primary', []) if row['url'] != url] + [item]
        news['research_sha256'] = hashlib.sha256(json.dumps(news['primary'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return True
    if tool not in {'get_market_data', 'get_fund_data'}:
        return False
    symbols = args.get('codes')
    if not isinstance(symbols, list):
        return False
    updated = {}
    external = {row['symbol']: row for row in report['external_market']}
    for symbol in symbols:
        if not isinstance(symbol, str) or not isinstance(raw.get(symbol), (dict, list)):
            continue
        if tool == 'get_fund_data':
            entry = raw[symbol]
            if not isinstance(entry, dict) or entry.get('_provenance', {}).get('caliber') != 'nav':
                continue
            metadata, rows, field = entry['_provenance'], entry.get('data'), 'unit_nav'
        else:
            metadata, rows, field = (raw.get('_provenance') or {}).get(symbol) or {}, raw[symbol], 'close'
        if not metadata.get('source') or metadata['source'] == 'unknown':
            continue
        observed = _observation(symbol, rows, field, metadata, report['as_of'])
        if not observed:
            continue
        if field == 'unit_nav':
            observed['quality'] = 'stale' if metadata.get('stale') else 'latest_disclosed'
            from src.market_observation import market_now
            expected = (raw.get('_session') or {}).get('latest_nav_date')
            if expected and report['as_of'] == market_now().date().isoformat():
                observed['expected_nav_date'] = expected
                observed['disclosure_reference_basis'] = 'calendar disclosure reference, not a guaranteed product publication time'
                if observed['as_of'] < expected:
                    observed['quality'] = 'stale'
        try:
            Observation.parse(symbol, observed, report['as_of'])
        except (ValueError, TypeError):
            continue
        prior = payload.get('evidence', {}).get(symbol)
        if prior and prior['as_of'] > observed['as_of']:
            continue
        updated[symbol] = observed
        if field == 'close' and symbol not in {row['symbol'] for row in report.get('regime', [])}:
            history = observed['history']
            change = (history[-1]['value'] / history[-2]['value'] - 1) * 100 if len(history) > 1 else None
            external[symbol] = {key: value for key, value in observed.items() if key != 'history'}
            external[symbol].update(price_change_percent=change, previous_as_of=history[-2]['date'] if len(history) > 1 else None,
                                    return_caliber='unadjusted price change, not total return')
            if not observed.get('currency') and symbol.endswith('.US'):
                external[symbol].update(currency='USD', currency_basis='canonical_us_venue')
    if not updated:
        return False
    payload.setdefault('evidence', {}).update(updated)
    inputs = payload.get('inputs')
    if inputs:
        fresh = assess_portfolio(inputs['account'], payload['evidence'], as_of=inputs['as_of'],
                                 rules=inputs['rules'], discipline=inputs['discipline'], identities=inputs['identities'], redemption_confirmation_date=inputs.get('redemption_confirmation_date'))
        for key in ('account_source', 'discipline_source', 'recorded_discipline', 'news', 'external_coverage', 'external_gaps', 'external_caliber', 'request_context'):
            if key in report:
                fresh[key] = report[key]
        fresh['data_gaps'].extend(row for row in report.get('data_gaps', []) if row.get('symbol') not in updated)
        payload['data'].clear()
        payload['data'].update(fresh)
    payload['data']['external_market'] = sorted(external.values(), key=lambda row: row['symbol'])
    return True
