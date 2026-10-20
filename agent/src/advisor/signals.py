"""Versioned deterministic conditions evaluated exclusively from frozen inputs."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from src.advisor.models import AssessmentInputs, Observation, ResultMapping, content_sha256, freeze

METHOD_VERSION = 'signals-v2-macd-12-26-9-adjust-false'
INDEX_NAMES = {'000001.SH': '上证指数', '000300.SH': '沪深300', '399006.SZ': '创业板指', '000688.SH': '科创50'}
EFFECTS = {'NO ADD': 'BLOCK_ADD', 'REDUCE': 'REDUCE_TRIGGER', 'ADD ALLOWED': 'PERMIT_ADD'}


@dataclass(frozen=True)
class RuleSet(ResultMapping):
    discipline: Mapping[str, Any] = field(default_factory=dict)
    account_rules: tuple[Mapping[str, Any], ...] = ()
    version: str = ''
    method_version: str = METHOD_VERSION
    schema_version: int = 2

    def __post_init__(self):
        object.__setattr__(self, 'discipline', freeze(self.discipline))
        object.__setattr__(self, 'account_rules', freeze(self.account_rules))
        if not self.version:
            object.__setattr__(self, 'version', content_sha256({'discipline': self.discipline, 'account_rules': self.account_rules}))

    @classmethod
    def from_discipline(cls, discipline: Any, account_rules=()) -> 'RuleSet':
        if hasattr(discipline, 'to_dict'):
            discipline = discipline.to_dict()
        return cls(discipline=discipline or {}, account_rules=tuple(row.to_dict() if hasattr(row, 'to_dict') else row for row in account_rules))

    @property
    def regime(self):
        return self.discipline.get('regime') or {}

    @property
    def tiers(self):
        return self.discipline.get('tiers') or ()


@dataclass(frozen=True)
class ConditionAssessment(ResultMapping):
    condition_id: str
    operator: str
    state: str
    display: str
    actual: Any = None
    threshold: Any = None
    unit: str | None = None
    reason_codes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    children: tuple['ConditionAssessment', ...] = ()

    def __post_init__(self):
        for name in ('actual', 'threshold', 'reason_codes', 'evidence_refs', 'children'):
            object.__setattr__(self, name, freeze(getattr(self, name)))


@dataclass(frozen=True)
class SignalAssessment(ResultMapping):
    signal_id: str
    rule_id: str
    rule_version: str
    method_version: str
    effect: str
    subject: Mapping[str, Any]
    config_status: str
    state: str
    applicability: str = 'UNRESOLVED'
    conditions: tuple[ConditionAssessment, ...] = ()
    evidence_as_of: str | None = None
    expected_session: str | None = None
    finality: str = 'unknown'
    freshness: str = 'unknown'
    next_check: str = ''
    transition: str = 'NO_BASELINE'
    previous_signal_ref: str | None = None
    evidence_refs: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    scope: str = 'market_reference'
    holding_ids: tuple[str, ...] = ()
    mapping_refs: tuple[str, ...] = ()
    input_sha256: str = ''
    schema_version: int = 2

    def __post_init__(self):
        for name in ('subject', 'conditions', 'evidence_refs', 'reason_codes', 'holding_ids', 'mapping_refs'):
            object.__setattr__(self, name, freeze(getattr(self, name)))


def _truth_and(children):
    if any(row.state == 'FALSE' for row in children):
        return 'FALSE'
    return 'UNKNOWN' if any(row.state == 'UNKNOWN' for row in children) else 'TRUE'


def _closed(observation: Observation | None) -> Observation | None:
    if not observation or observation.finality != 'in_progress':
        return observation
    history = tuple((day, value) for day, value in observation.history if day < observation.as_of)
    if not history:
        return None
    day, value = history[-1]
    quality = 'current' if observation.expected_closed_session == day else 'unknown'
    return replace(observation, value=value, as_of=day, history=history, finality='closed_session', quality=quality)


def _continuity(inputs, observation, count):
    if observation is None:
        return 'OBSERVATION_MISSING'
    if observation.field != 'close':
        return 'INDEX_CLOSE_REQUIRED'
    if observation.quality not in {'current', 'latest_disclosed', 'historical_requested'}:
        return 'OBSERVATION_STALE_OR_UNKNOWN'
    if observation.finality not in {'closed_session', 'historical_closed', 'closed'}:
        return 'CLOSE_FINALITY_UNCONFIRMED'
    if len(observation.history) < count:
        return 'HISTORY_INSUFFICIENT'
    if not inputs.trading_sessions:
        return 'CALENDAR_UNAVAILABLE'
    expected = tuple(day for day in inputs.trading_sessions if day <= observation.as_of)[-count:]
    if len(expected) < count:
        return 'CALENDAR_COVERAGE_INSUFFICIENT'
    actual = tuple(day for day, _ in observation.history[-count:])
    if actual != expected:
        return 'TRADING_SESSION_GAP'
    expected_latest = observation.expected_closed_session or max((day for day in inputs.trading_sessions if day <= inputs.as_of), default=None)
    if expected_latest and observation.as_of < expected_latest:
        return 'CLOSED_SESSION_STALE'
    return None


def _condition(text, cid, observation, inputs, regime):
    text = text.replace('{short}', str(regime.get('sma_short_days'))).replace('{long}', str(regime.get('sma_long_days'))).replace('{confirm}', str(regime.get('macd_confirm_days')))
    parts = re.split(r'\s*(?:且|\band\b|&&)\s*', text, flags=re.I)
    if len(parts) > 1:
        children = tuple(_condition(part, f'{cid}:{index}', observation, inputs, regime) for index, part in enumerate(parts))
        return ConditionAssessment(cid, 'AND', _truth_and(children), text, children=children, reason_codes=tuple(dict.fromkeys(code for row in children for code in row.reason_codes)))
    compact = re.sub(r'\s+', '', text)
    if '动能' in compact or 'momentum' in compact.casefold():
        return ConditionAssessment(cid, 'UNDEFINED', 'UNKNOWN', text, reason_codes=('MOMENTUM_DEFINITION_MISSING',))
    match = re.fullmatch(r'(?:收盘价?|close)(<=|>=|<|>|≤|≥)(短均线(?:[（(]SMA\d+[）)])?|长均线(?:[（(]SMA\d+[）)])?|SMA\d+|MA\d+)', compact, re.I)
    refs = (f'observation:{observation.symbol}:{observation.as_of}',) if observation else ()
    if match:
        operator, average = match.groups()
        window = regime.get('sma_short_days') if average.startswith('短') else regime.get('sma_long_days') if average.startswith('长') else int(re.search(r'\d+', average).group())
        if not isinstance(window, int) or window <= 0:
            return ConditionAssessment(cid, operator, 'UNKNOWN', text, reason_codes=('AVERAGE_WINDOW_UNDEFINED',))
        gap = _continuity(inputs, observation, window)
        threshold = float(observation.series().tail(window).mean()) if observation and len(observation.history) >= window else None
        actual = observation.value if observation else None
        state = 'UNKNOWN'
        if not gap:
            result = {'<': actual < threshold, '>': actual > threshold, '<=': actual <= threshold, '≤': actual <= threshold, '>=': actual >= threshold, '≥': actual >= threshold}[operator]
            state = 'TRUE' if result else 'FALSE'
        return ConditionAssessment(cid, operator, state, text, actual, threshold, 'points', (gap,) if gap else ('RECORDED_SMA_COMPARISON',), refs)
    macd = re.fullmatch(r'MACD柱连续(\d+)日转正', compact, re.I)
    if macd:
        count = int(macd.group(1))
        gap = _continuity(inputs, observation, 34 + count)
        actual = None
        if observation and len(observation.history) >= 34 + count:
            series = observation.series()
            line = series.ewm(span=12, adjust=False).mean() - series.ewm(span=26, adjust=False).mean()
            histogram = line - line.ewm(span=9, adjust=False).mean()
            actual = tuple(float(value) for value in histogram.tail(count))
        state = 'UNKNOWN' if gap else 'TRUE' if all(value > 0 for value in actual) else 'FALSE'
        return ConditionAssessment(cid, 'RECENT_POSITIVE', state, f'MACD柱最近{count}个已收盘交易日均大于零', actual, 0, 'points', (gap,) if gap else ('RECENT_POSITIVE_NOT_NEW_CROSS',), refs)
    return ConditionAssessment(cid, 'UNDEFINED', 'UNKNOWN', text, reason_codes=('RULE_DEFINITION_UNRESOLVED',))


def _make(inputs, rules, rule_id, effect, subject, conditions, *, config='CONFIGURED', observation=None, scope='market_reference'):
    state = _truth_and(conditions) if conditions else 'UNKNOWN'
    codes = tuple(dict.fromkeys(code for row in conditions for code in row.reason_codes))
    if any('DEFINITION' in code or code == 'AVERAGE_WINDOW_UNDEFINED' for code in codes):
        config = 'DEFINITION_INCOMPLETE'
    stable = content_sha256({'rule_id': rule_id, 'version': rules.version, 'subject': subject, 'scope': scope, 'method': rules.method_version})[:24]
    return SignalAssessment(signal_id=f'signal:{stable}', rule_id=rule_id, rule_version=rules.version, method_version=rules.method_version, effect=effect, subject=subject, config_status=config, state=state, applicability='RESOLVED' if scope in {'account', 'fund', 'cluster'} else 'UNRESOLVED', conditions=tuple(conditions), evidence_as_of=observation.as_of if observation else inputs.as_of if scope != 'market_reference' else None, expected_session=observation.expected_closed_session if observation else None, finality=observation.finality if observation else 'unknown', freshness=observation.quality if observation else 'unknown', next_check='补充条件定义' if config == 'DEFINITION_INCOMPLETE' else '等待下一完整收盘并重算记录窗口' if subject['kind'] == 'index' else '核对该项账户依赖', evidence_refs=tuple(dict.fromkeys(ref for row in conditions for ref in row.evidence_refs)), reason_codes=codes, scope=scope, input_sha256=inputs.input_sha256)


def assess_signals(inputs: AssessmentInputs, *, rules: RuleSet) -> tuple[SignalAssessment, ...]:
    """No network, calendar lookup, ledger read, model call or mutable source."""
    if isinstance(rules, Mapping) and not isinstance(rules, RuleSet):
        rules = RuleSet.from_discipline(rules.get('discipline', rules), account_rules=rules.get('account_rules', ()))
    result = []
    for symbol in rules.regime.get('indices', ()):
        observation = _closed(inputs.observations.get(symbol))
        subject = {'id': symbol, 'kind': 'index', 'symbol': symbol, 'name': INDEX_NAMES.get(symbol, symbol)}
        for index, tier in enumerate(rules.tiers):
            effect = EFFECTS.get(tier.get('action'), tier.get('effect', 'RISK_REVIEW'))
            rule_id = str(tier.get('id') or f'regime:{index}:{effect}')
            condition = _condition(str(tier.get('when') or ''), f'condition:{symbol}:{rule_id}', observation, inputs, rules.regime)
            result.append(_make(inputs, rules, rule_id, effect, subject, (condition,), observation=observation))
    positions = {row['symbol']: row for row in inputs.account.get('positions', ()) if row.get('symbol')}
    configured = {row.get('symbol') for row in rules.account_rules}
    for symbol in sorted(set(positions) | configured):
        subject = {'id': symbol, 'kind': 'fund', 'symbol': symbol, 'name': positions.get(symbol, {}).get('name') or inputs.identities.get(symbol, {}).get('name') or symbol}
        items = [row for row in rules.account_rules if row.get('symbol') == symbol]
        if not items:
            result.append(_make(inputs, rules, f'fund:{symbol}:unconfigured', 'RISK_REVIEW', subject, (), config='NOT_CONFIGURED', scope='fund'))
        for row in items:
            observation = inputs.observations.get(symbol)
            for key, operator in (('stop_loss', '<='), ('take_profit', '>='), ('max_quantity', '>')):
                threshold = row.get(key)
                if threshold is None:
                    continue
                actual = positions.get(symbol, {}).get('quantity') if key == 'max_quantity' else observation.value if observation and observation.quality in {'current', 'latest_disclosed', 'historical_requested'} else None
                state = 'UNKNOWN' if actual is None else 'TRUE' if (abs(actual) > threshold if key == 'max_quantity' else actual <= threshold if operator == '<=' else actual >= threshold) else 'FALSE'
                cid = f'condition:{symbol}:{key}'
                condition = ConditionAssessment(cid, operator, state, key, actual, threshold, 'shares' if key == 'max_quantity' else 'unit_nav' if symbol.endswith('.OF') else 'price', ('INPUT_MISSING',) if actual is None else ('RECORDED_LIMIT_COMPARISON',), (f'account:{symbol}',) if key == 'max_quantity' else (f'observation:{symbol}:{observation.as_of}',) if observation else ())
                result.append(_make(inputs, rules, f'fund:{symbol}:{key}', 'HARD_RISK' if key == 'max_quantity' else 'REDUCE_TRIGGER', subject, (condition,), observation=observation, scope='fund'))
    maximum = rules.discipline.get('max_drawdown_percent')
    if maximum is not None:
        performance = inputs.account.get('drawdown_evidence') or {}
        actual = performance.get('drawdown_percent')
        valid = performance.get('cashflows_reconciled') is True and performance.get('peak_basis') and performance.get('comparable_history') and performance.get('as_of') == inputs.as_of and actual is not None
        condition = ConditionAssessment('condition:account:max_drawdown', '>=', ('TRUE' if actual >= maximum else 'FALSE') if valid else 'UNKNOWN', '现金流核对后的账户峰值回撤', actual if valid else None, maximum, 'percent', ('RECONCILED_ACCOUNT_DRAWDOWN',) if valid else ('CASHFLOW_PEAK_HISTORY_REQUIRED',), ('account:drawdown_evidence',))
        result.append(_make(inputs, rules, 'account:max_drawdown', 'HARD_RISK', {'id': 'account', 'kind': 'account', 'name': '账户'}, (condition,), scope='account'))
    minimum = inputs.account.get('minimum_cash')
    if minimum is not None:
        cash = inputs.account.get('cash') or {}
        amount = cash.get('amount') if cash.get('as_of') == inputs.as_of and cash.get('currency') == 'CNY' else None
        pending = sum(order.get('amount') or 0 for order in inputs.account.get('pending_orders', ()) if order.get('state') == 'pending' and order.get('action') == 'subscribe')
        unreconciled = bool(inputs.account.get('errors')) or any(row.get('symbol') == '__CASH__' for row in inputs.account.get('unreconciled_events', ()))
        actual = amount - pending if amount is not None and not unreconciled else None
        condition = ConditionAssessment('condition:account:minimum_cash', '<', 'UNKNOWN' if actual is None else 'TRUE' if actual < minimum else 'FALSE', '可用现金低于已记录现金底线', actual, minimum, 'CNY', ('CASH_STALE_OR_UNKNOWN',) if actual is None else ('RECORDED_CASH_FLOOR',))
        result.append(_make(inputs, rules, 'account:minimum_cash', 'HARD_RISK', {'id': 'account', 'kind': 'account', 'name': '账户现金'}, (condition,), scope='account'))
    for cluster in rules.discipline.get('cluster_caps', ()):
        label = str(cluster.get('label') or 'cluster')
        members = set(cluster.get('members', ()))
        values, dates, complete = {}, set(), not bool(inputs.account.get('holding_groups'))
        for symbol, position in positions.items():
            observation = inputs.observations.get(symbol)
            quantity = position.get('quantity')
            if quantity is None or quantity < 0 or not observation or observation.currency != 'CNY' or observation.quality not in {'current','latest_disclosed','historical_requested'}:
                complete = False; continue
            values[symbol] = quantity * observation.value
            dates.add(observation.as_of)
            if any(word.casefold() in str(position.get('name','')).casefold() for word in cluster.get('keywords',())): members.add(symbol)
        exact = complete and len(dates) == 1 and sum(values.values()) > 0
        weight = 100 * sum(v for s,v in values.items() if s in members) / sum(values.values()) if exact else None
        maximum = cluster.get('max_percent')
        state = 'UNKNOWN' if weight is None or maximum is None else 'TRUE' if weight > maximum else 'FALSE'
        condition = ConditionAssessment(f'condition:cluster:{label}', '>', state, f'{label}持仓占比上限', actual=weight, threshold=maximum, unit='percent', reason_codes=('EXACT_CLUSTER_WEIGHT',) if exact else ('EXACT_CLUSTER_VALUATION_REQUIRED',))
        result.append(_make(inputs, rules, f'cluster:{label}:max_percent', 'HARD_RISK', {'id': label, 'kind': 'cluster', 'name': label, 'members': tuple(cluster.get('members', ())), 'keywords': tuple(cluster.get('keywords', ()))}, (condition,), scope='cluster'))
    loss_limit = inputs.account.get('max_loss_amount')
    if loss_limit is not None:
        condition = ConditionAssessment('condition:account:max_loss_amount','>=','UNKNOWN','现金流核对后的实际账户损失',threshold=loss_limit,unit='CNY',reason_codes=('RECONCILED_LOSS_BASIS_REQUIRED',))
        result.append(_make(inputs,rules,'account:max_loss_amount','HARD_RISK',{'id':'account','kind':'account','name':'账户'},(condition,),scope='account'))
    note = str(rules.discipline.get('note') or '')
    if '70' in note and '90' in note:
        for cluster in rules.discipline.get('cluster_caps',()):
            label = str(cluster.get('label') or 'cluster')
            condition = ConditionAssessment(f'condition:cluster:{label}:policy','UNDEFINED','UNKNOWN','常规线与上浮确认条件尚未结构化；沿用已记录硬上限',reason_codes=('RULE_DEFINITION_UNRESOLVED',))
            result.append(_make(inputs,rules,f'cluster:{label}:policy_definition','RISK_REVIEW',{'id':label,'kind':'cluster','name':label},(condition,),config='DEFINITION_INCOMPLETE',scope='cluster'))
    return tuple(result)
