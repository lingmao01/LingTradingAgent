"""Published, immutable forecast origins and descriptive evaluation.

Storage belongs to the existing publication journal. These functions perform no
I/O, scheduling, trading, strategy promotion or automatic parameter adjustment.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Mapping, Sequence

from src.advisor.comparisons import ForecastDraft, ReviewedResearch, _Contract, _day, _probability, _text, digest, plain


@dataclass(frozen=True)
class ResearchCheckpoint(_Contract):
    id: str
    event: str
    expected_at: str | None
    verified_at: str | None
    questions: tuple[str, ...]
    subject_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    schema_version: int = 2


@dataclass(frozen=True)
class ForecastRecord(_Contract):
    id: str
    forecast_id: str
    publication_id: str
    published_at: str
    origin: ForecastDraft
    original_terms_sha256: str
    input_sha256: str
    evidence_sha256: str
    body_sha256: str
    view_sha256: str
    review_id: str
    version: int = 1
    revisions: tuple[Mapping, ...] = ()
    execution_state: str = 'not_reported'
    schema_version: int = 2
    method_version: str = 'published-forecast-v1'

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        value['origin'] = ForecastDraft(**value['origin']) if isinstance(value.get('origin'), Mapping) else value['origin']
        record = cls(**value)
        if record.original_terms_sha256 != digest(record.origin):
            raise ValueError('original forecast terms were changed')
        return record


@dataclass(frozen=True)
class ForecastEvaluation(_Contract):
    as_of: str
    results: tuple[Mapping, ...]
    sample_count: int
    mean_brier: float | None
    groups: tuple[Mapping, ...]
    calibration_status: str = 'DESCRIPTIVE_ONLY'
    predictive_advantage: None = None
    advice_value: None = None
    realized_execution_performance: None = None
    schema_version: int = 2
    method_version: str = 'forecast-evaluation-v1'


def publish_forecasts(publication_id: str, candidates: ReviewedResearch, *, published_at: str) -> tuple[ForecastRecord, ...]:
    """Return deterministic journal rows after the caller verifies the actual receipt.

    The publication service must populate publication_binding only after session
    commit and the actual body/view hash checks. Repeated invocation returns the
    same IDs; the journal upserts these IDs, never adds duplicate scoring samples.
    """
    if not isinstance(candidates, ReviewedResearch):
        raise ValueError('only independently reviewed research can be published')
    binding = candidates.publication_binding or {}
    if (binding.get('committed') is not True or binding.get('publication_id') != publication_id
            or not binding.get('body_sha256') or not binding.get('view_sha256')
            or not candidates.review_id or not candidates.input_sha256 or not candidates.evidence_sha256
            or binding.get('input_sha256') != candidates.input_sha256
            or binding.get('evidence_sha256') != candidates.evidence_sha256
            or candidates.input_sha256 != candidates.candidates.input_sha256
            or candidates.evidence_sha256 != candidates.candidates.evidence_sha256):
        raise ValueError('committed publication/body/view/evidence/input binding required')
    accepted = set(candidates.accepted_forecast_ids)
    if len(accepted) != len(candidates.accepted_forecast_ids) or not accepted.issubset({row.id for row in candidates.candidates.forecasts}):
        raise ValueError('accepted forecast IDs must identify reviewed candidates uniquely')
    published_day = _day(published_at)
    records = []
    for forecast in candidates.candidates.forecasts:
        if forecast.id not in accepted: continue
        if published_day < _day(forecast.proposed_at) or published_day > forecast.horizon_start:
            raise ValueError('forecast must be published ex ante, by its fixed horizon start')
        terms = digest(forecast)
        records.append(ForecastRecord(id='forecast_' + digest([publication_id, forecast.id])[:24],
            forecast_id=forecast.id, publication_id=publication_id, published_at=published_at, origin=forecast,
            original_terms_sha256=terms, input_sha256=candidates.input_sha256,
            evidence_sha256=candidates.evidence_sha256, body_sha256=binding['body_sha256'], view_sha256=binding['view_sha256'],
            review_id=candidates.review_id))
    return tuple(records)


def revise_forecast(record: ForecastRecord, *, revised_at: str, reason: str, probability: float | None,
                    evidence_refs: Sequence[str] = (), probability_basis: Mapping | None = None) -> ForecastRecord:
    """Append an opinion revision. The origin's terms/probability never change.

    A revised probability is descriptive and cannot improve the origin's Brier
    score. A new scoring proposition requires a new ex-ante reviewed publication.
    """
    from dataclasses import replace
    if record.original_terms_sha256 != digest(record.origin):
        raise ValueError('original terms hash mismatch')
    if _day(revised_at) < _day(record.revisions[-1]['revised_at'] if record.revisions else record.published_at):
        raise ValueError('revisions must be chronological')
    if not evidence_refs or any(not isinstance(ref, str) or not ref for ref in evidence_refs):
        raise ValueError('revision must cite new evidence')
    if probability is not None:
        probability = _probability(probability)
        if not isinstance(probability_basis, Mapping) or probability_basis.get('kind') not in {'SUBJECTIVE_UNCALIBRATED', 'EMPIRICAL_FREQUENCY'}:
            raise ValueError('revised probability requires explicit basis')
        _text(probability_basis.get('description'), 'revised probability basis')
    revision = {'version': record.version + 1, 'revised_at': revised_at, 'reason': _text(reason, 'revision reason'),
                'probability': probability, 'probability_basis': plain(probability_basis), 'evidence_refs': list(evidence_refs),
                'original_terms_sha256': record.original_terms_sha256}
    return replace(record, version=record.version + 1, revisions=record.revisions + (revision,))


def _timestamp(value):
    text = str(value)
    return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp() if len(text) > 10 else datetime.fromisoformat(text).timestamp()


def _after(left, right):
    # Date-only cutoffs deliberately include the whole named date.
    if len(str(right)) == 10:
        return _day(left) > _day(right)
    if len(str(left)) == 10:
        return _day(left) > _day(right)
    return _timestamp(left) > _timestamp(right)


def _resolve(record, outcome, as_of):
    origin = record.origin
    result = {'id': record.id, 'forecast_id': record.forecast_id, 'version': 1, 'status': 'UNRESOLVED',
        'reason': 'fixed horizon or original source outcome unavailable', 'brier': None, 'probability': origin.probability,
        'outcome': None, 'original_terms_sha256': record.original_terms_sha256, 'advice_value': None}
    if record.original_terms_sha256 != digest(origin):
        return {**result, 'status': 'NOT_SCORABLE', 'reason': 'original forecast terms changed'}
    if _day(as_of) < origin.horizon_end or not isinstance(outcome, Mapping):
        return result
    if any(outcome.get(key) is True for key in ('lookahead_bias', 'survivorship_bias', 'parameter_selection_bias', 'post_hoc_window', 'invalidated')) or outcome.get('original_vintage_available') is False:
        return {**result, 'status': 'NOT_SCORABLE', 'reason': 'bias, changed conditions or unavailable original vintage'}
    if outcome.get('source') != origin.resolution_source:
        return {**result, 'status': 'NOT_SCORABLE', 'reason': 'resolution source differs from ex-ante source'}
    if outcome.get('resolution_rule', origin.resolution_rule) != origin.resolution_rule or outcome.get('original_terms_sha256', record.original_terms_sha256) != record.original_terms_sha256:
        return {**result, 'status': 'NOT_SCORABLE', 'reason': 'resolution rule or original terms differ'}
    if not outcome.get('observed_at') or not outcome.get('resolved_at') or not outcome.get('available_at'):
        return result
    try:
        observed = _day(outcome['observed_at'])
        if not origin.horizon_start <= observed <= origin.horizon_end:
            return {**result, 'status': 'NOT_SCORABLE', 'reason': 'observation outside fixed forecast horizon'}
        if _after(outcome['available_at'], as_of) or _after(outcome['resolved_at'], as_of):
            return result
        if _day(outcome['resolved_at']) < origin.horizon_end or _after(outcome['observed_at'], outcome['available_at']):
            return {**result, 'status': 'NOT_SCORABLE', 'reason': 'resolution precedes fixed horizon or outcome availability'}
    except (ValueError, TypeError, OverflowError):
        return {**result, 'status': 'NOT_SCORABLE', 'reason': 'invalid outcome timestamps'}
    event = outcome.get('value')
    if isinstance(event, bool): event = int(event)
    if not isinstance(event, (int, float)) or event not in (0, 1):
        return {**result, 'status': 'NOT_SCORABLE', 'reason': 'binary proposition requires an unambiguous binary outcome'}
    brier = (origin.probability - event) ** 2 if origin.probability is not None else None
    return {**result, 'status': 'RESOLVED', 'reason': 'resolved using frozen original terms', 'outcome': event, 'brier': brier}


def evaluate_forecasts(records: Sequence[ForecastRecord], outcomes: Mapping, *, as_of: str) -> ForecastEvaluation:
    """Score each original publication once; revisions never replace its terms.

    Outcomes are keyed by stable record ID (preferred) or local forecast ID. The
    caller supplies actual source evidence; absent or ambiguous data stays open.
    Calibration groups are always descriptive, regardless of sample size.
    """
    _day(as_of)
    unique, conflicts = {}, set()
    for raw in records:
        record = ForecastRecord.from_dict(raw) if isinstance(raw, Mapping) else raw
        if not isinstance(record, ForecastRecord): raise TypeError('published forecast records required')
        if record.id in unique and unique[record.id].original_terms_sha256 != record.original_terms_sha256:
            conflicts.add(record.id)
        unique.setdefault(record.id, record)
    results = []
    event_keys = set()
    for record in sorted(unique.values(),key=lambda r:r.published_at):
        origin = record.origin
        event_key = digest({k: plain(getattr(origin,k)) for k in ('subject_ids','proposition','horizon_start','horizon_end','resolution_rule','resolution_source','conditions')})
        if event_key in event_keys:
            results.append({'id':record.id,'forecast_id':record.forecast_id,'status':'NOT_SCORABLE','reason':'duplicate original event across publications','brier':None})
            continue
        event_keys.add(event_key)
        result = _resolve(record, outcomes.get(record.id, outcomes.get(record.forecast_id)), as_of)
        if record.id in conflicts:
            result.update(status='NOT_SCORABLE', reason='conflicting origins for the same publication', brier=None)
        results.append(result)
    scored = [row for row in results if row['brier'] is not None]
    buckets = {}
    for row in scored:
        lower = min(9, int(row['probability'] * 10))
        buckets.setdefault(lower, []).append(row)
    groups = tuple({'probability_range': [key / 10, (key + 1) / 10], 'count': len(rows),
                    'mean_probability': sum(r['probability'] for r in rows) / len(rows),
                    'base_rate': sum(r['outcome'] for r in rows) / len(rows), 'status': 'DESCRIPTIVE_ONLY'}
                   for key, rows in sorted(buckets.items()))
    return ForecastEvaluation(as_of=as_of, results=tuple(results), sample_count=len(scored),
        mean_brier=sum(row['brier'] for row in scored) / len(scored) if scored else None, groups=groups)


def resolve_forecast_outcomes(records, *, as_of, market_fetcher=None, fund_fetcher=None):
    """Resolve only a predeclared machine-readable close/NAV predicate.

    Free-text event propositions need verified event evidence and remain open.
    No model, source page or retrospective window can redefine the predicate.
    """
    import json
    import re
    from src.advisor.collection import bounded_collect
    from src.market_data import fetch_market_data
    from src.fund_data import fetch_fund_data
    market_fetcher, fund_fetcher = market_fetcher or fetch_market_data, fund_fetcher or fetch_fund_data
    def resolve(record):
        origin = record.origin
        if as_of[:10] < origin.horizon_end: return None
        symbol = origin.resolution_source
        if not re.fullmatch(r'(?:[0-9]{6}\.(?:SH|SZ|OF)|[A-Z][A-Z0-9.-]{0,14}\.US)',symbol):return None
        try: predicate=json.loads(origin.resolution_rule)
        except (ValueError,TypeError):return None
        field='unit_nav' if symbol.endswith('.OF') else 'close'
        if not isinstance(predicate,dict) or set(predicate)!={'field','operator','threshold','at'} or predicate.get('field')!=field or predicate.get('at')!='horizon_end' or predicate.get('operator') not in {'>','>=','<','<='}:return None
        threshold=predicate.get('threshold')
        if isinstance(threshold,bool) or not isinstance(threshold,(int,float)) or not math.isfinite(threshold):return None
        if symbol.endswith('.OF'):
            payload=fund_fetcher([symbol],view='nav',start_date=origin.horizon_end,end_date=origin.horizon_end,max_rows=0)
            entry=payload.get(symbol) or {};meta=entry.get('_provenance') or {};rows=entry.get('data') or []
            if meta.get('caliber')!='nav' or meta.get('stale'):return None
        else:
            payload=market_fetcher(codes=[symbol],start_date=origin.horizon_end,end_date=origin.horizon_end,max_rows=0,include_provenance=True)
            rows=payload.get(symbol);meta=(payload.get('_provenance') or {}).get(symbol) or {}
            if meta.get('finality') not in {'closed_session','historical_closed','closed'}:return None
        if not meta.get('source') or meta.get('quality') in {'unknown','stale','unavailable'}:return None
        if hasattr(rows,'reset_index'):rows=rows.reset_index().to_dict('records')
        if isinstance(rows,dict):
            if rows.get('truncated'):return None
            rows=rows.get('data')
        if not isinstance(rows,list):return None
        matching=[r for r in rows if str(r.get('date') or r.get('trade_date') or r.get('index') or '')[:10]==origin.horizon_end and r.get(field) is not None]
        if len(matching)!=1:return None
        value=matching[0][field]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):return None
        result={'>':value>threshold,'>=':value>=threshold,'<':value<threshold,'<=':value<=threshold}[predicate['operator']]
        available=meta.get('fetched_at')
        if not available:return None
        return {'value':int(result),'source':symbol,'resolution_rule':origin.resolution_rule,
            'original_terms_sha256':record.original_terms_sha256,'observed_at':origin.horizon_end,
            'available_at':available,'resolved_at':as_of,'original_vintage_available':True,
            'observation':{'value':value,'field':field,'source':meta['source'],'date':origin.horizon_end}}
    typed=[ForecastRecord.from_dict(row) if isinstance(row,Mapping) else row for row in records]
    completed,_=bounded_collect({r.id:(lambda r=r:resolve(r)) for r in typed},timeout_seconds=30,max_workers=2)
    return {key:value for key,value in completed.items() if value is not None}
