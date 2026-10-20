"""Hand-calculated fund-account assessments, including partial evidence."""
from __future__ import annotations

import pandas as pd
import pytest

pytestmark = pytest.mark.unit


def _obs(symbol, values, *, day="2026-10-15", field="unit_nav", quality="current"):
    dates = pd.bdate_range(end=day, periods=len(values))
    return {
        "symbol": symbol, "field": field, "value": values[-1], "as_of": day,
        "source": "fixture-provider", "currency": "CNY", "quality": quality,
        "history": [{"date": stamp.strftime("%Y-%m-%d"), "value": value} for stamp, value in zip(dates, values)],
    }


def _assess(account, observations, **kwargs):
    from src.advisor.assessment import assess_portfolio
    return assess_portfolio(account, observations, as_of="2026-10-15", **kwargs)


def test_nav_marking_and_pending_reservations_are_computed_from_the_account():
    account = {"positions": [
        {"symbol": "001236.OF", "quantity": 100, "name": "AI联接"},
        {"symbol": "001237.OF", "quantity": 200, "name": "宽基联接"},
    ], "cash": {"amount": 100, "currency": "CNY", "as_of": "2026-10-15"}, "pending_orders": [
        {"symbol": "001236.OF", "action": "redeem", "quantity": 25, "state": "pending"},
        {"symbol": "001237.OF", "action": "subscribe", "amount": 80, "state": "pending"},
    ]}
    report = _assess(account, {"001236.OF": _obs("001236.OF", [2]), "001237.OF": _obs("001237.OF", [1])})
    assert report["portfolio"]["fund_value"] == 400
    assert report["portfolio"]["account_value"] == 500
    assert report["portfolio"]["available_cash"] == 20
    first = report["holdings"][0]
    assert first["value"] == 200 and first["fund_weight"] == .5
    assert first["account_weight"] == .4 and first["available_quantity"] == 75
    assert first["valuation_as_of"] == "2026-10-15"


def test_unknown_holding_is_neither_zero_nor_a_reweighted_full_portfolio():
    report = _assess({"positions": [
        {"symbol": "001236.OF", "quantity": 100}, {"symbol": "001237.OF", "quantity": 200},
    ]}, {"001236.OF": _obs("001236.OF", [2])})
    assert report["portfolio"]["coverage_complete"] is False
    assert report["portfolio"]["fund_value"] is None
    assert report["holdings"][0]["value"] == 200
    assert report["holdings"][0]["fund_weight"] is None
    assert report["holdings"][1]["value"] is None
    assert report["risk"]["full_portfolio"] is None
    assert any("NAV" in gap for gap in report["holdings"][1]["gaps"])


def test_reported_amounts_produce_conditional_ranges_without_invented_share_counts():
    account = {
        "positions": [{"symbol": "001236.OF", "quantity": None, "reported_value": 500, "value_as_of": "2026-10-14"}],
        "holding_groups": [{"group_id": "combined", "candidate_symbols": ["001234.OF", "001235.OF"], "reported_value": 1000}],
        "valuation_date_range": ["2026-10-14", "2026-10-14"],
    }
    report = _assess(account, {
        "001236.OF": _obs("001236.OF", [1, 1.1]),
        "001234.OF": _obs("001234.OF", [1, 1.2]),
        "001235.OF": _obs("001235.OF", [2, 2.2]),
    })
    first, group = report["holdings"]
    assert first["quantity"] is None
    assert first["value"] is None  # even a point scenario is not a current observed value
    assert first["value_low"] == pytest.approx(550)
    assert group["value_low"] == pytest.approx(1100)
    assert group["value_high"] == pytest.approx(1200)
    assert group["valuation_kind"] == "conditional_estimate"
    assert report["portfolio"]["fund_value_low"] == pytest.approx(1650)
    assert report["portfolio"]["fund_value_high"] == pytest.approx(1750)
    assert report["risk"]["full_portfolio"] is None
    assert first["assumptions"]


