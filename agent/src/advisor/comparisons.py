"""Pure research contracts. Parsed candidates are drafts until separately reviewed.

This module never changes rule truth, grants action eligibility, or writes a ledger.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from types import MappingProxyType
from typing import Any, Mapping


METHOD_VERSION = 'comparative-research-v1'


def freeze(value):
    if is_dataclass(value) and getattr(value, '__dataclass_params__').frozen:
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    return value


def plain(value):
    if is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(plain(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class _Contract:
    def __post_init__(self):
        for f in fields(self):
            object.__setattr__(self, f.name, freeze(getattr(self, f.name)))

    def to_dict(self):
        return plain(self)


@dataclass(frozen=True)
class ComparisonAssessment(_Contract):
    id: str
    subject_ids: tuple[str, ...]
    horizon_start: str
    horizon_end: str
    objective: str
    alternatives: tuple[Mapping, ...]
    evidence_refs: tuple[str, ...]
    counter_evidence_refs: tuple[str, ...]
    assumptions: tuple[str, ...]
    invalidation: str
    conclusion: str
    preferred_alternative_id: str | None
    reasoning: str
    fragility: Mapping
    risk_analysis: tuple[Mapping, ...] = ()
    schema_version: int = 2
    method_version: str = METHOD_VERSION


@dataclass(frozen=True)
class ScenarioAssessment(_Contract):
    id: str
    subject_ids: tuple[str, ...]
    horizon_start: str
    horizon_end: str
    assumptions: tuple[str, ...]
    path: str
    impact: str
    invalidation: str
    evidence_refs: tuple[str, ...]
    counter_evidence_refs: tuple[str, ...]
    schema_version: int = 2
    method_version: str = METHOD_VERSION


@dataclass(frozen=True)
class ForecastDraft(_Contract):
    id: str
    subject_ids: tuple[str, ...]
    proposed_at: str
    proposition: str
    horizon_start: str
    horizon_end: str
    resolution_rule: str
    resolution_source: str
    conditions: tuple[str, ...]
    invalidation: str
    evidence_refs: tuple[str, ...]
    counter_evidence_refs: tuple[str, ...]
    probability: float | None = None
    probability_basis: Mapping | None = None
    base_rate: float | None = None
    base_rate_refs: tuple[str, ...] = ()
    schema_version: int = 2
    method_version: str = METHOD_VERSION


@dataclass(frozen=True)
class ResearchCandidateSet(_Contract):
    comparisons: tuple[ComparisonAssessment, ...] = ()
    scenarios: tuple[ScenarioAssessment, ...] = ()
    forecasts: tuple[ForecastDraft, ...] = ()
    proposals: tuple[Any, ...] = ()
    subjects: Mapping | None = None
    input_sha256: str = ''
    evidence_sha256: str = ''
    rejected: tuple[Mapping, ...] = ()
    schema_version: int = 2
    method_version: str = METHOD_VERSION


@dataclass(frozen=True)
class ReviewedResearch(_Contract):
    candidates: ResearchCandidateSet
    accepted_comparison_ids: tuple[str, ...] = ()
    accepted_scenario_ids: tuple[str, ...] = ()
    accepted_forecast_ids: tuple[str, ...] = ()
    accepted_proposal_ids: tuple[str, ...] = ()
    accepted_judgment_ids: tuple[str, ...] = ()
    review_id: str = ''
    input_sha256: str = ''
    evidence_sha256: str = ''
    publication_binding: Mapping | None = None
    schema_version: int = 2

    def to_dict(self):
        # Deliberately omit rejected/unreviewed drafts from display projections.
        result = {key: plain(getattr(self, key)) for key in ('schema_version', 'review_id', 'input_sha256', 'evidence_sha256',
            'accepted_comparison_ids', 'accepted_scenario_ids', 'accepted_forecast_ids', 'accepted_proposal_ids',
            'accepted_judgment_ids', 'publication_binding')}
        for name, accepted in (('comparisons', self.accepted_comparison_ids), ('scenarios', self.accepted_scenario_ids),
                               ('forecasts', self.accepted_forecast_ids), ('proposals', self.accepted_proposal_ids)):
            result[name] = [plain(row) for row in getattr(self.candidates, name) if row.id in accepted]
        result['subjects'] = plain(self.candidates.subjects or {})
        result['reviewed_sha256'] = digest({key: value for key, value in result.items() if key != 'publication_binding'})
        return result

    @classmethod
    def from_dict(cls, payload):
        if not isinstance(payload, Mapping):
            raise ValueError('reviewed research object required')
        value = plain(payload)
        supplied_hash = value.pop('reviewed_sha256', None)
        if not supplied_hash or supplied_hash != digest({key: item for key, item in value.items() if key != 'publication_binding'}):
            raise ValueError('reviewed research checksum mismatch')
        if value.get('schema_version') != 2 or not all(value.get(k) for k in ('review_id', 'input_sha256', 'evidence_sha256')):
            raise ValueError('reviewed research requires review/input/evidence bindings')
        constructors = {'comparisons': ComparisonAssessment, 'scenarios': ScenarioAssessment, 'forecasts': ForecastDraft}
        groups, seen = {}, set()
        for name, singular in (('comparisons','comparison'), ('scenarios','scenario'), ('forecasts','forecast'), ('proposals','proposal')):
            rows, accepted = value.get(name), value.get('accepted_' + singular + '_ids')
            if not isinstance(rows, list) or not isinstance(accepted, list) or len(set(accepted)) != len(accepted):
                raise ValueError('explicit unique accepted IDs and arrays required')
            if any(not isinstance(row, Mapping) or not isinstance(row.get('id'), str) for row in rows):
                raise ValueError('invalid reviewed object')
            identifiers = [row['id'] for row in rows]
            if len(identifiers) != len(set(identifiers)) or set(identifiers) != set(accepted) or seen.intersection(identifiers):
                raise ValueError('accepted IDs differ from reviewed objects')
            seen.update(identifiers)
            constructor = constructors.get(name)
            if constructor is None and rows:
                from src.advisor.actions import ActionProposal
                constructor = ActionProposal
            try:
                groups[name] = tuple(constructor(**row) for row in rows)
            except (TypeError, ValueError) as exc:
                raise ValueError('invalid reviewed contract') from exc
        candidates = ResearchCandidateSet(**groups, subjects=value.get('subjects', {}),
            input_sha256=value['input_sha256'], evidence_sha256=value['evidence_sha256'])
        if any(row.comparison_id not in value['accepted_comparison_ids'] for row in candidates.proposals):
            raise ValueError('proposal comparison was not reviewed')
        return cls(candidates=candidates, **{key: value[key] for key in ('accepted_comparison_ids','accepted_scenario_ids',
            'accepted_forecast_ids','accepted_proposal_ids','accepted_judgment_ids','review_id','input_sha256','evidence_sha256','publication_binding') if key in value})


RESEARCH_SCHEMA_PROMPT = '''同时可输出comparisons/scenarios/forecasts/proposals数组；无证据可为空，不要求产生预判。
所有条目id全局唯一，subject_ids用研究对象目录ID，evidence_refs与counter_evidence_refs用精确目录ID；反证未取得时明确待证条件。
comparisons: {id,subject_ids,horizon_start,horizon_end,objective,alternatives:[{id,action:HOLD|ADD|REDUCE_CASH|ROTATE,subject_ids,tradeoff}],evidence_refs,counter_evidence_refs,assumptions:[文本],invalidation,conclusion:preferred|conditional|no_clear_advantage|insufficient_evidence,preferred_alternative_id,reasoning,fragility:{small_change_reverses:布尔,explanation:文本},risk_analysis:[]}。
同期限比较原仓/加原仓/减仓留现金/换仓，不能给虚构精确收益、得分、排名；不可行替代方案说明阻挡。小变动翻转结论须标脆弱性。
scenarios: {id,subject_ids,horizon_start,horizon_end,assumptions:[文本],path,impact,invalidation,evidence_refs,counter_evidence_refs}。压力不是实际回撤或发生概率。
forecasts: {id,subject_ids,proposed_at,proposition,horizon_start,horizon_end,resolution_rule,resolution_source,conditions:[文本],invalidation,evidence_refs,counter_evidence_refs,probability:null,probability_basis:null,base_rate:null}。
预判必须为未来固定期限可判定命题，解析来源用官方或已核验来源的精确地址；没有概率依据保留null，证据质量不等于事件概率。
proposals: {id,action:ADD|REDUCE|ROTATE|HOLD_REVIEW,subject_ids,origin:"RESEARCH_JUDGMENT",evidence_refs,condition_refs:[],comparison_id,parameters:{}}，parameters仅为建议，不是纪律，不赋交易资格。
研究可独立建议减仓、增仓或未持有对象，不能冒充技术规则触发或跨越用户硬约束；政策宣布不等于兑现、盈利增长不等于价格有吸引力，未知共识不称超预期。
'''


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 2400:
        raise ValueError(name + ' requires substantive text')
    return value.strip()


def _day(value):
    return date.fromisoformat(_text(value, 'date')[:10]).isoformat()


def _refs(value, known, *, required=False):
    if not isinstance(value, (list, tuple)) or (required and not value) or len(value) > 16:
        raise ValueError('evidence references must be a bounded array')
    if any(not isinstance(ref, str) or ref not in known for ref in value):
        raise ValueError('unknown evidence reference')
    return tuple(value)


def _probability(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('probability must be finite and between zero and one')
    return float(value)


def _source_map(bundle, catalog):
    facts = dict(_get(catalog, 'facts', {}) or {})
    items = _get(bundle, 'items', ()) or ()
    if isinstance(items, Mapping):
        facts.update({key: plain(value) for key, value in items.items()})
    else:
        facts.update({_get(item, 'id', _get(item, 'evidence_id', '')): plain(item) for item in items})
    return facts


def _bound_row(row, *, known, subjects, as_of):
    if not isinstance(row, Mapping):
        raise ValueError('research item must be an object')
    identifier = row.get('id')
    if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier):
        raise ValueError('invalid research id')
    ids = row.get('subject_ids')
    if not isinstance(ids, (list, tuple)) or not ids or any(not isinstance(s, str) or s not in subjects for s in ids):
        raise ValueError('unknown research subject')
    refs = _refs(row.get('evidence_refs', []), known, required=True)
    counters = _refs(row.get('counter_evidence_refs', []), known)
    for ref in refs + counters:
        fact = known[ref]
        for key in ('as_of', 'published_at', 'available_at', 'vintage'):
            if _get(fact, key) and _day(_get(fact, key)) > as_of:
                raise ValueError('evidence vintage is after frozen inputs')
        if _get(fact, 'usable') is False:
            raise ValueError('unverified, stale or unavailable source is not usable evidence')
        if _get(fact, 'is_model_opinion', False) or _get(fact, 'kind') in {'MODEL_OPINION', 'opinion'}:
            raise ValueError('model opinion is not original evidence')
    return identifier, tuple(ids), refs, counters


def _horizon(row, as_of):
    start, end = _day(row.get('horizon_start')), _day(row.get('horizon_end'))
    if start > end or end <= as_of:
        raise ValueError('research horizon must be ordered and forward looking')
    return start, end


def _strings(value, name):
    if not isinstance(value, (list, tuple)) or not value or len(value) > 12:
        raise ValueError(name + ' requires an explicit bounded array')
    return tuple(_text(item, name) for item in value)


def _claim_checks(row, facts):
    text = ' '.join(str(row.get(key) or '') for key in ('reasoning', 'path', 'impact', 'proposition', 'objective', 'invalidation'))
    metadata = json.dumps(plain(facts), ensure_ascii=False).lower()
    if re.search(r'(?:预期收益|预计收益|目标价|胜率|年化回报|事件概率).{0,12}\d', json.dumps(plain(row), ensure_ascii=False)):
        raise ValueError('precise future return, target or probability requires a separately grounded numerical contract')
    requirements = ((r'低估|价格有吸引力|估值便宜', r'valuation|估值|pe_ratio|pb_ratio'),
        (r'超预期|预期差', r'consensus|一致预期'), (r'抢筹|资金涌入', r'fund_flow|资金流'),
        (r'政策已经兑现|政策已兑现', r'implementation|执行结果|实际落地'))
    for claim, needed in requirements:
        if re.search(claim, text) and not re.search(r'不能|不等于|未知|待核验|无法|尚未', text) and not re.search(needed, metadata):
            raise ValueError('stronger market claim lacks corresponding evidence')
    if re.search(r'共识未知.{0,20}(?:但|仍).{0,20}超预期|(?:均线|情绪).{0,20}(?:意味着|说明|证明).{0,20}独立买入优势', text):
        raise ValueError('technical or unavailable consensus evidence does not establish independent advantage')
    if re.search(r'压力.{0,40}(?:实际回撤|未来上界|最大损失确定)', text) and not re.search(r'不能|不等于|无法', text):
        raise ValueError('stress scenario is not realized drawdown or future bound')
    forbidden = {'score', 'expected_return', 'target_price', 'return_rank', 'event_probability', 'win_rate'}
    def visit(value):
        if isinstance(value, Mapping):
            if forbidden.intersection(value):
                raise ValueError('invented numerical ranking or target is not supported')
            for child in value.values(): visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value: visit(child)
    visit(row)


def parse_candidates(document, *, inputs=None, bundle=None, catalog=None, subjects=None):
    """Parse a joint expert document; legacy judgments remain the expert's contract.

    Each invalid item is rejected locally. Cross-reference validation occurs before
    any independent review, so no proposal can borrow a rejected comparison.
    """
    as_of = _day(_get(inputs, 'as_of', _get(catalog, 'as_of', date.today().isoformat())))
    known = _source_map(bundle, catalog)
    subjects = dict(subjects or _get(catalog, 'subjects', {}) or {})
    if not subjects:
        subjects = {str(key): {'id': str(key)} for key in _get(catalog, 'scopes', ()) if key not in {'PORTFOLIO', 'MACRO'}}
    groups, rejected, seen = {name: [] for name in ('comparisons', 'scenarios', 'forecasts', 'proposals')}, [], set()
    if not isinstance(document, Mapping):
        document = {}
        rejected.append({'id': 'document', 'reason': 'research document must be an object'})
    all_ids = [row.get('id') for name in (*groups,'judgments') for row in document.get(name, []) if isinstance(row, Mapping)] if all(isinstance(document.get(n, []), (list, tuple)) for n in groups) else []
    duplicates = {key for key in all_ids if isinstance(key, str) and all_ids.count(key) > 1}
    for name in groups:
        rows = document.get(name, [])
        if not isinstance(rows, (list, tuple)) or len(rows) > 8:
            rejected.append({'id': name, 'reason': 'bounded research array required'})
            continue
        for row in rows:
            identifier = str(_get(row, 'id', 'invalid'))
            try:
                identifier, ids, refs, counters = _bound_row(row, known=known, subjects=subjects, as_of=as_of)
                if identifier in duplicates or identifier in seen:
                    raise ValueError('duplicate research id')
                seen.add(identifier)
                _claim_checks(row, {ref: known[ref] for ref in refs + counters})
                if name == 'proposals':
                    from src.advisor.actions import ActionProposal
                    if row.get('origin') != 'RESEARCH_JUDGMENT' or row.get('action') not in {'ADD', 'REDUCE', 'ROTATE', 'HOLD_REVIEW'}:
                        raise ValueError('research action contract mismatch')
                    comparison = row.get('comparison_id')
                    if comparison not in {c.id for c in groups['comparisons']}:
                        raise ValueError('proposal requires valid comparative assessment')
                    conditions = row.get('condition_refs', [])
                    known_conditions = _get(catalog, 'conditions', {}) or {}
                    if not isinstance(conditions, (list, tuple)) or any(ref not in known_conditions for ref in conditions):
                        raise ValueError('unknown rule condition')
                    groups[name].append(ActionProposal(id=identifier, action=row['action'], subject_ids=ids, origin='RESEARCH_JUDGMENT', evidence_refs=refs,
                        condition_refs=tuple(conditions), comparison_id=comparison, parameters=row.get('parameters', {})))
                    continue
                start, end = _horizon(row, as_of)
                invalidation = _text(row.get('invalidation'), 'invalidation')
                common = dict(id=identifier, subject_ids=ids, horizon_start=start, horizon_end=end,
                              evidence_refs=refs, counter_evidence_refs=counters, invalidation=invalidation)
                if name == 'comparisons':
                    alternatives = row.get('alternatives')
                    if not isinstance(alternatives, (list, tuple)) or len(alternatives) != 4:
                        raise ValueError('compare hold, add, reduce to cash and rotate')
                    if {a.get('action') for a in alternatives if isinstance(a, Mapping)} != {'HOLD', 'ADD', 'REDUCE_CASH', 'ROTATE'}:
                        raise ValueError('four comparable alternatives required')
                    for alternative in alternatives:
                        _text(alternative.get('id'), 'alternative id')
                        _text(alternative.get('tradeoff'), 'alternative tradeoff')
                        if any(s not in ids for s in alternative.get('subject_ids', ())) or not alternative.get('subject_ids'):
                            raise ValueError('alternative subject mismatch')
                        if alternative.get('horizon_start', start) != start or alternative.get('horizon_end', end) != end:
                            raise ValueError('alternative horizon differs')
                    if len({a['id'] for a in alternatives}) != 4:
                        raise ValueError('duplicate alternative ids')
                    conclusion = row.get('conclusion')
                    preferred = row.get('preferred_alternative_id')
                    if conclusion not in {'preferred', 'conditional', 'no_clear_advantage', 'insufficient_evidence'}:
                        raise ValueError('unknown comparative conclusion')
                    if preferred is not None and preferred not in {a['id'] for a in alternatives}:
                        raise ValueError('unknown preferred alternative')
                    if conclusion in {'preferred', 'conditional'} and not preferred:
                        raise ValueError('preferred alternative required')
                    fragility = row.get('fragility')
                    if not isinstance(fragility, Mapping) or not isinstance(fragility.get('small_change_reverses'), bool):
                        raise ValueError('fragility assessment required')
                    _text(fragility.get('explanation'), 'fragility explanation')
                    risks = row.get('risk_analysis', [])
                    if not isinstance(risks, (list, tuple)):
                        raise ValueError('risk analysis array required')
                    for risk in risks:
                        if risk.get('kind') not in {'THEME', 'MECHANISM', 'DISCLOSED_OVERLAP', 'HISTORICAL_CORRELATION'}:
                            raise ValueError('common-risk method must be explicit')
                        _text(risk.get('explanation'), 'common risk explanation')
                        risk_refs = _refs(risk.get('evidence_refs', []), known, required=True)
                        _bound_row({'id':identifier,'subject_ids':ids,'evidence_refs':risk_refs},known=known,subjects=subjects,as_of=as_of)
                        for stamp in ('as_of','disclosure_as_of','window_end'):
                            if risk.get(stamp) and _day(risk[stamp]) > as_of: raise ValueError('risk disclosure vintage is after frozen inputs')
                        if risk['kind'] == 'DISCLOSED_OVERLAP' and (not risk.get('disclosure_as_of') or not risk.get('as_of')):
                            raise ValueError('disclosed overlap requires holdings date and disclosure date')
                        if risk['kind'] == 'HISTORICAL_CORRELATION' and not all(risk.get(k) for k in ('window_start', 'window_end', 'method')):
                            raise ValueError('historical correlation requires method and observation window')
                    groups[name].append(ComparisonAssessment(**common, objective=_text(row.get('objective'), 'objective'), alternatives=tuple(alternatives),
                        assumptions=_strings(row.get('assumptions'), 'assumptions'), conclusion=conclusion, preferred_alternative_id=preferred,
                        reasoning=_text(row.get('reasoning'), 'reasoning'), fragility=fragility, risk_analysis=tuple(risks)))
                elif name == 'scenarios':
                    groups[name].append(ScenarioAssessment(**common, assumptions=_strings(row.get('assumptions'), 'assumptions'),
                        path=_text(row.get('path'), 'scenario path'), impact=_text(row.get('impact'), 'scenario impact')))
                else:
                    proposed_at = _text(row.get('proposed_at'), 'proposed_at')
                    if _day(proposed_at) > as_of or _day(proposed_at) > start:
                        raise ValueError('forecast cannot be proposed after frozen inputs or horizon start')
                    probability, basis, base_rate = row.get('probability'), row.get('probability_basis'), row.get('base_rate')
                    if probability is not None:
                        probability = _probability(probability)
                        if not isinstance(basis, Mapping) or basis.get('kind') not in {'SUBJECTIVE_UNCALIBRATED', 'EMPIRICAL_FREQUENCY'}:
                            raise ValueError('event probability requires explicit grounded basis, not evidence quality')
                        _text(basis.get('description'), 'probability basis')
                        basis_refs = _refs(basis.get('evidence_refs', []), known, required=True)
                        _bound_row({'id':identifier, 'subject_ids':ids,'evidence_refs':basis_refs}, known=known, subjects=subjects, as_of=as_of)
                    base_refs = _refs(row.get('base_rate_refs', []), known)
                    if base_refs: _bound_row({'id':identifier,'subject_ids':ids,'evidence_refs':base_refs},known=known,subjects=subjects,as_of=as_of)
                    if base_rate is not None:
                        base_rate = _probability(base_rate)
                        if not base_refs: raise ValueError('base rate requires source references')
                    resolution_source = _text(row.get('resolution_source'), 'resolution source')
                    anchored = False
                    for ref in refs + counters:
                        fact = known[ref]
                        data = _get(fact, 'data', {}) or {}
                        source_names = {ref, str(_get(fact, 'url', '')), str(_get(fact, 'source', '')),
                                        str(_get(data, 'source', '')), str(_get(data, 'url', ''))}
                        verified = _get(fact, 'verified') is True or _get(fact, 'usable') is True or _get(fact, 'kind') in {'observation', 'market_observation'}
                        if resolution_source in source_names and verified and _get(fact, 'usable') is not False:
                            anchored = True
                    if not anchored:
                        raise ValueError('resolution source must match a cited verified source or canonical observation')
                    groups[name].append(ForecastDraft(**common, proposed_at=proposed_at,
                        proposition=_text(row.get('proposition'), 'proposition'), resolution_rule=_text(row.get('resolution_rule'), 'resolution rule'),
                        resolution_source=resolution_source, conditions=_strings(row.get('conditions'), 'conditions'),
                        probability=probability, probability_basis=basis, base_rate=base_rate, base_rate_refs=base_refs))
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                rejected.append({'id': identifier, 'kind': name, 'reason': str(exc)})
    return ResearchCandidateSet(**{name: tuple(rows) for name, rows in groups.items()}, subjects=subjects,
        input_sha256=str(_get(inputs, 'input_sha256', _get(catalog, 'input_sha256', ''))),
        evidence_sha256=str(_get(bundle, 'bundle_hash', _get(bundle, 'sha256', _get(bundle, 'evidence_sha256', _get(catalog, 'sha256', ''))))), rejected=tuple(rejected))


def validate_research_candidates(document, catalog, *, inputs=None, bundle=None, subjects=None):
    """Adapter matching the legacy validator's (accepted, rejected) convention."""
    result = parse_candidates(document, inputs=inputs, bundle=bundle, catalog=catalog, subjects=subjects)
    return result, list(plain(result.rejected))


