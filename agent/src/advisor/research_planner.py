"""Question-driven holding and exploration research through existing readonly tools."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import date
import json
import re
from typing import Mapping

from src.advisor.collection import bounded_collect
from src.advisor.news import primary_url, read_original
from src.advisor.research_budget import BudgetExhausted
from src.advisor.research_evidence import EvidenceBundle


@dataclass(frozen=True)
class ResearchQuestion:
    question_id: str
    scope: str
    proposition: str
    possible_decision: str
    support_query: str
    counter_query: str
    required_as_of: str | None
    priority: int
    stop_reason: str | None = None


@dataclass(frozen=True)
class ResearchPlan:
    questions: tuple[ResearchQuestion, ...]
    as_of: str | None = None
    max_supplement_rounds: int = 1
    stop_reason: str | None = None
    account_version: str | None = None

    def to_dict(self): return asdict(self)


def plan_research(context, assessment: Mapping, capabilities, budget) -> ResearchPlan:
    cutoff = assessment.get('as_of')
    # Strictly public identifiers; balances, free-text notes and identity never leave here.
    symbols = [str(row.get('symbol', '')) for row in context.holdings]
    symbols = [symbol for symbol in symbols if re.fullmatch(r'[A-Za-z0-9^=.-]{1,24}', symbol)][:6]
    subject = ' '.join(symbols) or '中国 ETF 主要资产'
    resolved = set(assessment.get('research_resolved_questions') or [])
    questions = (
        ResearchQuestion('holding-risk', 'holding', '持仓依据或风险是否发生足以改变增减仓判断的变化', '增仓/减仓/保持',
                         f'{cutoff or ""} {subject} 盈利 政策 实施 估值 公告',
                         f'{cutoff or ""} {subject} 风险 反证 费用 公告', cutoff, 100,
                         'resolved' if 'holding-risk' in resolved else None),
        ResearchQuestion('outside-opportunities', 'candidate', '持仓以外是否出现值得与原仓及现金比较的可行方向', '换仓/保持/留现金',
                         f'{cutoff or ""} 中国 ETF 行业 资产类别 基本面 估值 官方 公告',
                         f'{cutoff or ""} 中国 ETF 行业 风险 估值 已计价 公告', cutoff, 90,
                         'resolved' if 'outside-opportunities' in resolved else None),
    )
    stop = 'budget_exhausted' if budget.remaining_seconds('source') <= 0 else None
    return ResearchPlan(questions, cutoff, 1, stop, context.account_version)


class _DispatchRegistry:
    """Every real registry dispatch, including original-page followups, is charged."""
    def __init__(self, registry, budget): self.registry, self.budget = registry, budget
    def get(self, name): return self.registry.get(name)
    def execute(self, name, args):
        tool = self.registry.get(name)
        if name not in {'web_search', 'read_url', 'search_symbol', 'get_market_data', 'get_fund_data'} or not tool or not tool.is_readonly:
            raise ValueError('readonly research source unavailable')
        if not self.budget.acquire_source(): raise BudgetExhausted('shared source budget exhausted')
        return self.registry.execute(name, args)


def collect_research_evidence(plan: ResearchPlan, registry, *, initial: EvidenceBundle, budget) -> EvidenceBundle:
    coverage, gaps, rows = [], [], []
    if any(row.get('supplement_round') for row in initial.coverage):
        return initial.extend(gaps=[{'scope':'research', 'reason':'maximum_supplement_rounds_reached'}])
    proxy = _DispatchRegistry(registry, budget)
    tool = registry.get('web_search')
    if plan.stop_reason or not tool or not tool.is_readonly:
        return initial.extend(gaps=[{'scope':q.scope, 'question_id':q.question_id,
                                     'reason':plan.stop_reason or 'readonly_search_unavailable'} for q in plan.questions],
                              coverage=[{'scope':q.scope, 'question_id':q.question_id, 'status':'incomplete', 'supplement_round':1} for q in plan.questions])
    jobs = {}
    questions = {}
    for question in sorted(plan.questions, key=lambda q:-q.priority):
        if question.stop_reason == 'resolved' or any(c.get('question_id') == question.question_id and c.get('status') == 'complete' for c in initial.coverage):
            coverage.append({'scope':question.scope, 'question_id':question.question_id, 'status':'resolved', 'supplement_round':1})
            continue
        for side, query in [('support', question.support_query), ('counter', question.counter_query)]:
            key = question.question_id + ':' + side
            questions[key] = question
            def search(query=query):
                result = json.loads(proxy.execute('web_search', {'query':query, 'max_results':4}))
                if result.get('status') != 'ok': raise ValueError('search source failed')
                return result.get('results', [])
            jobs[key] = search
    remaining = budget.remaining_seconds('source')
    if jobs and remaining > 0:
        completed, failures = bounded_collect(jobs, timeout_seconds=min(20, remaining), max_workers=3)
    else:
        completed, failures = {}, dict.fromkeys(jobs, 'budget_exhausted')
    originals = {}
    candidate_discoveries = False
    for key, results in completed.items():
        question = questions[key]
        coverage.append({'scope':question.scope, 'question_id':question.question_id, 'side':key.split(':')[-1], 'status':'complete', 'supplement_round':1})
        for result in results:
            if not isinstance(result, Mapping): continue
            if question.scope == 'candidate': candidate_discoveries = True
            row = {**result, 'verified':False, 'evidence_quality':'search_snippet', 'subject':result.get('subject') or question.scope}
            rows.append(row)
            url = str(result.get('url') or '')
            if primary_url(url):
                original = originals.setdefault(url, {**result, 'url':url, 'categories':[]})
                if question.scope not in original['categories']: original['categories'].append(question.scope)
    for key in failures:
        question = questions[key]
        coverage.append({'scope':question.scope, 'question_id':question.question_id, 'side':key.split(':')[-1], 'status':'incomplete', 'supplement_round':1})
        gaps.append({'scope':question.scope, 'question_id':question.question_id, 'reason':'source_failed_or_budget_exhausted'})
    remaining = budget.remaining_seconds('source')
    reader = registry.get('read_url')
    if originals and reader and reader.is_readonly and remaining > 0 and plan.as_of:
        selected = list(originals.values())[:4]
        verified, failed = bounded_collect({row['url']:(lambda row=row:read_original(proxy, row, date.fromisoformat(plan.as_of))) for row in selected}, timeout_seconds=min(25, remaining), max_workers=4)
        for row in verified.values():
            rows.append(row)
            rows.extend({**row, **fact} for fact in row.get('facts', []))
            if 'candidate' in row.get('categories', []):
                # Only an explicitly mentioned asset/industry becomes a direction; a CPI
                # release or arbitrary article title is not an investment candidate.
                body = str(row.get('content') or '')
                directions = ('国债', '债券', '红利', '黄金', '半导体', '创新药', '医疗', '新能源', '人工智能', '原油', '房地产')
                for direction in directions:
                    if direction not in body: continue
                    rows.append({**row, 'subject':direction, 'metric':'candidate',
                                 'value':{'candidate':True, 'direction_only':True, 'name':direction,
                                          'why_now':'本轮原文明确提及此方向，尚待比较研究',
                                          'counter_evidence_checked':False, 'comparison':'cash'}})
        gaps.extend({'scope':'original', 'url':url, 'reason':'original_verification_failed'} for url in failed)
        if any('candidate' in originals[url].get('categories', ()) for url in failed):
            coverage.append({'scope':'candidate', 'status':'incomplete', 'supplement_round':1})
    elif originals:
        gaps.append({'scope':'candidate', 'reason':'original_reader_unavailable_or_budget_exhausted'})
        coverage.append({'scope':'candidate', 'status':'incomplete', 'supplement_round':1})
    if candidate_discoveries and not any(row.get('verified') for row in rows):
        coverage.append({'scope':'candidate', 'status':'incomplete', 'supplement_round':1})
        gaps.append({'scope':'candidate', 'reason':'discovery_without_original_verification'})
    gaps.extend({'scope':domain, 'reason':'UNKNOWN_no_verified_measure'} for domain in ('consensus', 'crowding', 'fund_flows', 'valuation'))
    return initial.extend(rows, coverage=coverage, gaps=gaps)
