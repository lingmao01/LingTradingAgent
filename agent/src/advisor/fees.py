"""Product- and lot-specific fee estimates; purchase dates are not confirmations."""
from __future__ import annotations

from datetime import date
from typing import Any


def redemption_fee(position: dict[str, Any], observation: Any, identity: dict[str, Any], confirmation_date: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {"estimate": None, "is_settlement_amount": False, "lots": [], "reason": "product terms or confirmed lot dates unavailable"}
    versions = [identity, *(identity.get("terms_history") or [])]
    applicable = [version for version in versions if isinstance(version, dict) and isinstance(version.get("terms"), dict)
                  and version["terms"].get("effective_from") and confirmation_date
                  and version["terms"]["effective_from"] <= confirmation_date]
    # A newer effective version supersedes an older open-ended version. An
    # expired latest version does not revive its predecessor.
    selected = max(applicable, key=lambda version: version["terms"]["effective_from"]) if applicable else {}
    terms = selected.get("terms") or {}
    lots = position.get("lots") or []
    if not observation or not confirmation_date or not lots or not terms.get("effective_from"):
        return result
    if confirmation_date < terms["effective_from"] or (terms.get("effective_until") and confirmation_date > terms["effective_until"]):
        result["reason"] = "no product terms applicable to the redemption confirmation date"
        return result
    estimates = []
    for lot in lots:
        if not lot.get("confirmed_at"):
            return result
        days = (date.fromisoformat(confirmation_date) - date.fromisoformat(lot["confirmed_at"])).days
        if days < 0:
            result["reason"] = "lot is not confirmed at the proposed redemption date"
            return result
        rate = next((tier["fee_percent"] for tier in sorted(terms.get("redemption_tiers", []), key=lambda row: row["holding_days_below"]) if days < tier["holding_days_below"]), terms.get("otherwise_fee_percent"))
        if rate is None:
            result["reason"] = "product fee beyond the recorded tiers is unknown"
            return result
        estimate = lot["quantity"] * observation.value * rate / 100
        estimates.append(estimate)
        result["lots"].append({"lot_id": lot.get("lot_id"), "days_held": days, "fee_percent": rate, "estimate": estimate})
    result.update(terms_effective_from=terms['effective_from'], terms_source_ref=selected.get('source_ref'),
                  terms_source_kind=selected.get('source_kind', identity.get('source_kind', 'unspecified')),
                  holding_day_basis='explicit confirmation-date scenario; product settlement rules still apply')
    result.update(estimate=sum(estimates), reason="estimate at the latest disclosed NAV; actual settlement NAV is unknown", nav_as_of=observation.as_of, confirmation_date=confirmation_date)
    return result
