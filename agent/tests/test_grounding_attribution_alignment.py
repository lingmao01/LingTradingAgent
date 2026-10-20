"""One number, several instruments: attribution is read as the answer writes it.

Five honest readings the gate has to keep — and the dishonest ones each of
them could be bent into:

* an ETF's twin feeder classes (A/C) declare the same daily move, so the
  block's first covering declaration may be the sibling the row is not about;
* an index is named inline beside its own close while the table row's symbol
  cell resolves to a wrapper fund;
* a derivation is anchored by the user's own amounts on a ledger holding
  several instruments' bars;
* a declaration writes its instrument's own name or bare code ("HSTECH SMA20",
  "你 09-29 台账：019667 收益") where the row spells it in full;
* a derivation rests on a total the block itself summed from those amounts.

Every rule is pinned from both sides: the reading that must survive and the
near-miss that must still be refused.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agent.grounding import GroundingLedger

pytestmark = pytest.mark.unit

TWIN_A = "012733.OF"
TWIN_C = "012734.OF"
WRAPPER = "005658.OF"
SH = "000001.SH"
HS300 = "000300.SH"
KC50 = "000688.SH"
CY = "399006.SZ"
HSTECH = "HSTECH.HK"
FEEDER = "013309.OF"
DRUG = "019667.OF"


def _nav(
    symbol: str,
    *,
    unit_nav: float,
    daily_growth: float,
    date: str = "2026-10-08",
) -> str:
    """One fund's NAV row in the shape ``get_fund_data`` returns."""
    return json.dumps(
        {
            symbol: {
                "data": [
                    {
                        "date": date,
                        "unit_nav": unit_nav,
                        "cum_nav": unit_nav,
                        "daily_growth": daily_growth,
                    }
                ],
                "_provenance": {
                    "source": "eastmoney",
                    "as_of": date,
                    "caliber": "nav",
                    "currency": "CNY",
                },
            },
            "_session": {"source": "tushare"},
        }
    )


def _bars(symbol: str, close: float, *, date: str = "2026-10-08") -> str:
    """One index's daily bar in the shape ``get_market_data`` returns."""
    return json.dumps(
        {
            symbol: [
                {
                    "trade_date": date,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1,
                }
            ],
            "_provenance": {
                symbol: {"source": "akshare", "currency_conversion": "none"}
            },
        }
    )


def _ledger(
    tmp_path: Path,
    *calls: tuple[str, dict[str, Any], Any, str],
    message: str = "",
) -> GroundingLedger:
    ledger = GroundingLedger(
        run_dir=tmp_path, user_message=message or f"分析 {TWIN_A} 与 {TWIN_C}"
    )
    for tool, arguments, payload, call_id in calls:
        ledger.ingest_tool_result(
            tool_name=tool,
            arguments=arguments,
            result=payload if isinstance(payload, str) else json.dumps(payload),
            call_id=call_id,
            success=True,
        )
    return ledger


def _block(*rows: str) -> str:
    return "\n\n```figures\n" + "\n".join(rows) + "\n```"


def _reasons(result: Any) -> list[str]:
    return [str(issue.get("reason")) for issue in result.issues]


def _codes(result: Any) -> list[str]:
    return [str(issue.get("code")) for issue in result.issues]


NAV_A = ("get_fund_data", {"codes": ["012733"]}, _nav(TWIN_A, unit_nav=1.8595, daily_growth=-2.97), "call_a")
NAV_C = ("get_fund_data", {"codes": ["012734"]}, _nav(TWIN_C, unit_nav=1.8513, daily_growth=-2.97), "call_c")


# ---------------------------------------------------------------------------
# A — twin share classes declaring the same move
# ---------------------------------------------------------------------------


