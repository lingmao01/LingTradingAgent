"""Independent adversarial foundation review; no user profile or source I/O."""
from dataclasses import replace

import pytest

pytestmark = pytest.mark.unit


def test_unknown_recorded_add_blocker_cannot_be_overridden_by_positive_permission():
    from tests.test_advisor_signal_actions import setup
    from src.advisor.actions import assess_actions

    inputs, signals, mapping = setup()
    permit = next(s for s in signals if s.effect == 'PERMIT_ADD' and s.subject['id'] == '000300.SH')
    block = replace(permit, effect='BLOCK_ADD', signal_id='unknown-block', state='UNKNOWN')
    rows = assess_actions(inputs, {}, [replace(permit, state='TRUE'), block], mapping)
    action = next(r for r in rows if r.action == 'ADD' and r.subject_ids == ('000051.OF',))
    assert action.eligibility == 'UNDETERMINED'


def test_position_id_alias_preserves_direct_fund_hard_risk_for_index_permission():
    from tests.test_advisor_signal_actions import setup
    from src.advisor.actions import assess_actions
    from src.advisor.applicability import resolve_applicability
    from src.advisor.signals import RuleSet, assess_signals

    inputs, signals, _ = setup()
    inputs = replace(inputs, account={**inputs.account, 'positions': [
        {'id': 'holding-1', 'symbol': '000051.OF', 'quantity': 10}]}, input_sha256='')
    rules = RuleSet.from_discipline({}, account_rules=[{'symbol': '000051.OF', 'max_quantity': 5}])
    direct = assess_signals(inputs, rules=rules)
    permit = next(s for s in signals if s.effect == 'PERMIT_ADD' and s.subject['id'] == '000300.SH')
    rows = assess_actions(inputs, {}, [replace(permit, state='TRUE'), *direct],
                          resolve_applicability(inputs.account, inputs.identities, rules))
    action = next(r for r in rows if r.action == 'ADD' and r.subject_ids == ('holding-1',))
    assert action.eligibility == 'BLOCKED'


def test_group_mapping_cannot_use_a_future_verified_relationship():
    from src.advisor.applicability import resolve_applicability
    from src.advisor.signals import RuleSet

    account = {'as_of': '2026-10-19', 'holding_groups': [
        {'id': 'g1', 'candidate_symbols': ['000051.OF']}]}
    identities = {'000051.OF': {'tracking_index': '000300.SH', 'verified': True,
                               'source': 'official', 'as_of': '2030-01-01'}}
    mapping = resolve_applicability(account, identities, RuleSet.from_discipline({}))
    assert mapping.for_subject('g1')[0].status == 'UNRESOLVED'


def test_parsed_add_amount_cannot_spend_through_recorded_cash_floor():
    from tests.test_advisor_comparative_reasoning import research_fixture
    from src.advisor.actions import assess_actions
    from src.advisor.applicability import resolve_applicability
    from src.advisor.comparisons import parse_candidates
    from src.advisor.models import AssessmentInputs
    from src.advisor.signals import RuleSet, assess_signals

    _, bundle, subjects, comparison = research_fixture()
    subjects['outside']['availability'] = 'AVAILABLE'
    inputs = AssessmentInputs(account={'positions': [{'symbol': 'holding'}],
        'cash': {'amount': 100, 'currency': 'CNY', 'as_of': '2026-10-19'},
        'minimum_cash': 50}, observations={}, identities={}, as_of='2026-10-19')
    document = {'comparisons': [comparison], 'proposals': [{
        'id': 'spend-floor', 'action': 'ADD', 'subject_ids': ['outside'],
        'origin': 'RESEARCH_JUDGMENT', 'evidence_refs': ['support'], 'condition_refs': [],
        'comparison_id': 'cmp1', 'parameters': {'amount': 90}}]}
    candidates = parse_candidates(document, inputs=inputs, bundle=bundle, subjects=subjects)
    assert candidates.rejected == ()
    assert candidates.proposals[0].parameters['amount'] == 90
    rules = RuleSet.from_discipline({})
    rows = assess_actions(inputs, {}, assess_signals(inputs, rules=rules),
                          resolve_applicability(inputs.account, {}, rules),
                          proposals=candidates.proposals, subjects=subjects)
    action = next(r for r in rows if r.proposal_id == 'spend-floor')
    assert action.eligibility == 'BLOCKED' or action.sizing_status == 'BLOCKED'


def test_conflicting_candidate_records_cannot_first_win_into_comparison_ready():
    from tests.test_advisor_opportunity_scan import candidate, bundle_rows
    from src.advisor.research_budget import ResearchBudget
    from src.advisor.research_context import build_personal_context, CapabilityMap
    from src.advisor.opportunities import screen_opportunities

    bundle = bundle_rows([candidate(), candidate(identity_conflict=True)])
    assert bundle.conflicts
    context = build_personal_context({'personal_context': {'market_access': ['US']}}, {}, [])
    scan = screen_opportunities(context, bundle, CapabilityMap(), budget=ResearchBudget.from_limits())
    assert scan.candidates[0].maturity != 'COMPARISON_READY'


