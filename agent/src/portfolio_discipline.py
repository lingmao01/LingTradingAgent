"""The user's portfolio discipline: standing rules that outrank a single call.

The per-instrument store (:mod:`src.risk_rules`) holds price levels — a stop, a
target, a cap. The user's own discipline is a level up from that: it is
*portfolio* rules that decide whether adding is even permitted, how much of the
book one theme may occupy, and when a drawdown has gone far enough to stop
adding. Those rules have no single symbol to hang on, so they live here:

* :class:`PortfolioDisciplineStore` — the JSON store under the runtime root
  (``portfolio_discipline.json``), written atomically (temp file in the same
  directory + ``os.replace``) so an interrupted write can never truncate or
  half-write the document.
* :class:`PortfolioDiscipline` — one document, not a row per symbol: the
  cluster caps (a theme's ceiling, matched to holdings by name keyword),
  the regime state machine's windows, the permission gates that machine
  opens (its tiers), the drawdown circuit breaker, the conditions an add must
  satisfy, the redemption-fee tiers, and any standing guardrails the user has
  recorded.
* :func:`render_discipline_block` — the markdown lines injected into every
  turn's user message (inside the ``<portfolio-discipline>`` tag) and merged
  into the grounding ledger's user corpus, because the rules are the user's
  own statement and a ``held`` figure quoting one reconciles as theirs.

Everything here is data, never code: the module knows nothing about any
particular theme, ceiling or window, and it ships no default document. A fresh
install renders no block; the user's own figures arrive through the store (or
the ``portfolio_discipline`` tool) and are re-read on every turn. That is
deliberate — a ceiling frozen into the Python source could not follow the user
when they change it, which is the failure this layer exists to avoid.

The discipline names no holdings and no amounts. Cluster membership is a
*rule* — a holding joins a cluster when the name the user recorded for it
carries one of the cluster's keywords (``members`` is the explicit override
for a holding whose name carries none). The holdings themselves, and their
market values, stay where the user stated them; this module only says how to
group and cap them.

Storage shape (``version 1``)::

    {"version": 1, "updated_at": "...", "discipline": {
        "cluster_caps": [
            {"label": "科技簇", "max_percent": 70, "keywords": ["人工智能", "芯片"],
             "members": []}],
        "regime": {"indices": ["000001.SH"], "sma_short_days": 20,
                   "sma_long_days": 50, "macd_confirm_days": 2, "note": ""},
        "tiers": [{"action": "NO ADD", "label": "禁加仓线",
                   "when": "收盘 < 短均线（SMA{short}）", "note": ""}],
        "max_drawdown_percent": 20,
        "add_conditions": ["..."],
        "redemption_fees": [{"holding_days_below": 7, "fee_percent": 1.5}],
        "guardrails": ["..."],
        "note": ""}}
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config.paths import get_runtime_root

logger = logging.getLogger(__name__)

#: Discipline file name under the runtime root.
DISCIPLINE_FILENAME = "portfolio_discipline.json"
#: On-disk schema version; bumped only on an incompatible shape change.
DISCIPLINE_VERSION = 1

# A label, keyword or note is re-injected into every future turn's context, so
# each is capped — the same paste-safety reason book notes are capped.
_MAX_LABEL_CHARS = 60
_MAX_KEYWORD_CHARS = 40
_MAX_TEXT_CHARS = 300
_MAX_ITEMS = 40

#: Sections the tool may write, in render order. A write that names a section
#: outside this set is refused rather than silently stored where nothing reads
#: it.
SECTIONS = (
    "cluster_caps",
    "regime",
    "tiers",
    "max_drawdown_percent",
    "add_conditions",
    "redemption_fees",
    "guardrails",
    "note",
)

#: The gate vocabulary a tier may carry, frozen so the machine-readable tail of
#: a scheduled report can name a portfolio state without free text. A tier is a
#: *permission* on an action: several gates are open at once, so unlike a
#: first-match-wins state machine these are evaluated independently.
TIER_ACTIONS = ("NO ADD", "REDUCE", "ADD ALLOWED")


def _utc_now() -> str:
    """UTC ISO8601 timestamp (seconds precision), matching sibling stores."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _finite_number(value: Any, name: str) -> float:
    """Coerce a user-supplied number, rejecting bools, NaN and infinities.

    Raises:
        ValueError: The value is not a finite real number.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be a finite number")
    return parsed


def _positive_percent(value: Any, name: str) -> float:
    """Coerce a percentage, which must be finite and in ``(0, 100]``.

    Raises:
        ValueError: The value is not a number in ``(0, 100]``.
    """
    parsed = _finite_number(value, name)
    if parsed <= 0 or parsed > 100:
        raise ValueError(f"{name} must be in (0, 100]")
    return parsed


def _positive_int(value: Any, name: str) -> int:
    """Coerce a whole count, which must be a positive integer.

    Raises:
        ValueError: The value is not a positive integer.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    parsed = _finite_number(value, name)
    if parsed != int(parsed) or parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return int(parsed)


