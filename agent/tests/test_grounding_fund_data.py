"""Grounding for off-exchange funds: identity, evidence, and release routing.

An ``.OF`` identity must lock like any listed instrument, its NAV rows must
become source-linked evidence, and a rejected draft about a fund must be sent
back to ``get_fund_data`` — never to ``get_market_data``, which cannot serve a
fund's NAV series at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agent.grounding import GroundingLedger
from src.agent.grounding.identity import (
    _CANONICAL_SYMBOL_RE,
    _infer_currency,
    _infer_instrument_type,
    _infer_venue,
    _scan_symbols,
)
from src.agent.grounding.release import MAX_PRICE_EVIDENCE_ATTEMPTS

pytestmark = pytest.mark.unit

FUND = "000051.OF"
# Canonical Shanghai spelling: the ``.SS`` alias folds onto ``.SH`` during
# normalization, so this is the symbol the ledger locks and authorizes.
POOL = "562500.SH"
FUND_NAME = "华夏沪深300ETF联接A"


def _ledger(tmp_path: Path) -> GroundingLedger:
    return GroundingLedger(run_dir=tmp_path, user_message=f"分析{FUND_NAME}并给出买入价")


def _resolver_payload(
    symbol: str = FUND,
    *,
    query: str = FUND_NAME,
    name: str = FUND_NAME,
    type_: str = "OTCFUND",
) -> str:
    return json.dumps(
        {
            "ok": True,
            "source": "symbol_search",
            "data": {
                "query": query,
                "count": 1,
                "candidates": [
                    {
                        "symbol": symbol,
                        "name": name,
                        "market": "cn",
                        "type": type_,
                        "source": "eastmoney",
                    }
                ],
                "sources": {"eastmoney": "ok"},
            },
        },
        ensure_ascii=False,
    )


def _fund_payload(symbol: str = FUND) -> str:
    return json.dumps(
        {
            symbol: {
                "data": [
                    {
                        "date": "2026-06-24",
                        "unit_nav": 1.734,
                        "cum_nav": 2.345,
                        "daily_growth": 0.52,
                    },
                    {
                        "date": "2026-06-23",
                        "unit_nav": 1.725,
                        "cum_nav": 2.336,
                        "daily_growth": -0.35,
                    },
                ],
                "_provenance": {
                    "source": "eastmoney",
                    "as_of": "2026-06-24",
                    "caliber": "nav",
                    "currency": "CNY",
                    "rows_available": 2,
                },
            },
            "_session": {
                "is_trading_day": True,
                "latest_nav_date": "2026-06-24",
                "source": "tushare",
            },
        }
    )


def _lock_fund(ledger: GroundingLedger, *, call_id: str = "resolve") -> None:
    ledger.ingest_tool_result(
        tool_name="search_symbol",
        arguments={"query": FUND_NAME},
        result=_resolver_payload(),
        call_id=call_id,
        success=True,
    )


class TestFundIdentityContract:
    def test_the_canonical_regex_sees_the_of_suffix(self):
        assert _CANONICAL_SYMBOL_RE.fullmatch(FUND) is not None
        assert _CANONICAL_SYMBOL_RE.fullmatch("000051.of") is not None

    def test_scan_symbols_canonicalizes_the_case(self):
        assert _scan_symbols("000051.of 的单位净值是 1.7340") == {FUND}

    def test_venue_currency_and_instrument_type(self):
        assert _infer_venue(FUND) == "otc_fund"
        assert _infer_currency(FUND) == "CNY"
        assert _infer_instrument_type(FUND) == "fund"

    def test_a_provider_type_cell_cannot_outrank_the_of_marker(self):
        # A feeder fund's live type cell reads "股票型-标准指数"; the ``.OF``
        # marker has to win, or the identity reads as a listed security.
        assert _infer_instrument_type(FUND, "股票型-标准指数") == "fund"
        assert _infer_instrument_type(FUND, "OTCFUND") == "fund"


class TestFundIdentityLock:
    def test_the_resolver_locks_the_fund_identity(self, tmp_path: Path):
        ledger = _ledger(tmp_path)
        _lock_fund(ledger)

        summary = ledger.identity_summary()
        assert summary["status"] == "locked"
        assert summary["authorized_symbols"] == [FUND]
        record = summary["records"][-1]
        assert record["symbol"] == FUND
        assert record["instrument_type"] == "fund"
        assert record["currency"] == "CNY"
        # The resolver's own market label wins for the lock; the symbol-derived
        # venue for an ``.OF`` code is ``otc_fund``.
        assert record["venue"] == "cn"

    def test_a_bare_code_is_authorized_against_the_fund_lock(self, tmp_path: Path):
        ledger = _ledger(tmp_path)
        _lock_fund(ledger)

        summary = ledger.identity_summary()
        assert summary["authorized_symbols"] == [FUND]


class TestFundEvidence:
    def test_nav_rows_become_observed_evidence(self, tmp_path: Path):
        ledger = _ledger(tmp_path)
        ledger.ingest_tool_result(
            tool_name="get_fund_data",
            arguments={"codes": ["000051"]},
            result=_fund_payload(),
            call_id="fund",
            success=True,
        )

        records = [item for item in ledger._evidence if item.tool == "get_fund_data"]
        # Two rows x three numeric leaves; the date is the timestamp, never a
        # value of its own.
        assert len(records) == 6
        assert {item.field for item in records} == {"unit_nav", "cum_nav", "daily_growth"}
        assert {item.timestamp for item in records} == {"2026-06-24", "2026-06-23"}
        for item in records:
            assert item.symbol == FUND
            assert item.source == "eastmoney"
            assert item.status == "observed"
            assert item.currency == "CNY"
            assert item.venue == "otc_fund"

    def test_money_market_rows_keep_their_own_caliber(self, tmp_path: Path):
        payload = json.dumps(
            {
                FUND: {
                    "data": [
                        {
                            "date": "2026-06-24",
                            "income_per_10k": 0.6543,
                            "seven_day_yield": 1.812,
                        }
                    ],
                    "_provenance": {
                        "source": "eastmoney",
                        "as_of": "2026-06-24",
                        "caliber": "money_market",
                        "currency": "CNY",
                    },
                },
                "_session": {},
            }
        )
        ledger = _ledger(tmp_path)
        ledger.ingest_tool_result(
            tool_name="get_fund_data",
            arguments={"codes": ["000051"]},
            result=payload,
            call_id="fund",
            success=True,
        )

        records = [item for item in ledger._evidence if item.tool == "get_fund_data"]
        assert {item.field for item in records} == {"income_per_10k", "seven_day_yield"}

    def test_unresolved_codes_become_unavailable_evidence(self, tmp_path: Path):
        payload = json.dumps(
            {
                "_session": {},
                "_unresolved": ["600519.SH", "999999.OF"],
            }
        )
        ledger = _ledger(tmp_path)
        ledger.ingest_tool_result(
            tool_name="get_fund_data",
            arguments={"codes": ["600519.SH", "999999.OF"]},
            result=payload,
            call_id="fund",
            success=True,
        )

        records = [item for item in ledger._evidence if item.tool == "get_fund_data"]
        assert [(item.symbol, item.status, item.field) for item in records] == [
            ("600519.SH", "unavailable", "availability"),
            ("999999.OF", "unavailable", "availability"),
        ]

    def test_malformed_and_failed_results_are_recorded_as_tool_failures(
        self, tmp_path: Path
    ):
        ledger = _ledger(tmp_path)
        ledger.ingest_tool_result(
            tool_name="get_fund_data",
            arguments={"codes": ["000051"]},
            result="not json",
            call_id="fund-bad-body",
            success=True,
        )
        ledger.ingest_tool_result(
            tool_name="get_fund_data",
            arguments={"codes": ["000051"]},
            result=json.dumps({"ok": False, "error": "provider refused"}),
            call_id="fund-refused",
            success=False,
        )

        failures = [item for item in ledger._tool_failures if item["tool"] == "get_fund_data"]
        assert [(item["call_id"], item["status"]) for item in failures] == [
            ("fund-bad-body", "unavailable"),
            ("fund-refused", "unavailable"),
        ]


class TestFundRecoveryRouting:
    def test_missing_nav_evidence_routes_to_the_fund_tool(self, tmp_path: Path):
        ledger = _ledger(tmp_path)
        _lock_fund(ledger)

        validation = ledger.validate_final_answer(f"{FUND} 现价 9.999。")

        assert ledger.identity_status == "locked"
        assert ledger.recovery_action(validation) == "get_fund_data"

    def test_the_fund_correction_prompt_names_the_fund_tool(self, tmp_path: Path):
        ledger = _ledger(tmp_path)
        _lock_fund(ledger)
        validation = ledger.validate_final_answer(f"{FUND} 现价 9.999。")

        prompt = ledger.correction_prompt(validation)

        assert "get_fund_data" in prompt
        assert ".OF" in prompt
        assert "get_market_data" not in prompt
        assert "Do NOT ask the user to confirm or continue" in prompt

    def test_the_fund_recovery_prompt_is_executable(self, tmp_path: Path):
        ledger = _ledger(tmp_path)
        _lock_fund(ledger)
        validation = ledger.validate_final_answer(f"{FUND} 现价 9.999。")

        prompt = ledger.recovery_prompt("get_fund_data", validation)

        assert prompt.startswith("[GROUNDING RECOVERY]")
        assert "get_fund_data" in prompt
        assert ".OF" in prompt
        assert "Do NOT ask the user to confirm or continue" in prompt

    def test_fund_recovery_spends_the_price_evidence_budget(self, tmp_path: Path):
        ledger = _ledger(tmp_path)
        _lock_fund(ledger)
        validation = ledger.validate_final_answer(f"{FUND} 现价 9.999。")

        for _ in range(MAX_PRICE_EVIDENCE_ATTEMPTS):
            assert ledger.recovery_action(validation) == "get_fund_data"
            ledger.record_recovery("get_fund_data")

        assert ledger.recovery_summary()["price_evidence_attempts"] == (
            MAX_PRICE_EVIDENCE_ATTEMPTS
        )
        assert ledger.recovery_action(validation) is None

    def test_a_listed_identity_keeps_the_market_tool(self, tmp_path: Path):
        ledger = _ledger(tmp_path)
        ledger.ingest_tool_result(
            tool_name="search_symbol",
            arguments={"query": "机器人ETF"},
            result=_resolver_payload(symbol=POOL, query="机器人ETF", name="机器人ETF", type_="ETF"),
            call_id="resolve",
            success=True,
        )
        validation = ledger.validate_final_answer(f"{POOL} 现价 9.999。")

        assert ledger.recovery_action(validation) == "get_market_data"

    def test_a_mixed_lock_does_not_prefer_the_fund_tool(self, tmp_path: Path):
        ledger = _ledger(tmp_path)
        _lock_fund(ledger, call_id="resolve-fund")
        ledger.ingest_tool_result(
            tool_name="search_symbol",
            arguments={"query": "机器人ETF"},
            result=_resolver_payload(symbol=POOL, query="机器人ETF", name="机器人ETF", type_="ETF"),
            call_id="resolve-etf",
            success=True,
        )

        assert ledger.authorized_symbols == {FUND, POOL}
        validation = ledger.validate_final_answer(f"{FUND} 现价 9.999。")
        assert ledger.recovery_action(validation) == "get_market_data"

    def test_no_lock_keeps_the_market_tool_default(self, tmp_path: Path):
        ledger = _ledger(tmp_path)
        assert ledger.authorized_symbols == set()
        assert ledger._prefer_fund_recovery() is False

    def test_a_wrong_figure_is_corrected_not_refetched(self, tmp_path: Path):
        """A mismatch against observed NAV is a correction, not missing data."""
        ledger = _ledger(tmp_path)
        _lock_fund(ledger)
        ledger.ingest_tool_result(
            tool_name="get_fund_data",
            arguments={"codes": ["000051"]},
            result=_fund_payload(),
            call_id="fund",
            success=True,
        )
        validation = ledger.validate_final_answer(f"{FUND} 现价 9.999。")

        assert validation.valid is False
        assert ledger.recovery_action(validation) is None

    def test_a_grounded_fund_answer_needs_no_recovery(self, tmp_path: Path):
        ledger = _ledger(tmp_path)
        _lock_fund(ledger)
        ledger.ingest_tool_result(
            tool_name="get_fund_data",
            arguments={"codes": ["000051"]},
            result=_fund_payload(),
            call_id="fund",
            success=True,
        )
        validation = ledger.validate_final_answer(
            f"{FUND}（eastmoney，CNY）2026-06-24 单位净值 1.7340。"
        )

        assert validation.valid is True
        assert ledger.recovery_action(validation) is None