def test_twin_share_declarations_align_with_each_row(tmp_path: Path) -> None:
    """Each feeder class declares its own -2.97%; the row reads its own declaration."""
    table = (
        "\n\n| 代码 | 名称 | 单位净值 | 日涨跌 |\n"
        "|---|---|---|---|\n"
        f"| {TWIN_A} | 易方达人工智能ETF联接A | 1.8595 | −2.97% |\n"
        f"| {TWIN_C} | 易方达人工智能ETF联接C | 1.8513 | −2.97% |\n"
    )
    block = _block(
        "1.8595 | observed | 012733.OF 单位净值 2026-10-08 | call_a",
        "−2.97% | observed | 012733.OF 日涨跌 2026-10-08 | call_a",
        "1.8513 | observed | 012734.OF 单位净值 2026-10-08 | call_c",
        "−2.97% | observed | 012734.OF 日涨跌 2026-10-08 | call_c",
    )

    result = _ledger(tmp_path, NAV_A, NAV_C).validate_final_answer(
        "科技簇两只基金（eastmoney，CNY）" + table + block
    )

    assert result.valid is True, result.issues


def test_a_single_declaration_cannot_cover_the_twin_row(tmp_path: Path) -> None:
    """Only one -2.97% is declared (for the A class); the C row is still a conflict."""
    table = (
        "\n\n| 代码 | 日涨跌 |\n"
        "|---|---|\n"
        f"| {TWIN_A} | −2.97% |\n"
        f"| {TWIN_C} | −2.97% |\n"
    )
    block = _block("−2.97% | observed | 012733.OF 日涨跌 2026-10-08 | call_a")

    result = _ledger(tmp_path, NAV_A, NAV_C).validate_final_answer(
        "孪生两只（eastmoney，CNY）" + table + block
    )

    assert result.valid is False
    assert "symbol_mismatch" in _reasons(result), result.issues


# ---------------------------------------------------------------------------
# B — the index named inline beside its own close
# ---------------------------------------------------------------------------


def _index_ledger(tmp_path: Path) -> GroundingLedger:
    funds = {
        "008585.OF": 1.41,
        "012733.OF": 1.8595,
        "012734.OF": 1.8513,
        "008887.OF": 1.22,
        "018344.OF": 1.05,
        "017854.OF": 0.98,
        "011613.OF": 1.33,
    }
    payload = json.dumps(
        {
            symbol: {
                "data": [
                    {
                        "date": "2026-10-08",
                        "unit_nav": nav,
                        "cum_nav": nav,
                        "daily_growth": -0.5,
                    }
                ],
                "_provenance": {
                    "source": "eastmoney",
                    "as_of": "2026-10-08",
                    "caliber": "nav",
                    "currency": "CNY",
                },
            }
            for symbol, nav in funds.items()
        }
        | {
            WRAPPER: {
                "data": [
                    {
                        "date": "2026-10-08",
                        "unit_nav": 2.06,
                        "cum_nav": 2.06,
                        "daily_growth": 0.3,
                    }
                ],
                "_provenance": {
                    "source": "eastmoney",
                    "as_of": "2026-10-08",
                    "caliber": "nav",
                    "currency": "CNY",
                },
            },
            "_session": {"source": "tushare"},
        }
    )
    ledger = GroundingLedger(run_dir=tmp_path, user_message="检查科技簇持仓纪律")
    ledger.ingest_tool_result(
        tool_name="get_fund_data",
        arguments={"codes": list(funds) + [WRAPPER]},
        result=payload,
        call_id="funds",
        success=True,
    )
    for call_id, symbol, close in (
        ("call_sh", SH, 3890.51),
        ("call_hs300", HS300, 4466.94),
        ("call_kc", KC50, 1578.34),
    ):
        ledger.ingest_tool_result(
            tool_name="get_market_data",
            arguments={"codes": [symbol]},
            result=_bars(symbol, close),
            call_id=call_id,
            success=True,
        )
    return ledger


