"""Typed observations: market values, user inputs and calculations stay distinct."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

import pandas as pd

from src.account_ledger import calendar_date, number


def freeze(value: Any) -> Any:
    """Copy mutable containers, recursively; no references to a live ledger."""
    if is_dataclass(value) and getattr(value, '__dataclass_params__').frozen:
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Return independent JSON containers without deepcopying mapping proxies."""
    if is_dataclass(value):
        return {item.name: thaw(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [thaw(item) for item in value]
    return value


def content_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(thaw(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class ResultMapping(Mapping[str, Any]):
    """Typed results also permit legacy read-only Mapping consumers."""
    def __getitem__(self, key: str) -> Any:
        if key not in {item.name for item in fields(self)}:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self):
        return (item.name for item in fields(self))

    def __len__(self) -> int:
        return len(fields(self))

    def to_dict(self) -> dict[str, Any]:
        return thaw(self)


@dataclass(frozen=True)
class Observation:
    symbol: str
    field: str
    value: float
    as_of: str
    source: str
    currency: str | None
    quality: str
    history: tuple[tuple[str, float], ...]
    fetched_at: str | None = None
    finality: str = "unknown"
    expected_closed_session: str | None = None

    @classmethod
    def parse(cls, symbol: str, raw: Mapping[str, Any], cutoff: str) -> "Observation":
        if raw.get("symbol") and str(raw["symbol"]).upper() != symbol.upper():
            raise ValueError("observation subject differs from its requested symbol")
        field = str(raw.get("field") or "")
        if field not in {"unit_nav", "close"}:
            raise ValueError("unsupported observation field")
        if symbol.endswith(".OF") and field != "unit_nav":
            raise ValueError("a feeder requires its own NAV, not an ETF close")
        stamp = calendar_date(str(raw.get("as_of") or "")[:10], "observation date")
        if stamp > cutoff:
            raise ValueError("observation is after the analysis cutoff")
        value = number(raw.get("value"), "observation value", minimum=0)
        if value <= 0:
            raise ValueError("a fund NAV or close must be positive")
        source = str(raw.get("source") or "").strip()
        if not source or raw.get("quality") == "unavailable":
            raise ValueError("observation has no usable source")
        history: dict[str, float] = {}
        for row in raw.get("history", []):
            if not isinstance(row, Mapping):
                continue
            day = calendar_date(str(row.get("date") or "")[:10])
            if day > cutoff:
                continue
            reading = number(row.get("value"), "historical observation", minimum=0)
            if reading > 0:
                history[day] = reading
        if stamp in history and abs(history[stamp] - value) > max(1e-8, value * 1e-8):
            raise ValueError("latest observation conflicts with its own history")
        history[stamp] = value
        return cls(symbol, field, value, stamp, source, raw.get("currency"), str(raw.get("quality") or "unknown"), tuple(sorted(history.items())), raw.get("fetched_at"), str(raw.get("finality") or "unknown"), raw.get("expected_closed_session"))

    def series(self) -> pd.Series:
        return pd.Series(
            [value for _, value in self.history],
            index=pd.to_datetime([day for day, _ in self.history]), name=self.symbol, dtype=float,
        )

    def ref(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "field": self.field, "as_of": self.as_of,
                "source": self.source, "quality": self.quality, "fetched_at": self.fetched_at,
                "finality": self.finality, "expected_closed_session": self.expected_closed_session}


@dataclass(frozen=True)
class AssessmentInputs(ResultMapping):
    account: Mapping[str, Any]
    observations: Mapping[str, Observation]
    identities: Mapping[str, Any]
    as_of: str
    calendar_version: str = 'unavailable'
    method_versions: Mapping[str, Any] = field(default_factory=dict)
    input_sha256: str = ''
    trading_sessions: tuple[str, ...] = ()
    discipline: Mapping[str, Any] = field(default_factory=dict)
    account_rules: tuple[Mapping[str, Any], ...] = ()
    gaps: tuple[Mapping[str, Any], ...] = ()
    schema_version: int = 2

    def __post_init__(self):
        for key in ('account', 'observations', 'identities', 'method_versions', 'discipline', 'account_rules', 'gaps'):
            object.__setattr__(self, key, freeze(getattr(self, key)))
        object.__setattr__(self, 'trading_sessions', tuple(sorted(set(self.trading_sessions))))
        if not self.input_sha256:
            object.__setattr__(self, 'input_sha256', content_sha256({key: value for key, value in self.items() if key != 'input_sha256'}))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> 'AssessmentInputs':
        raw = payload.get('inputs') or {}
        as_of = calendar_date(raw.get('as_of') or (payload.get('data') or {}).get('as_of'))
        observations, gaps = {}, []
        for symbol, value in (payload.get('evidence') or {}).items():
            try:
                observations[symbol] = value if isinstance(value, Observation) else Observation.parse(symbol, value, as_of)
            except (ValueError, TypeError, AttributeError) as exc:
                gaps.append({'symbol': symbol, 'reason': str(exc)})
        calendar = raw.get('calendar') or {}
        sessions = raw.get('trading_sessions') or calendar.get('trading_sessions') or calendar.get('sessions') or ()
        if isinstance(sessions, Mapping):
            sessions = [day for day, row in sessions.items() if row is True or isinstance(row, Mapping) and row.get('is_open')]
        return cls(account=raw.get('account') or {}, observations=observations, identities=raw.get('identities') or {}, as_of=as_of,
                   calendar_version=str(raw.get('calendar_version') or calendar.get('version') or 'unavailable'),
                   method_versions=raw.get('method_versions') or {'signals': 'signals-v2', 'macd': '12-26-9-adjust-false-v1'},
                   trading_sessions=tuple(str(day)[:10] for day in sessions), discipline=raw.get('discipline') or {}, account_rules=tuple(raw.get('rules') or ()), gaps=tuple(gaps))


@dataclass(frozen=True)
class ResearchSubject(ResultMapping):
    id: str
    kind: str
    canonical_symbol: str
    name: str
    availability: str = 'UNKNOWN'
    mapping_refs: tuple[str, ...] = ()
    schema_version: int = 2

    def __post_init__(self):
        object.__setattr__(self, 'mapping_refs', tuple(self.mapping_refs))