def _discipline(tiers):
    return {"regime": {"indices": ["000300.SH"], "sma_short_days": 2, "sma_long_days": 3, "macd_confirm_days": 2}, "tiers": tiers}


def test_regime_gates_follow_computed_history_and_do_not_guess_undefined_momentum():
    discipline = _discipline([
        {"action": "NO ADD", "when": "收盘 < 短均线（SMA{short}）", "label": "gate"},
        {"action": "REDUCE", "when": "收盘 ≥ 短均线（SMA{short}）且动能确认", "label": "reduce"},
    ])
    report = _assess({"positions": []}, {"000300.SH": _obs("000300.SH", [1, 2, 3], field="close")}, discipline=discipline)
    market = report["regime"][0]
    assert market["sma_short"] == 2.5 and market["sma_long"] == 2
    assert market["gates"][0]["state"] == "CLOSED"
    assert market["gates"][1]["state"] == "UNKNOWN"
    assert "momentum" in market["gates"][1]["reason"]
    falling = _assess({"positions": []}, {"000300.SH": _obs("000300.SH", [3, 2, 1], field="close")}, discipline=discipline)
    assert falling["regime"][0]["gates"][0]["state"] == "OPEN"


def test_independent_gates_are_not_collapsed_to_first_match():
    report = _assess({"positions": []}, {"000300.SH": _obs("000300.SH", [1, 2, 3], field="close")}, discipline=_discipline([
        {"action": "REDUCE", "when": "收盘 >= SMA2", "label": "reduce"},
        {"action": "ADD ALLOWED", "when": "收盘 >= SMA2", "label": "add"},
    ]))
    assert [g["state"] for g in report["regime"][0]["gates"]] == ["OPEN", "OPEN"]


def test_stop_and_quantity_cap_use_the_fund_nav_not_a_proxy_etf():
    report = _assess({"positions": [{"symbol": "001236.OF", "quantity": 100}]}, {
        "001236.OF": _obs("001236.OF", [1.8]), "510330.SH": _obs("510330.SH", [4.4], field="close"),
    }, rules=[{"symbol": "001236.OF", "stop_loss": 2, "max_quantity": 90}])
    statuses = report["holdings"][0]["rules"]
    assert statuses["stop_loss"]["state"] == "BREACHED"
    assert statuses["stop_loss"]["observed"] == 1.8
    assert statuses["max_quantity"]["state"] == "OVER CAP"


def test_fee_estimate_uses_confirmed_lots_and_the_applicable_product_terms():
    account = {"positions": [{"symbol": "001236.OF", "quantity": 25, "lots": [
        {"lot_id": "old", "quantity": 10, "confirmed_at": "2026-10-01"},
        {"lot_id": "new", "quantity": 15, "confirmed_at": "2026-10-14"},
    ]}]}
    report = _assess(account, {"001236.OF": _obs("001236.OF", [2])},
        identities={"001236.OF": {"terms": {
            "effective_from": "2026-01-01", "redemption_tiers": [{"holding_days_below": 7, "fee_percent": 1.5}],
            "otherwise_fee_percent": 0,
        }}}, redemption_confirmation_date="2026-10-16")
    fee = report["holdings"][0]["redemption_fee"]
    assert fee["estimate"] == pytest.approx(.45)
    assert fee["is_settlement_amount"] is False
    assert [lot["days_held"] for lot in fee["lots"]] == [15, 2]


def test_unconfirmed_purchase_date_never_becomes_an_exact_fee_window():
    report = _assess({"positions": [{"symbol": "001236.OF", "quantity": 25, "purchase_date": "2026-10-14"}]},
        {"001236.OF": _obs("001236.OF", [2])}, identities={"001236.OF": {"terms": {
            "effective_from": "2026-01-01", "redemption_tiers": [{"holding_days_below": 7, "fee_percent": 1.5}],
        }}}, redemption_confirmation_date="2026-10-16")
    assert report["holdings"][0]["redemption_fee"]["estimate"] is None


