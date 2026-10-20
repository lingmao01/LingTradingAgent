"""Versioned evidence retaining original provenance, conflicts and point-in-time gaps."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import date
import hashlib
import json
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit
from src.advisor.models import freeze, thaw


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _url(value):
    try:
        part = urlsplit(str(value))
        return urlunsplit((part.scheme.lower(), part.netloc.lower(), part.path.rstrip('/'), part.query, ''))
    except ValueError:
        return str(value)


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    subject: str
    claim: str
    value: Any
    kind: str
    unit: str | None
    currency: str | None
    caliber: str | None
    reference_period: str | None
    event_time: str | None
    published_at: str | None
    fetched_at: str | None
    source: str
    url: str | None
    origin_group: str
    vintage: str | None
    quality: str
    verified: bool
    usable: bool
    uncertainty: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, 'value', freeze(self.value))
        object.__setattr__(self, 'metadata', freeze(self.metadata))

    def to_dict(self): return thaw(self)

    def for_as_of(self, as_of):
        gaps = list(self.uncertainty)
        for stamp in (self.published_at, self.vintage):
            if stamp:
                try: date.fromisoformat(str(stamp)[:10])
                except ValueError: gaps.append('invalid_timestamp')
            if as_of and stamp and str(stamp)[:10] > str(as_of)[:10]:
                gaps.append('future_vintage')
        gaps = tuple(dict.fromkeys(gaps))
        return self if gaps == self.uncertainty else replace(self, usable=False, uncertainty=gaps)

    @classmethod
    def parse(cls, raw, *, as_of=None):
        raw = thaw(raw)
        kind = str(raw.get('kind') or 'fact')
        quality = str(raw.get('evidence_quality') or raw.get('quality') or ('verified_source' if raw.get('verified') else 'unknown'))
        verified = bool(raw.get('verified'))
        published = raw.get('published_at') or raw.get('as_of')
        vintage = raw.get('revision_at') or raw.get('vintage') or published
        gaps = []
        if kind not in {'fact', 'calculation'}:
            gaps.append('not_factual_evidence')
        if not verified or quality in {'search_snippet', 'directory', 'summary', 'unknown'}:
            gaps.append('unverified_discovery')
        for stamp in (published, vintage):
            if stamp:
                try: date.fromisoformat(str(stamp)[:10])
                except ValueError: gaps.append('invalid_timestamp')
            if as_of and stamp and str(stamp)[:10] > str(as_of)[:10]:
                gaps.append('future_vintage')
        if not published:
            gaps.append('publication_time_unknown')
        if raw.get('cached') or raw.get('stale') or quality == 'stale':
            gaps.append('cached_requires_revalidation')
        source = str(raw.get('source') or raw.get('source_domain') or raw.get('url') or '')
        if not source:
            gaps.append('source_unknown')
        origin = raw.get('origin_group') or raw.get('origin_url') or raw.get('source_url') or raw.get('original_url')
        if not origin:
            origin = raw.get('content_sha256') or raw.get('url') or source
        origin = _url(origin)
        normalized = {'subject': str(raw.get('subject') or raw.get('symbol') or 'market'),
                      'claim': str(raw.get('claim') or raw.get('metric') or raw.get('field') or raw.get('title') or 'source_content'),
                      'value': deepcopy(raw.get('value', raw.get('content', raw.get('snippet')))),
                      'kind': kind, 'unit': raw.get('unit'), 'currency': raw.get('currency'),
                      'caliber': raw.get('caliber') or raw.get('return_caliber'),
                      'reference_period': raw.get('reference_period') or raw.get('statistical_period'),
                      'event_time': raw.get('event_time'), 'published_at': published, 'fetched_at': raw.get('fetched_at'),
                      'source': source, 'url': raw.get('url'), 'origin_group': origin,
                      'vintage': vintage, 'quality': quality, 'verified': verified,
                      'usable': not gaps, 'uncertainty': tuple(dict.fromkeys(gaps)), 'metadata': raw}
        return cls(evidence_id='ev_' + _hash(normalized)[:24], **normalized)


@dataclass(frozen=True)
class EvidenceBundle:
    items: tuple[EvidenceItem, ...] = ()
    as_of: str | None = None
    version: int = 1
    parent_hash: str | None = None
    bundle_hash: str = ''
    conflicts: tuple[Mapping[str, Any], ...] = ()
    gaps: tuple[Mapping[str, Any], ...] = ()
    coverage: tuple[Mapping[str, Any], ...] = ()

    @property
    def usable_items(self): return tuple(item for item in self.items if item.usable)

    @property
    def independent_source_count(self): return len({item.origin_group for item in self.usable_items})

    def __post_init__(self):
        for name in ('conflicts', 'gaps', 'coverage'):
            object.__setattr__(self, name, freeze(getattr(self, name)))

    def to_dict(self): return thaw(self)

    @classmethod
    def from_dict(cls, raw):
        raw = thaw(raw)
        items = tuple(EvidenceItem(**{**row, 'uncertainty':tuple(row.get('uncertainty', ()))}) for row in raw.get('items', ()))
        restored = cls.build(items, as_of=raw.get('as_of'), coverage=raw.get('coverage', ()),
                             gaps=[gap for gap in raw.get('gaps', ()) if 'evidence_id' not in gap],
                             version=raw.get('version', 1), parent_hash=raw.get('parent_hash'))
        if not raw.get('bundle_hash') or restored.bundle_hash != raw['bundle_hash']:
            raise ValueError('evidence bundle hash mismatch')
        return restored

    @classmethod
    def build(cls, rows=(), *, as_of=None, coverage=(), gaps=(), version=1, parent_hash=None):
        items = tuple(row.for_as_of(as_of) if isinstance(row, EvidenceItem) else EvidenceItem.parse(row, as_of=as_of) for row in rows)
        # Preserve all contradictory observations; never average or count web votes.
        by_claim = {}
        conflicts = []
        for item in items:
            if not item.usable: continue
            key = (item.subject, item.claim, item.reference_period)
            for previous in by_claim.get(key, []):
                if (previous.value, previous.unit, previous.currency, previous.caliber, previous.vintage) != (item.value, item.unit, item.currency, item.caliber, item.vintage):
                    conflicts.append({'subject': item.subject, 'claim': item.claim,
                        'evidence_refs': [previous.evidence_id, item.evidence_id],
                        'status': 'CHECK_OBJECT_TIME_UNIT_AND_VINTAGE', 'resolution': None})
            by_claim.setdefault(key, []).append(item)
        missing = [row for row in thaw(gaps) if 'evidence_id' not in row]
        missing.extend({'evidence_id': item.evidence_id, 'reason': reason} for item in items for reason in item.uncertainty)
        state = {'items': [item.to_dict() for item in items], 'as_of': as_of, 'version': version,
                 'parent_hash': parent_hash, 'conflicts': conflicts, 'gaps': missing, 'coverage': thaw(coverage)}
        return cls(items, as_of, version, parent_hash, _hash(state), tuple(conflicts), tuple(missing), tuple(thaw(coverage)))

    @classmethod
    def from_payload(cls, payload, *, as_of=None):
        payload = thaw(payload)
        report = payload.get('data', payload)
        cutoff = as_of or report.get('as_of') or (payload.get('inputs') or {}).get('as_of')
        news = report.get('news') or payload.get('news') or {}
        rows = []
        for row in news.get('primary', []):
            quality = row.get('evidence_quality') or ('verified_original' if row.get('verified') else 'unknown')
            rows.append({**row, 'evidence_quality': quality})
            for fact in row.get('facts', []):
                rows.append({**row, **fact, 'evidence_quality': quality})
        rows.extend({**row, 'evidence_quality':'search_snippet', 'verified':False} for row in news.get('discovery', []))
        for symbol, observation in (payload.get('evidence') or {}).items():
            if isinstance(observation, Mapping):
                rows.append({**observation, 'subject':symbol, 'verified':bool(observation.get('source') and observation.get('quality') not in {'unknown','unavailable'}),
                             'published_at':observation.get('as_of')})
        for row in report.get('external_market', []):
            verified = bool(row.get('source') and row.get('quality') not in {'unknown','unavailable'})
            rows.append({**row, 'verified':verified, 'published_at':row.get('as_of')})
            if row.get('symbol') and verified:
                rows.append({**row, 'verified':verified, 'published_at':row.get('as_of'), 'metric':'candidate',
                             'value':{'candidate':True, 'why_now':'本轮已核验的外围资产报价，可作为待研究对照',
                                      'price_as_of':row.get('as_of'), 'comparison':'cash', 'identity_verified':False}})
        return cls.build(rows, as_of=cutoff, gaps=news.get('gaps', []) + list(report.get('external_gaps') or []), coverage=news.get('coverage', []))

    def extend(self, rows=(), *, coverage=(), gaps=()):
        unique = {item.evidence_id:item for item in self.items}
        for row in rows:
            item = row if isinstance(row, EvidenceItem) else EvidenceItem.parse(row, as_of=self.as_of)
            unique.setdefault(item.evidence_id, item)
        # Recomputed per-item gaps should not multiply with every supplement.
        inherited = [gap for gap in self.gaps if 'evidence_id' not in gap]
        return self.build(unique.values(), as_of=self.as_of, version=self.version + 1,
                          parent_hash=self.bundle_hash, coverage=(*self.coverage, *coverage), gaps=(*inherited, *gaps))
