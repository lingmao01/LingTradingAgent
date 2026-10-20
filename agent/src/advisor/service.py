"""Collect one bounded, shared evidence snapshot for deterministic assessment."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable, Mapping

from src.account_ledger import calendar_date, number
from src.advisor.assessment import assess_portfolio
from src.advisor.collection import bounded_collect
from src.advisor.budget_scope import source_call
from src.fund_data import fetch_fund_data
from src.fund_identity import FundIdentityStore
from src.market_data import fetch_market_data
from src.market_observation import market_now
from src.portfolio_discipline import PortfolioDisciplineStore
from src.position_book import PositionBookStore
from src.risk_rules import RiskRulesStore

DEFAULT_INDICES = ("000001.SH", "000300.SH", "399006.SZ", "000688.SH")


class AssessmentService:
    def __init__(
        self, *, book: PositionBookStore | None = None,
        fund_fetcher: Callable[..., dict[str, Any]] | None = None,
        market_fetcher: Callable[..., dict[str, Any]] | None = None,
        collection_timeout_seconds: float = 45,
    ):
        self.collection_timeout_seconds = collection_timeout_seconds
        self.book = book or PositionBookStore()
        self.fund_fetcher = fund_fetcher or fetch_fund_data
        self.market_fetcher = market_fetcher or fetch_market_data

    def collect(self, *, as_of: str, lookback_days: int = 180, no_cache: bool = False, redemption_confirmation_date: str | None = None, scope_symbols: list[str] | None = None) -> dict[str, Any]:
        cutoff = date.fromisoformat(calendar_date(as_of))
        if cutoff > market_now().date():
            raise ValueError("analysis cutoff cannot be in the future")
        if isinstance(lookback_days, bool) or not isinstance(lookback_days, int) or not 30 <= lookback_days <= 1825:
            raise ValueError("lookback_days must be an integer between 30 and 1825")
        if not isinstance(no_cache, bool):
            raise ValueError("no_cache must be boolean")
        if redemption_confirmation_date is not None:
            redemption_confirmation_date = calendar_date(redemption_confirmation_date, 'redemption confirmation date')
        account = self.book.load_account()
        root = self.book.path.parent
        gaps = []
        if scope_symbols is not None:
            from src.fund_data import canonical_fund_symbol
            if not isinstance(scope_symbols, list) or any(not isinstance(symbol, str) or canonical_fund_symbol(symbol) != symbol for symbol in scope_symbols):
                raise ValueError('scope_symbols must be explicit canonical off-exchange fund codes')
            scope = set(scope_symbols)
            selected = [row for row in account.get('positions', []) if row.get('symbol') in scope]
            groups = [row for row in account.get('holding_groups', []) if scope.intersection(row.get('candidate_symbols', []))]
            known = {row['symbol'] for row in selected} | {symbol for row in groups for symbol in row.get('candidate_symbols', [])}
            for symbol in sorted(scope - known):
                selected.append({'symbol': symbol, 'quantity': None, 'name': '本轮指定标的；持仓未在账本确认'})
                gaps.append({'symbol': symbol, 'reason': 'specified fund has no confirmed account holding'})
            account = {**account, 'positions': selected, 'holding_groups': groups, 'analysis_scope': 'requested_subportfolio'}
        rules = [rule.to_dict() for rule in RiskRulesStore(root / "risk_rules.json").load()]
        if scope_symbols is not None:
            in_scope = {row['symbol'] for row in account.get('positions', [])} | {symbol for row in account.get('holding_groups', []) for symbol in row.get('candidate_symbols', [])}
            rules = [rule for rule in rules if rule['symbol'] in in_scope]
        saved_discipline = PortfolioDisciplineStore(root / "portfolio_discipline.json").load()
        discipline = saved_discipline.to_dict() if saved_discipline else {}
        if not discipline.get("regime"):
            # These are descriptive analysis windows, never invented personal
            # gates or an instruction to trade.
            discipline["regime"] = {
                "indices": list(DEFAULT_INDICES), "sma_short_days": 20, "sma_long_days": 50,
                "parameter_source": "descriptive_defaults",
            }
        try:
            identities = FundIdentityStore(root / "fund_identities.json").load()
        except (ValueError, TypeError, OSError) as exc:
            identities = {}
            gaps.append({"scope": "fund_identity", "reason": f"fund identity sidecar unavailable: {exc}"})
        symbols = {row["symbol"] for row in account.get("positions", []) if row.get("symbol")}
        symbols.update(symbol for group in account.get("holding_groups", []) for symbol in group.get("candidate_symbols", []))
        symbols.update(rule["symbol"] for rule in rules)
        fund_symbols = sorted(symbol for symbol in symbols if symbol.endswith(".OF"))
        market_symbols = set(symbols) - set(fund_symbols)
        market_symbols.update(discipline["regime"].get("indices", []))
        for symbol in fund_symbols:
            identity = identities.get(symbol) or {}
            market_symbols.update(value for key in ("target_etf", "tracking_index") if (value := identity.get(key)))
        start_date = (cutoff - timedelta(days=lookback_days)).isoformat()
        # Include an older explicitly reported valuation date if needed for a
        # no-transactions scenario; never replace that date with today.
        reported_dates = list(account.get("valuation_date_range", [])) + [
            row["value_as_of"] for row in account.get("positions", []) if row.get("value_as_of")
        ]
        if reported_dates:
            start_date = min(start_date, *reported_dates)
        evidence: dict[str, dict[str, Any]] = {}

        exact_quantity_book = bool(account.get("positions")) and not account.get("holding_groups") and all(row.get("quantity") is not None for row in account["positions"])
        fund_start = start_date if exact_quantity_book else (cutoff - timedelta(days=14)).isoformat()
        historical_window = None
        if not exact_quantity_book and reported_dates:
            earliest, latest = min(reported_dates), max(reported_dates)
            if earliest >= (cutoff - timedelta(days=45)).isoformat():
                fund_start = min(fund_start, earliest)
            elif latest < fund_start:
                historical_window = (earliest, latest)

        def fund(symbol: str) -> tuple[str, dict[str, Any] | None]:
            payload = source_call(self.fund_fetcher,
                [symbol], view="nav", start_date=fund_start, end_date=as_of,
                max_rows=min(1200, max(40, int((cutoff - date.fromisoformat(fund_start)).days * .75) + 10)),
                no_cache=no_cache,
            )
            entry = payload.get(symbol)
            if not isinstance(entry, dict):
                return symbol, None
            metadata = entry.get("_provenance") or {}
            if metadata.get("caliber") != "nav":
                return symbol, None
            rows = list(entry.get("data") or [])
            if historical_window:
                try:
                    old = source_call(self.fund_fetcher,[symbol], view="nav", start_date=historical_window[0], end_date=historical_window[1], max_rows=0, no_cache=no_cache).get(symbol) or {}
                    if old.get("_provenance", {}).get("caliber") == "nav":
                        rows.extend(old.get("data") or [])
                except Exception:
                    pass  # latest observation survives a missing historical anchor
            observation = _observation(symbol, rows, "unit_nav", metadata, as_of)
            if observation:
                observation["quality"] = "stale" if metadata.get("stale") else "latest_disclosed"
                expected = (payload.get("_session") or {}).get("latest_nav_date")
                if expected and as_of == market_now().date().isoformat():
                    observation["expected_nav_date"] = expected
                    observation["disclosure_reference_basis"] = "calendar disclosure reference, not a guaranteed product publication time"
                    if observation["as_of"] < expected:
                        observation["quality"] = "stale"
            return symbol, observation

        def market():
            return source_call(self.market_fetcher,
                codes=sorted(market_symbols), start_date=start_date, end_date=as_of,
                include_provenance=True, max_rows=0,
            )
        def calendar_snapshot():
            from src.market_calendar import frozen_calendar
            return source_call(frozen_calendar, start_date, as_of)
        jobs = {"market": market, "calendar": calendar_snapshot} if market_symbols else {}
        jobs.update({symbol: (lambda symbol=symbol: fund(symbol)) for symbol in fund_symbols})
        completed, failures = bounded_collect(jobs, timeout_seconds=self.collection_timeout_seconds, max_workers=4)
        for symbol in fund_symbols:
            observation = completed.get(symbol, (symbol, None))[1]
            if observation:
                evidence[symbol] = observation
            else:
                gaps.append({"symbol": symbol, "reason": failures.get(symbol, "fund source returned no dated NAV observations")})
        if "market" in failures:
            gaps.extend({"symbol": symbol, "reason": failures["market"]} for symbol in sorted(market_symbols))
        elif market_symbols:
            payload = completed.get("market") or {}
            provenance = payload.get("_provenance") or {}
            for symbol in sorted(market_symbols):
                observation = _observation(symbol, payload.get(symbol), "close", provenance.get(symbol) or {}, as_of)
                if observation:
                    evidence[symbol] = observation
                else:
                    gaps.append({"symbol": symbol, "reason": "market source returned no dated close observations"})
        report = assess_portfolio(account, evidence, as_of=as_of, rules=rules, discipline=discipline, identities=identities, redemption_confirmation_date=redemption_confirmation_date)
        report["data_gaps"].extend(gaps)
        report["account_source"] = {
            key: account.get(key) for key in ("source_ref", "reported_at", "quality", "valuation_date_range")
        }
        report["discipline_source"] = "user_recorded" if saved_discipline else "descriptive_defaults_no_personal_rules"
        report["recorded_discipline"] = discipline
        return {"ok": True, "data": report, "evidence": evidence,
                "inputs": {"account": account, "rules": rules, "discipline": discipline,
                           "identities": identities, "as_of": as_of,
                           "trading_sessions": (completed.get("calendar") or {}).get("trading_sessions", []),
                           "calendar_version": (completed.get("calendar") or {}).get("calendar_version", "unavailable"),
                           "redemption_confirmation_date": redemption_confirmation_date}}


def _observation(symbol: str, raw: Any, field: str, metadata: Mapping[str, Any], cutoff: str) -> dict[str, Any] | None:
    if hasattr(raw, "reset_index"):
        raw = raw.reset_index().to_dict("records")
    if isinstance(raw, dict):
        if raw.get("truncated"):
            return None  # sampled history cannot support consecutive-bar signals
        raw = raw.get("data")
    if not isinstance(raw, list):
        return None
    history: dict[str, float] = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        stamp = next((str(row[key])[:10] for key in ("date", "trade_date", "datetime", "timestamp", "index") if row.get(key) is not None), None)
        try:
            if not stamp or calendar_date(stamp) > cutoff:
                continue
            value = number(row.get(field), field, minimum=0)
        except (ValueError, TypeError):
            continue
        if value > 0:
            history[stamp] = value
    if not history:
        return None
    latest = max(history)
    expected_closed = None
    if metadata.get("finality") == "in_progress":
        from src.market_calendar import previous_trading_day
        previous = previous_trading_day(date.fromisoformat(latest))
        expected_closed = previous.isoformat() if previous else None
    return {
        "symbol": symbol, "field": field, "value": history[latest], "as_of": latest,
        "currency": metadata.get("currency") or metadata.get("quote_currency"),
        "source": metadata.get("source") or "unknown",
        "quality": metadata.get("quality") or "unknown", "fetched_at": metadata.get("fetched_at"),
        "quote_unit": metadata.get("quote_unit"),
        "finality": metadata.get("finality", "unknown"),
        "expected_closed_session": expected_closed,
        "history": [{"date": day, "value": value} for day, value in sorted(history.items())],
    }