def _text(value: Any, limit: int) -> str:
    """Collapse free text to one whitespace-normalized line, capped."""
    return " ".join(str(value or "").split())[:limit]


def _text_list(value: Any, name: str, limit: int = _MAX_TEXT_CHARS) -> list[str]:
    """Coerce a list of short single-line strings, dropping empties.

    Raises:
        ValueError: The value is not a list, or holds more than :data:`_MAX_ITEMS`.
    """
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list")
    if len(value) > _MAX_ITEMS:
        raise ValueError(f"{name} must hold at most {_MAX_ITEMS} items")
    items: list[str] = []
    for entry in value:
        if isinstance(entry, (dict, list, tuple)):
            raise ValueError(f"{name} must hold plain strings")
        text = _text(entry, limit)
        if text:
            items.append(text)
    return items


def _strict_text_list(value: Any, name: str, limit: int) -> list[str]:
    """Like :func:`_text_list`, but refuses an item that would be truncated.

    The read path truncates silently so a legacy file still loads; on the write
    path a truncation would quietly change the user's rule — a keyword cut to
    the cap may stop matching the holding it was meant to match — so it is
    refused instead.

    Raises:
        ValueError: The value is not a list, is over-long, or holds an item
            longer than *limit*.
    """
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list")
    if len(value) > _MAX_ITEMS:
        raise ValueError(f"{name} must hold at most {_MAX_ITEMS} items")
    items: list[str] = []
    for entry in value:
        if isinstance(entry, (dict, list, tuple)):
            raise ValueError(f"{name} must hold plain strings")
        text = " ".join(str(entry or "").split())
        if len(text) > limit:
            raise ValueError(
                f"{name} item exceeds {limit} characters: {text[:20]}…"
            )
        if text:
            items.append(text)
    return items


def _raw_list(value: Any, name: str) -> list[Any]:
    """Coerce a value to a list for parsing, tolerating a bad shape.

    Raises:
        ValueError: The value is not a list.
    """
    if isinstance(value, (list, tuple)):
        return list(value)
    raise ValueError(f"{name} must be a list")


