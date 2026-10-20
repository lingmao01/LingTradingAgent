"""Recover dated user-reported holdings without changing the user's memory files.

Only explicit per-holding ledger bullets from user-category memories qualify.
Combined codes stay a single group; amounts never become share quantities.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_HOLDING = re.compile(
    r"^\s*[-*]\s*(?P<codes>\d{6}(?:\s*/\s*\d{6})*)\s+"
    r"(?P<name>[^：:\n]{1,180})[：:]\s*(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?:元)?(?P<tail>.*)$"
)
_CASH = re.compile(r"^\s*[-*]\s*活钱[：:]\s*(?:约\s*)?(\d[\d,]*(?:\.\d+)?)", re.M)


def recalled_account(memory_root: Path) -> dict[str, Any]:
    """Newest explicit ledger, with source/date/ambiguity preserved; read-only."""
    candidates = []
    for path in sorted((memory_root / "user").glob("*.md")):
        if path.stat().st_size > 1_000_000:
            continue
        text = path.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            continue
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(meta, dict) or meta.get("type") != "user":
            continue
        body = parts[2]
        if "台账" not in body or "联接" not in body:
            continue
        rows = [match for line in body.splitlines() if (match := _HOLDING.match(line))]
        if not rows:
            continue
        recorded = str(meta.get("updated_at") or meta.get("created_at") or "")
        if not re.match(r"^\d{4}-\d{2}-\d{2}", recorded):
            continue
        candidates.append((recorded, str(path), body, rows))
    if not candidates:
        return {"positions": [], "holding_groups": [], "pending_orders": []}
    recorded, source, body, rows = max(candidates, key=lambda item: (item[0], item[1]))
    year = recorded[:4]
    date_text = " ".join(line for line in body.splitlines() if "金额" in line and "口径" in line)
    dates = sorted(set(
        full if full else f"{year}-{short}"
        for full, short in re.findall(r"((?:19|20)\d{2}-\d{2}-\d{2})|(?<![\d-])(\d{2}-\d{2})(?!\d)", date_text)
    ))
    positions, groups = [], []
    for match in rows:
        symbols = [code.strip() + ".OF" for code in match["codes"].split("/")]
        value = float(match["value"].replace(",", ""))
        tail = match["tail"]
        growth = re.search(r"([+−-]?\d+(?:\.\d+)?)\s*%", tail)
        purchase = re.search(r"\b\d{4}-\d{2}-\d{2}\b", tail)
        common = {
            "name": match["name"].strip(), "quantity": None, "reported_value": value,
            "value_as_of": dates[0] if len(dates) == 1 else None,
            "reported_at": recorded[:10], "source_ref": source,
            "purchase_date": purchase.group(0) if purchase else None,
            "reported_return_pct": float(growth.group(1).replace("−", "-")) if growth else None,
            "updated_at": recorded, "quality": "user_reported",
        }
        if len(symbols) == 1:
            positions.append({"symbol": symbols[0], **common})
        else:
            groups.append({"group_id": "/".join(symbols), "candidate_symbols": symbols, **common})
    cash = _CASH.search(body)
    account: dict[str, Any] = {
        "positions": positions, "holding_groups": groups, "pending_orders": [],
        "source_ref": source, "reported_at": recorded[:10], "quality": "user_reported",
        "valuation_date_range": [dates[0], dates[-1]] if dates else [],
        "reported_fund_value": sum(row["reported_value"] for row in [*positions, *groups]),
        "warnings": [
            "Historical user-reported amounts are not current market values; share quantities and confirmation dates were not supplied.",
            *(["A combined-code holding remains one unresolved group, not two separately funded positions."] if groups else []),
        ],
    }
    if cash:
        account["cash"] = {
            "amount": float(cash.group(1).replace(",", "")), "currency": "CNY",
            "as_of": dates[0] if len(dates) == 1 else None,
            "reported_at": recorded[:10], "quality": "user_reported", "source_ref": source,
        }
    return account