def test_partial_history_exposes_original_weights_and_only_subportfolio_risk():
    report = _assess({"positions": [
        {"symbol": "001236.OF", "quantity": 100}, {"symbol": "001237.OF", "quantity": 100},
    ]}, {
        "001236.OF": _obs("001236.OF", [1.5 + i / 100 for i in range(50)]),
        "001237.OF": _obs("001237.OF", [1.99]),
    })
    risk = report["risk"]
    assert risk["full_portfolio"] is None
    assert risk["coverage_fund_weight"] == .5
    assert risk["original_weights"] == {"001236.OF": .5, "001237.OF": .5}
    assert risk["available_subportfolio"]["scope"] == "available_subportfolio"


def test_cluster_cap_reports_user_defined_membership_and_overweight():
    report = _assess({"positions": [
        {"symbol": "001236.OF", "quantity": 90, "name": "AI联接"},
        {"symbol": "001237.OF", "quantity": 10, "name": "红利联接"},
    ]}, {"001236.OF": _obs("001236.OF", [2]), "001237.OF": _obs("001237.OF", [2])}, discipline={
        "cluster_caps": [{"label": "technology", "keywords": ["AI"], "max_percent": 60}],
    })
    cap = report["clusters"][0]
    assert cap["fund_weight_percent"] == 90
    assert cap["state"] == "OVER CAP"
    assert cap["membership_basis"] == "user_defined_keywords_and_members"


def test_valuation_aligns_the_funds_to_a_common_disclosed_date():
    first = _obs("001236.OF", [1, 2])
    second = _obs("001237.OF", [3], day="2026-10-14")
    report = _assess({"positions": [
        {"symbol": "001236.OF", "quantity": 10}, {"symbol": "001237.OF", "quantity": 10},
    ]}, {"001236.OF": first, "001237.OF": second})
    assert report["portfolio"]["fund_value"] == 40  # 10*1 + 10*3, both October 14
    assert [row["valuation_as_of"] for row in report["holdings"]] == ["2026-10-14", "2026-10-14"]


def test_an_observation_for_a_different_subject_does_not_value_the_holding():
    raw = _obs("001237.OF", [999])
    report = _assess({"positions": [{"symbol": "001236.OF", "quantity": 100}]}, {"001236.OF": raw})
    assert report["holdings"][0]["value"] is None
    assert any("subject" in gap["reason"] for gap in report["data_gaps"])


def test_many_individual_bars_with_only_two_shared_dates_are_not_a_full_risk_sample():
    first = _obs("001236.OF", [1 + i / 100 for i in range(50)])
    second = _obs("001237.OF", [2 + i / 100 for i in range(50)], day="2026-08-01")
    # Append two current shared observations while retaining its own long old history.
    second["history"] += first["history"][-2:]
    second["as_of"], second["value"] = first["as_of"], first["value"]
    report = _assess({"positions": [
        {"symbol": "001236.OF", "quantity": 100}, {"symbol": "001237.OF", "quantity": 100},
    ]}, {"001236.OF": first, "001237.OF": second})
    assert report["risk"]["full_portfolio"] is None
    assert "shared" in report["risk"]["reason"]


def test_full_account_risk_includes_the_recorded_cash_weight():
    values = [1.51 + i / 100 for i in range(50)]
    report = _assess({"positions": [
        {"symbol": "001236.OF", "quantity": 100}, {"symbol": "001237.OF", "quantity": 100},
    ], "cash": {"amount": 200, "currency": "CNY", "as_of": "2026-10-15"}}, {
        "001236.OF": _obs("001236.OF", values), "001237.OF": _obs("001237.OF", values),
    })
    risk = report["risk"]
    assert risk["full_portfolio"]["scope"] == "full_account"
    assert risk["full_portfolio"]["inputs"]["weights"]["CASH.CNY"] == pytest.approx(1 / 3, abs=1e-8)
    assert risk["full_portfolio"]["volatility"]["daily_vol"] == pytest.approx(risk["full_fund_sleeve"]["volatility"]["daily_vol"] * 2 / 3)


