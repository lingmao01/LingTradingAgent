"""Time-ordered, cost-explicit paper baselines; no invented execution or odds."""
from __future__ import annotations

from math import isfinite

import pandas as pd


def compare_baselines(histories, weights, *, published_as_of, cutoff, cost_bps, benchmark=None,
                      horizons=(5, 20), periodic_every=5, rebalance_every=5, advice_weights=None):
    if not weights or cost_bps is None or isinstance(cost_bps, bool):
        raise ValueError('known initial weights and explicit scenario costs are required')
    costs = float(cost_bps) / 10000
    if not isfinite(costs) or not 0 <= costs < 1:
        raise ValueError('scenario cost must be finite and below 100 percent')
    values = {symbol: float(weight) for symbol, weight in weights.items()}
    if any(not isfinite(weight) or weight < 0 for weight in values.values()) or abs(sum(values.values()) - 1) > 1e-8:
        raise ValueError('initial weights must be finite, nonnegative and sum to one')
    if type(periodic_every) is not int or type(rebalance_every) is not int or min(periodic_every, rebalance_every) < 1:
        raise ValueError('invalid evaluation schedule')
    def series(rows):
        points = {}
        for row in rows:
            day = str(row['date'])[:10]
            pd.Timestamp(day)
            value = float(row['value'])
            if isfinite(value) and value > 0 and published_as_of < day <= cutoff:
                points[day] = value
        return pd.Series(points, dtype=float).sort_index()
    frame = pd.DataFrame({symbol: series(histories.get(symbol, [])) for symbol in values}).dropna(how='any')
    if frame.empty:
        raise ValueError('no common observations after publication and before cutoff')
    targets = None
    if advice_weights is not None:
        targets = {symbol: float(advice_weights.get(symbol, 0)) for symbol in values}
        if set(advice_weights) - set(values) or any(not isfinite(v) or v < 0 for v in targets.values()) or sum(targets.values()) > 1 + 1e-8:
            raise ValueError('invalid frozen advice target allocation')
    cash = {'hold': 1., 'periodic_invest': 1., 'rebalance': 1.}
    if targets is not None:
        cash['advice_scenario'] = 1.
    traded = {name: 0. for name in cash}
    turnover_paths = {name: [] for name in cash}
    fee_paths = {name: [] for name in cash}
    quantities = {name: {symbol: 0. for symbol in values} for name in cash}
    paths = {name: [] for name in cash}
    for index, (_, prices) in enumerate(frame.iterrows()):
        for name in paths:
            qty = quantities[name]
            deploy = cash[name] if index == 0 and name != 'periodic_invest' else min(cash[name], .25) if name == 'periodic_invest' and index % periodic_every == 0 else 0
            chosen = values
            if name == 'advice_scenario':
                invested = sum(targets.values())
                deploy = invested if index == 0 else 0
                chosen = {symbol: weight / invested for symbol, weight in targets.items()} if invested else targets
            if deploy:
                for symbol, weight in chosen.items():
                    qty[symbol] += deploy / (1 + costs) * weight / prices[symbol]
                cash[name] -= deploy
                traded[name] += deploy / (1 + costs)
            if name == 'rebalance' and index and index % rebalance_every == 0:
                positions = {symbol: qty[symbol] * prices[symbol] for symbol in values}
                wealth = cash[name] + sum(positions.values())
                # Solve wealth_after + fee(turnover) = wealth_before, so a
                # fully invested rebalance never borrows its transaction cost.
                low, high = 0., wealth
                for _ in range(48):
                    mid = (low + high) / 2
                    spent = mid + costs * sum(abs(mid * values[symbol] - positions[symbol]) for symbol in values)
                    if spent > wealth:
                        high = mid
                    else:
                        low = mid
                traded[name] += sum(abs(low * values[symbol] - positions[symbol]) for symbol in values)
                for symbol in values:
                    qty[symbol] = low * values[symbol] / prices[symbol]
                cash[name] = 0.
            # Liquidation-equivalent end wealth: held assets pay the same
            # terminal scenario cost, uninvested cash is not charged.
            liquidation = sum(qty[symbol] * prices[symbol] for symbol in values)
            paths[name].append(cash[name] + liquidation * (1 - costs))
            turnover_paths[name].append(traded[name] + liquidation)
            fee_paths[name].append((traded[name] + liquidation) * costs)
    bench = series(benchmark or [])
    result = {'method_version': 'paper-baselines-v1', 'start_date': str(frame.index[0]), 'cutoff': cutoff,
        'realized_execution_performance': None, 'probability_of_success': None,
        'assumptions': {'initial_capital': 1, 'cost_bps': cost_bps, 'cost_scope': 'each buy/sell; terminal liquidation included; hypothetical, not product fees',
            'cash_yield': 0, 'periodic_every_observations': periodic_every, 'periodic_tranches': 4,
            'rebalance_every_observations': rebalance_every, 'weights': values,
            'dividends': 'price/NAV paths only; no distributions or reinvestment assumed',
            'timing': 'first common mark strictly after publication cutoff; no same-day hindsight fill',
            'interpretation': 'frozen initial-allocation scenarios; no proof that a conditional action was executed',
            'advice_target_weights': targets, 'turnover_basis': 'gross buys plus sells including terminal liquidation / initial capital; not annualized',
            'drawdown_basis': 'liquidation-equivalent common-observation wealth including scenario costs'},
        'horizons': {}, 'unavailable_horizons': [], 'gaps': []}
    for horizon in horizons:
        if type(horizon) is not int or horizon < 1:
            raise ValueError('invalid review horizon')
        if len(frame) <= horizon:
            result['unavailable_horizons'].append(horizon)
            continue
        day = frame.index[horizon]
        def drawdown(path):
            peak, worst = 1., 0.
            for wealth in path:
                peak = max(peak, wealth)
                worst = max(worst, 1 - wealth / peak)
            return worst
        rows = {name: {'ending_wealth': path[horizon], 'net_return': path[horizon] - 1, 'as_of': str(day),
            'max_drawdown': drawdown(path[:horizon + 1]), 'turnover': turnover_paths[name][horizon],
            'scenario_cost': fee_paths[name][horizon]} for name, path in paths.items()}
        rows['benchmark'] = None
        if frame.index[0] in bench and day in bench:
            wealth = bench[day] / bench[frame.index[0]] / (1 + costs) * (1 - costs)
            coverage = all(stamp in bench for stamp in frame.index[:horizon + 1])
            if not coverage:
                result['gaps'].append({'scope': 'benchmark_drawdown', 'horizon': horizon, 'reason': 'intermediate common dates are missing; full-window drawdown is unknown'})
            bench_path = [bench[stamp] / bench[frame.index[0]] / (1 + costs) * (1 - costs) for stamp in frame.index[:horizon + 1] if stamp in bench]
            gross = 1 / (1 + costs) + bench[day] / bench[frame.index[0]] / (1 + costs)
            rows['benchmark'] = {'ending_wealth': wealth, 'net_return': wealth - 1, 'as_of': str(day),
                'max_drawdown': drawdown(bench_path) if coverage else None, 'drawdown_coverage_complete': coverage, 'turnover': gross, 'scenario_cost': gross * costs}
        if rows['benchmark'] is None:
            result['gaps'].append({'scope': 'benchmark', 'horizon': horizon, 'reason': 'matching start or end mark unavailable'})
        result['horizons'][str(horizon)] = rows
    return result


