"""Real 2026-10-15 advice failures, with adverse controls for fact integrity."""
from __future__ import annotations

import json

import pytest

from src.agent.grounding import GroundingLedger
from src.agent.grounding.figures import parse_figures_block, scan_figures
from src.agent.grounding.policies import _formula_in_note

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("formula", "result", "operands"),
    [
        ("-1311723792 / 100000000", -13.11723792, [-1311723792.0, 100000000.0]),
        ("(-12 + -8) / 2", -10, [-12.0, -8.0, 2.0]),
        ("-(-12) / 2", 6, [12.0, 2.0]),
        ("-(12 + 8) / 2", -10, [12.0, 8.0, 2.0]),
    ],
)
def test_signed_observation_operands_survive_formula_parsing(formula, result, operands):
    actual = _formula_in_note(formula)
    assert actual is not None
    assert actual[0] == pytest.approx(result)
    assert actual[1] == operands


@pytest.mark.parametrize("date", ["10-15", "10/15", "11-12", "12-31"])
def test_quote_parenthetical_date_is_structure_in_a_table(date):
    text = f"| 代码 | 收盘 |\n|---|---|\n| 510330.SH | 4.427（{date}） |"
    figures = scan_figures(text, parse_figures_block(text))
    assert [(f.text, f.shape) for f in figures if f.text == "4.427"] == [("4.427", "measured")]
    assert all(f.shape == "exempt" for f in figures if f.text != "4.427")


@pytest.mark.parametrize(
    "text",
    [
        "| 区间 |\n|---|\n| 11-12 |",
        "| 买入价 |\n|---|\n| （11-12）元 |",
        "| 买入价 |\n|---|\n| 4.427（11-12）元 |",
        "| 收盘 |\n|---|\n| 4.427（13-32） |",
    ],
)
def test_a_price_range_or_invalid_date_is_not_exempted(text):
    figures = scan_figures(text, parse_figures_block(text))
    assert any(f.shape == "measured" and f.text != "4.427" for f in figures)


def _flows(tmp_path):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="分析 588000.SH 与 510330.SH 的资金流")
    ledger.ingest_tool_result(
        tool_name="get_fund_flow", arguments={"codes": ["588000.SH", "510330.SH"]}, call_id="flow",
        success=True, result=json.dumps({
            "ok": True, "source": "eastmoney", "data": {
                "588000.SH": {"symbol": "588000.SH", "rows": [
                    {"timestamp": "2026-10-15", "main": -1311723792, "super_large": -2669000000},
                ]},
                "510330.SH": {"symbol": "510330.SH", "source": "tushare", "rows": [
                    {"timestamp": "2026-10-15", "main": 200000000},
                ]},
            },
        }),
    )
    return ledger


def test_multi_symbol_flow_keeps_subject_source_and_signed_money(tmp_path):
    ledger = _flows(tmp_path)
    records = [r for r in ledger._evidence if r.field.endswith(".main")]
    assert [(r.symbol, r.source, r.value, r.currency) for r in records] == [
        ("588000.SH", "eastmoney", -1311723792, "CNY"),
        ("510330.SH", "tushare", 200000000, "CNY"),
    ]
    draft = (
        "588000.SH（eastmoney，CNY）2026-10-15 主力净流入 −13.12 亿元。\n"
        "```figures\n-13.12 | derived | -1311723792 / 100000000 | flow\n```"
    )
    verdict = ledger.revalidate(draft)
    assert verdict.valid, verdict.issues


def test_wrong_sign_or_unobserved_additive_flow_stays_blocked(tmp_path):
    ledger = _flows(tmp_path)
    for text, formula in [
        ("+13.12", "-1311723792 / 100000000"),
        ("-13.02", "(-1311723792 + 10000000) / 100000000"),
    ]:
        verdict = ledger.revalidate(
            f"588000.SH（eastmoney，CNY）2026-10-15 主力净流入 {text} 亿元。\n"
            f"```figures\n{text} | derived | {formula} | flow\n```"
        )
        assert not verdict.valid
