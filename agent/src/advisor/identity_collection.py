"""Bounded public-profile discovery; candidates never modify account identity inputs.

This stage intentionally does not verify mappings. A third-party profile's
benchmark is a lead, not a tracking relationship or an execution permission.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import math
import re
import time
from typing import Mapping

from src.advisor.applicability import _relation_at
from src.advisor.collection import bounded_collect
from src.advisor.models import content_sha256

_FUND = re.compile(r'^\d{6}\.OF$')
_INDEX = re.compile(r'(?<![A-Za-z0-9])\d{6}\.(?:SH|SZ|CSI|CNI)(?![A-Za-z0-9])', re.I)
_FIELDS = {
    '业绩比较基准': 'performance_benchmark', '业绩基准': 'performance_benchmark',
    '比较基准': 'performance_benchmark', 'benchmark': 'performance_benchmark',
    '跟踪指数': 'tracking_index', '标的指数': 'tracking_index', 'tracking_index': 'tracking_index',
    '目标ETF': 'target_etf', '目标基金': 'target_etf', 'target_etf': 'target_etf',
}


def _targets(payload):
    raw = payload.get('inputs') or {}
    account, identities = raw.get('account') or {}, raw.get('identities') or {}
    cutoff = (payload.get('data') or {}).get('as_of') or raw.get('as_of')
    holdings = [(str(row.get('id') or row.get('symbol')), [row.get('symbol')])
                for row in account.get('positions', ()) if isinstance(row, Mapping)]
    holdings.extend((str(row.get('id') or row.get('group_id') or f'group:{offset}'),
                     row.get('candidate_symbols') or [])
                    for offset, row in enumerate(account.get('holding_groups', ())))
    selected, omitted, affected = {}, [], set()
    for holding, symbols in holdings:
        for symbol in symbols:
            if not isinstance(symbol, str) or not _FUND.fullmatch(symbol):
                continue
            if _relation_at(symbol, identities, cutoff)[1] == 'RESOLVED':
                continue
            if holding not in affected and len(affected) >= 2:
                omitted.append({'holding_ids': [holding], 'symbol': symbol, 'reason': 'holding_limit_reached'})
                continue
            if symbol not in selected and len(selected) >= 2:
                omitted.append({'holding_ids': [holding], 'symbol': symbol, 'reason': 'symbol_limit_reached'})
                continue
            affected.add(holding)
            if holding not in selected.setdefault(symbol, []):
                selected[symbol].append(holding)
    return selected, omitted, identities, cutoff


def _extract(symbol, holding_ids, entry, identities, cutoff, fetched_at):
    if not isinstance(entry, Mapping) or not isinstance(entry.get('data'), list):
        return [], [{'symbol': symbol, 'reason': 'profile_missing_or_malformed'}]
    provenance = entry.get('_provenance') or {}
    if not isinstance(provenance, Mapping):
        provenance = {}
    source = str(provenance.get('source') or '')[:200]
    published = provenance.get('as_of') or provenance.get('published_at')
    gaps = ['secondary_profile_unverified']
    if not source:
        gaps.append('source_unknown')
    if not published:
        gaps.append('publication_time_unknown')
    else:
        try:
            stamp = date.fromisoformat(str(published)[:10])
            if cutoff and stamp.isoformat() > str(cutoff)[:10]:
                gaps.append('future_publication')
        except ValueError:
            gaps.append('invalid_publication_date')
    if provenance.get('stale') or provenance.get('cached'):
        gaps.append('stale_profile')
    if provenance.get('truncated') or entry.get('truncated') or len(entry['data']) > 100:
        gaps.append('truncated_profile')
    candidates = []
    for raw in entry['data'][:100]:
        if not isinstance(raw, Mapping):
            continue
        field = str(raw.get('item') or '').strip()
        kind = _FIELDS.get(field)
        text = str(raw.get('value') or '').strip()
        if not kind or not text:
            continue
        uncertainty = list(gaps)
        if len(text) > 2000 or text.endswith(('...', '…')):
            uncertainty.append('truncated_profile')
        text = text[:2000]
        codes = sorted(set(match.upper() for match in _INDEX.findall(text))) if kind == 'tracking_index' else []
        index = codes[0] if len(codes) == 1 else None
        if len(codes) > 1:
            uncertainty.append('ambiguous_target')
        if index is None:
            uncertainty.append('canonical_tracking_index_unconfirmed')
        if kind == 'performance_benchmark':
            uncertainty.append('benchmark_is_not_tracking_relationship')
        declaration = identities.get(symbol) or {}
        conflicts = []
        if index and declaration.get('tracking_index') and declaration['tracking_index'] != index:
            conflicts.append({'declared_tracking_index': declaration['tracking_index'],
                              'candidate_tracking_index': index, 'resolution': None})
            uncertainty.append('relationship_conflict')
        row = {'symbol': symbol, 'holding_ids': list(holding_ids), 'relationship_kind': kind,
               'claim_field': field, 'claim_text': text, 'tracking_index': index,
               'source': source or None, 'source_ref': provenance.get('url') or source or None,
               'published_at': published, 'as_of': published, 'fetched_at': fetched_at,
               'verification_status': 'DECLARED_CANDIDATE', 'verified': False,
               'execution_usable': False, 'conflicts': conflicts,
               'gaps': list(dict.fromkeys(uncertainty))}
        row['candidate_id'] = 'identity_candidate:' + content_sha256(row)[:24]
        candidates.append(row)
    targets = {row['claim_text'] for row in candidates if row['relationship_kind'] == 'tracking_index'}
    if len(targets) > 1:
        for row in candidates:
            if row['relationship_kind'] == 'tracking_index':
                row['conflicts'].append({'candidate_claims': sorted(targets), 'resolution': None})
                row['gaps'] = list(dict.fromkeys([*row['gaps'], 'relationship_conflict']))
    missing = [] if candidates else [{'symbol': symbol, 'reason': 'relationship_not_in_profile'}]
    return candidates, missing


def collect_identity_candidates(registry, payload, budget, *, max_seconds=10):
    """Read at most two unresolved fund codes using the shared actual-call budget.

    Pass the registered registry directly, not a second budget-debiting wrapper.
    Store the returned JSON in report/snapshot evidence, never in ``inputs`` or
    FundIdentityStore. No private account query or mapping promotion occurs.
    """
    if not isinstance(max_seconds, (int, float)) or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError('identity collection timeout must be finite and positive')
    selected, omitted, identities, cutoff = _targets(payload)
    result = {'candidates': [], 'gaps': omitted, 'coverage': [],
              'status': 'incomplete', 'method_version': 'identity-profile-discovery-v1'}

    def stop(reason):
        for symbol, holding_ids in selected.items():
            result['coverage'].append({'symbol': symbol, 'holding_ids': holding_ids, 'status': 'incomplete'})
            result['gaps'].append({'symbol': symbol, 'holding_ids': holding_ids, 'reason': reason})
        return result

    if not selected:
        result['status'] = 'not_needed' if not omitted else 'incomplete'
        return result
    tool = registry.get('get_fund_data')
    if not tool or getattr(tool, 'is_readonly', False) is not True:
        return stop('readonly_profile_unavailable')
    if budget.cancelled:
        return stop('cancelled')
    remaining = budget.remaining_seconds('source')
    if remaining <= 0:
        return stop('shared_budget_exhausted')
    deadline = time.monotonic() + min(float(max_seconds), 10.0, remaining)

    def dispatch():
        if time.monotonic() >= deadline or not budget.acquire_source():
            raise RuntimeError('shared_budget_exhausted')
        raw = registry.execute('get_fund_data', {'codes': list(selected), 'view': 'profile', 'no_cache': True})
        parsed = json.loads(raw)
        if not isinstance(parsed, Mapping) or parsed.get('status') == 'error' or parsed.get('ok') is False:
            raise ValueError('profile_source_failed')
        return parsed

    available = deadline - time.monotonic()
    if available <= 0:
        return stop('shared_budget_exhausted')
    completed, failures = bounded_collect({'profiles': dispatch}, timeout_seconds=available, max_workers=1)
    if budget.cancelled:
        return stop('cancelled')
    if failures:
        reason = 'shared_budget_exhausted' if failures.get('profiles') == 'shared_budget_exhausted' else 'profile_source_failed_or_timeout'
        return stop(reason)
    fetched = datetime.now(timezone.utc).isoformat()
    for symbol, holding_ids in selected.items():
        rows, gaps = _extract(symbol, holding_ids, completed['profiles'].get(symbol), identities, cutoff, fetched)
        result['candidates'].extend(rows)
        result['gaps'].extend({**gap, 'holding_ids': holding_ids} for gap in gaps)
        result['gaps'].extend({'symbol': symbol, 'holding_ids': holding_ids,
                               'candidate_id': row['candidate_id'], 'reason': reason}
                              for row in rows for reason in row['gaps'])
        result['coverage'].append({'symbol': symbol, 'holding_ids': holding_ids,
                                   'status': 'incomplete', 'candidate_count': len(rows),
                                   'source': 'get_fund_data:profile', 'fetched_at': fetched})
    return result
