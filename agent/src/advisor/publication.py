"""Stage validated reports; commit only after durable user-visible delivery."""
import hashlib
import json
from pathlib import Path

from src.advisor.journal import PublishedAdviceStore
from src.advisor.workflow import persist_json


def stage_publication(run_dir, journal_path, report, decision, content, delivery):
    persist_json(Path(run_dir) / 'artifacts/advisor_publication.json', {
        'journal_path': str(Path(journal_path).resolve()), 'report': report, 'decision': decision,
        'content_sha256': hashlib.sha256(content.encode()).hexdigest(), 'delivery': delivery,
    })


def finalize_publication(run_dir, content, *, published_at=None):
    path = Path(run_dir) / 'artifacts/advisor_publication.json'
    if not path.exists():
        return None
    staged = json.loads(path.read_text(encoding='utf-8'))
    if hashlib.sha256(content.encode()).hexdigest() != staged['content_sha256']:
        raise ValueError('the committed response differs from the staged advice')
    entry = PublishedAdviceStore(staged['journal_path']).publish(
        run_id=Path(run_dir).name, report=staged['report'], decision=staged['decision'],
        content=content, delivery=staged['delivery'], published_at=published_at)
    persist_json(Path(run_dir) / 'artifacts/advisor_publication_receipt.json', {'id': entry['id'], 'published_at': entry['published_at'], 'content_sha256': entry['content_sha256'], 'view_sha256': entry.get('view_sha256'), 'forecast_ids':[row['id'] for row in entry.get('forecasts', [])]})
    return entry['id']
