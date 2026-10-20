"""Frozen-input adapter joining deterministic signals and bounded research."""
from __future__ import annotations
from dataclasses import replace
from src.advisor.models import AssessmentInputs, ResearchSubject
from src.advisor.signals import RuleSet, assess_signals
from src.advisor.applicability import resolve_applicability
from src.advisor.actions import assess_actions, ActionProposal
from src.advisor.report_view import serial
from src.advisor.signal_history import compare_signals


def frozen_inputs(payload):
    return AssessmentInputs.from_payload(payload)

def initialize_assessment(payload, *, previous=None):
    report=payload['data']; inputs=frozen_inputs(payload)
    rules=RuleSet.from_discipline(inputs.discipline,account_rules=inputs.account_rules)
    applicability=resolve_applicability({**inputs.account,'analysis_as_of':inputs.as_of},inputs.identities,rules)
    signals=assess_signals(inputs,rules=rules)
    # Technical states remain visible without verified fund/index relationships.
    applied=[]
    for signal in signals:
        entries=applicability.for_subject(signal.subject.get('id',''))
        resolved=[r for r in entries if r.status=='RESOLVED']
        applied.append(replace(signal,applicability='RESOLVED' if resolved else signal.applicability,
            holding_ids=tuple(sorted({h for r in resolved for h in r.holding_ids})),
            mapping_refs=tuple(sorted({ref for r in resolved for ref in r.mapping_refs}))))
    signals=compare_signals(applied,previous)
    actions=assess_actions(inputs,report,signals,applicability)
    import copy
    report['assessment_frozen_payload'] = copy.deepcopy({'inputs': payload.get('inputs', {}), 'evidence': payload.get('evidence', {}), 'data': {'as_of': report['as_of']}})
    report.update({'assessment_schema_version':2,'assessment_input_sha256':inputs.input_sha256,
        'signal_assessments':serial(signals),'action_assessments':serial(actions),
        'rule_action_assessments':serial(actions),'applicability':applicability.to_dict(),
        'rule_version':rules.version})
    subjects={'account':ResearchSubject('account','account','','账户','UNKNOWN'),
              'cash':ResearchSubject('cash','cash','','留现金','UNKNOWN')}
    for row in report.get('holdings',[]):
        sid=row['id'];subjects[sid]=ResearchSubject(sid,'fund',row.get('symbol') or '',row.get('name') or sid,'UNKNOWN')
    for signal in signals:
        row=signal.subject;sid=row['id']
        subjects.setdefault(sid,ResearchSubject(sid,row.get('kind','index'),row.get('symbol') or sid,row.get('name') or sid,'UNKNOWN'))
    report['research_subjects']=serial(subjects)
    return inputs,signals,actions,applicability


def research_setup(payload, registry, budget):
    from src.advisor.research_context import build_personal_context,build_capability_map
    from src.advisor.research_evidence import EvidenceBundle
    from src.advisor.research_planner import plan_research
    raw=payload.get('inputs') or {};report=payload['data']
    context=build_personal_context(raw.get('account') or {},raw.get('discipline') or {},report.get('request_context',{}).get('personal_statements',[]))
    capability=build_capability_map(registry)
    plan=plan_research(context,report,capability,budget)
    return context,capability,plan,EvidenceBundle.from_payload(payload)


def finish_research(payload,context,capability,plan,bundle,budget):
    from src.advisor.opportunities import screen_opportunities
    from src.advisor.research_evidence import EvidenceBundle
    report=payload['data']
    # Add healthy primary news gathered in parallel without losing the supplement lineage.
    extra=EvidenceBundle.from_payload(payload)
    bundle=bundle.extend(extra.items,coverage=extra.coverage,gaps=extra.gaps)
    opportunities=screen_opportunities(context,bundle,capability,budget=budget)
    report.update({'personal_context':context.to_dict(),'capabilities':capability.to_dict(),
        'research_plan':plan.to_dict(),'research_evidence':bundle.to_dict(),
        'opportunity_scan':opportunities.to_dict(),'research_budget':budget.receipt()})
    report['research_subjects'].update(serial(opportunities.subjects))


def reassess_reviewed(payload):
    from src.advisor.comparisons import ReviewedResearch
    report=payload['data'];reviewed=ReviewedResearch.from_dict(report['reviewed_research'])
    inputs=frozen_inputs(payload)
    if reviewed.input_sha256!=inputs.input_sha256: raise ValueError('research input binding differs')
    rules=RuleSet.from_discipline(inputs.discipline,account_rules=inputs.account_rules)
    applicability=resolve_applicability({**inputs.account,'analysis_as_of':inputs.as_of},inputs.identities,rules)
    signals=assess_signals(inputs,rules=rules)
    subjects={k:ResearchSubject(**v) for k,v in report.get('research_subjects',{}).items()}
    comparisons={r.id:r.to_dict() for r in reviewed.candidates.comparisons if r.id in reviewed.accepted_comparison_ids}
    proposals=[]
    for p in reviewed.candidates.proposals:
        if p.id not in reviewed.accepted_proposal_ids: continue
        # Comparison is provenance, never a hard user rule.
        params=dict(p.parameters)
        if p.comparison_id in comparisons:params.setdefault('comparison',comparisons[p.comparison_id])
        proposals.append(replace(p,parameters=params))
    report['action_assessments']=serial(assess_actions(inputs,report,signals,applicability,proposals=proposals,subjects=subjects))