def _cluster_table() -> str:
    # Verbatim from the run: the row's first symbol cell resolves to the first
    # fund in the cluster, while each close is named with its own index inline.
    return (
        "\n\n| 标的 | 状态 | 触发条件 | 动作 |\n"
        "|---|---|---|---|\n"
        "| 科技簇：008585.OF / 012733.OF / 012734.OF / 008887.OF / "
        "018344.OF / 017854.OF / 011613.OF | 持有 | 收盘 ≥ SMA20"
        "（上证 3890.51 / 沪深300 4466.94 / 科创50 1578.34，均为观测值） "
        "| 收盘仍 < SMA20 则继续不动 |\n"
        f"| {WRAPPER} 沪深300 | 持有 | 沪深300 收盘 ≥ 4466.94 | 同上 |\n"
    )


def test_an_inline_index_label_confirms_the_declared_index(tmp_path: Path) -> None:
    """A close written beside its index's name is about that index, not the row's fund."""
    block = _block(
        "3890.51 | observed | 000001.SH 收盘 2026-10-08 | call_sh",
        "4466.94 | observed | 000300.SH 收盘 2026-10-08 | call_hs300",
        "4466.94 | observed | 000300.SH 收盘 2026-10-08 | call_hs300",
        "1578.34 | observed | 000688.SH 布林中轨(≈SMA20) | call_kc",
    )

    result = _index_ledger(tmp_path).validate_final_answer(
        f"{WRAPPER} 跟踪沪深300（eastmoney/akshare，CNY）" + _cluster_table() + block
    )

    assert result.valid is True, result.issues


def test_a_wrapper_fund_name_is_not_an_inline_label(tmp_path: Path) -> None:
    """'华夏沪深300ETF联接C' merely contains the index name; it is no label."""
    table = (
        "\n\n| 代码 | 单位净值 |\n"
        "|---|---|\n"
        f"| {WRAPPER} 华夏沪深300ETF联接C | 4466.94 |\n"
    )
    block = _block("4466.94 | observed | 000300.SH 收盘 2026-10-08 | call_hs300")

    result = _index_ledger(tmp_path).validate_final_answer(
        f"{WRAPPER}（eastmoney/akshare，CNY）" + table + block
    )

    assert result.valid is False
    assert "symbol_mismatch" in _reasons(result), result.issues


def test_an_inline_label_for_a_different_index_is_still_a_conflict(tmp_path: Path) -> None:
    """The sentence names 上证 beside 3890.51 but the declaration says 沪深300."""
    table = (
        "\n\n| 代码 | 触发条件 |\n"
        "|---|---|\n"
        f"| {WRAPPER} | 上证 3890.51 |\n"
    )
    block = _block("3890.51 | observed | 000300.SH 收盘 2026-10-08 | call_hs300")

    result = _index_ledger(tmp_path).validate_final_answer(
        f"{WRAPPER}（eastmoney/akshare，CNY）" + table + block
    )

    assert result.valid is False
    assert "symbol_mismatch" in _reasons(result), result.issues


@pytest.mark.parametrize(
    "trigger",
    [
        # Verbatim shape from the run: the row's fund cell is followed by a
        # trigger whose sentence names the index, a direction phrase and the
        # indicator mention between that name and the number.
        "加到 ADD ALLOWED：000300.SH 收盘站上 SMA20(4466.94) 且 MACD 柱连续 2 日转正",
        # The same reading with the index's display name and a possessive.
        "沪深300 收盘站上其 SMA20 4466.94 才恢复加仓",
    ],
)
def test_a_trigger_sentence_naming_the_index_confirms_its_sma(
    tmp_path: Path, trigger: str
) -> None:
    """The trigger cell writes the index's own SMA beside the index's name."""
    table = (
        "\n\n| 代码 | 状态 | 触发条件 |\n"
        "|---|---|---|\n"
        f"| {WRAPPER} | 持有 | {trigger} |\n"
    )
    block = _block("4466.94 | observed | 000300.SH SMA20（布林中轨）| call_hs300")

    result = _index_ledger(tmp_path).validate_final_answer(
        f"{WRAPPER}（eastmoney/akshare，CNY）" + table + block
    )

    assert result.valid is True, result.issues


