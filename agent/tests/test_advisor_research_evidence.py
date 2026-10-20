import pytest
pytestmark = pytest.mark.unit


def release(**kwargs):
    return {'subject': 'CN', 'metric': 'cpi', 'value': 2, 'source': 'official',
        'url': 'https://stats.gov.cn/release/1', 'origin_url': 'https://stats.gov.cn/release/1',
        'verified': True, 'published_at': '2026-10-15', 'reference_period': '2026-09',
        'fetched_at': '2026-10-19', 'kind': 'fact', **kwargs}


def test_reprints_share_origin_and_conflicting_calibers_are_not_averaged():
    from src.advisor.research_evidence import EvidenceBundle
    bundle = EvidenceBundle.build([release(), release(url='https://news/a'), release(url='https://news/b'),
        release(value=3, unit='percent')], as_of='2026-10-19')
    assert len({item.origin_group for item in bundle.items}) == 1
    assert bundle.independent_source_count == 1
    assert bundle.conflicts
    assert {item.value for item in bundle.items} == {2, 3}


def test_publication_and_vintage_prevent_future_revision_and_snippet_as_fact():
    from src.advisor.research_evidence import EvidenceBundle
    bundle = EvidenceBundle.build([release(revision_at='2026-10-20'),
        release(verified=False, evidence_quality='search_snippet'), release()], as_of='2026-10-19')
    assert len(bundle.usable_items) == 1
    assert {gap['reason'] for gap in bundle.gaps} >= {'future_vintage', 'unverified_discovery'}
    item = bundle.usable_items[0]
    assert item.reference_period != item.published_at != item.fetched_at


def test_frozen_payload_initial_evidence_and_extension_have_version_parent_chain():
    from src.advisor.research_evidence import EvidenceBundle
    payload = {'data': {'as_of': '2026-10-19', 'news': {'primary': [release()]}},
        'evidence': {'X': {'symbol': 'X', 'field': 'close', 'value': 10, 'as_of': '2026-10-16', 'source': 'exchange', 'quality': 'official'}}}
    initial = EvidenceBundle.from_payload(payload)
    extended = initial.extend([release(metric='credit')])
    assert len(initial.items) == 2
    assert extended.parent_hash == initial.bundle_hash
    assert extended.version == initial.version + 1
    assert extended.bundle_hash != initial.bundle_hash
    payload['data']['news']['primary'][0]['value'] = 99
    assert initial.items[0].value == 2


def test_bundle_roundtrip_is_immutable_and_rejects_hash_tampering():
    from src.advisor.research_evidence import EvidenceBundle
    initial = EvidenceBundle.build([release(value={'candidate':True})], as_of='2026-10-19')
    bundle = initial.extend([release(metric='other')])
    raw = bundle.to_dict()
    restored = EvidenceBundle.from_dict(raw)
    assert restored.bundle_hash == bundle.bundle_hash
    assert restored.parent_hash == initial.bundle_hash and restored.version == 2
    with pytest.raises(TypeError): restored.items[0].value['candidate'] = False
    raw['items'][0]['value']['candidate'] = False
    with pytest.raises(ValueError, match='hash'): EvidenceBundle.from_dict(raw)


def test_model_opinion_is_never_factual_evidence_and_invalid_dates_are_unusable():
    from src.advisor.research_evidence import EvidenceBundle
    bundle = EvidenceBundle.build([release(kind='inference'), release(published_at='not a date')], as_of='2026-10-19')
    assert not bundle.usable_items
