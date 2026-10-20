"""Actively collect outside-market observations from the existing registered client."""
from datetime import date, timedelta

from src.advisor.collection import bounded_collect
from src.advisor.supplemental import absorb

FAMILIES = {
    '海外股市观察': ['SPY.US', 'QQQ.US'],
    '油金期货观察': ['GC=F', 'CL=F'],
    '汇率与利率指数观察': ['USDCNY=X', '^TNX'],
}


def collect_external(registry, payload):
    report = payload['data']
    report.setdefault('external_market', [])
    coverage, gaps = [], []
    if not registry.get('get_market_data'):
        report['external_gaps'] = [{'scope': '外围行情', 'reason': 'registered market data unavailable'}]
        report['external_coverage'] = []
        return
    cutoff = report['as_of']
    start = (date.fromisoformat(cutoff) - timedelta(days=14)).isoformat()
    def fetch(codes):
        args = {'codes': codes, 'start_date': start, 'end_date': cutoff, 'source': 'yahoo', 'max_rows': 0}
        return args, registry.execute('get_market_data', args)
    completed, failures = bounded_collect({family: (lambda codes=codes: fetch(codes)) for family, codes in FAMILIES.items()}, timeout_seconds=40, max_workers=3)
    for family, codes in FAMILIES.items():
        if family in completed:
            args, raw = completed[family]
            absorb(payload, 'get_market_data', args, raw)
            present = {row['symbol'] for row in payload['data'].get('external_market', [])}
            missing = [symbol for symbol in codes if symbol not in present]
            if missing:
                gaps.append({'scope': family, 'symbols': missing, 'reason': 'no dated observations for the requested outside series'})
            coverage.append({'family': family, 'requested_symbols': codes, 'observed_symbols': [symbol for symbol in codes if symbol in present]})
        else:
            coverage.append({'family': family, 'requested_symbols': codes, 'observed_symbols': []})
            gaps.append({'scope': family, 'symbols': codes, 'reason': failures.get(family, 'outside observations unavailable')})
    payload['data']['external_coverage'] = coverage
    payload['data']['external_gaps'] = gaps
    # Native instrument quotes: ETFs/futures are not index/spot substitutions.
    payload['data']['external_caliber'] = 'raw listed ETF, futures, FX pair and yield-index observations; not feeder NAV or spot commodity prices'