def _validate_section(name: str, value: Any) -> None:
    """Raise ValueError when a section value could not be stored as given.

    Reading is lenient on purpose: a damaged file degrades instead of failing
    the turn. Writing is not. A caller that names a section is saying the user
    just set it, so a value the lenient parser would silently drop or truncate
    is refused here — otherwise the store reports success while the rule the
    user just gave is gone.

    Raises:
        ValueError: The named section's value cannot be stored as given.
    """
    if name == "cluster_caps":
        for entry in _raw_list(value, name):
            if not isinstance(entry, dict):
                raise ValueError("each cluster cap must be an object")
            label = " ".join(str(entry.get("label") or "").split())
            if not label:
                raise ValueError("a cluster cap needs a label")
            if len(label) > _MAX_LABEL_CHARS:
                raise ValueError(
                    f"cluster cap label exceeds {_MAX_LABEL_CHARS} characters"
                )
            _positive_percent(entry.get("max_percent"), "max_percent")
            _strict_text_list(
                entry.get("keywords") or [], "keywords", _MAX_KEYWORD_CHARS
            )
            _strict_text_list(entry.get("members") or [], "members", _MAX_TEXT_CHARS)
        return
    if name == "regime":
        if value is None:
            return
        if not isinstance(value, dict):
            raise ValueError("regime must be an object")
        _strict_text_list(value.get("indices") or [], "indices", _MAX_TEXT_CHARS)
        for key in ("sma_short_days", "sma_long_days", "macd_confirm_days"):
            _optional_positive_int(value.get(key), key)
        note = " ".join(str(value.get("note") or "").split())
        if len(note) > _MAX_TEXT_CHARS:
            raise ValueError(f"regime note exceeds {_MAX_TEXT_CHARS} characters")
        return
    if name == "tiers":
        entries = _raw_list(value, name)
        if len(entries) > _MAX_ITEMS:
            raise ValueError(f"{name} must hold at most {_MAX_ITEMS} items")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("each tier must be an object")
            action = " ".join(str(entry.get("action") or "").split())
            if action not in TIER_ACTIONS:
                raise ValueError(
                    "tier action must be one of " + ", ".join(TIER_ACTIONS)
                )
            label = " ".join(str(entry.get("label") or "").split())
            if not label:
                raise ValueError("a tier needs a label")
            if len(label) > _MAX_LABEL_CHARS:
                raise ValueError(f"tier label exceeds {_MAX_LABEL_CHARS} characters")
            when = " ".join(str(entry.get("when") or "").split())
            if not when:
                raise ValueError("a tier needs a condition")
            if len(when) > _MAX_TEXT_CHARS:
                raise ValueError(f"tier condition exceeds {_MAX_TEXT_CHARS} characters")
            note = " ".join(str(entry.get("note") or "").split())
            if len(note) > _MAX_TEXT_CHARS:
                raise ValueError(f"tier note exceeds {_MAX_TEXT_CHARS} characters")
        return
    if name == "max_drawdown_percent":
        if value is not None:
            _positive_percent(value, name)
        return
    if name in ("add_conditions", "guardrails", "note"):
        if name == "note":
            if value is not None and len(" ".join(str(value).split())) > _MAX_TEXT_CHARS:
                raise ValueError(f"note exceeds {_MAX_TEXT_CHARS} characters")
            return
        _strict_text_list(value, name, _MAX_TEXT_CHARS)
        return
    if name == "redemption_fees":
        for entry in _raw_list(value, name):
            if not isinstance(entry, dict):
                raise ValueError("each redemption tier must be an object")
            _positive_int(entry.get("holding_days_below"), "holding_days_below")
            fee = _finite_number(entry.get("fee_percent"), "fee_percent")
            if fee < 0:
                raise ValueError("fee_percent cannot be negative")
        return


@dataclass(frozen=True)
class ClusterCap:
    """One theme's ceiling on the share of the book it may occupy.

    Attributes:
        label: The theme's name, as the user calls it.
        max_percent: Ceiling on the cluster's combined share of the book.
        keywords: Name fragments that place a holding in this cluster.
        members: Explicit holdings that belong here whatever their name says.
    """

    label: str
    max_percent: float
    keywords: tuple[str, ...] = ()
    members: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready object."""
        return {
            "label": self.label,
            "max_percent": self.max_percent,
            "keywords": list(self.keywords),
            "members": list(self.members),
        }

    @staticmethod
    def from_dict(row: Any) -> "ClusterCap | None":
        """Parse one persisted cluster, or None when the row is unusable.

        A cluster needs a label and a ceiling; keywords and members are
        optional. A malformed keyword list drops the row rather than
        half-reading a rule that would cap the wrong names.
        """
        if not isinstance(row, dict):
            return None
        label = _text(row.get("label"), _MAX_LABEL_CHARS)
        if not label:
            return None
        try:
            max_percent = _positive_percent(row.get("max_percent"), "max_percent")
            keywords = _text_list(
                row.get("keywords") or [], "keywords", _MAX_KEYWORD_CHARS
            )
            members = _text_list(row.get("members") or [], "members")
        except ValueError:
            return None
        return ClusterCap(
            label=label,
            max_percent=max_percent,
            keywords=tuple(keywords),
            members=tuple(members),
        )


@dataclass(frozen=True)
class Regime:
    """The broad-index state machine whose tier decides whether adding is on.

    The windows are the user's own figures (a short and a long moving average,
    and how many sessions an MACD confirmation must hold); the indices are the
    broad gauges the tier is read from. None of it is computed here — this
    module has no price source, so the model reads the tier off prices a tool
    returned in the turn, against the windows recorded here.

    Attributes:
        indices: Broad-index symbols the tier is read from.
        sma_short_days: Short moving-average window, in sessions.
        sma_long_days: Long moving-average window, in sessions.
        macd_confirm_days: Consecutive sessions an MACD confirmation must hold.
        note: Free-text remark the user attached (e.g. an unresolved code).
    """

    indices: tuple[str, ...] = ()
    sma_short_days: int | None = None
    sma_long_days: int | None = None
    macd_confirm_days: int | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready object."""
        return {
            "indices": list(self.indices),
            "sma_short_days": self.sma_short_days,
            "sma_long_days": self.sma_long_days,
            "macd_confirm_days": self.macd_confirm_days,
            "note": self.note,
        }

    @staticmethod
    def from_dict(row: Any) -> "Regime | None":
        """Parse the persisted regime, or None when the object is unusable.

        Every field is optional: a regime that names only a window is still a
        real rule, and one that says nothing at all is not stored.
        """
        if not isinstance(row, dict):
            return None
        try:
            indices = _text_list(row.get("indices") or [], "indices")
            short = _optional_positive_int(row.get("sma_short_days"), "sma_short_days")
            long = _optional_positive_int(row.get("sma_long_days"), "sma_long_days")
            confirm = _optional_positive_int(
                row.get("macd_confirm_days"), "macd_confirm_days"
            )
        except ValueError:
            return None
        regime = Regime(
            indices=tuple(indices),
            sma_short_days=short,
            sma_long_days=long,
            macd_confirm_days=confirm,
            note=_text(row.get("note"), _MAX_TEXT_CHARS),
        )
        if (
            not regime.indices
            and regime.sma_short_days is None
            and regime.sma_long_days is None
            and regime.macd_confirm_days is None
            and not regime.note
        ):
            return None
        return regime