# ---------------------------------------------------------------------------
# C — the user's own amounts anchoring a derivation
# ---------------------------------------------------------------------------


def _two_index_ledger(tmp_path: Path, *, message: str) -> GroundingLedger:
    return _ledger(
        tmp_path,
        ("get_market_data", {"codes": [SH]}, _bars(SH, 3890.51), "c_sh"),
        ("get_market_data", {"codes": [HS300]}, _bars(HS300, 4466.94), "c_hs300"),
        message=message,
    )


def test_the_users_own_amounts_anchor_a_derivation(tmp_path: Path) -> None:
    """268996 / 389911 ≈ 69% rests on the amounts the user stated, verbatim."""
    ledger = _two_index_ledger(
        tmp_path,
        message="我的科技簇市值 268996 元，基金总市值 389911 元，帮我算占比",
    )
    block = _block(
        "3890.51 | observed | 000001.SH 收盘 | c_sh",
        "4466.94 | observed | 000300.SH 收盘 | c_hs300",
        "69% | derived | 268996 / 389911 | held_user_figures",
    )

    result = ledger.validate_final_answer(
        "我的科技簇市值 268,996 元，基金总市值 389,911 元。"
        "科技簇占比 = 268,996 / 389,911 ≈ 69%。"
        "上证与沪深300（akshare，CNY）仅供参考。" + block
    )

    assert result.valid is True, result.issues


def test_observed_operands_from_several_symbols_still_need_a_symbol(
    tmp_path: Path,
) -> None:
    """With two indexes' bars and no user figures, 4466.94 / 3890.51 borrows by accident."""
    ledger = _two_index_ledger(tmp_path, message="分析上证与沪深300")
    block = _block(
        "3890.51 | observed | 000001.SH 收盘 | c_sh",
        "4466.94 | observed | 000300.SH 收盘 | c_hs300",
        "1.148 | derived | 4466.94 / 3890.51 | akshare",
    )

    result = ledger.validate_final_answer(
        "上证与沪深300（akshare，CNY）之比约 1.148。" + block
    )

    assert result.valid is False
    assert "no_symbol" in _reasons(result), result.issues


# ---------------------------------------------------------------------------
# D — a declaration's display-name label names its index
# ---------------------------------------------------------------------------


def _label_ledger(tmp_path: Path) -> GroundingLedger:
    """Two indexes whose SMA20 levels the draft labels by display name alone."""
    ledger = GroundingLedger(
        run_dir=tmp_path,
        user_message="请复盘科创50与创业板指并给出收复参考位",
    )
    for prefix, symbol, close, sma in (
        ("kc", KC50, 1578.34, 1580.803),
        ("cy", CY, 3244.20, 3277.104),
    ):
        ledger.ingest_tool_result(
            tool_name="get_market_data",
            arguments={"codes": [symbol]},
            result=_bars(symbol, close),
            call_id=f"c_{prefix}_bars",
            success=True,
        )
        ledger.ingest_tool_result(
            tool_name="technical_indicators",
            arguments={"symbol": symbol},
            result=json.dumps(
                {
                    "ok": True,
                    "symbol": symbol,
                    "latest_close": close,
                    "indicators": {"sma_20": sma},
                }
            ),
            call_id=f"c_{prefix}_ind",
            success=True,
        )
    return ledger