def frozen_advice_scenario(report, decision):
    """Declare the comparison policy at publication, never fit it to outcomes."""
    weights = {row['symbol']: row['fund_weight'] for row in report['holdings'] if row.get('symbol') and row.get('fund_weight') is not None}
    base = {'version': 'frozen-allocation-v1', 'target_weights': None,
        'interpretation': 'synthetic initial-capital allocation; no actual user transaction or future conditional-signal execution inferred',
        'reason': 'conditional permission or judgment does not specify an executable allocation'}
    if not weights or len(weights) != len(report['holdings']) or report['portfolio'].get('analysis_scope'):
        return {**base, 'reason': 'full original fund weights are not exact'}
    unresolved_execution = any(row.get('available_quantity') is not None and row.get('quantity') is not None and row['available_quantity'] != row['quantity'] or row.get('gaps') for row in report['holdings'])
    unquantified = any(row['action'] in {'REVIEW_REDUCTION', 'RESTORE_CASH'} for row in decision['cards'])
    if unresolved_execution or unquantified:
        return {**base, 'reason': 'unquantified concurrent risk review or unresolved execution/holding state'}
    if decision['stance'] in {'NO_ADD', 'MAINTAIN_WITH_REVIEW'}:
        return {**base, 'target_weights': weights, 'reason': 'no new subscription; original fund exposure kept in the frozen scenario'}
    cards = [row for row in decision['cards'] if row['action'] in {'REDUCE_CLUSTER', 'REVIEW_REDUCTION', 'RESTORE_CASH'}]
    if decision['stance'] != 'REDUCE_RISK' or len(cards) != 1 or cards[0]['action'] != 'REDUCE_CLUSTER':
        return base
    card = cards[0]
    if card.get('pending_redemption_estimate') or not report['portfolio'].get('fund_value'):
        return {**base, 'reason': 'pending execution or initial fund capital is unresolved'}
    cluster = next((row for row in report['clusters'] if row['label'] == card['scope']), None)
    members = {row['symbol'] for row in report['holdings'] if cluster and row['id'] in cluster['members']}
    total = sum(weights.get(symbol, 0) for symbol in members)
    reduction = card['cash_redemption_estimate'] / report['portfolio']['fund_value']
    if not total or not 0 <= reduction <= total:
        return base
    targets = {symbol: weight * (1 - reduction / total) if symbol in members else weight for symbol, weight in weights.items()}
    return {**base, 'target_weights': targets, 'reason': 'one dated theme-cap trim; proportional member reduction fixed before outcomes; remainder held as zero-yield cash'}
