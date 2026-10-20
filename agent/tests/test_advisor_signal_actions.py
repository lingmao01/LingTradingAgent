"""Direction, scope and execution dependencies are evaluated independently."""
from dataclasses import replace

import pytest

from tests.test_advisor_signals import sample

pytestmark = pytest.mark.unit


def setup(values=None):
    from src.advisor.models import AssessmentInputs
    from src.advisor.signals import RuleSet, assess_signals
    from src.advisor.applicability import resolve_applicability
    payload = sample(values)
    payload['inputs']['account'] = {'positions': [{'symbol': '000051.OF', 'quantity': None}], 'cash': {'amount': 100, 'currency': 'CNY', 'as_of': '2025-01-01'}}
    payload['inputs']['identities'] = {'000051.OF': {'tracking_index': '000300.SH', 'verified': True, 'source_ref': 'official', 'as_of': '2026-01-01'}}
    inputs = AssessmentInputs.from_payload(payload)
    rules = RuleSet.from_discipline(payload['inputs']['discipline'])
    return inputs, assess_signals(inputs, rules=rules), resolve_applicability(inputs.account, inputs.identities, rules)


def test_block_add_and_reduce_survive_together_and_missing_shares_only_blocks_sizing():
    from src.advisor.actions import assess_actions
    inputs, signals, mapping = setup()
    technical = [s for s in signals if s.subject['kind'] == 'index']
    signals = [replace(s, state='TRUE') if s.effect in {'BLOCK_ADD', 'REDUCE_TRIGGER'} else s for s in technical]
    rows = assess_actions(inputs, {}, signals, mapping)
    add = next(row for row in rows if row.action == 'ADD' and row.subject_ids == ('000051.OF',))
    reduce = next(row for row in rows if row.action == 'REDUCE' and row.subject_ids == ('000051.OF',))
    assert add.eligibility == 'BLOCKED'
    assert reduce.eligibility == 'ELIGIBLE' and reduce.sizing_status == 'INSUFFICIENT_DATA'
    assert 'QUANTITY_UNKNOWN' in reduce.blocked_by
    assert 'CASH_STALE' not in reduce.blocked_by


def test_hard_constraint_overrides_technical_add_and_unknown_never_becomes_maintain():
    from src.advisor.actions import assess_actions
    inputs, signals, mapping = setup()
    rows = assess_actions(inputs, {'clusters': [{'state': 'BREACHED', 'members': ['000051.OF']}]}, signals, mapping)
    add = next(row for row in rows if row.action == 'ADD' and row.subject_ids == ('000051.OF',))
    assert add.eligibility == 'BLOCKED'
    unknown = assess_actions(inputs, {}, [replace(row, state='UNKNOWN') for row in signals], mapping)
    assert not any(row.action == 'MAINTAIN' or row.eligibility == 'ELIGIBLE' for row in unknown)


def test_research_reduce_has_independent_origin_and_rotation_requires_two_sides():
    from src.advisor.actions import ActionProposal, assess_actions
    from src.advisor.models import ResearchSubject
    inputs, signals, mapping = setup()
    proposals = [ActionProposal(id='research-1', action='REDUCE', subject_ids=('000051.OF',), evidence_refs=('evidence:1',)), ActionProposal(id='rotation-1', action='ROTATE', subject_ids=('000051.OF', '000052.OF'), comparison_id='compare:1', evidence_refs=('evidence:1',))]
    subjects = {'000051.OF': ResearchSubject(id='000051.OF', kind='fund', canonical_symbol='000051.OF', name='原仓', availability='AVAILABLE'), '000052.OF': ResearchSubject(id='000052.OF', kind='fund', canonical_symbol='000052.OF', name='候选', availability='AVAILABLE')}
    rows = assess_actions(inputs, {}, signals, mapping, proposals=proposals, subjects=subjects)
    research = next(row for row in rows if row.proposal_id == 'research-1')
    assert research.origin == 'RESEARCH_JUDGMENT' and research.eligibility == 'ELIGIBLE'
    rotation = next(row for row in rows if row.proposal_id == 'rotation-1')
    assert rotation.eligibility == 'UNDETERMINED'
    assert rotation.rotation.status == 'INSUFFICIENT_EVIDENCE'


def test_all_index_account_policy_does_not_turn_one_positive_index_into_permission():
    from src.advisor.actions import assess_actions
    from src.advisor.applicability import resolve_applicability
    from src.advisor.signals import RuleSet
    inputs, signals, _ = setup()
    permit = next(row for row in signals if row.effect == 'PERMIT_ADD')
    other = replace(permit, signal_id='signal:other', subject={'id': '000688.SH', 'kind': 'index'}, state='FALSE')
    rules = RuleSet.from_discipline({'regime': {'indices': ['000300.SH', '000688.SH']}, 'scope_policy': {'kind': 'account', 'indices': ['000300.SH', '000688.SH'], 'aggregation': 'all'}})
    mapping = resolve_applicability(inputs.account, {}, rules)
    rows = assess_actions(inputs, {}, [permit, other], mapping)
    assert next(row for row in rows if row.subject_ids == ('account',) and row.action == 'ADD').eligibility == 'BLOCKED'


