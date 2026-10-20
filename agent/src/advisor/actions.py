"""Object-level action qualification, with separate execution and sizing checks."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from src.advisor.applicability import ApplicabilityMap
from src.advisor.models import AssessmentInputs, ResearchSubject, ResultMapping, content_sha256, freeze
from src.advisor.signals import SignalAssessment


@dataclass(frozen=True)
class ActionProposal(ResultMapping):
    id: str
    action: str
    subject_ids: tuple[str, ...]
    origin: str = 'RESEARCH_JUDGMENT'
    evidence_refs: tuple[str, ...] = ()
    condition_refs: tuple[str, ...] = ()
    comparison_id: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 2

    def __post_init__(self):
        for key in ('subject_ids', 'evidence_refs', 'condition_refs', 'parameters'):
            object.__setattr__(self, key, freeze(getattr(self, key)))


@dataclass(frozen=True)
class RotationAssessment(ResultMapping):
    source_subject_id: str
    target_subject_id: str
    status: str
    comparison_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    fees: Mapping[str, Any] = field(default_factory=dict)
    funding_timing: Mapping[str, Any] = field(default_factory=dict)
    alternatives: tuple[Any, ...] = ()
    exposure_change: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 2

    def __post_init__(self):
        for key in ('evidence_refs', 'reason_codes', 'fees', 'funding_timing', 'alternatives', 'exposure_change'):
            object.__setattr__(self, key, freeze(getattr(self, key)))


@dataclass(frozen=True)
class ActionAssessment(ResultMapping):
    action_id: str
    action: str
    subject_ids: tuple[str, ...]
    origin: str
    eligibility: str
    signal_refs: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    sizing_status: str = 'INSUFFICIENT_DATA'
    execution_checks: tuple[Mapping[str, Any], ...] = ()
    proposal_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    condition_refs: tuple[str, ...] = ()
    parameters: Mapping[str, Any] = field(default_factory=dict)
    rotation: RotationAssessment | None = None
    input_sha256: str = ''
    rule_version: str = ''
    method_version: str = 'actions-v2'
    scope: str = 'fund'
    reason_codes: tuple[str, ...] = ()
    schema_version: int = 2

    def __post_init__(self):
        for key in ('subject_ids', 'signal_refs', 'blocked_by', 'execution_checks', 'evidence_refs', 'condition_refs', 'parameters', 'reason_codes'):
            object.__setattr__(self, key, freeze(getattr(self, key)))


def _get(row, key, default=None):
    return row.get(key, default) if isinstance(row, Mapping) else getattr(row, key, default)


def _subject(signal):
    subject = _get(signal, 'subject', {})
    return subject.get('id') or subject.get('symbol')


def _check(name, state, *reasons, **details):
    return {'check_id': name, 'state': state, 'reason_codes': tuple(reasons), **details}


def _rule_targets(signals, applicability, aliases):
    targets = {}
    for signal in signals:
        sid = _subject(signal)
        kind = _get(signal, 'subject', {}).get('kind')
        if kind == 'fund':
            targets.setdefault(aliases.get(sid, sid), []).append(signal)
        elif kind == 'index':
            entries = applicability.for_subject(sid)
            relevant = [entry for entry in entries if entry.status == 'RESOLVED' and entry.holding_ids]
            if relevant:
                for entry in relevant:
                    for target in entry.holding_ids:
                        targets.setdefault(aliases.get(target, target), []).append(signal)
            else:
                targets.setdefault(sid, []).append(signal)
    return targets


def _cluster_applies(cluster, subject_id, inputs, report):
    position = next((row for row in inputs.account.get('positions', ()) if subject_id in {row.get('symbol'), row.get('id')}), {})
    holding = next((row for row in report.get('holdings', ()) if subject_id in {row.get('symbol'), row.get('id')}), {})
    members = cluster.get('members') or ()
    aliases = {subject_id, position.get('id'), position.get('symbol'), holding.get('id'), holding.get('symbol')} - {None}
    return bool(aliases.intersection(members)) or any(str(word).casefold() in str(position.get('name') or '').casefold() for word in cluster.get('keywords', ()))


def _hard_checks(subject_id, inputs, report, signals):
    blocked, unknown = [], []
    position = next((row for row in inputs.account.get('positions', ()) if subject_id in {row.get('id'), row.get('symbol')}), {})
    aliases = {subject_id, position.get('id'), position.get('symbol')} - {None}
    for signal in signals:
        if _get(signal, 'effect') != 'HARD_RISK' or _get(signal, 'config_status') == 'NOT_CONFIGURED':
            continue
        subject = _get(signal, 'subject', {})
        kind = subject.get('kind')
        applies = kind == 'account' or _subject(signal) in aliases or kind == 'cluster' and _cluster_applies(subject, subject_id, inputs, report)
        if not applies:
            continue
        # Report's exact cluster result is handled below; an unknown frozen
        # cluster condition never becomes a made-up point estimate.
        if kind == 'cluster' and any(row.get('label') == subject.get('id') for row in report.get('clusters', ())):
            continue
        if _get(signal, 'state') == 'TRUE':
            blocked.append('HARD_RISK:' + _get(signal, 'rule_id'))
        elif _get(signal, 'state') == 'UNKNOWN':
            unknown.append('HARD_RISK_UNRESOLVED:' + _get(signal, 'rule_id'))
    for cluster in report.get('clusters', ()):
        if not _cluster_applies(cluster, subject_id, inputs, report):
            continue
        state = cluster.get('state')
        if state in {'OVER CAP', 'AT CAP', 'BREACHED', 'OVER_CAP'}:
            blocked.append('CLUSTER_CAP_REACHED')
        elif state in {'UNKNOWN', None}:
            unknown.append('CLUSTER_VALUATION_UNCERTAIN')
    if position.get('subscription_allowed') is False or position.get('tradable') is False:
        blocked.append('SUBSCRIPTION_NOT_ALLOWED')
    return blocked, unknown


def _execution(subject_id, action, inputs, report, parameters):
    checks = []
    position = next((row for row in inputs.account.get('positions', ()) if subject_id in {row.get('symbol'), row.get('id')}), {})
    holding = next((row for row in report.get('holdings', ()) if subject_id in {row.get('symbol'), row.get('id')}), {})
    pending = [row for row in inputs.account.get('pending_orders', ()) if row.get('state') == 'pending']
    if action in {'REDUCE', 'ROTATE'}:
        quantity = position.get('quantity')
        reserved = sum(row.get('quantity') or 0 for row in pending if row.get('symbol') == position.get('symbol', subject_id) and row.get('action') == 'redeem')
        available = max(0, quantity - reserved) if quantity is not None else None
        checks.append(_check('quantity', 'UNKNOWN' if quantity is None else 'PASS', *('QUANTITY_UNKNOWN',) if quantity is None else (), available_quantity=available, reserved_quantity=reserved))
        if reserved:
            checks.append(_check('pending_redemptions', 'BLOCKED' if available == 0 else 'REQUIRES_CHECK', 'PENDING_REDEMPTION_RESERVED', available_quantity=available))
        requested = parameters.get('quantity')
        if requested is not None and available is not None:
            checks.append(_check('requested_quantity', 'PASS' if 0 < requested <= available else 'BLOCKED', *('REQUESTED_QUANTITY_EXCEEDS_AVAILABLE',) if requested > available or requested <= 0 else ()))
        fees = holding.get('redemption_fee') or {}
        known_fee = fees.get('fee_percent') is not None or fees.get('fee_amount') is not None or fees.get('state') in {'KNOWN', 'EXACT'}
        checks.append(_check('redemption_fees', 'PASS' if known_fee else 'UNKNOWN', *('REDEMPTION_FEES_UNKNOWN',) if not known_fee else ()))
        checks.append(_check('lots', 'PASS' if position.get('lots') else 'UNKNOWN', *('LOT_TIMING_UNKNOWN',) if not position.get('lots') else ()))
    if action in {'ADD', 'ROTATE'}:
        cash = inputs.account.get('cash') or {}
        current = cash.get('as_of') == inputs.as_of and cash.get('currency') == 'CNY' and cash.get('amount') is not None
        reserved = sum(row.get('amount') or 0 for row in pending if row.get('action') == 'subscribe')
        available = cash.get('amount', 0) - reserved if current else None
        unresolved = any(row.get('symbol') == '__CASH__' for row in inputs.account.get('unreconciled_events', ())) or bool(inputs.account.get('errors'))
        if unresolved:
            current, available = False, None
        checks.append(_check('available_cash', 'PASS' if current and available >= 0 else 'UNKNOWN' if available is None else 'BLOCKED', *('CASH_STALE',) if not current else ('NEGATIVE_AVAILABLE_CASH',) if available < 0 else (), available_cash=available, reserved_cash=reserved))
        amount = parameters.get('amount')
        if amount is not None and available is not None:
            checks.append(_check('requested_amount', 'PASS' if 0 < amount <= available else 'BLOCKED', *('INSUFFICIENT_AVAILABLE_CASH',) if amount > available or amount <= 0 else ()))
            minimum = inputs.account.get('minimum_cash')
            if minimum is not None:
                remaining = available - amount
                checks.append(_check('post_trade_cash_floor', 'PASS' if remaining >= minimum else 'BLOCKED', *('POST_TRADE_CASH_BELOW_MINIMUM',) if remaining < minimum else (), remaining_cash=remaining, minimum_cash=minimum))
        identity = inputs.identities.get(position.get('symbol') or subject_id) or {}
        checks.append(_check('subscription_terms', 'PASS' if identity.get('terms', {}).get('subscription_verified') is True else 'UNKNOWN', 'SUBSCRIPTION_TERMS_REQUIRE_CHECK'))
    return tuple(checks)


def _rotation(proposal, subjects, inputs):
    ids, params = proposal.subject_ids, proposal.parameters
    reasons, unsupported = [], []
    outbound, inbound = (ids + ('', ''))[:2]
    if len(ids) != 2 or outbound == inbound:
        unsupported.append('ROTATION_REQUIRES_DISTINCT_SIDES')
    if not proposal.comparison_id:
        reasons.append('COMPARISON_MISSING')
    if not params.get('outbound_evidence_refs'):
        reasons.append('OUTBOUND_EVIDENCE_MISSING')
    if not params.get('inbound_evidence_refs'):
        reasons.append('INBOUND_EVIDENCE_MISSING')
    for subject in ids:
        if subject not in subjects or _get(subjects[subject], 'availability') not in {'AVAILABLE', 'available'}:
            reasons.append('ROTATION_SUBJECT_UNAVAILABLE:' + subject)
    alternatives = tuple(params.get('alternatives') or ())
    if not alternatives or not params.get('relative_advantage'):
        reasons.append('ALTERNATIVES_COMPARISON_INCOMPLETE')
    fees = params.get('fees') or {}
    if fees.get('outbound') is None or fees.get('inbound') is None:
        reasons.append('BOTH_SIDES_FEES_UNKNOWN')
    timing = params.get('funding_timing') or {}
    if not timing.get('redemption_confirmed_at') or not timing.get('funds_available_at') or not timing.get('subscription_at'):
        reasons.append('FUNDING_TIMING_UNKNOWN')
    elif timing['funds_available_at'] > timing['subscription_at']:
        unsupported.append('FUNDING_TIMING_MISMATCH')
    exposure = params.get('exposure_change') or {}
    if not exposure:
        reasons.append('EXPOSURE_CHANGE_UNKNOWN')
    elif exposure.get('duplicate_exposure') is True and not exposure.get('justification'):
        unsupported.append('DUPLICATE_EXPOSURE_WITHOUT_ADVANTAGE')
    if params.get('transaction_feasible') is not True:
        reasons.append('TRANSACTION_FEASIBILITY_UNVERIFIED')
    return RotationAssessment(outbound, inbound, 'NOT_SUPPORTED' if unsupported else 'INSUFFICIENT_EVIDENCE' if reasons else 'SUPPORTED', proposal.comparison_id, proposal.evidence_refs, tuple(unsupported + reasons), fees, timing, alternatives, exposure)


def assess_actions(inputs: AssessmentInputs, report: Mapping[str, Any], signals: Sequence[SignalAssessment], applicability: ApplicabilityMap, *, proposals: Sequence[ActionProposal] = (), subjects: Mapping[str, ResearchSubject] | None = None) -> tuple[ActionAssessment, ...]:
    """Evaluate accepted proposals again on the same snapshot; never invent trades."""
    subjects = subjects or {}
    aliases = {}
    position_ids = set()
    for row in inputs.account.get('positions', ()):
        target = str(row.get('id') or row.get('symbol'))
        position_ids.add(target)
        aliases.update((str(alias), target) for alias in (row.get('id'), row.get('symbol')) if alias)
    position_ids.update(str(row.get('id') or row.get('group_id') or f'group:{index}') for index, row in enumerate(inputs.account.get('holding_groups', ())))
    positions = set(aliases) | position_ids
    targets = _rule_targets(signals, applicability, aliases)
    for target in position_ids:
        targets.setdefault(target, [])
    output = []

    def evaluate(target, action, rows, proposal=None):
        origin = proposal.origin if proposal else 'RECORDED_RULE'
        reasons, blocked = [], []
        is_research = proposal is not None
        market_only = target not in positions and target != 'account' and not is_research
        def effective_state(effect):
            selected = [row for row in rows if _get(row, 'effect') == effect and _get(row, 'config_status') != 'NOT_CONFIGURED']
            states = [_get(row, 'state') for row in selected]
            policy = applicability.scope_policy or {}
            if target == 'account' and applicability.aggregation_status == 'RESOLVED':
                represented = {_subject(row) for row in selected}
                states.extend('UNKNOWN' for index in policy.get('indices', ()) if index not in represented)
                if str(policy.get('aggregation')).lower() in {'all', 'specified'}:
                    return 'FALSE' if 'FALSE' in states else 'UNKNOWN' if not states or 'UNKNOWN' in states else 'TRUE'
            return 'TRUE' if 'TRUE' in states else 'UNKNOWN' if not states or 'UNKNOWN' in states else 'FALSE'
        effective = {effect: effective_state(effect) for effect in ('BLOCK_ADD', 'PERMIT_ADD', 'REDUCE_TRIGGER')}
        true = {effect: effective[effect] == 'TRUE' for effect in effective}
        unknown = {effect: effective[effect] == 'UNKNOWN' for effect in effective}
        relevant = [row for row in rows if _get(row, 'effect') in ({'BLOCK_ADD', 'PERMIT_ADD'} if action == 'ADD' else {'REDUCE_TRIGGER'} if action == 'REDUCE' else set())]
        eligibility = 'UNDETERMINED'
        if action == 'ADD':
            if true['BLOCK_ADD']:
                blocked.append('BLOCK_ADD_SIGNAL')
                eligibility = 'BLOCKED'
            elif true['PERMIT_ADD'] or is_research:
                eligibility = 'ELIGIBLE'
            elif effective['PERMIT_ADD'] == 'FALSE':
                eligibility = 'BLOCKED'
                blocked.append('ADD_PERMISSION_NOT_SATISFIED')
            else:
                reasons.append('ADD_BASIS_UNDETERMINED')
            hard, hard_unknown = _hard_checks(target, inputs, report, signals)
            blocked.extend(hard)
            reasons.extend(hard_unknown)
            # A research opinion cannot bypass a recorded right-side gate.
            if is_research and any(_get(row, 'effect') == 'PERMIT_ADD' for row in rows) and not true['PERMIT_ADD']:
                reasons.append('RECORDED_ADD_GATE_UNSATISFIED')
                eligibility = 'BLOCKED' if not unknown['PERMIT_ADD'] else 'UNDETERMINED'
            if hard:
                eligibility = 'BLOCKED'
            elif hard_unknown and eligibility == 'ELIGIBLE':
                eligibility = 'UNDETERMINED'
            if unknown['BLOCK_ADD'] and any(_get(row, 'effect') == 'BLOCK_ADD' and _get(row, 'config_status') != 'NOT_CONFIGURED' for row in rows):
                reasons.append('BLOCK_ADD_SIGNAL_UNRESOLVED')
                if eligibility != 'BLOCKED':
                    eligibility = 'UNDETERMINED'
        elif action == 'REDUCE':
            if true['REDUCE_TRIGGER'] or is_research and proposal.evidence_refs:
                eligibility = 'ELIGIBLE'
            elif effective['REDUCE_TRIGGER'] == 'FALSE':
                eligibility = 'NOT_APPLICABLE'
                reasons.append('REDUCE_RULE_NOT_TRIGGERED')
            else:
                reasons.append('REDUCE_BASIS_UNDETERMINED')
        elif action == 'HOLD_REVIEW':
            reasons.append('HOLD_OPTIMALITY_NOT_ESTABLISHED')
        if true['PERMIT_ADD'] and true['REDUCE_TRIGGER'] and not true['BLOCK_ADD'] and action in {'ADD', 'REDUCE'}:
            reasons.append('RULE_DIRECTION_CONFLICT')
            if eligibility != 'BLOCKED':
                eligibility = 'UNDETERMINED'
        if market_only:
            eligibility = 'UNDETERMINED'
            reasons.append('APPLICABILITY_UNRESOLVED')
        if is_research:
            known = target in positions or target in subjects and _get(subjects[target], 'availability') in {'AVAILABLE', 'available'}
            if not known or not proposal.evidence_refs:
                eligibility = 'UNDETERMINED'
                reasons.append('RESEARCH_SUBJECT_OR_EVIDENCE_UNAVAILABLE')
            if action == 'REDUCE' and target not in positions:
                eligibility = 'NOT_APPLICABLE'
                reasons.append('NO_CONFIRMED_HOLDING_TO_REDUCE')
            if action == 'ADD' and target in positions and not any(_get(row, 'effect') in {'BLOCK_ADD', 'PERMIT_ADD'} for row in rows) and any(_get(row, 'effect') == 'PERMIT_ADD' for row in signals):
                eligibility = 'UNDETERMINED'
                reasons.append('TECHNICAL_APPLICABILITY_UNRESOLVED')
            known_conditions = {child.condition_id for signal in rows for condition in _get(signal, 'conditions', ()) for child in _walk(condition)}
            if any(ref not in known_conditions for ref in proposal.condition_refs):
                eligibility = 'BLOCKED'
                blocked.append('CONDITION_REFERENCE_OUTSIDE_SUBJECT')
        params = proposal.parameters if proposal else {}
        execution = _execution(target, action, inputs, report, params)
        execution_codes = [code for row in execution if row['state'] != 'PASS' for code in row['reason_codes']]
        if any(row['state'] == 'BLOCKED' for row in execution):
            sizing = 'BLOCKED'
        elif execution and all(row['state'] == 'PASS' for row in execution):
            sizing = 'ASSESSABLE'
        else:
            sizing = 'INSUFFICIENT_DATA'
        if 'RULE_DIRECTION_CONFLICT' in reasons or market_only:
            sizing = 'BLOCKED'
        refs = tuple(dict.fromkeys(_get(row, 'signal_id') for row in relevant))
        version = applicability.rule_version
        identity = {'action': action, 'subject': target, 'origin': origin, 'proposal': proposal.id if proposal else None, 'rules': version, 'inputs': inputs.input_sha256}
        return ActionAssessment('action:' + content_sha256(identity)[:24], action, (target,), origin, eligibility, refs, tuple(dict.fromkeys(blocked + execution_codes)), sizing, execution, proposal.id if proposal else None, proposal.evidence_refs if proposal else (), proposal.condition_refs if proposal else (), params, input_sha256=inputs.input_sha256, rule_version=version, scope='market_reference' if market_only else 'account' if target == 'account' else 'fund', reason_codes=tuple(dict.fromkeys(reasons)))

    for target, rows in sorted(targets.items()):
        output.extend(evaluate(target, action, rows) for action in ('ADD', 'REDUCE'))
    for raw in proposals:
        proposal = raw if isinstance(raw, ActionProposal) else ActionProposal(**{key: value for key, value in raw.items() if key in ActionProposal.__dataclass_fields__})
        if proposal.action == 'ROTATE':
            rotation = _rotation(proposal, subjects, inputs)
            outgoing = evaluate(rotation.source_subject_id, 'REDUCE', targets.get(aliases.get(rotation.source_subject_id, rotation.source_subject_id), ()), proposal)
            incoming = evaluate(rotation.target_subject_id, 'ADD', targets.get(aliases.get(rotation.target_subject_id, rotation.target_subject_id), ()), proposal)
            eligibility = 'BLOCKED' if 'BLOCKED' in {outgoing.eligibility, incoming.eligibility} or rotation.status == 'NOT_SUPPORTED' else 'ELIGIBLE' if outgoing.eligibility == incoming.eligibility == 'ELIGIBLE' and rotation.status == 'SUPPORTED' else 'UNDETERMINED'
            reasons = tuple(dict.fromkeys((*outgoing.blocked_by, *incoming.blocked_by, *rotation.reason_codes)))
            output.append(ActionAssessment('action:' + content_sha256({'proposal': proposal.id, 'inputs': inputs.input_sha256})[:24], 'ROTATE', proposal.subject_ids, proposal.origin, eligibility, (*outgoing.signal_refs, *incoming.signal_refs), reasons, 'ASSESSABLE' if outgoing.sizing_status == incoming.sizing_status == 'ASSESSABLE' and rotation.status == 'SUPPORTED' else 'INSUFFICIENT_DATA', (*outgoing.execution_checks, *incoming.execution_checks), proposal.id, proposal.evidence_refs, proposal.condition_refs, proposal.parameters, rotation, inputs.input_sha256, applicability.rule_version, reason_codes=rotation.reason_codes))
        else:
            for target in proposal.subject_ids:
                output.append(evaluate(target, proposal.action, targets.get(aliases.get(target, target), ()), proposal))
    return tuple(output)


def _walk(condition):
    if isinstance(condition, Mapping) and not hasattr(condition, 'condition_id'):
        yield type('ConditionRef', (), {'condition_id': condition.get('condition_id')})()
    else:
        yield condition
    for child in _get(condition, 'children', ()):
        yield from _walk(child)
