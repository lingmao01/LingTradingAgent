"""Read-only, sanitized portfolio context for model-assisted analysis."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.portfolio.service import PortfolioService


class PortfolioSummaryTool(BaseTool):
    """Expose the latest local portfolio snapshot as sanitized analysis context."""

    name = "portfolio_summary"
    description = (
        "Read the latest sanitized snapshot of the user's locally configured "
        "read-only brokerage accounts. Returns deterministic totals, account "
        "allocation, combined holdings, weights, unrealized P/L, data-quality "
        "warnings, and risk_xray_args (symbols + weights) that can be passed "
        "straight to the portfolio_risk_xray tool. A source that failed to "
        "refresh is reported as an error and excluded from the totals, so a "
        "snapshot with complete=false is missing accounts. It never returns "
        "credentials, account numbers, order IDs, personal names, or local "
        "paths. Use the Web Portfolio refresh button before requesting current "
        "data."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    repeatable = True
    is_readonly = True

    def execute(self, **_: Any) -> str:
        """Return the sanitized portfolio context as a JSON envelope.

        Returns:
            A JSON string with ``status`` ``ok`` and the context, or ``empty``
            with a hint when no usable snapshot exists.
        """
        context = PortfolioService().analysis_context()
        if context is None:
            return json.dumps(
                {
                    "status": "empty",
                    "message": "No portfolio snapshot exists. Refresh the Portfolio page first.",
                },
                ensure_ascii=False,
            )
        return json.dumps({"status": "ok", "context": context}, ensure_ascii=False)