def _optional_positive_int(value: Any, name: str) -> int | None:
    """Return None for an absent window, or the coerced positive integer.

    Raises:
        ValueError: A present value is not a positive integer.
    """
    if value is None:
        return None
    return _positive_int(value, name)


@dataclass(frozen=True)
class Tier:
    """One permission gate of the broad-index state machine.

    The user's machine is not first-match-wins: ① forbids adding, ② opens the
    trim permission once a bounce has carried the close back over the short
    average, ③ reopens adding only on a stricter right-side confirmation. Each
    tier is therefore a gate evaluated on its own, and several may be open at
    once.

    The condition is stored as text, not as a formula, because this module has
    no price source: the model evaluates it against closes a tool returned in
    the turn. ``{short}``, ``{long}`` and ``{confirm}`` in the text are filled
    from the recorded :class:`Regime` windows when the block is rendered, so
    changing a window in the data changes the condition with it.

    Attributes:
        action: One of :data:`TIER_ACTIONS` — the permission this gate grants.
        label: The user's own name for the gate (e.g. 禁加仓线).
        when: The condition, in prose, evaluated against this run's closes.
        note: Free-text remark the user attached.
    """

    action: str
    label: str
    when: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready object."""
        return {
            "action": self.action,
            "label": self.label,
            "when": self.when,
            "note": self.note,
        }

    @staticmethod
    def from_dict(row: Any) -> "Tier | None":
        """Parse one persisted tier, or None when the row is unusable.

        A gate needs an action from the frozen vocabulary, a name, and a
        condition to evaluate — anything less is not a rule.
        """
        if not isinstance(row, dict):
            return None
        action = " ".join(str(row.get("action") or "").split())
        label = _text(row.get("label"), _MAX_LABEL_CHARS)
        when = _text(row.get("when"), _MAX_TEXT_CHARS)
        if action not in TIER_ACTIONS or not label or not when:
            return None
        return Tier(
            action=action,
            label=label,
            when=when,
            note=_text(row.get("note"), _MAX_TEXT_CHARS),
        )


@dataclass(frozen=True)
class RedemptionTier:
    """One step of the redemption-fee ladder: hold too briefly, pay more.

    Attributes:
        holding_days_below: The tier applies while held fewer than this many days.
        fee_percent: The fee charged in that window, as a percentage.
    """

    holding_days_below: int
    fee_percent: float

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready object."""
        return {
            "holding_days_below": self.holding_days_below,
            "fee_percent": self.fee_percent,
        }

    @staticmethod
    def from_dict(row: Any) -> "RedemptionTier | None":
        """Parse one persisted tier, or None when the row is unusable.

        A fee may be zero (a tier that charges nothing is a real, if unusual,
        statement); the day bound must be a positive integer.
        """
        if not isinstance(row, dict):
            return None
        try:
            days = _positive_int(row.get("holding_days_below"), "holding_days_below")
            fee = _finite_number(row.get("fee_percent"), "fee_percent")
        except ValueError:
            return None
        if fee < 0:
            return None
        return RedemptionTier(holding_days_below=days, fee_percent=fee)


