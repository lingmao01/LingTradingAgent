"""Business delivery status, separate from successful pipeline execution."""
from __future__ import annotations

from typing import Any


def delivery_outcome(
    *, execution_status: str, release_path: str | None, content: str,
    advice_required: bool = False,
) -> dict[str, Any]:
    """Never equate an executed price-only fallback with completed advice."""
    if execution_status != "success" or not content.strip() or release_path == "fallback":
        completion, evidence, actionability = "incomplete", "unavailable", "unavailable"
    elif release_path in {"partial", "redacted"}:
        completion, evidence, actionability = "partial", "partial", "unverified" if advice_required else "not_requested"
    else:
        completion = "partial" if advice_required else "complete"
        evidence = "verified" if release_path in {"clean", "repaired"} else "not_evaluated"
        actionability = "unverified" if advice_required else "not_requested"
    return {
        "execution_status": execution_status,
        "completion_status": completion,
        "evidence_status": evidence,
        "actionability_status": actionability,
        "release_path": release_path,
    }