def test_an_old_cash_record_is_not_a_current_exact_account_balance():
    report = _assess({'positions': [{'symbol': '001236.OF', 'quantity': 100}],
                     'cash': {'amount': 200, 'currency': 'CNY', 'as_of': '2026-09-29'}},
                    {'001236.OF': _obs('001236.OF', [2])})
    assert report['portfolio']['fund_value'] == 200
    assert report['portfolio']['account_value'] is None
    assert report['portfolio']['account_value_low'] == 400  # explicit unchanged-cash scenario
    assert report['portfolio']['cash_assumption']


def test_intraday_index_cannot_trigger_a_recorded_closing_price_gate():
    observation = _obs('000300.SH', [3, 2, 1], field='close')
    observation.update(finality='in_progress', expected_closed_session='2026-10-14')
    report = _assess({'positions': []}, {'000300.SH': observation}, discipline=_discipline([
        {'action': 'NO ADD', 'when': '收盘 < SMA2'},
    ]))
    row = report['regime'][0]
    assert row['as_of'] == '2026-10-14'
    assert row['close'] == 2
    assert row['sma_short'] == 2.5
    assert row['intraday_observation']['value'] == 1
    assert row['finality'] == 'closed_session'


def test_intraday_with_no_previous_closed_bar_leaves_close_gate_unknown():
    observation = _obs('000300.SH', [1], field='close')
    observation.update(finality='in_progress', expected_closed_session='2026-10-14')
    report = _assess({'positions': []}, {'000300.SH': observation}, discipline=_discipline([
        {'action': 'NO ADD', 'when': '收盘 < SMA2'},
    ]))
    assert report['regime'][0]['close'] is None
    assert report['regime'][0]['gates'][0]['state'] == 'UNKNOWN'


def test_fee_estimate_selects_historical_terms_for_the_explicit_confirmation_date():
    account = {'positions': [{'symbol': '001236.OF', 'quantity': 10, 'lots': [{'lot_id': 'x', 'quantity': 10, 'confirmed_at': '2026-10-01'}]}]}
    identity = {'terms': {'effective_from': '2026-10-16', 'otherwise_fee_percent': 1},
        'terms_history': [{'terms': {'effective_from': '2026-01-01', 'otherwise_fee_percent': .5}, 'source_ref': 'old-prospectus'}]}
    report = _assess(account, {'001236.OF': _obs('001236.OF', [2])}, identities={'001236.OF': identity}, redemption_confirmation_date='2026-10-15')
    fee = report['holdings'][0]['redemption_fee']
    assert fee['estimate'] == pytest.approx(.1)
    assert fee['terms_source_ref'] == 'old-prospectus'
    assert fee['terms_effective_from'] == '2026-01-01'


def test_known_unreconciled_cash_change_invalidates_all_account_cash_derived_values():
    account={'positions':[{'symbol':'001236.OF','quantity':100}], 'cash':{'amount':1000,'currency':'CNY','as_of':'2026-10-15'}, 'peak_value':2000,
             'unreconciled_events':[{'symbol':'__CASH__','statement':'现金更新为数百元','as_of':'2026-10-15'}]}
    report=_assess(account,{'001236.OF':_obs('001236.OF',[2])})
    assert report['portfolio']['fund_value']==200
    assert report['portfolio']['account_value'] is None
    assert report['portfolio']['account_value_low'] is None
    assert report['holdings'][0]['account_weight'] is None
    assert report['risk']['full_portfolio'] is None
    assert 'peak_decline_percent' not in report['portfolio']
    assert all(row['account_loss_percent_low'] is None for row in report['stress_scenarios'])