def review_candidates(candidates, review_document, *, review_id='', accepted_judgment_ids=()):
    if not isinstance(candidates, ResearchCandidateSet):
        raise TypeError('structured candidate set required')
    approvals, seen = set(), set()
    reviews = review_document.get('reviews', []) if isinstance(review_document, Mapping) else []
    for row in reviews if isinstance(reviews, (list, tuple)) else ():
        if not isinstance(row, Mapping) or not isinstance(row.get('id'), str): continue
        identifier = row['id']
        if identifier in seen:
            approvals.discard(identifier)
        elif row.get('supported') is True and isinstance(row.get('reason'), str) and row['reason'].strip():
            approvals.add(identifier)
        seen.add(identifier)
    accepted = {name: tuple(row.id for row in getattr(candidates, name) if row.id in approvals)
                for name in ('comparisons', 'scenarios', 'forecasts', 'proposals')}
    # A reviewed proposal cannot inherit an unreviewed comparison.
    accepted['proposals'] = tuple(row.id for row in candidates.proposals if row.id in accepted['proposals'] and row.comparison_id in accepted['comparisons'])
    return ReviewedResearch(candidates=candidates, accepted_comparison_ids=accepted['comparisons'], accepted_scenario_ids=accepted['scenarios'],
        accepted_forecast_ids=accepted['forecasts'], accepted_proposal_ids=accepted['proposals'], accepted_judgment_ids=tuple(accepted_judgment_ids),
        review_id=review_id or digest(review_document), input_sha256=candidates.input_sha256, evidence_sha256=candidates.evidence_sha256)