def test_pending_redemptions_are_reserved_and_fees_do_not_erase_reduce():
    from src.advisor.actions import assess_actions
    inputs, signals, mapping = setup()
    inputs = replace(inputs, account={**inputs.account, 'positions': [{'symbol': '000051.OF', 'quantity': 10}], 'pending_orders': [{'symbol': '000051.OF', 'action': 'redeem', 'state': 'pending', 'quantity': 8}]}, input_sha256='')
    signals = [replace(row, state='TRUE' if row.effect == 'REDUCE_TRIGGER' else 'FALSE') for row in signals]
    row = next(row for row in assess_actions(inputs, {}, signals, mapping) if row.action == 'REDUCE' and row.subject_ids == ('000051.OF',))
    assert row.eligibility == 'ELIGIBLE'
    quantity = next(check for check in row.execution_checks if check['check_id'] == 'quantity')
    assert quantity['available_quantity'] == 2
    assert 'REDEMPTION_FEES_UNKNOWN' in row.blocked_by


def test_different_fund_block_does_not_contaminate_positive_fund():
    from src.advisor.actions import assess_actions
    from src.advisor.applicability import resolve_applicability
    from src.advisor.signals import RuleSet
    inputs, signals, _ = setup()
    inputs = replace(inputs, account={**inputs.account, 'positions': [{'symbol': '000051.OF'}, {'symbol': '000052.OF'}]}, identities={**inputs.identities, '000052.OF': {'tracking_index': '000688.SH', 'verified': True, 'source_ref': 'official', 'as_of': '2026-01-01'}}, input_sha256='')
    permit = next(row for row in signals if row.effect == 'PERMIT_ADD')
    block = replace(permit, effect='BLOCK_ADD', subject={'id': '000688.SH', 'kind': 'index'})
    mapping = resolve_applicability(inputs.account, inputs.identities, RuleSet.from_discipline({}))
    rows = assess_actions(inputs, {}, [permit, block], mapping)
    assert next(row for row in rows if row.subject_ids == ('000051.OF',) and row.action == 'ADD').eligibility == 'ELIGIBLE'
    assert next(row for row in rows if row.subject_ids == ('000052.OF',) and row.action == 'ADD').eligibility == 'BLOCKED'


def test_unheld_candidate_cannot_be_reduced_and_unknown_mapping_cannot_bypass_add_gate():
    from src.advisor.actions import ActionProposal, assess_actions
    from src.advisor.applicability import resolve_applicability
    from src.advisor.models import ResearchSubject
    from src.advisor.signals import RuleSet
    inputs, signals, _ = setup()
    mapping = resolve_applicability(inputs.account, {}, RuleSet.from_discipline({}))
    subjects = {'000052.OF': ResearchSubject('000052.OF', 'fund', '000052.OF', '候选', 'AVAILABLE')}
    proposals = [ActionProposal('unheld-reduce', 'REDUCE', ('000052.OF',), evidence_refs=('e1',)), ActionProposal('unmapped-add', 'ADD', ('000051.OF',), evidence_refs=('e1',))]
    rows = assess_actions(inputs, {}, signals, mapping, proposals=proposals, subjects=subjects)
    assert next(row for row in rows if row.proposal_id == 'unheld-reduce').eligibility == 'NOT_APPLICABLE'
    assert next(row for row in rows if row.proposal_id == 'unmapped-add').eligibility == 'UNDETERMINED'


def test_conditional_concentration_is_not_an_exact_breach():
    from src.advisor.actions import assess_actions
    inputs, signals, mapping = setup()
    cluster = {'state': 'UNKNOWN', 'conditional_state': 'OVER_CAP', 'members': ['000051.OF']}
    row = next(row for row in assess_actions(inputs, {'clusters': [cluster]}, signals, mapping) if row.action == 'ADD' and row.subject_ids == ('000051.OF',))
    assert row.eligibility == 'UNDETERMINED'
    assert 'CLUSTER_CAP_REACHED' not in row.blocked_by


def test_rotation_funding_mismatch_and_duplicate_exposure_remain_unsupported():
    from src.advisor.actions import ActionProposal, assess_actions
    from src.advisor.models import ResearchSubject
    inputs, signals, mapping = setup()
    subjects = {symbol: ResearchSubject(symbol, 'fund', symbol, symbol, 'AVAILABLE') for symbol in ['000051.OF', '000052.OF']}
    proposal = ActionProposal('rotate', 'ROTATE', tuple(subjects), evidence_refs=('e1', 'e2'), comparison_id='comparison:1', parameters={'outbound_evidence_refs': ['e1'], 'inbound_evidence_refs': ['e2'], 'alternatives': ['retain', 'cash'], 'relative_advantage': 'less concentration', 'fees': {'outbound': 0, 'inbound': 0}, 'funding_timing': {'redemption_confirmed_at': '2026-03-27', 'funds_available_at': '2026-03-31', 'subscription_at': '2026-03-30'}, 'exposure_change': {'duplicate_exposure': True}, 'transaction_feasible': True})
    row = next(row for row in assess_actions(inputs, {}, signals, mapping, proposals=[proposal], subjects=subjects) if row.action == 'ROTATE')
    assert row.rotation.status == 'NOT_SUPPORTED'
    assert {'FUNDING_TIMING_MISMATCH', 'DUPLICATE_EXPOSURE_WITHOUT_ADVANTAGE'} <= set(row.rotation.reason_codes)
