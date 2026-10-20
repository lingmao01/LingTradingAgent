"""Declared, verified and conflicting relationships stay distinct."""
import pytest

pytestmark = pytest.mark.unit


def resolve(identities, account=None, discipline=None):
    from src.advisor.applicability import resolve_applicability
    from src.advisor.signals import RuleSet
    return resolve_applicability(account or {'positions': [{'symbol': '000051.OF'}]}, identities, RuleSet.from_discipline(discipline or {'regime': {'indices': ['000300.SH']}}))


def test_verified_relation_resolves_but_declaration_and_missing_do_not():
    relation = {'tracking_index': '000300.SH', 'verified': True, 'source_ref': 'https://fund.example/prospectus', 'as_of': '2026-03-01'}
    known = resolve({'000051.OF': relation})
    assert known.for_subject('000300.SH')[0].status == 'RESOLVED'
    assert known.aggregation_status == 'AGGREGATION_UNDEFINED'
    relation['verified'] = False
    declared = resolve({'000051.OF': relation}).for_subject('000300.SH')[0]
    assert declared.status == 'UNRESOLVED' and 'DECLARED_ONLY' in declared.reason_codes
    assert resolve({}).for_subject('000051.OF')[0].status == 'UNRESOLVED'


def test_conflicts_and_group_candidate_disagreement_never_resolve_arbitrarily():
    identities = {'000051.OF': {'tracking_index': '000300.SH', 'verified': True, 'source_ref': 'official', 'as_of': '2026-03-01', 'conflicts': ['another index']}}
    assert resolve(identities).for_subject('000300.SH')[0].status == 'UNRESOLVED'
    identities['000051.OF'].pop('conflicts')
    identities['000052.OF'] = {**identities['000051.OF'], 'tracking_index': '000688.SH'}
    mapping = resolve(identities, {'positions': [], 'holding_groups': [{'id': 'merged', 'candidate_symbols': ['000051.OF', '000052.OF']}]})
    assert all(row.status == 'UNRESOLVED' for row in mapping.entries)
    assert any('CANDIDATE_MAPPING_CONFLICT' in row.reason_codes for row in mapping.entries)


def test_market_reference_cannot_grant_portfolio_permission_or_expand_subportfolio():
    mapping = resolve({}, {'analysis_scope': 'requested_subportfolio', 'positions': [{'symbol': '000051.OF'}]}, {'regime': {'indices': ['000300.SH']}, 'scope_policy': {'kind': 'account', 'indices': ['000300.SH'], 'aggregation': 'all'}})
    assert not any(row.status == 'RESOLVED' and row.scope == 'account' for row in mapping.entries)


def test_legacy_group_id_and_expired_or_future_mapping_preserve_unresolved_scope():
    identity = {'tracking_index': '000300.SH', 'verified': True, 'source_ref': 'official', 'as_of': '2026-01-01', 'effective_until': '2026-02-01'}
    mapping = resolve({'000051.OF': identity}, {'as_of': '2026-03-01', 'positions': [{'symbol': '000051.OF'}]})
    assert mapping.for_subject('000051.OF')[0].status == 'UNRESOLVED'
    identity.pop('effective_until')
    mapping = resolve({'000051.OF': identity, '000052.OF': identity}, {'holding_groups': [{'group_id': 'legacy-group', 'candidate_symbols': ['000051.OF', '000052.OF']}]})
    assert mapping.for_subject('000300.SH')[0].holding_ids == ('legacy-group',)
