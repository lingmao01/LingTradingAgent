"""Review only published decisions, with unchanged ex-ante comparison methods."""
from __future__ import annotations

from datetime import date, datetime

from src.advisor.collection import bounded_collect
from src.advisor.evaluation import compare_baselines
from src.advisor.journal import PublishedAdviceStore
from src.fund_data import fetch_fund_data
from src.market_data import fetch_market_data
from src.market_observation import SHANGHAI, market_now


class ReviewService:
    def __init__(self, *, store=None, fund_fetcher=None, market_fetcher=None):
        self.store = store or PublishedAdviceStore()
        self.fund_fetcher = fund_fetcher or fetch_fund_data
        self.market_fetcher = market_fetcher or fetch_market_data

    def review(self, publication_id, *, as_of):
        cutoff = date.fromisoformat(as_of)
        if cutoff > market_now().date():
            raise ValueError('review cutoff cannot be in the future')
        entry = next((row for row in self.store.list() if row['id'] == publication_id), None)
        if entry is None:
            raise ValueError('published advice not found')
        result = {'publication_id': publication_id, 'as_of': as_of, 'scope': 'fund_sleeve_paper_baselines',
                  'execution_state': entry.get('execution_state', 'not_reported'),
                  'realized_execution_performance': None, 'scenarios': [], 'review_status': 'unavailable', 'gaps': []}
        if entry.get('forecasts'):
            from src.advisor.forecasts import evaluate_forecasts, resolve_forecast_outcomes
            # Outcome resolution must use the record's original declared source.
            # Absence of such future evidence remains UNRESOLVED, not a miss.
            outcomes = resolve_forecast_outcomes(entry['forecasts'],as_of=as_of,market_fetcher=self.market_fetcher,fund_fetcher=self.fund_fetcher)
            result['forecasts'] = evaluate_forecasts(entry['forecasts'], outcomes, as_of=as_of).to_dict()
            self.store.add_evaluation(publication_id,result)
        holdings = entry['holdings']
        if not holdings or any(not row.get('symbol') or row.get('fund_weight') is None for row in holdings):
            result['gaps'].append('published initial fund weights were not exact; a paper portfolio is not fabricated')
            return result
        weights = {row['symbol']: row['fund_weight'] for row in holdings}
        start = max(entry['information_as_of'], datetime.fromisoformat(entry['published_at']).astimezone(SHANGHAI).date().isoformat())
        benchmark = entry['review_protocol']['benchmark']
        def fund(symbol):
            entry = self.fund_fetcher([symbol], view='nav', start_date=start, end_date=as_of, max_rows=0).get(symbol) or {}
            if entry.get('_provenance', {}).get('caliber') != 'nav':
                raise ValueError('fund NAV caliber unavailable')
            return [{'date': row['date'], 'value': row['unit_nav']} for row in entry.get('data', []) if row.get('unit_nav') is not None]
        def market():
            symbols = [benchmark, *[symbol for symbol in weights if not symbol.endswith('.OF')]]
            payload = self.market_fetcher(codes=symbols, start_date=start, end_date=as_of, max_rows=0, include_provenance=True)
            return {symbol: [{'date': next((row[key] for key in ('date', 'trade_date', 'datetime', 'timestamp') if key in row), ''), 'value': row['close']} for row in payload.get(symbol, []) if row.get('close') is not None] for symbol in symbols}
        jobs = {'market': market, **{symbol: (lambda symbol=symbol: fund(symbol)) for symbol in weights if symbol.endswith('.OF')}}
        completed, failures = bounded_collect(jobs, timeout_seconds=45, max_workers=4)
        histories = completed.pop('market', {})
        benchmark_history = histories.pop(benchmark, [])
        histories.update(completed)
        result['gaps'].extend({'symbol': symbol, 'reason': reason} for symbol, reason in failures.items())
        if not benchmark_history:
            result['gaps'].append('benchmark history unavailable; baseline portfolio paths can still be reviewed')
        if any(not histories.get(symbol) for symbol in weights):
            result['gaps'].append('at least one original holding has no comparable history; weights are not redistributed')
            return result
        advice = entry['review_protocol'].get('advice_scenario') or {}
        target_weights = advice.get('target_weights')
        if target_weights is None:
            result['gaps'].append('published advice cannot be scored as a specified allocation: ' + advice.get('reason', 'no ex-ante advice-scenario protocol was recorded'))
        result['advice_scenario'] = advice
        for costs in entry['review_protocol'].get('cost_sensitivity_bps', [0, 50, 150]):
            try:
                comparison = compare_baselines(histories, weights, published_as_of=start, cutoff=as_of,
                    cost_bps=costs, benchmark=benchmark_history, horizons=entry['review_protocol']['observation_horizons'], advice_weights=target_weights)
                result['scenarios'].append({'assumed_cost_bps': costs, 'result': comparison})
                result['gaps'].extend(comparison.get('gaps', []))
            except ValueError as exc:
                result['gaps'].append(str(exc))
        if result['scenarios']:
            unavailable = sorted(set(horizon for row in result['scenarios'] for horizon in row['result']['unavailable_horizons']))
            result['unavailable_horizons'] = unavailable
            result['review_status'] = 'partial' if unavailable or result['gaps'] else 'complete'
            self.store.add_evaluation(publication_id, result)
        return result
