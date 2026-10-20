"""Dated feeder → target ETF → index relationships and product-specific terms."""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.account_ledger import calendar_date, number
from src.config.paths import get_runtime_root
from src.core.file_lock import store_transaction
from src.fund_data import canonical_fund_symbol


def validate_terms(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("product terms must be an object")
    result = dict(raw)
    for key in ("effective_from", "effective_until"):
        if result.get(key):
            result[key] = calendar_date(result[key], key)
    if result.get("effective_from") and result.get("effective_until") and result["effective_until"] < result["effective_from"]:
        raise ValueError("product terms effective_until precedes effective_from")
    if "otherwise_fee_percent" in result:
        rate = number(result["otherwise_fee_percent"], "otherwise fee percent", minimum=0)
        if rate > 100:
            raise ValueError("invalid otherwise fee percent")
        result["otherwise_fee_percent"] = rate
    tiers = result.get("redemption_tiers")
    if tiers is not None:
        if not isinstance(tiers, list):
            raise ValueError("redemption fee tiers must be a list")
        parsed = []
        for tier in tiers:
            if not isinstance(tier, dict):
                raise ValueError("redemption fee tier must be an object")
            days = number(tier.get("holding_days_below"), "fee holding days", minimum=1)
            fee = number(tier.get("fee_percent"), "fee percent", minimum=0)
            if days != int(days) or fee > 100:
                raise ValueError("invalid redemption fee tier")
            parsed.append({**tier, "holding_days_below": int(days), "fee_percent": fee})
        if len({tier["holding_days_below"] for tier in parsed}) != len(parsed):
            raise ValueError("duplicate redemption fee boundary")
        result["redemption_tiers"] = sorted(parsed, key=lambda tier: tier["holding_days_below"])
    cutoff = result.get("cutoff_time")
    if cutoff and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(cutoff)):
        raise ValueError("invalid product cutoff time")
    return result


class FundIdentityStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else get_runtime_root() / "fund_identities.json"

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("funds"), dict):
            raise ValueError("fund identity store is malformed")
        return payload["funds"]

    def lookup(self, symbol: str) -> dict[str, Any] | None:
        canonical = canonical_fund_symbol(symbol)
        return self.load().get(canonical) if canonical else None

    @store_transaction
    def record(
        self, symbol: str, *, name: str | None = None, share_class: str | None = None,
        target_etf: str | None = None, tracking_index: str | None = None,
        source_ref: str | None = None, as_of: str | None = None,
        terms: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a declared relationship, never promote it to verified fact."""
        canonical = canonical_fund_symbol(symbol)
        if not canonical:
            raise ValueError("fund identity requires an off-exchange fund code")
        if target_etf is not None and not re.fullmatch(r"(?:5\d{5}\.SH|15\d{4}\.SZ)", target_etf.upper()):
            raise ValueError("target ETF must carry a listed ETF symbol, not a feeder code")
        if as_of:
            calendar_date(as_of, "relationship as_of date")
        funds = self.load()
        record = dict(funds.get(canonical) or {})
        old_source = {key: record.get(key) for key in ("source_ref", "as_of")}
        record.update({
            key: value for key, value in {
                "name": name, "share_class": share_class,
                "target_etf": target_etf.upper() if target_etf else target_etf,
                "tracking_index": tracking_index.upper() if tracking_index else tracking_index,
                "source_ref": source_ref, "as_of": as_of,
            }.items() if value is not None
        })
        if terms is not None:
            validated = validate_terms(terms)
            if record.get("terms") and record["terms"] != validated:
                record["terms_history"] = [*record.get("terms_history", []), {
                    "terms": record["terms"], **old_source,
                }]
            record["terms"] = validated
        record.update({
            "symbol": canonical, "source_kind": "user_declared", "verified": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        funds[canonical] = record
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=self.path.parent, delete=False, encoding="utf-8") as handle:
            temporary = Path(handle.name)
            json.dump({"version": 1, "funds": funds}, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        return record
