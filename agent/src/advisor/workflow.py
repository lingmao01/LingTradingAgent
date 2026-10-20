"""Trusted orchestration: registered assessment -> durable snapshot -> delivery."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from src.advisor.assessment import assess_portfolio
from src.advisor.collection import bounded_collect
from src.advisor.decisions import decide
from src.advisor.delivery import render_advice, validate_delivery
from src.advisor.news import collect_news


def persist_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def prepare_assessment(registry, run_dir: Path, as_of: str, *, assessment_executor=None, request_context=None, research_budget=None) -> dict[str, Any]:
    """Run directories come from the loop, never a model-controlled argument."""
    try:
        execute = assessment_executor or registry.execute
        arguments = {'as_of': as_of}
        if request_context and request_context.get('scope_symbols') is not None:
            arguments['scope_symbols'] = request_context['scope_symbols']
        raw = execute('get_portfolio_assessment', arguments)
        payload = json.loads(raw)
        if payload.get('ok') is not True:
            raise ValueError(payload.get('error') or 'assessment unavailable')
        if not isinstance(payload.get('data'), dict) or not validate_delivery(payload['data'], decide(payload['data'])):
            raise ValueError('assessment contract was not satisfied')
    except Exception as exc:
        report = assess_portfolio({}, {}, as_of=as_of)
        report['account_warnings'] = [f'account assessment unavailable: {exc}']
        report['data_gaps'] = [{'scope': 'assessment', 'reason': str(exc)}]
        payload = {'ok': True, 'data': report, 'evidence': {}, 'collection_error': str(exc)}
    if request_context:
        payload['data']['request_context'] = request_context
        payload['data']['data_gaps'].extend({'scope': 'current_user_statement', 'reason': reason} for reason in request_context.get('gaps', []))
    from src.advisor.intelligence import initialize_assessment, research_setup, finish_research
    from src.advisor.research_budget import ResearchBudget
    from src.advisor.budget_scope import source_call
    from src.advisor.research_planner import collect_research_evidence
    budget = research_budget or ResearchBudget.from_limits()
    previous = None
    try:
        from src.advisor.journal import PublishedAdviceStore
        published = PublishedAdviceStore().list()
        previous = next((row['signal_assessments'] for row in published if row.get('signal_assessments')), None)
    except (ValueError, OSError, KeyError):
        pass
    context, capability, plan, initial = research_setup(payload, registry, budget)
    class Sources:
        def get(self, name): return registry.get(name)
        def execute(self, name, arguments):
            tool = registry.get(name)
            if name not in {'web_search','read_url','get_market_data','search_symbol','get_fund_data'} or not tool or not getattr(tool,'is_readonly',True):
                raise ValueError('research permits only readonly sources')
            if research_budget is None:
                if not budget.acquire_source(): raise RuntimeError('shared source budget exhausted')
                return registry.execute(name, arguments)
            return source_call(registry.execute, name, arguments)
    sources = Sources()
    themes = [row.get('name', '') for row in payload['data'].get('holdings', [])]
    from src.advisor.external import collect_external
    seeds = [{'url': 'https://www.bls.gov/news.release/cpi.nr0.htm', 'title': 'Consumer Price Index release', 'categories': ['全球宏观与流动性']}]
    completed, failed = bounded_collect({
        'outside': lambda: collect_external(sources, payload),
        'research': lambda: collect_research_evidence(plan, registry, initial=initial, budget=budget),
        'news': lambda: collect_news(sources, as_of=as_of, themes=themes, seeds=seeds),
    }, timeout_seconds=max(.01,min(50,budget.remaining_seconds('source'))), max_workers=3)
    payload['data']['news'] = completed.get('news') or {'primary': [], 'coverage': [], 'gaps': [{'scope': '外围原文', 'reason': failed.get('news', 'news unavailable')}]}
    if 'outside' in failed:
        payload['data']['external_gaps'] = [{'scope': '外围行情', 'reason': failed['outside']}]
    bundle = completed.get('research') or initial.extend(gaps=[{'scope':'candidate','reason':failed.get('research','research unavailable')}], coverage=[{'scope':'candidate','status':'incomplete'}])
    from src.advisor.identity_collection import collect_identity_candidates
    payload['data']['identity_candidate_collection'] = collect_identity_candidates(registry,payload,budget)
    initialize_assessment(payload, previous=previous)
    finish_research(payload,context,capability,plan,bundle,budget)
    persist_json(run_dir / 'artifacts/advisor_snapshot.json', payload)
    return payload


def deliver_assessment(payload: dict[str, Any], run_dir: Path, *, execution_status: str, model_error: str | None = None, research_playbook: str | None = None):
    report = payload['data']
    decision = decide(report)
    if report.get('assessment_schema_version') == 2:
        if not validate_delivery(report, decision): raise ValueError('v2 delivery differs from frozen inputs or reviewed evidence')
        from src.advisor.report_view import build_report_view
        view = build_report_view(report, report['signal_assessments'], report['action_assessments'])
        content = render_advice(view)
        report['advisor_report'] = view.to_dict()
    else:
        content = render_advice(report, decision, model_error=model_error)
    if research_playbook:
        from src.advisor.scheduled import verdict_tail
        content += '\n\n' + verdict_tail(report)
    partial = bool(report.get('expert_analysis', {}).get('status') != 'accepted' or model_error or report.get('news', {}).get('gaps') or report.get('external_gaps') or report.get('data_gaps') or report.get('account_warnings') or not report['portfolio'].get('valuation_exact') or any(row.get('quality') in {'stale', 'unknown'} or row.get('close') is None for row in report.get('regime', [])))
    status = {
        'execution_status': execution_status,
        'completion_status': 'partial' if partial else 'complete',
        'evidence_status': 'partial' if partial else 'verified',
        'actionability_status': 'limited' if decision['stance'] == 'LIMITED_JUDGMENT' else 'conditional',
        'release_path': 'structured_assessment',
        'expert_status': report.get('expert_analysis', {}).get('status', 'unavailable'),
    }
    if report.get('advisor_report'):
        status['advisor_report'] = report['advisor_report']
        status['research_budget'] = report.get('research_budget', {})
    persist_json(run_dir / 'artifacts/advisor_delivery.json', {'decision': decision, 'delivery': status, 'content': content, 'model_error': model_error})
    return content, status
