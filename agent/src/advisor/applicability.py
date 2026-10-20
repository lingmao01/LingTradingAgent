"""Relationship evidence and explicit scope, separate from market truth values."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.advisor.models import ResultMapping, freeze
from src.advisor.signals import RuleSet


@dataclass(frozen=True)
class ApplicabilityEntry(ResultMapping):
    subject_id: str
    holding_ids: tuple[str, ...]
    status: str
    scope: str = 'fund'
    mapping_refs: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    relationship: Mapping[str, Any] | None = None

    def __post_init__(self):
        for key in ('holding_ids', 'mapping_refs', 'reason_codes', 'relationship'):
            object.__setattr__(self, key, freeze(getattr(self, key)))


@dataclass(frozen=True)
class ApplicabilityMap(ResultMapping):
    entries: tuple[ApplicabilityEntry, ...]
    aggregation_status: str = 'AGGREGATION_UNDEFINED'
    scope_policy: Mapping[str, Any] | None = None
    rule_version: str = ''
    schema_version: int = 2

    def __post_init__(self):
        object.__setattr__(self, 'entries', tuple(self.entries))
        object.__setattr__(self, 'scope_policy', freeze(self.scope_policy))

    def for_subject(self, subject_id: str) -> tuple[ApplicabilityEntry, ...]:
        return tuple(row for row in self.entries if row.subject_id == subject_id or subject_id in row.holding_ids)


def _relation(symbol, identities):
    raw = identities.get(symbol) or {}
    # New sourced relationships may coexist with the original declared row.
    candidates = tuple(raw.get('relationships') or ())
    if candidates:
        relevant = [row for row in candidates if row.get('kind', 'tracking_index') == 'tracking_index']
        indices = {row.get('tracking_index') or row.get('target') for row in relevant}
        if len(indices - {None}) > 1:
            return raw.get('tracking_index') or next(iter(sorted(indices - {None})), symbol), 'UNRESOLVED', ('RELATIONSHIP_CONFLICT',), raw
        verified = [row for row in relevant if row.get('verified') is True or row.get('verification_status') == 'VERIFIED']
        if verified:
            raw = {**raw, **verified[0], 'tracking_index': verified[0].get('tracking_index') or verified[0].get('target')}
    index = raw.get('tracking_index')
    if not index:
        return symbol, 'UNRESOLVED', ('MAPPING_MISSING',), raw
    if raw.get('conflicts') or raw.get('conflict') or raw.get('verification_status') == 'CONFLICT':
        return index, 'UNRESOLVED', ('RELATIONSHIP_CONFLICT',), raw
    if not (raw.get('verified') is True or raw.get('verification_status') == 'VERIFIED'):
        return index, 'UNRESOLVED', ('DECLARED_ONLY',), raw
    if not (raw.get('source_ref') or raw.get('source')) or not (raw.get('as_of') or raw.get('effective_from')):
        return index, 'UNRESOLVED', ('RELATIONSHIP_PROVENANCE_MISSING',), raw
    return index, 'RESOLVED', ('VERIFIED_TRACKING_RELATIONSHIP',), raw


def _relation_at(symbol, identities, stamp):
    index, status, reasons, relation = _relation(symbol, identities)
    if stamp and (relation.get('effective_from', '') > stamp or relation.get('effective_until') and relation['effective_until'] < stamp or relation.get('as_of', '') > stamp):
        status, reasons = 'UNRESOLVED', ('RELATIONSHIP_OUTSIDE_EFFECTIVE_DATE',)
    return index, status, reasons, relation


def resolve_applicability(account: Mapping[str, Any], identities: Mapping[str, Any], rules: RuleSet) -> ApplicabilityMap:
    if isinstance(rules, Mapping) and not isinstance(rules, RuleSet):
        rules = RuleSet.from_discipline(rules.get('discipline', rules), account_rules=rules.get('account_rules', ()))
    entries = []
    stamp = account.get('analysis_as_of') or account.get('as_of')
    for position in account.get('positions', ()):
        symbol = position.get('symbol')
        if not symbol:
            continue
        index, status, reasons, relation = _relation_at(symbol, identities, stamp)
        holding = str(position.get('id') or symbol)
        entries.append(ApplicabilityEntry(str(index), (holding,), status, mapping_refs=(str(relation.get('source_ref') or relation.get('source')), ) if relation.get('source_ref') or relation.get('source') else (), reason_codes=reasons, relationship=relation))
    for offset, group in enumerate(account.get('holding_groups', ())):
        group_id = str(group.get('id') or group.get('group_id') or f'group:{offset}')
        candidates = [_relation_at(symbol, identities, stamp) for symbol in group.get('candidate_symbols', ())]
        same = len({row[0] for row in candidates}) == 1 and bool(candidates)
        resolved = same and all(row[1] == 'RESOLVED' for row in candidates)
        for index in sorted({str(row[0]) for row in candidates}):
            refs = tuple(dict.fromkeys(str(row[3]['source_ref']) for row in candidates if row[3].get('source_ref')))
            entries.append(ApplicabilityEntry(index, (group_id,), 'RESOLVED' if resolved else 'UNRESOLVED', mapping_refs=refs, reason_codes=('VERIFIED_GROUP_TRACKING_RELATIONSHIP',) if resolved else ('CANDIDATE_MAPPING_CONFLICT',) if not same else ('CANDIDATE_MAPPING_UNCONFIRMED',), relationship={'candidate_symbols': tuple(group.get('candidate_symbols', ())), 'tracking_indices': tuple(row[0] for row in candidates)}))
    policy = rules.discipline.get('scope_policy') or rules.regime.get('scope_policy') or {}
    aggregation = 'AGGREGATION_UNDEFINED'
    indices = tuple(policy.get('indices') or ()) if isinstance(policy, Mapping) else ()
    if isinstance(policy, Mapping) and policy.get('aggregation') in {'all', 'any', 'specified', 'ALL', 'ANY', 'SPECIFIED'} and indices and policy.get('kind', policy.get('scope')) in {'account', 'portfolio'}:
        aggregation = 'RESOLVED' if account.get('analysis_scope') != 'requested_subportfolio' else 'NOT_APPLICABLE'
        if aggregation == 'RESOLVED':
            entries.extend(ApplicabilityEntry(str(index), ('account',), 'RESOLVED', scope='account', mapping_refs=('discipline:scope_policy',), reason_codes=('EXPLICIT_ACCOUNT_SCOPE_POLICY',)) for index in indices)
    mapped = {row.subject_id for row in entries}
    entries.extend(ApplicabilityEntry(symbol, (), 'UNRESOLVED', scope='market_reference', reason_codes=('AGGREGATION_UNDEFINED',)) for symbol in rules.regime.get('indices', ()) if symbol not in mapped)
    return ApplicabilityMap(tuple(entries), aggregation, policy, rules.version)
