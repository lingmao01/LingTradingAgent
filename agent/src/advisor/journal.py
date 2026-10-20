"""Validated publications, separate from legacy model-authored draft notes."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.advisor.delivery import validate_delivery
from src.advisor.evaluation import frozen_advice_scenario
from src.advisor.workflow import persist_json
from src.config.paths import get_runtime_root


@contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.lock').open('a+b') as handle:
        if __import__('os').name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class PublishedAdviceStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else get_runtime_root() / 'published_advice.json'

    def _read(self):
        if not self.path.exists():
            return {'version': 1, 'entries': []}
        payload = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or not isinstance(payload.get('entries'), list):
            raise ValueError('published advice store is unreadable; existing data preserved')
        return payload

    def publish(self, *, run_id: str, report: dict, decision: dict, content: str, delivery: dict, published_at=None):
        if not content.strip() or delivery.get('release_path') != 'structured_assessment' or not validate_delivery(report, decision):
            raise ValueError('only a validated, delivered advice report may be published')
        stamp = published_at or datetime.now(timezone.utc).isoformat()
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None:
            raise ValueError('publication timestamp must include timezone')
        identifier = hashlib.sha256((run_id + ':' + report['snapshot_sha256']).encode()).hexdigest()
        entry = {
            'id': identifier, 'run_id': run_id, 'published_at': stamp,
            'information_as_of': report['as_of'], 'snapshot_sha256': report['snapshot_sha256'],
            'research_sha256': report.get('news', {}).get('research_sha256'),
            'method_version': report['method_version'], 'decision': decision,
            'delivery': delivery, 'content_sha256': hashlib.sha256(content.encode()).hexdigest(),
            'execution_state': 'not_reported', 'realized_execution_performance': None,
            'review_protocol': {'version': 'paper-advice-v2', 'advice_scenario': frozen_advice_scenario(report, decision), 'observation_horizons': [5, 20],
                'review_after_calendar_days': 7, 'benchmark': '000300.SH',
                'benchmark_basis': 'descriptive comparison, not an inferred personal target',
                'assumed_cost_bps': None, 'cost_sensitivity_bps': [0, 50, 150], 'investment_horizon': report.get('personal_parameters', {}).get('investment_horizon'),
                'scope': 'paper comparisons; conditional advice is not an executed order'},
            'holdings': report['holdings'], 'portfolio': report['portfolio'], 'evaluations': [],
        }
        if report.get('assessment_schema_version') == 2:
            from dataclasses import replace
            from src.advisor.comparisons import ReviewedResearch
            from src.advisor.forecasts import publish_forecasts
            view = report.get('advisor_report') or {}
            entry.update({'schema_version': 2, 'signal_assessments': report.get('signal_assessments', []),
                'action_assessments': report.get('action_assessments', []), 'advisor_report': view,
                'view_sha256': view.get('view_sha256'), 'forecasts': [],
                'reviewed_research': report.get('reviewed_research')})
            if report.get('reviewed_research'):
                reviewed = ReviewedResearch.from_dict(report['reviewed_research'])
                bound = replace(reviewed, publication_binding={'publication_id':identifier,'committed':True,
                    'input_sha256':reviewed.input_sha256,'evidence_sha256':reviewed.evidence_sha256,
                    'body_sha256':entry['content_sha256'],'view_sha256':view.get('view_sha256')})
                entry['forecasts'] = [r.to_dict() for r in publish_forecasts(identifier,bound,published_at=stamp)]
        with _locked(self.path):
            payload = self._read()
            existing = next((row for row in payload['entries'] if row['id'] == identifier), None)
            if existing:
                if existing['content_sha256'] != entry['content_sha256']:
                    raise ValueError('the same run snapshot already published different content')
                return existing
            payload['entries'].append(entry)
            persist_json(self.path, payload)
        return entry

    def list(self, *, due_as_of: str | None = None):
        rows = self._read()['entries']
        if due_as_of:
            cutoff = date.fromisoformat(due_as_of)
            def due(row):
                cadence = timedelta(days=row['review_protocol']['review_after_calendar_days'])
                evaluations = row.get('evaluations') or []
                if evaluations:
                    latest = evaluations[-1]['data']
                    if not latest.get('unavailable_horizons') and latest.get('review_status') == 'complete':
                        return False
                    last = date.fromisoformat(latest['as_of'])
                else:
                    from src.market_observation import SHANGHAI
                    last = datetime.fromisoformat(row['published_at']).astimezone(SHANGHAI).date()
                return last + cadence <= cutoff
            rows = [row for row in rows if due(row)]
        return sorted(rows, key=lambda row: (row['published_at'], row['id']), reverse=True)

    def page(self, *, offset=0, limit=20, due_as_of=None):
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 100:
            raise ValueError('invalid journal pagination')
        rows = self.list(due_as_of=due_as_of)
        return {'entries': rows[offset:offset + limit], 'total': len(rows), 'offset': offset,
                'next_offset': offset + limit if offset + limit < len(rows) else None}

    def add_evaluation(self, entry_id: str, evaluation: dict[str, Any]):
        json.dumps(evaluation, allow_nan=False)
        with _locked(self.path):
            payload = self._read()
            row = next((row for row in payload['entries'] if row['id'] == entry_id), None)
            if row is None:
                raise ValueError('publication not found')
            digest = hashlib.sha256(json.dumps(evaluation, sort_keys=True).encode()).hexdigest()
            if not any(item.get('sha256') == digest for item in row.get('evaluations', [])):
                row.setdefault('evaluations', []).append({'sha256': digest, 'data': evaluation})
                persist_json(self.path, payload)
            return row
