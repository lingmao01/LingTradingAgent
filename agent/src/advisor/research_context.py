"""Minimal personal context with explicit unknowns and observed tool health."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any, Mapping, Sequence
from src.advisor.models import freeze, thaw

CONTEXT_FIELDS = ('goals', 'investment_horizon', 'liquidity_need', 'market_access', 'risk_constraints', 'total_wealth')
STRENGTHS = {'USER_HARD', 'USER_GUIDANCE', 'ANALYTIC_REFERENCE'}


@dataclass(frozen=True)
class PersonalContext:
    account_version: str
    holdings: tuple[Mapping[str, Any], ...] = ()
    known: Mapping[str, Any] = field(default_factory=dict)
    unknown_fields: tuple[str, ...] = ()
    constraints: tuple[Mapping[str, Any], ...] = ()
    preferences: tuple[Mapping[str, Any], ...] = ()
    evidence_dates: Mapping[str, Any] = field(default_factory=dict)
    minimum_questions: tuple[Mapping[str, Any], ...] = ()
    market_access: tuple[str, ...] = ()
    cash: Mapping[str, Any] | None = None
    pending_orders: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self):
        for name in ('holdings', 'known', 'constraints', 'preferences', 'evidence_dates', 'minimum_questions', 'cash', 'pending_orders'):
            object.__setattr__(self, name, freeze(getattr(self, name)))

    def to_dict(self): return thaw(self)


def build_personal_context(account: Mapping, discipline: Mapping, user_statements: Sequence[Mapping]) -> PersonalContext:
    account, discipline, user_statements = thaw(account), thaw(discipline), thaw(user_statements)
    personal = deepcopy(dict(account.get('personal_context') or {}))
    known = {key: personal[key] for key in CONTEXT_FIELDS if personal.get(key) is not None}
    dates = {key: {'source': 'recorded personal context', 'as_of': account.get('updated_at')} for key in known}
    # Current explicit statements override history, regardless of input ordering.
    rows = [row for row in user_statements if isinstance(row, Mapping) and row.get('kind', 'user_statement') in {'user_statement', 'personal_statement'}]
    for row in sorted(rows, key=lambda row: (bool(row.get('current')), str(row.get('as_of') or ''))):
        key = row.get('field')
        if key in CONTEXT_FIELDS:
            known[key] = deepcopy(row.get('value'))
            dates[key] = {'source': row.get('source') or 'explicit user statement', 'as_of': row.get('as_of'), 'version': row.get('version')}
    doc = discipline.get('discipline', discipline)
    constraints = []
    for row in doc.get('constraints', []):
        if isinstance(row, Mapping):
            constraints.append({**deepcopy(dict(row)), 'strength': row.get('strength') if row.get('strength') in STRENGTHS else 'UNKNOWN', 'source': row.get('source') or 'recorded discipline'})
    for value in doc.get('guardrails', []):
        constraints.append({'field': 'guardrail', 'value': deepcopy(value), 'strength': 'USER_HARD', 'source': 'recorded user guardrail', 'as_of': discipline.get('updated_at')})
    # Existing standing rules remain visible even when their strength was never
    # recorded. UNKNOWN is an explicit review dependency, not a silent downgrade.
    for key in ('tiers', 'max_drawdown_percent', 'cluster_caps', 'add_conditions', 'regime', 'redemption_fees'):
        if key in doc:
            constraints.append({'field':key, 'value':deepcopy(doc[key]), 'strength':'UNKNOWN',
                                'source':'recorded discipline', 'as_of':discipline.get('updated_at'),
                                'version':discipline.get('version')})
    unknown = tuple(key for key in CONTEXT_FIELDS if known.get(key) is None)
    dependencies = account.get('decision_dependencies') or []
    questions = tuple({'field': key, 'question': f'请补充会影响本次判断的{key}。'} for key in unknown if key in dependencies)[:1]
    version = account.get('account_version') or account.get('revision') or account.get('version')
    if version is None:
        version = hashlib.sha256(json.dumps(dict(account), sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    access = known.get('market_access')
    return PersonalContext(str(version), tuple(deepcopy(account.get('positions') or [])), known, unknown,
                           tuple(constraints), tuple(deepcopy(personal.get('preferences') or [])), dates, questions,
                           tuple(access) if isinstance(access, (list, tuple)) else (),
                           deepcopy(account.get('cash')), tuple(deepcopy(account.get('pending_orders') or [])))


@dataclass(frozen=True)
class Capability:
    registered: bool = False
    available: bool = False
    observed_failure: str | None = None
    readonly: bool = True
    operational_status: str = 'NOT_OBSERVED'


@dataclass(frozen=True)
class CapabilityMap:
    tools: Mapping[str, Capability] = field(default_factory=dict)
    unknown_domains: tuple[str, ...] = ('valuation', 'consensus', 'crowding', 'fund_flows')

    def available(self, name):
        item = self.tools.get(name)
        return bool(item and item.available and item.readonly)

    def to_dict(self): return asdict(self)


def build_capability_map(registry) -> CapabilityMap:
    tools = {}
    failures = getattr(registry, 'registration_failures', {}) or {}
    imports = getattr(registry, 'import_failures', {}) or {}
    names = set(getattr(registry, 'tool_names', ())) | set(failures)
    for module in imports:
        names.add(module.removesuffix('_tool'))
    for name in sorted(names):
        tool = registry.get(name)
        failed = name in failures or name + '_tool' in imports or name in imports
        available = False
        failure = 'registry construction failed' if failed else None
        if tool:
            try:
                available = bool(tool.check_available())
                if not available: failure = 'dependency unavailable'
            except Exception:
                failure = 'availability check failed'
        tools[name] = Capability(bool(tool), available, failure, bool(getattr(tool, 'is_readonly', True)))
    return CapabilityMap(tools)
