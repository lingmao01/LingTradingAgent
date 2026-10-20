"""Source-backed exploration maturity; no portfolio writes or invented symbols."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Mapping
import hashlib

from src.advisor.models import ResearchSubject


@dataclass(frozen=True)
class OpportunityAssessment:
    candidate_id: str
    subject: ResearchSubject
    maturity: str
    why_now: str
    support_refs: tuple[str, ...] = ()
    counter_refs: tuple[str, ...] = ()
    key_gaps: tuple[str, ...] = ()
    applicable_horizon: str | None = None
    comparison: str = 'cash'
    scope: str = 'direction_research'
    diversification_verified: bool = False
    price_as_of: str | None = None

    def to_dict(self): return asdict(self)


@dataclass(frozen=True)
class OpportunityScan:
    covered_universe: tuple[Mapping[str, Any], ...] = ()
    coverage_status: str = 'INCOMPLETE'
    candidates: tuple[OpportunityAssessment, ...] = ()
    rejected_reasons: tuple[Mapping[str, Any], ...] = ()
    remaining_questions: tuple[str, ...] = ()
    cash_baseline: Mapping[str, Any] = field(default_factory=lambda:{'subject_id':'cash', 'role':'comparison', 'return_assumption':None})
    deep_dive_count: int = 0
    evidence_bundle_hash: str = ''

    @property
    def subjects(self): return {row.subject.id:row.subject for row in self.candidates}

    def to_dict(self): return asdict(self)


def screen_opportunities(context, bundle, capabilities, *, budget) -> OpportunityScan:
    held = {str(row.get('symbol')) for row in context.holdings}
    coverage = tuple(dict(row) for row in bundle.coverage if row.get('scope') == 'candidate')
    attempted = any(row.get('status') in {'complete', 'resolved'} for row in coverage)
    incomplete = not attempted or any(row.get('status') == 'incomplete' for row in coverage)
    candidates, rejected, questions = [], [], []
    seen = set()
    deep = 0
    grouped = {}
    for item in bundle.items:
        data = item.value if isinstance(item.value, Mapping) else item.metadata
        if data.get('candidate') or item.claim == 'candidate':
            key = str(item.metadata.get('symbol') or data.get('canonical_symbol') or item.metadata.get('name') or data.get('name') or item.subject)
            grouped.setdefault(key, []).append(item)
    for item in bundle.items:
        data = item.value if isinstance(item.value, Mapping) else item.metadata
        if not data.get('candidate') and item.claim != 'candidate': continue
        symbol = str(item.metadata.get('symbol') or data.get('canonical_symbol') or '')
        subject_name = str(item.metadata.get('name') or data.get('name') or item.subject)
        key = symbol or subject_name
        if key in seen or symbol in held: continue
        seen.add(key)
        subject_id = str(data.get('candidate_id') or 'candidate_' + hashlib.sha256(key.encode()).hexdigest()[:16])
        missing = []
        if not item.usable: missing.extend(item.uncertainty)
        duplicate_refs = {row.evidence_id for row in grouped[key]}
        conflict_refs = tuple(dict.fromkeys(ref for conflict in bundle.conflicts if duplicate_refs.intersection(conflict.get('evidence_refs', ())) for ref in conflict.get('evidence_refs', ())))
        identity_conflict = any((row.value if isinstance(row.value, Mapping) else row.metadata).get('identity_conflict') for row in grouped[key])
        if conflict_refs: missing.append('candidate_evidence_conflict')
        identity = bool(symbol and data.get('identity_verified') is True and not identity_conflict and not conflict_refs)
        if not identity: missing.append('identity_unknown_or_conflicting')
        market = str(data.get('market') or '')
        access = market in context.market_access if market and context.market_access else None
        if access is None: missing.append('market_access_unknown')
        elif not access: missing.append('market_not_accessible')
        quote = str(data.get('price_as_of') or '')
        timely = False
        try:
            delta = (date.fromisoformat(bundle.as_of[:10]) - date.fromisoformat(quote[:10])).days
            timely = 0 <= delta <= 7
        except (ValueError, TypeError): pass
        if not timely: missing.append('timely_price_missing')
        if not data.get('investment_logic'): missing.append('investment_logic_missing')
        if data.get('counter_evidence_checked') is not True: missing.append('important_counter_evidence_unchecked')
        if not data.get('comparison'): missing.append('comparison_missing')
        if data.get('transaction_conditions_verified') is not True: missing.append('transaction_conditions_unknown')
        if data.get('signal_basis') in {'price_ranking', 'popularity'} and not data.get('investment_logic'):
            missing.append('ranking_alone_insufficient')
        # A directory record can describe a product, but cannot certify a user's access.
        availability = 'AVAILABLE' if identity and access is True else ('UNAVAILABLE' if access is False else 'UNKNOWN')
        subject = ResearchSubject(subject_id, str(data.get('kind') or ('instrument' if symbol else 'direction')),
                                  symbol, subject_name, availability, (item.evidence_id,))
        maturity = 'WATCH'
        if data.get('rejected') or access is False:
            maturity = 'REJECTED'
            rejected.append({'candidate_id':subject_id, 'reasons':missing or ['source_rejected']})
        elif not missing:
            if deep < 2 and budget.remaining_seconds('source') > 0:
                maturity = 'COMPARISON_READY'; deep += 1
            else:
                if budget.remaining_seconds('source') <= 0:
                    missing.append('budget_exhausted')
                    incomplete = True
                else:
                    missing.append('deep_dive_limit')
        candidates.append(OpportunityAssessment(subject_id, subject, maturity,
                          str(data.get('why_now') or data.get('investment_logic') or item.claim),
                          (item.evidence_id,) if item.usable else (), tuple(dict.fromkeys((*(data.get('counter_refs') or ()), *conflict_refs))),
                          tuple(dict.fromkeys(missing)), data.get('horizon'), str(data.get('comparison') or 'cash'),
                          'instrument_research' if identity else 'direction_research',
                          bool(data.get('diversification_verified') and data.get('underlying_overlap_refs')),
                          quote or None))
        questions.extend(f'{subject_name}: {gap}' for gap in missing)
    status = 'INCOMPLETE' if incomplete else ('COMPLETE' if any(row.maturity == 'COMPARISON_READY' for row in candidates) else 'NO_QUALIFIED_CANDIDATE')
    if not attempted: questions.insert(0, '持仓外轻量扫描尚未完成；不能推断市场没有机会')
    return OpportunityScan(coverage, status, tuple(candidates), tuple(rejected), tuple(dict.fromkeys(questions)),
                           {'subject_id':'cash', 'role':'comparison', 'return_assumption':None}, deep, bundle.bundle_hash)