def test_a_declarations_display_name_label_names_its_index(tmp_path: Path) -> None:
    """The replay's two recovery levels: labelled by name, no canonical code.

    Verbatim shape from the run: the note is "科创50 触发参考位，取观测 SMA20
    = 1580.803" with no ref at all, and the run held prices for two indexes —
    so before the label rule the level was answered with no_symbol even though
    the index it is about sat at the head of its own note.
    """
    block = _block(
        "1580.80 | proposed | 科创50 触发参考位，取观测 SMA20 = 1580.803",
        "3277.10 | proposed | 创业板触发参考位，取观测 SMA20 = 3277.104",
        "1580.803 | observed | 000688.SH SMA20 2026-10-09",
        "3277.104 | observed | 399006.SZ SMA20 2026-10-09",
    )

    result = _label_ledger(tmp_path).validate_final_answer(
        "参考位：科创50 需收复 **1580.80** 上方、创业板需收复 **3277.10** 上方"
        "（proposed，取自已观测的 SMA20）。\n"
        "000688.SH 与 399006.SZ 的行情与均线（akshare，CNY）如下。" + block
    )

    assert "no_symbol" not in _reasons(result), result.issues
    assert result.valid is True, result.issues


def test_a_fund_name_extension_is_not_read_as_an_index_label(tmp_path: Path) -> None:
    """'沪深300ETF联接C 触发参考位' names the fund, never the index inside it.

    The near-miss of the test above: the same head position, the same known
    index label — but the label runs straight on into a fund's display name, so
    it is one instrument's name and the level stays unattributed (no_symbol),
    not silently read as the index's.
    """
    ledger = _ledger(
        tmp_path,
        ("get_market_data", {"codes": [HS300]}, _bars(HS300, 4466.94), "c_hs300"),
        (
            "get_fund_data",
            {"codes": ["005658"]},
            _nav(WRAPPER, unit_nav=2.06, daily_growth=0.3),
            "c_wrapper",
        ),
    )
    block = _block(
        "4466.94 | proposed | 沪深300ETF联接C 触发参考位",
        "4466.94 | observed | 000300.SH 收盘 2026-10-08 | c_hs300",
        "2.06 | observed | 005658.OF 单位净值 2026-10-08 | c_wrapper",
    )

    result = ledger.validate_final_answer(
        "华夏沪深300ETF联接C 的触发参考位为 4466.94（eastmoney/akshare，CNY）。\n"
        "005658.OF 为其场外代码。" + block
    )

    assert "no_symbol" in _reasons(result), result.issues


# ---------------------------------------------------------------------------
# E — a declaration that spells its instrument bare
# ---------------------------------------------------------------------------


def _bare_code_ledger(tmp_path: Path, *, message: str) -> GroundingLedger:
    """An index's own level beside two funds, each able to cover −2.54%."""
    ledger = GroundingLedger(run_dir=tmp_path, user_message=message)
    ledger.ingest_tool_result(
        tool_name="get_market_data",
        arguments={"codes": [HSTECH]},
        result=_bars(HSTECH, 4169.57),
        call_id="c_hstech",
        success=True,
    )
    ledger.ingest_tool_result(
        tool_name="technical_indicators",
        arguments={"symbol": HSTECH},
        result=json.dumps(
            {
                "ok": True,
                "symbol": HSTECH,
                "latest_close": 4169.57,
                "indicators": {"sma_20": 4278.197},
            }
        ),
        call_id="c_ind",
        success=True,
    )
    for prefix, symbol, nav in (("drug", DRUG, 1.0135), ("twin", TWIN_C, 1.854)):
        ledger.ingest_tool_result(
            tool_name="get_fund_data",
            arguments={"codes": [symbol.split(".")[0]]},
            result=_nav(symbol, unit_nav=nav, daily_growth=0.38),
            call_id=f"c_{prefix}",
            success=True,
        )
    return ledger


#: The same −2.54% twice: HSTECH's fall to its SMA20, and the fund's own ledger
#: return. The block's first covering declaration is therefore the index's.
_TWO_254 = (
    f"−2.54% | derived | ({4169.57} − 4278.197) / 4278.197 | {HSTECH}",
    "−2.54% | held | 你 09-29 台账：019667 收益",
)


