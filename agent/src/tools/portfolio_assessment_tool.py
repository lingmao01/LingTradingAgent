"""Registered read-only personal ETF-feeder account assessment."""
from __future__ import annotations

import json
from typing import Any

from src.advisor.service import AssessmentService
from src.agent.tools import BaseTool
from src.market_observation import market_now


class PortfolioAssessmentTool(BaseTool):
    name = "get_portfolio_assessment"
    description = (
        "Assess the user's recorded fund account and market discipline from one "
        "dated snapshot. Automatically reads the effective book (including dated "
        "user-memory amounts and unresolved combined-code holdings), fetches each "
        "feeder's own NAV and the recorded broad indices, and computes valuation "
        "coverage, conditional value ranges, cash reservations, risk-rule states, "
        "independent regime gates, user-defined cluster limits and historical risk. "
        "Use before personal fund operation advice. This does not place orders. "
        "Missing evidence affects only its own calculations; estimates are not "
        "observed positions, and an ADD ALLOWED gate is permission, not a buy call."
    )
    parameters = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "as_of": {"type": "string", "description": "Information cutoff YYYY-MM-DD; default today in Asia/Shanghai."},
            "lookback_days": {"type": "integer", "minimum": 30, "maximum": 1825, "description": "Calendar history window, default 180; source limitations remain explicit."},
            "scope_symbols": {"type": "array", "items": {"type": "string"}, "description": "Explicitly requested analysis subset; never presented as a complete account or used to infer full-account concentration."},
            "redemption_confirmation_date": {"type": "string", "description": "Optional explicitly supplied hypothetical redemption confirmation date YYYY-MM-DD for lot-specific fee estimates; never inferred from today or order time."},
            "no_cache": {"type": "boolean", "description": "Explicitly refetch published fund NAV instead of using a fresh memo."},
        }, "required": [],
    }
    is_readonly = True
    repeatable = True
    replay_after_compaction = True

    def __init__(self, service: AssessmentService | None = None):
        self.service = service or AssessmentService()

    def execute(self, **kwargs: Any) -> str:
        try:
            unknown = set(kwargs) - {"as_of", "lookback_days", "no_cache", "run_dir", "redemption_confirmation_date", "scope_symbols"}
            if unknown:
                raise ValueError(f"unsupported assessment parameters: {sorted(unknown)}")
            result = self.service.collect(
                as_of=kwargs.get("as_of") or market_now().date().isoformat(),
                lookback_days=kwargs.get("lookback_days", 180), no_cache=kwargs.get("no_cache", False),
                redemption_confirmation_date=kwargs.get("redemption_confirmation_date"), scope_symbols=kwargs.get("scope_symbols"),
            )
            return json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, OSError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
