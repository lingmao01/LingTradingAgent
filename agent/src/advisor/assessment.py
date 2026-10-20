"""Pure fund-account assessment, with conditional ranges and explicit coverage."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any, Mapping, Sequence

import pandas as pd

from backtest.risk_xray import compute_risk_xray
from src.account_ledger import available_cash, calendar_date
from src.advisor.fees import redemption_fee
from src.advisor.models import Observation
from src.advisor.regime import regime_assessments


def assess_portfolio(
    account: Mapping[str, Any], observations: Mapping[str, Mapping[str, Any]], *, as_of: str,
    rules: Sequence[Mapping[str, Any]] = (), discipline: Mapping[str, Any] | None = None,
    identities: Mapping[str, Mapping[str, Any]] | None = None,
    redemption_confirmation_date: str | None = None,
) -> dict[str, Any]:
    """Compute from a frozen input set; no model, source fetch or trading side effect."""
    calendar_date(as_of)
    if redemption_confirmation_date is not None:
        calendar_date(redemption_confirmation_date, 'redemption confirmation date')
    parsed: dict[str, Observation] = {}
    gaps: list[dict[str, Any]] = []
    for symbol, raw in observations.items():
        try:
            parsed[symbol] = Observation.parse(symbol, raw, as_of)
        except (ValueError, TypeError) as exc:
            gaps.append({"symbol": symbol, "reason": str(exc)})
    identities = identities or {}
    discipline = dict(discipline or {})
    positions = list(account.get("positions", []))
    groups = list(account.get("holding_groups", []))
    fund_symbols = {
        symbol for position in [*positions, *groups]
        for symbol in ([position["symbol"]] if position.get("symbol") else position.get("candidate_symbols", []))
        if symbol in parsed
    }
    common = set.intersection(*(set(dict(parsed[symbol].history)) for symbol in fund_symbols)) if fund_symbols else set()
    valuation_date = max(common) if common else None
    valuation_observations = dict(parsed)
    if valuation_date:
        for symbol in fund_symbols:
            observation = parsed[symbol]
            valuation_observations[symbol] = replace(
                observation, value=dict(observation.history)[valuation_date], as_of=valuation_date,
                history=tuple((day, value) for day, value in observation.history if day <= valuation_date),
            )
    holdings = []
    for position in [*positions, *groups]:
        row = _value_holding(position, valuation_observations, account)
        symbol = position.get("symbol")
        observation = parsed.get(symbol)
        if any(event.get('symbol') in row['candidates'] for event in account.get('unreconciled_events', [])):
            position = {**position, 'quantity': None, 'lots': []}
            observation = None
        row["rules"] = _rules(position, observation, [r for r in rules if r.get("symbol") == symbol])
        row["redemption_fee"] = redemption_fee(position, observation, dict(identities.get(symbol) or {}), redemption_confirmation_date)
        row["available_quantity"] = None if position.get("quantity") is None else float(position["quantity"]) - sum(
            float(order.get("quantity") or 0) for order in account.get("pending_orders", [])
            if order.get("symbol") == symbol and order.get("action") == "redeem" and order.get("state") == "pending"
        )
        if row["available_quantity"] is not None and row["available_quantity"] < 0:
            row["gaps"].append("pending redemptions exceed recorded shares")
        holdings.append(row)
    covered = not account.get("errors") and all(row["value_low"] is not None for row in holdings) and bool(holdings) and bool(valuation_date)
    exact = covered and all(row["valuation_kind"] == "marked_from_nav" for row in holdings)
    low = sum(row["value_low"] for row in holdings) if covered else None
    high = sum(row["value_high"] for row in holdings) if covered else None
    cash = account.get("cash") or {}
    recorded_available_cash = available_cash(dict(account))
    cash_unresolved = bool(account.get('errors') or any(event.get('symbol') == '__CASH__' for event in account.get('unreconciled_events', [])) or recorded_available_cash is not None and recorded_available_cash < 0)
    calculation_cash = {} if cash_unresolved else cash
    cash_value = calculation_cash.get("amount") if calculation_cash.get("currency") == "CNY" else None
    cash_aligned = cash_value is not None and cash.get("as_of") == valuation_date
    portfolio = {
        "coverage_complete": covered, "valuation_exact": exact,
        "fund_value": low if exact else None, "fund_value_low": low, "fund_value_high": high,
        "account_value": low + cash_value if exact and cash_aligned else None,
        "account_value_low": low + cash_value if low is not None and cash_value is not None else None,
        "account_value_high": high + cash_value if high is not None and cash_value is not None else None,
        "available_cash": available_cash(dict(account)), "cash_as_of": cash.get("as_of"),
        "available_cash_currency": cash.get("currency"),
        "cash_assumption": None if cash_aligned else "conditional scenario only: recorded cash has not changed since its stated date",
        "valuation_as_of": valuation_date,
        "mark_dates": sorted({row["valuation_as_of"] for row in holdings if row["valuation_as_of"]}),
        "caliber": "latest disclosed marks; each holding retains its own date",
    }
    if cash_unresolved:
        portfolio['cash_assumption'] = '现金变更或在途资金冲突尚未核对，旧现金不能构成当前账户余额、权重或压力比例'
        gaps.append({'scope': 'cash', 'reason': portfolio['cash_assumption']})
    for row in holdings:
        row["fund_weight"] = row["value"] / low if exact and low and row["value"] is not None else None
        row["account_weight"] = row["value"] / portfolio["account_value"] if portfolio["account_value"] and row["value"] is not None else None
        row["fund_weight_low"] = row["value_low"] / high if covered and high else None
        row["fund_weight_high"] = row["value_high"] / low if covered and low else None
    risk = _risk(holdings, parsed, exact, portfolio, calculation_cash)
    if account.get("peak_value") and portfolio["account_value"] is not None:
        portfolio["peak_decline_percent"] = (1 - portfolio["account_value"] / account["peak_value"]) * 100
        portfolio["peak_decline_basis"] = "unadjusted account balance; not an investment return without cash-flow reconciliation"
    report = {
        "schema_version": 1, "method_version": "fund-assessment-v1", "as_of": as_of,
        "holdings": holdings, "portfolio": portfolio, "risk": risk,
        "stress_scenarios": _stress(portfolio, calculation_cash),
        "regime": regime_assessments(parsed, discipline),
        "clusters": _clusters(holdings, discipline), "data_gaps": gaps,
        "account_warnings": list(account.get("warnings", [])) + list(account.get("errors", [])),
        "observations": {symbol: observation.ref() for symbol, observation in parsed.items()},
        "personal_parameters": {key: account.get(key) for key in ("investment_horizon", "max_loss_amount", "minimum_cash")},
        "limitations": ["Historical risk does not bound future loss.", "User-defined gates express discipline, not a validated return forecast."],
    }
    if account.get('analysis_scope') == 'requested_subportfolio':
        report['portfolio'].update(analysis_scope='requested_subportfolio', account_value=None, account_value_low=None, account_value_high=None)
        report['portfolio'].pop('peak_decline_percent', None)
        report['portfolio'].pop('peak_decline_basis', None)
        for row in holdings:
            row['account_weight'] = None
        for scenario in report['stress_scenarios']:
            scenario.update(account_loss_percent_low=None, account_loss_percent_high=None)
        for cluster in report['clusters']:
            cluster.update(state='UNKNOWN', conditional_state='UNKNOWN', fund_weight_percent=None, fund_weight_low_percent=None, fund_weight_high_percent=None)
        risk['available_subportfolio'] = risk.get('full_fund_sleeve') or risk.get('available_subportfolio')
        risk.update(full_portfolio=None, full_fund_sleeve=None, scope='requested_subportfolio')
        if risk['available_subportfolio']:
            risk['available_subportfolio']['scope'] = 'requested_subportfolio'
        report['data_gaps'].append({'scope': 'analysis_scope', 'reason': '本轮仅评估指定子组合；子组合权重与估值不能替代全账户集中度和资金约束审查'})
    held_symbols = {row['symbol'] for row in holdings if row.get('symbol')}
    report['rule_assessments'] = []
    for symbol in sorted({rule.get('symbol') for rule in rules if rule.get('symbol')} - held_symbols):
        checked = _rules({'quantity': None}, parsed.get(symbol), [rule for rule in rules if rule.get('symbol') == symbol])
        for rule in checked.values():
            rule.update(state='UNKNOWN', reason='position is not recorded; no trade is inferred from this standing rule')
        report['rule_assessments'].append({'symbol': symbol, 'position_missing': True, 'rules': checked})
        report['data_gaps'].append({'symbol': symbol, 'reason': 'standing rule has no confirmed holding or direction'})
    report["snapshot_sha256"] = hashlib.sha256(json.dumps({"account": account, "observations": observations, "rules": list(rules), "discipline": discipline, "identities": identities, "as_of": as_of, "redemption_confirmation_date": redemption_confirmation_date}, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return report


def _stress(portfolio, cash):
    low, high = portfolio.get("fund_value_low"), portfolio.get("fund_value_high")
    if low is None or high is None:
        return []
    cash_value = cash.get("amount") if cash.get("currency") == "CNY" else None
    rows = []
    for change in (-10, -20):
        loss_low, loss_high = low * abs(change) / 100, high * abs(change) / 100
        rows.append({
            "fund_change_percent": change, "fund_loss_low": loss_low, "fund_loss_high": loss_high,
            "account_loss_percent_low": loss_low / (low + cash_value) * 100 if cash_value is not None and low + cash_value > 0 else None,
            "account_loss_percent_high": loss_high / (high + cash_value) * 100 if cash_value is not None and high + cash_value > 0 else None,
            "probability": None, "not_forecast": True,
            "valuation_kind": "disclosed_marks" if portfolio["valuation_exact"] else "conditional_historical_amount_scenario",
            "assumptions": ["all fund NAV marks change by the same hypothetical percentage", "no transactions or cash changes; no tax, fee or distribution adjustment", "scenario shocks are analytical parameters, not user risk limits or a market forecast"],
        })
    return rows


def _value_holding(position: Mapping[str, Any], observations: dict[str, Observation], account: Mapping[str, Any]) -> dict[str, Any]:
    symbol = position.get("symbol")
    candidates = [symbol] if symbol else list(position.get("candidate_symbols", []))
    row = {
        "id": symbol or position.get("group_id"), "symbol": symbol, "candidates": candidates,
        "name": position.get("name", ""), "quantity": position.get("quantity"),
        "value": None, "value_low": None, "value_high": None, "valuation_kind": "unavailable",
        "valuation_as_of": None, "price": None, "price_as_of": None,
        "candidate_marks": [],
        "evidence_refs": [], "assumptions": [], "gaps": [],
    }
    if any(event.get('symbol') in candidates for event in account.get('unreconciled_events', [])):
        row['quantity'] = None
        row['gaps'].append('本轮已报告持仓变动尚未核对，旧份额与无交易估值假设已失效；仅需更新该项剩余份额或当期金额')
        return row
    observation = observations.get(symbol)
    if not observation and position.get("quantity") is not None:
        row["gaps"].append("fund NAV observation unavailable; the recorded share quantity is retained")
        return row
    if observation:
        row.update(price=observation.value, price_as_of=observation.as_of, valuation_as_of=observation.as_of)
        row["evidence_refs"].append(observation.ref())
    if observation and position.get("quantity") is not None and observation.currency == "CNY":
        value = float(position["quantity"]) * observation.value
        if value < 0:
            row["gaps"].append("short holdings require a separate signed-exposure risk model")
            return row
        row.update(value=value, value_low=value, value_high=value, valuation_kind="marked_from_nav")
        if observation.quality in {"stale", "unknown"}:
            row["gaps"].append("mark freshness is not confirmed for this decision")
        return row
    reported = position.get("reported_value")
    dates = [position["value_as_of"]] if position.get("value_as_of") else list(account.get("valuation_date_range", []))
    if reported is None or not dates or not candidates:
        row["gaps"].append("share quantity or a dated historical value is not recorded")
        return row
    factors, as_ofs = [], []
    for candidate in candidates:
        observed = observations.get(candidate)
        if not observed or observed.currency != "CNY":
            row["gaps"].append(f"missing fund NAV history for {candidate}")
            return row
        history = dict(observed.history)
        row["candidate_marks"].append({"symbol": candidate, "value": observed.value,
                                       "as_of": observed.as_of, "source": observed.source})
        for day in set(dates):
            if day not in history:
                row["gaps"].append(f"historical NAV unavailable for {candidate} at {day}")
                return row
            factors.append(observed.value / history[day])
        row["evidence_refs"].append(observed.ref())
        as_ofs.append(observed.as_of)
    row.update(value_low=float(reported) * min(factors), value_high=float(reported) * max(factors), valuation_kind="conditional_estimate", valuation_as_of=min(as_ofs))
    row["assumptions"] = [
        "reported monetary value is taken as stated",
        "no transactions, distributions or reinvestment changed share counts since the reported date",
        "group splits and valuation dates remain unresolved; the range spans these possibilities",
    ]
    return row


def _rules(position: Mapping[str, Any], observation: Observation | None, rules: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    output = {}
    for rule in rules:
        quantity = position.get("quantity")
        for key, level in rule.items():
            if key not in {"stop_loss", "take_profit", "max_quantity"} or level is None:
                continue
            value = quantity if key == "max_quantity" else observation.value if observation else None
            state = "UNKNOWN"
            if value is not None:
                if key == "max_quantity":
                    state = "OVER CAP" if abs(value) > level else "CLEAR"
                elif observation.quality not in {"current", "latest_disclosed", "historical_requested"}:
                    state = "UNKNOWN"
                elif key == "stop_loss":
                    state = "BREACHED" if (value <= level if quantity is None or quantity >= 0 else value >= level) else "CLEAR"
                else:
                    state = "REACHED" if (value >= level if quantity is None or quantity >= 0 else value <= level) else "CLEAR"
            output[key] = {"state": state, "level": level, "observed": value, "as_of": observation.as_of if observation and key != "max_quantity" else None}
    return output


def _clusters(holdings: list[dict[str, Any]], discipline: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for cluster in discipline.get("cluster_caps", []):
        members = [row for row in holdings if any(str(member) in row["candidates"] for member in cluster.get("members", [])) or any(str(word).casefold() in row["name"].casefold() for word in cluster.get("keywords", []))]
        known = all(row["fund_weight"] is not None for row in holdings)
        weight = sum(row["fund_weight"] for row in members) * 100 if known else None
        low = sum(row["fund_weight_low"] for row in members) * 100 if all(row.get("fund_weight_low") is not None for row in holdings) else None
        high = sum(row["fund_weight_high"] for row in members) * 100 if all(row.get("fund_weight_high") is not None for row in holdings) else None
        maximum = cluster["max_percent"]
        result.append({
            "label": cluster["label"], "members": [row["id"] for row in members], "max_percent": maximum,
            "fund_weight_percent": weight, "fund_weight_low_percent": low, "fund_weight_high_percent": high,
            "state": "UNKNOWN" if weight is None else "OVER CAP" if weight > maximum else "AT CAP" if weight == maximum else "CLEAR",
            "conditional_state": "WITHIN_CAP" if high is not None and high <= maximum else "OVER_CAP" if low is not None and low > maximum else "STRADDLES_CAP" if low is not None and high is not None else "UNKNOWN",
            "membership_basis": "user_defined_keywords_and_members",
        })
    return result


def _risk(holdings: list[dict[str, Any]], observations: dict[str, Observation], exact: bool, portfolio: dict[str, Any], cash: dict[str, Any]) -> dict[str, Any]:
    weights = {row["symbol"]: row["fund_weight"] for row in holdings if row["symbol"] and row["fund_weight"] is not None}
    result: dict[str, Any] = {"full_portfolio": None, "full_fund_sleeve": None, "available_subportfolio": None, "original_weights": weights, "coverage_fund_weight": None, "scope": "fund_sleeve", "reason": "exact fund weights and sufficient common history required"}
    if not exact or not weights:
        return result
    kept = {symbol: observations[symbol].series() for symbol in weights if symbol in observations and len(observations[symbol].history) >= 30}
    result["coverage_fund_weight"] = sum(weights[symbol] for symbol in kept)
    if not kept:
        return result
    frame = pd.DataFrame(kept).dropna(how="any")
    if len(frame) < 30:
        result["reason"] = "fewer than 30 shared observation dates; aggregate risk is not estimated"
        return result
    try:
        report = compute_risk_xray(frame, {symbol: weights[symbol] for symbol in kept})
    except ValueError as exc:
        result["reason"] = str(exc)
        return result
    report["scope"] = "full_fund_sleeve" if len(kept) == len(weights) else "available_subportfolio"
    report["return_caliber"] = "published NAV price returns; reconcile distributions before treating as total-return performance"
    if len(kept) == len(weights):
        result["full_fund_sleeve"] = report
        account_value = portfolio.get("account_value")
        if account_value and cash.get("currency") == "CNY" and cash.get("as_of") == portfolio["valuation_as_of"]:
            fund_ratio = portfolio["fund_value"] / account_value
            account_weights = {symbol: weight * fund_ratio for symbol, weight in weights.items()}
            account_weights["CASH.CNY"] = cash["amount"] / account_value
            account_frame = frame.copy()
            account_frame["CASH.CNY"] = 1.0
            account_report = compute_risk_xray(account_frame, account_weights)
            account_report["scope"] = "full_account"
            account_report["cash_assumption"] = "cash earns zero in this static-weight historical scenario; actual deposits/withdrawals are not modeled"
            account_report["return_caliber"] = report["return_caliber"]
            result["full_portfolio"] = account_report
    else:
        result["available_subportfolio"] = report
    return result