def build_research_candidates(inputs, context, report, bundle, opportunities, llm, *, budget):
    """One bounded generation on the existing provider; independent review is separate.

    Actual provider requests debit the same budget through budget_scope, including
    retries. The adapter opens no fresh quota and preserves the review reserve.
    Joint generation in expert.py should call parse_candidates directly.
    """
    import threading
    from src.advisor.budget_scope import budget_scope
    from src.advisor.collection import bounded_collect
    from src.advisor.expert import _parse
    subjects = dict(_get(report, 'research_subjects', {}) or {})
    subjects.update(_get(opportunities, 'subjects', {}) or {})
    catalog = report.get('research_catalog')
    def empty(reason):
        from dataclasses import replace
        draft = parse_candidates({}, inputs=inputs, bundle=bundle, catalog=catalog, subjects=subjects)
        return replace(draft, rejected=({'id':'generation','reason':reason},))
    remaining = budget.remaining_seconds('generate')
    if remaining <= 0 or budget.receipt()['model_calls'] >= max(0, budget.max_model_calls - 1):
        return empty('shared research generation budget exhausted; review reserve preserved')
    if not callable(getattr(llm, 'stream_chat', None)):
        return empty('existing streaming provider unavailable')
    payload = {'context': plain(context), 'as_of': _get(inputs, 'as_of'), 'subjects': plain(subjects),
        'evidence': _source_map(bundle, catalog), 'opportunities': plain(_get(opportunities, 'candidates', ())),
        'coverage_status': _get(opportunities, 'coverage_status', 'INCOMPLETE')}
    stopped = threading.Event()
    timeout = min(55.0, remaining)
    def cancelled():
        return stopped.is_set() or budget.cancelled or budget.remaining_seconds('generate') <= 0
    def call():
        with budget_scope(budget, stage='generate'):
            return llm.stream_chat([{'role':'system','content':RESEARCH_SCHEMA_PROMPT},
                {'role':'user','content':json.dumps(payload, ensure_ascii=False, allow_nan=False)}],
                tools=None, timeout=timeout, idle_timeout_s=min(25.0, timeout), should_cancel=cancelled)
    completed, errors = bounded_collect({'response':call}, timeout_seconds=timeout, max_workers=1)
    stopped.set()
    if errors:
        return empty(errors['response'])
    try:
        document = _parse(_get(completed['response'], 'content', ''))
    except (ValueError, TypeError) as exc:
        return empty('invalid generation JSON: ' + str(exc))
    return parse_candidates(document, inputs=inputs, bundle=bundle, catalog=catalog, subjects=subjects)


def restore_reviewed(payload):
    return ReviewedResearch.from_dict(payload)
