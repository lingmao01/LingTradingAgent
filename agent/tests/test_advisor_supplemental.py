"""Registered supplementary facts must reach the final personal report."""
import json

import pytest

from src.advisor.assessment import assess_portfolio
from src.advisor.decisions import decide
from src.advisor.delivery import render_advice

pytestmark = pytest.mark.unit


def _payload():
    account = {'positions': [{'symbol': '001236.OF', 'quantity': 100}]}
    report = assess_portfolio(account, {}, as_of='2026-10-16')
    report['news'] = {'primary': [], 'coverage': [], 'gaps': []}
    return {'data': report, 'evidence': {}, 'inputs': {'account': account, 'rules': [], 'discipline': {}, 'identities': {}, 'as_of': '2026-10-16'}}


def test_external_observations_are_subject_bound_and_render_native_units():
    from src.advisor.supplemental import absorb
    payload = _payload()
    raw = {'SPY.US': [{'trade_date': '2026-10-14', 'close': 400}, {'trade_date': '2026-10-15', 'close': 410}],
           'UNREQUESTED.US': [{'trade_date': '2026-10-15', 'close': 999}],
           '_provenance': {'SPY.US': {'source': 'yahoo', 'quote_currency': 'USD', 'finality': 'closed_session'}}}
    assert absorb(payload, 'get_market_data', {'codes': ['SPY.US']}, json.dumps(raw))
    external = payload['data']['external_market']
    assert len(external) == 1 and external[0]['symbol'] == 'SPY.US'
    assert external[0]['price_change_percent'] == pytest.approx(2.5)
    assert external[0]['currency'] == 'USD' and external[0]['as_of'] == '2026-10-15'
    text = render_advice(payload['data'], decide(payload['data']))
    assert 'SPY.US' in text and '410.00' in text and 'USD' in text
    assert 'UNREQUESTED' not in text


def test_later_recovered_fund_recomputes_the_same_frozen_account(tmp_path):
    from src.advisor.supplemental import absorb
    payload = _payload()
    previous = payload['data']['snapshot_sha256']
    raw = {'001236.OF': {'data': [{'date': '2026-10-15', 'unit_nav': 2}], '_provenance': {'source': 'eastmoney', 'caliber': 'nav', 'currency': 'CNY'}}}
    assert absorb(payload, 'get_fund_data', {'codes': ['001236.OF']}, json.dumps(raw))
    assert payload['data']['holdings'][0]['value'] == 200
    assert payload['data']['holdings'][0]['quantity'] == 100
    assert payload['data']['snapshot_sha256'] != previous


def test_search_snippet_and_future_or_unrequested_rows_cannot_update_the_snapshot():
    from src.advisor.supplemental import absorb
    payload = _payload()
    assert not absorb(payload, 'web_search', {}, json.dumps({'results': [{'title': '全部利好'}]}))
    assert not absorb(payload, 'get_market_data', {'codes': ['SPY.US']}, json.dumps({'SPY.US': [{'date': '2027-01-01', 'close': 999}]}))
    assert payload['data']['external_market'] == []


def test_us_venue_currency_is_labeled_as_venue_inference_when_provider_omits_it():
    from src.advisor.supplemental import absorb
    payload = _payload()
    assert absorb(payload, 'get_market_data', {'codes': ['SPY.US']}, json.dumps({'SPY.US': [{'date': '2026-10-15', 'close': 410}], '_provenance': {'SPY.US': {'source': 'sina'}}}))
    row = payload['data']['external_market'][0]
    assert row['currency'] == 'USD'
    assert row['currency_basis'] == 'canonical_us_venue'