@dataclass(frozen=True)
class PortfolioDiscipline:
    """The user's whole portfolio discipline, in force until they change it.

    Attributes:
        cluster_caps: Theme ceilings, each matched to holdings by keyword.
        regime: The broad-index state machine, when one is recorded.
        tiers: The permission gates of the state machine, each evaluated alone.
        max_drawdown_percent: Drawdown at which the user's breaker trips.
        add_conditions: What an add must satisfy before it is allowed.
        redemption_fees: The redemption-fee ladder in force.
        guardrails: Standing behavioural rules the user asked to be held to.
        note: Free-text remark the user attached.
        updated_at: UTC ISO8601 timestamp of the last write.
    """

    cluster_caps: tuple[ClusterCap, ...] = ()
    regime: Regime | None = None
    tiers: tuple[Tier, ...] = ()
    max_drawdown_percent: float | None = None
    add_conditions: tuple[str, ...] = ()
    redemption_fees: tuple[RedemptionTier, ...] = ()
    guardrails: tuple[str, ...] = ()
    note: str = ""
    updated_at: str = ""

    def is_empty(self) -> bool:
        """Return True when the document states nothing."""
        return not (
            self.cluster_caps
            or self.regime
            or self.tiers
            or self.max_drawdown_percent is not None
            or self.add_conditions
            or self.redemption_fees
            or self.guardrails
            or self.note
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready document."""
        return {
            "cluster_caps": [cap.to_dict() for cap in self.cluster_caps],
            "regime": self.regime.to_dict() if self.regime else None,
            "tiers": [tier.to_dict() for tier in self.tiers],
            "max_drawdown_percent": self.max_drawdown_percent,
            "add_conditions": list(self.add_conditions),
            "redemption_fees": [tier.to_dict() for tier in self.redemption_fees],
            "guardrails": list(self.guardrails),
            "note": self.note,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(row: Any) -> "PortfolioDiscipline | None":
        """Parse the persisted document, or None when it is unusable.

        Parsing is lenient per section — a section that cannot be read is
        dropped like an absent one, never repaired by guessing — so one bad
        field cannot take the whole discipline down. A document left with
        nothing in it is reported as absent.
        """
        if not isinstance(row, dict):
            return None

        caps: list[ClusterCap] = []
        try:
            for entry in _raw_list(row.get("cluster_caps") or [], "cluster_caps"):
                cap = ClusterCap.from_dict(entry)
                if cap is not None:
                    caps.append(cap)
        except ValueError:
            caps = []

        regime = Regime.from_dict(row.get("regime"))

        gate_tiers: list[Tier] = []
        try:
            for entry in _raw_list(row.get("tiers") or [], "tiers")[:_MAX_ITEMS]:
                tier = Tier.from_dict(entry)
                if tier is not None:
                    gate_tiers.append(tier)
        except ValueError:
            gate_tiers = []

        max_drawdown: float | None = None
        if row.get("max_drawdown_percent") is not None:
            try:
                max_drawdown = _positive_percent(
                    row.get("max_drawdown_percent"), "max_drawdown_percent"
                )
            except ValueError:
                max_drawdown = None

        try:
            add_conditions = _text_list(row.get("add_conditions") or [], "add_conditions")
        except ValueError:
            add_conditions = []

        fee_tiers: list[RedemptionTier] = []
        try:
            for entry in _raw_list(row.get("redemption_fees") or [], "redemption_fees"):
                tier = RedemptionTier.from_dict(entry)
                if tier is not None:
                    fee_tiers.append(tier)
        except ValueError:
            fee_tiers = []

        try:
            guardrails = _text_list(row.get("guardrails") or [], "guardrails")
        except ValueError:
            guardrails = []

        discipline = PortfolioDiscipline(
            cluster_caps=tuple(caps),
            regime=regime,
            tiers=tuple(gate_tiers),
            max_drawdown_percent=max_drawdown,
            add_conditions=tuple(add_conditions),
            redemption_fees=tuple(
                sorted(fee_tiers, key=lambda tier: tier.holding_days_below)
            ),
            guardrails=tuple(guardrails),
            note=_text(row.get("note"), _MAX_TEXT_CHARS),
            updated_at=str(row.get("updated_at") or ""),
        )
        return None if discipline.is_empty() else discipline


class PortfolioDisciplineStore:
    """JSON-backed store for the user's portfolio discipline."""

    def __init__(self, path: Path | str | None = None) -> None:
        """Initialize the store.

        Args:
            path: Explicit discipline file path (tests); defaults to
                ``<runtime-root>/portfolio_discipline.json``, resolved here at
                construction time.
        """
        self.path = (
            Path(path) if path is not None else get_runtime_root() / DISCIPLINE_FILENAME
        )

    def load(self) -> PortfolioDiscipline | None:
        """Return the persisted discipline, or None.

        A missing file is no discipline. An unreadable or malformed file is
        logged and treated as none rather than failing the turn that reads it;
        sections that fail to parse are dropped, never repaired by guessing.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "portfolio discipline %s unreadable (%s); treated as absent",
                self.path,
                exc,
            )
            return None
        row = raw.get("discipline") if isinstance(raw, dict) else None
        return PortfolioDiscipline.from_dict(row)

    def save(self, discipline: PortfolioDiscipline) -> PortfolioDiscipline:
        """Persist *discipline*, stamping it with the current time.

        Args:
            discipline: The document to store.

        Returns:
            The stored document, with ``updated_at`` set to now.
        """
        stamped = _replace_updated_at(discipline, _utc_now())
        self._save(stamped)
        return stamped

    def update(self, **sections: Any) -> PortfolioDiscipline:
        """Merge the named sections into the stored document and persist it.

        A section the caller does not name keeps the value already recorded,
        so changing one ceiling never drops the rest of the discipline. An
        empty list or an explicit ``None`` for a scalar clears that section.

        Args:
            **sections: Any of :data:`SECTIONS` as keyword arguments.

        Returns:
            The stored document.

        Raises:
            ValueError: A section name is not one of :data:`SECTIONS`, or a
                value fails validation.
        """
        current = self.load() or PortfolioDiscipline()
        payload = current.to_dict()
        payload["updated_at"] = ""
        for name, value in sections.items():
            if name not in SECTIONS:
                raise ValueError(
                    f"unknown section: {name!r}; use one of {', '.join(SECTIONS)}"
                )
            # Strict on the write path: never report a section saved when the
            # lenient parser would have dropped or truncated it.
            _validate_section(name, value)
            payload[name] = value
        parsed = PortfolioDiscipline.from_dict(payload)
        if parsed is None:
            raise ValueError("the update would leave the discipline empty")
        return self.save(parsed)

    def clear(self) -> bool:
        """Remove the whole document.

        The file is removed whether or not it parses: a document damaged enough
        to load as absent must still be clearable, or a bad write would wedge
        the store with no way back.

        Returns:
            True when a document was removed, False when none was stored.
        """
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True

    def _save(self, discipline: PortfolioDiscipline) -> None:
        """Write the document atomically (temp file in the same directory + rename)."""
        payload = {
            "version": DISCIPLINE_VERSION,
            "updated_at": discipline.updated_at,
            "discipline": discipline.to_dict(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp_path, self.path)


def _replace_updated_at(
    discipline: PortfolioDiscipline, stamp: str
) -> PortfolioDiscipline:
    """Return *discipline* with its timestamp replaced."""
    return PortfolioDiscipline(
        cluster_caps=discipline.cluster_caps,
        regime=discipline.regime,
        tiers=discipline.tiers,
        max_drawdown_percent=discipline.max_drawdown_percent,
        add_conditions=discipline.add_conditions,
        redemption_fees=discipline.redemption_fees,
        guardrails=discipline.guardrails,
        note=discipline.note,
        updated_at=stamp,
    )


def _fmt_number(value: float) -> str:
    """Render a stored number without a trailing ``.0`` for whole units."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _cluster_line(cap: ClusterCap) -> str:
    """One markdown line for a cluster ceiling."""
    parts = [f"max {_fmt_number(cap.max_percent)}% of the book"]
    if cap.keywords:
        parts.append("keywords " + ", ".join(cap.keywords))
    if cap.members:
        parts.append("always includes " + ", ".join(cap.members))
    return f"- Cluster cap 「{cap.label}」: {', '.join(parts)}"


def _regime_line(regime: Regime) -> str:
    """One markdown line for the regime state machine."""
    parts: list[str] = []
    if regime.indices:
        parts.append("broad indices " + ", ".join(regime.indices))
    if regime.sma_short_days is not None and regime.sma_long_days is not None:
        parts.append(
            f"short average {regime.sma_short_days} sessions, "
            f"long average {regime.sma_long_days} sessions"
        )
    elif regime.sma_short_days is not None:
        parts.append(f"short average {regime.sma_short_days} sessions")
    elif regime.sma_long_days is not None:
        parts.append(f"long average {regime.sma_long_days} sessions")
    if regime.macd_confirm_days is not None:
        parts.append(
            f"momentum confirmation holds {regime.macd_confirm_days} consecutive sessions"
        )
    line = "- Regime state machine: " + "; ".join(parts) if parts else "- Regime state machine"
    if regime.note:
        line += f" — {regime.note}"
    return line


def _fill_condition(text: str, regime: Regime | None) -> str:
    """Substitute the recorded window values into a gate's condition text.

    A gate stores its condition and note as prose with ``{short}``/``{long}``/
    ``{confirm}`` placeholders, so changing a window in the data changes the
    text with it. A window the regime does not record is spoken rather than
    left as a bare placeholder, so the line still reads as a rule.
    """
    short = regime.sma_short_days if regime else None
    long = regime.sma_long_days if regime else None
    confirm = regime.macd_confirm_days if regime else None
    return (
        text.replace("{short}", str(short) if short is not None else "the short average")
        .replace("{long}", str(long) if long is not None else "the long average")
        .replace("{confirm}", str(confirm) if confirm is not None else "N")
    )


def _tier_line(tier: Tier, regime: Regime | None) -> str:
    """One markdown line for a permission gate of the state machine."""
    line = (
        f"- Gate 「{tier.label}」 → {tier.action}: "
        f"{_fill_condition(tier.when, regime)}"
    )
    if tier.note:
        line += f" — {_fill_condition(tier.note, regime)}"
    return line


def _redemption_line(tier: RedemptionTier) -> str:
    """One markdown line for a redemption-fee tier."""
    return (
        f"- Redemption fee: held fewer than {tier.holding_days_below} days "
        f"charges {_fmt_number(tier.fee_percent)}%"
    )


def render_discipline_block(store: PortfolioDisciplineStore | None = None) -> str:
    """Render the discipline as the markdown lines injected into a turn, or "".

    The lines are the user's own standing rules, so callers quote them as
    theirs: they go inside the ``<portfolio-discipline>`` block of the user
    message and into the grounding ledger's user corpus that a ``held`` figure
    is reconciled against. They state the rules and the windows only. No tier,
    no cluster share and no drawdown figure is computed here — this module has
    no price source, so the model reads those off prices a tool returned in
    the turn, against the figures recorded here.

    Args:
        store: Store to read; a fresh default store when omitted, so the
            runtime root is resolved at call time.

    Returns:
        One ``- ...`` line per recorded rule, or "" when nothing is recorded.
    """
    discipline = (store or PortfolioDisciplineStore()).load()
    if discipline is None:
        return ""
    lines: list[str] = [_cluster_line(cap) for cap in discipline.cluster_caps]
    if discipline.regime is not None:
        lines.append(_regime_line(discipline.regime))
    lines.extend(_tier_line(tier, discipline.regime) for tier in discipline.tiers)
    if discipline.max_drawdown_percent is not None:
        lines.append(
            "- Drawdown circuit breaker: "
            f"{_fmt_number(discipline.max_drawdown_percent)}% from the peak"
        )
    if discipline.add_conditions:
        lines.append("- Adding requires: " + "; ".join(discipline.add_conditions))
    lines.extend(_redemption_line(tier) for tier in discipline.redemption_fees)
    if discipline.guardrails:
        lines.append("- Standing guardrails: " + "; ".join(discipline.guardrails))
    if discipline.note:
        lines.append(f"- Note: {discipline.note}")
    if discipline.updated_at:
        lines.append(f"- (recorded {discipline.updated_at[:10]})")
    return "\n".join(lines)