@pytest.mark.parametrize('operation', ['build', 'extend'])
def test_prebuilt_evidence_revalidates_vintage_against_destination_bundle(operation):
    from src.advisor.research_evidence import EvidenceItem, EvidenceBundle

    item = EvidenceItem.parse({'subject': 'X', 'value': 1, 'source': 'official',
        'verified': True, 'published_at': '2030-01-01'}, as_of='2030-01-01')
    assert item.usable
    bundle = (EvidenceBundle.build([item], as_of='2026-10-19') if operation == 'build'
              else EvidenceBundle.build([], as_of='2026-10-19').extend([item]))
    assert not bundle.usable_items


def test_valid_bundle_roundtrip_preserves_hash_ids_and_parent_chain():
    from src.advisor.research_evidence import EvidenceBundle

    row = {'subject': 'X', 'value': 1, 'source': 'official', 'verified': True,
           'published_at': '2026-10-18', 'revision_at': '2026-10-19'}
    original = EvidenceBundle.build([row], as_of='2026-10-19')
    extended = original.extend([{**row, 'subject': 'Y'}])
    restored = EvidenceBundle.from_dict(extended.to_dict())
    assert restored.bundle_hash == extended.bundle_hash
    assert restored.parent_hash == original.bundle_hash
    assert restored.version == 2
    assert [item.evidence_id for item in restored.items] == [item.evidence_id for item in extended.items]
    assert len(restored.usable_items) == 2


@pytest.mark.parametrize('change', ['future_vintage', 'unusable'])
def test_nested_common_risk_evidence_obeys_same_vintage_and_usability_contract(change):
    from tests.test_advisor_comparative_reasoning import research_fixture
    from src.advisor.comparisons import parse_candidates

    inputs, bundle, subjects, row = research_fixture()
    bundle.items['risk-only'] = {'id': 'risk-only', 'as_of': '2026-10-18',
                                'usable': True, 'verified': True, 'text': 'holdings disclosure'}
    if change == 'future_vintage':
        bundle.items['risk-only']['as_of'] = '2030-01-01'
    else:
        bundle.items['risk-only']['usable'] = False
    row['risk_analysis'] = [{'kind': 'DISCLOSED_OVERLAP', 'evidence_refs': ['risk-only'],
                            'explanation': 'disclosed holdings overlap',
                            'disclosure_as_of': '2026-10-18', 'as_of': '2026-09-30'}]
    parsed = parse_candidates({'comparisons': [row]}, inputs=inputs, bundle=bundle, subjects=subjects)
    assert not parsed.comparisons


def test_holding_id_cannot_bypass_cluster_cap_recorded_with_fund_symbol():
    from tests.test_advisor_signal_actions import setup
    from src.advisor.actions import assess_actions
    from src.advisor.applicability import resolve_applicability
    from src.advisor.signals import RuleSet

    inputs, signals, _ = setup()
    inputs = replace(inputs, account={**inputs.account, 'positions': [
        {'id': 'holding-1', 'symbol': '000051.OF', 'quantity': 10}]}, input_sha256='')
    permit = next(s for s in signals if s.effect == 'PERMIT_ADD' and s.subject['id'] == '000300.SH')
    mapping = resolve_applicability(inputs.account, inputs.identities, RuleSet.from_discipline({}))
    rows = assess_actions(inputs, {'clusters': [{'state': 'BREACHED', 'members': ['000051.OF']}]},
                          [replace(permit, state='TRUE')], mapping)
    action = next(r for r in rows if r.action == 'ADD' and r.subject_ids == ('holding-1',))
    assert action.eligibility == 'BLOCKED'
    assert 'CLUSTER_CAP_REACHED' in action.blocked_by


@pytest.mark.parametrize('proposal_subject', ['holding-1', '000051.OF'])
def test_fund_symbol_block_applies_to_both_position_proposal_aliases(proposal_subject):
    from tests.test_advisor_signal_actions import setup
    from src.advisor.actions import ActionProposal, assess_actions
    from src.advisor.applicability import resolve_applicability
    from src.advisor.signals import RuleSet

    inputs, signals, _ = setup()
    inputs = replace(inputs, account={**inputs.account, 'positions': [
        {'id': 'holding-1', 'symbol': '000051.OF'}]}, input_sha256='')
    permit = next(s for s in signals if s.effect == 'PERMIT_ADD' and s.subject['id'] == '000300.SH')
    block = replace(permit, effect='BLOCK_ADD', signal_id='fund-block', state='TRUE',
                    subject={'id': '000051.OF', 'kind': 'fund'})
    proposal = ActionProposal('alias-proposal', 'ADD', (proposal_subject,), evidence_refs=('e1',))
    mapping = resolve_applicability(inputs.account, inputs.identities, RuleSet.from_discipline({}))
    rows = assess_actions(inputs, {}, [replace(permit, state='TRUE'), block], mapping, proposals=[proposal])
    action = next(r for r in rows if r.proposal_id == 'alias-proposal')
    assert action.eligibility == 'BLOCKED'
    assert 'fund-block' in action.signal_refs