def test_a_declarations_bare_code_names_the_fund_the_row_writes(
    tmp_path: Path,
) -> None:
    """The held line writes "019667" alone; the row it lands in writes 019667.OF.

    Verbatim shape from the run: the ledger's held lines name the fund by its
    code with no venue suffix, so the declaration resolved to no instrument and
    the index's declaration covered the row's number instead.
    """
    table = (
        "\n\n| 代码 | 单位净值 | 台账收益 |\n"
        "|---|---|---|\n"
        f"| {DRUG} | 1.0135 | −2.54% |\n"
    )
    block = _block(*_TWO_254, "019667 | held | 你 09-29 台账：019667 金额")

    result = _bare_code_ledger(
        tmp_path, message="019667 金额 30721，收益 −2.54%"
    ).validate_final_answer("持仓（eastmoney/akshare，CNY/HKD）" + table + block)

    assert result.valid is True, result.issues


def test_a_bare_code_naming_another_fund_is_still_a_conflict(tmp_path: Path) -> None:
    """The held line writes "012734" while the row is about 019667.OF."""
    table = (
        "\n\n| 代码 | 单位净值 | 台账收益 |\n"
        "|---|---|---|\n"
        f"| {DRUG} | 1.0135 | −2.54% |\n"
    )
    block = _block(
        _TWO_254[0],
        "−2.54% | held | 你 09-29 台账：012734 收益",
        "012734 | held | 你 09-29 台账：012734 金额",
    )

    result = _bare_code_ledger(
        tmp_path, message="019667 金额 30721，收益 −2.54%；012734 收益 −2.54%"
    ).validate_final_answer("持仓（eastmoney，CNY）" + table + block)

    assert "symbol_mismatch" in _reasons(result), result.issues


def test_two_bare_codes_in_one_note_name_neither(tmp_path: Path) -> None:
    """A note writing two funds' bare codes names neither, so the row stands."""
    table = (
        "\n\n| 代码 | 单位净值 | 台账收益 |\n"
        "|---|---|---|\n"
        f"| {DRUG} | 1.0135 | −2.54% |\n"
    )
    block = _block(
        _TWO_254[0],
        "−2.54% | held | 你 09-29 台账：019667 与 012734 合并收益",
    )

    result = _bare_code_ledger(
        tmp_path, message="019667 与 012734 合并收益 −2.54%"
    ).validate_final_answer("持仓（eastmoney，CNY）" + table + block)

    assert "symbol_mismatch" in _reasons(result), result.issues


# ---------------------------------------------------------------------------
# F — a row that names both the index and the fund it feeds
# ---------------------------------------------------------------------------


def _feeder_ledger(tmp_path: Path) -> GroundingLedger:
    """HSTECH's own level beside the feeder fund the row's symbol cell resolves to."""
    ledger = GroundingLedger(
        run_dir=tmp_path, user_message=f"分析 {HSTECH} 与我持有的 {FEEDER}"
    )
    ledger.ingest_tool_result(
        tool_name="technical_indicators",
        arguments={"symbol": HSTECH},
        result=json.dumps(
            {
                "ok": True,
                "symbol": HSTECH,
                "latest_close": 4169.57,
                "indicators": {"sma_20": 4278.197},
            }
        ),
        call_id="c_ind",
        success=True,
    )
    ledger.ingest_tool_result(
        tool_name="get_fund_data",
        arguments={"codes": [FEEDER.split(".")[0]]},
        result=_nav(FEEDER, unit_nav=0.9822, daily_growth=2.82),
        call_id="c_fund",
        success=True,
    )
    return ledger


def _combined_row(trigger: str) -> str:
    return (
        "\n\n| 标的 | 状态 | 触发条件 |\n"
        "|---|---|---|\n"
        f"| {trigger} | 持有·禁加仓 | 收盘 ≥ **4278.197**(SMA20) 且 MACD柱连续 2 日转正 |\n"
    )


