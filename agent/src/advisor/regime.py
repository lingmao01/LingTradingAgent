"""Deterministic, three-valued evaluation of recorded market conditions."""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from src.advisor.models import Observation


def regime_assessments(observations: dict[str, Observation], discipline: dict[str, Any]) -> list[dict[str, Any]]:
    regime = discipline.get("regime") or {}
    output = []
    for symbol in regime.get("indices", []):
        observation = observations.get(symbol)
        row: dict[str, Any] = {"symbol": symbol, "close": None, "sma_short": None, "sma_long": None, "gates": []}
        if observation and observation.finality == "in_progress":
            row["intraday_observation"] = {"value": observation.value, "as_of": observation.as_of, "fetched_at": observation.fetched_at}
            history = tuple((day, value) for day, value in observation.history if day < observation.as_of)
            if history:
                stamp, value = history[-1]
                quality = "current" if observation.expected_closed_session == stamp else "stale" if observation.expected_closed_session else "unknown"
                observation = replace(observation, value=value, as_of=stamp, history=history, quality=quality, finality="closed_session")
            else:
                observation = None
        if observation:
            series = observation.series()
            row.update(close=observation.value, as_of=observation.as_of, source=observation.source, quality=observation.quality, finality=observation.finality)
            for key, window_key in (("sma_short", "sma_short_days"), ("sma_long", "sma_long_days")):
                window = regime.get(window_key)
                if isinstance(window, int) and window > 0 and len(series) >= window:
                    row[key] = float(series.tail(window).mean())
            if len(series) >= 35:
                line = series.ewm(span=12, adjust=False).mean() - series.ewm(span=26, adjust=False).mean()
                histogram = line - line.ewm(span=9, adjust=False).mean()
                confirmation = regime.get("macd_confirm_days")
                row["macd_histogram"] = float(histogram.iloc[-1])
                if isinstance(confirmation, int) and confirmation > 0 and len(series) >= 34 + confirmation:
                    row["macd_confirmed"] = bool((histogram.tail(confirmation) > 0).all())
        for tier in discipline.get("tiers", []):
            condition = str(tier.get("when") or "")
            value, reason = _condition(condition, row, observation, regime)
            row["gates"].append({
                "action": tier.get("action"), "label": tier.get("label"), "condition": condition,
                "state": "UNKNOWN" if value is None else "OPEN" if value else "CLOSED", "reason": reason,
            })
        output.append(row)
    return output


def _condition(condition: str, row: dict[str, Any], observation: Observation | None, regime: dict[str, Any]) -> tuple[bool | None, str]:
    if observation is None or observation.quality not in {"current", "latest_disclosed", "historical_requested"}:
        return None, "missing or stale index observation"
    text = condition.replace("{short}", str(regime.get("sma_short_days"))).replace("{long}", str(regime.get("sma_long_days"))).replace("{confirm}", str(regime.get("macd_confirm_days")))
    # Preserve unknown clauses instead of dropping qualifications that make a
    # free-text rule stricter. No eval(), and no invented momentum definition.
    parts = re.split(r"\s*(?:且|\band\b|&&)\s*", text, flags=re.I)
    if len(parts) > 1:
        results = [_condition(part, row, observation, regime) for part in parts]
        if any(value is False for value, _ in results):
            return False, "a required condition is false"
        unknown = [reason for value, reason in results if value is None]
        return (None, "; ".join(unknown)) if unknown else (True, "all recorded conditions are satisfied")
    compact = re.sub(r"\s+", "", text)
    match = re.fullmatch(r"(?:收盘价?|close)(<=|>=|<|>|≤|≥)(短均线(?:[（(]SMA\d+[）)])?|长均线(?:[（(]SMA\d+[）)])?|SMA\d+|MA\d+)", compact, re.I)
    if match:
        operator, average = match.groups()
        if average.startswith("短"):
            threshold = row["sma_short"]
        elif average.startswith("长"):
            threshold = row["sma_long"]
        else:
            window = int(re.search(r"\d+", average).group())
            series = observation.series()
            threshold = float(series.tail(window).mean()) if len(series) >= window else None
        if threshold is None:
            return None, "not enough consecutive observations for the recorded average"
        close = row["close"]
        return {"<": close < threshold, ">": close > threshold, "<=": close <= threshold, "≤": close <= threshold, ">=": close >= threshold, "≥": close >= threshold}[operator], "comparison against the recorded moving-average window"
    momentum = re.fullmatch(r"MACD柱连续(\d+)日转正", compact, re.I)
    if momentum and int(momentum.group(1)) == regime.get("macd_confirm_days"):
        return row.get("macd_confirmed"), "recorded consecutive positive MACD histogram condition"
    if "动能" in compact or "momentum" in compact.casefold():
        return None, "momentum confirmation has no recorded quantitative definition"
    return None, "recorded free-text condition has no deterministic interpretation"