def test_a_row_naming_both_instruments_confirms_the_index_level(
    tmp_path: Path,
) -> None:
    """The row writes "HSTECH / 013309.OF": one row about both, not a misattribution."""
    block = _block("4278.197 | observed | HSTECH.HK SMA20 | c_ind")

    result = _feeder_ledger(tmp_path).validate_final_answer(
        "恒生科技与联接基金（eastmoney，CNY）"
        + _combined_row(f"{HSTECH} / {FEEDER}")
        + block
    )

    assert result.valid is True, result.issues


def test_a_declarations_index_name_names_the_row_it_sits_in(tmp_path: Path) -> None:
    """The note writes the index's own name ("HSTECH SMA20"), not its code."""
    block = _block("4278.197 | observed | HSTECH SMA20 | c_ind")

    result = _feeder_ledger(tmp_path).validate_final_answer(
        "恒生科技与联接基金（eastmoney，CNY）"
        + _combined_row(f"{HSTECH} / {FEEDER}")
        + block
    )

    assert result.valid is True, result.issues


def test_a_row_naming_only_the_feeder_fund_is_still_a_conflict(
    tmp_path: Path,
) -> None:
    """The same level in a row about the fund alone belongs to the fund."""
    block = _block("4278.197 | observed | HSTECH.HK SMA20 | c_ind")

    result = _feeder_ledger(tmp_path).validate_final_answer(
        "联接基金（eastmoney，CNY）" + _combined_row(FEEDER) + block
    )

    assert "symbol_mismatch" in _reasons(result), result.issues


# ---------------------------------------------------------------------------
# G — a derivation over the block's own totals
# ---------------------------------------------------------------------------


def test_a_weight_over_the_blocks_own_totals_rests_on_the_users_amounts(
    tmp_path: Path,
) -> None:
    """75% = 150000 / 200000, both totals the block itself summed from the user's amounts."""
    ledger = _two_index_ledger(
        tmp_path, message="我的科技簇金额 100000 元，其他持仓金额 50000 元"
    )
    block = _block(
        "3890.51 | observed | 000001.SH 收盘 | c_sh",
        "4466.94 | observed | 000300.SH 收盘 | c_hs300",
        "100000 | held | 你自述：科技簇金额",
        "50000 | held | 你自述：其他持仓金额",
        "150000 | derived | 100000 + 50000 | 你自述的金额",
        "200000 | derived | 150000 + 50000 | 你自述的金额",
        "75% | derived | 150000 / 200000 | 你自述的金额",
    )

    result = ledger.validate_final_answer(
        "科技簇占基金总市值的 **75%**（据你自述金额推算）。"
        "上证与沪深300（akshare，CNY）仅供参考。" + block
    )

    assert result.valid is True, result.issues


def test_a_weight_over_numbers_the_chain_never_reaches_is_still_refused(
    tmp_path: Path,
) -> None:
    """60% = 120000 / 200000 where the chain leaves both of them unanchored."""
    ledger = _two_index_ledger(
        tmp_path, message="我的科技簇金额 100000 元，其他持仓金额 50000 元"
    )
    block = _block(
        "3890.51 | observed | 000001.SH 收盘 | c_sh",
        "4466.94 | observed | 000300.SH 收盘 | c_hs300",
        "100000 | held | 你自述：科技簇金额",
        "50000 | held | 你自述：其他持仓金额",
        "150000 | derived | 100000 + 50000 | 你自述的金额",
        "200000 | derived | 150000 + 400000 | 你自述的金额",
        "60% | derived | 120000 / 200000 | 你自述的金额",
    )

    result = ledger.validate_final_answer(
        "科技簇占比约 **60%**（据台账推算）。上证与沪深300（akshare，CNY）。" + block
    )

    assert "numeric_claim_conflict" in _codes(result), result.issues
