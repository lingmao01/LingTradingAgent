"""Indicator readings in their own units ground figures of their own kind.

The metric pool was built from field names alone: rsi and macd belong to no
analysis kind, so an observed reading of theirs had no pool to be checked
against, and the gate answered a real RSI value with the instrument's price
range — a value_mismatch the model then read as "the data is missing" and
retracted the indicator it had just retrieved (2026-10-08 replay, four
indices, twelve figures). ``technical_indicators`` now registers its metric
leaves the way it registers its price leaves, and the percent pool keeps the
registered readings out: an RSI of 55.2 must not stand in for a win rate
written as 55%.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agent.grounding import GroundingLedger
from src.agent.grounding.evidence import (
    _REGISTERED_METRIC_INDICATORS,
    _is_registered_metric_indicator,
    _is_registered_price_indicator,
)

pytestmark = pytest.mark.unit

SH = "000001.SH"
HS = "000300.SH"
KC = "000688.SH"
CY = "399006.SZ"

# The readings the replay's ``technical_indicators`` calls actually returned
# for the four indices (runs/20261008_192905_94_233b8e).
_READINGS = {
    SH: {"rsi": 36.781417426173746, "hist": -10.8789, "line": -22.2315, "signal": -11.3525, "ratio": 1.006368827427361},
    HS: {"rsi": 29.709802214391033, "hist": -15.9766, "line": -65.7525, "signal": -49.7758, "ratio": 1.124109663243993},
    KC: {"rsi": 32.20659389188286, "hist": -8.429, "line": -32.8391, "signal": -24.4101, "ratio": 1.2062082419507285},
    CY: {"rsi": 28.682770897463598, "hist": -21.2262, "line": -83.6563, "signal": -62.4301, "ratio": 0.9596155618617878},
}

_ROWS = {
    SH: {"open": 3869.09, "high": 3896.31, "low": 3857.42, "close": 3888.53},
    HS: {"open": 4556.30, "high": 4593.75, "low": 4541.88, "close": 4580.12},
    KC: {"open": 1191.75, "high": 1205.20, "low": 1187.44, "close": 1198.44},
    CY: {"open": 2872.66, "high": 2898.10, "low": 2865.02, "close": 2886.75},
}


def _bars(symbol: str) -> str:
    row = dict(_ROWS[symbol])
    row["trade_date"] = "2026-10-08"
    row["volume"] = 2_800_000
    return json.dumps(
        {
            symbol: [row],
            "_provenance": {symbol: {"source": "tushare", "currency_conversion": "none"}},
        }
    )


def _indicators(symbol: str) -> str:
    readings = _READINGS[symbol]
    return json.dumps(
        {
            "ok": True,
            "symbol": symbol,
            "latest_close": _ROWS[symbol]["close"],
            "indicators": {
                "rsi_14": readings["rsi"],
                "macd": {
                    "histogram": readings["hist"],
                    "macd_line": readings["line"],
                    "signal_line": readings["signal"],
                },
                "volume": {"ratio_20": readings["ratio"], "latest": 2_800_000},
            },
        }
    )


def _ledger(tmp_path: Path, *symbols: str) -> GroundingLedger:
    ledger = GroundingLedger(
        run_dir=tmp_path,
        user_message="请全面抓取整理当前数据与行情，给出科学合理的操作建议",
    )
    for index, symbol in enumerate(symbols, start=1):
        ledger.ingest_tool_result(
            tool_name="get_market_data",
            arguments={"codes": [symbol]},
            result=_bars(symbol),
            call_id=f"prices{index}",
            success=True,
        )
        ledger.ingest_tool_result(
            tool_name="technical_indicators",
            arguments={"symbol": symbol},
            result=_indicators(symbol),
            call_id=f"indicators{index}",
            success=True,
        )
    return ledger


def _block(*rows: str) -> str:
    return "\n\n```figures\n" + "\n".join(rows) + "\n```"


def _codes(result: object) -> list[str]:
    return [str(issue.get("code")) for issue in result.issues]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Registration is per tool AND per leaf, mirroring the price registrations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "path", "registered"),
    [
        ("technical_indicators", "indicators.rsi_14", True),
        ("technical_indicators", "indicators.macd.histogram", True),
        ("technical_indicators", "indicators.macd.macd_line", True),
        ("technical_indicators", "indicators.macd.signal_line", True),
        ("technical_indicators", "indicators.volume.ratio_20", True),
        ("technical_indicators", "indicators.volume.ratio_5", True),
        # A family prefix admits a numeric parameter only: "rsi_cross" is not a
        # reading. A leaf the tool registered as a price stays out of the metric
        # pool, and vice versa (sma_20 is level evidence, not a reading).
        ("technical_indicators", "indicators.rsi_cross", False),
        ("technical_indicators", "indicators.bollinger.bandwidth", False),
        ("technical_indicators", "indicators.sma_20", False),
        ("technical_indicators", "indicators.volume.latest", False),
        ("technical_indicators", "indicators.volume.sma_20", False),
        # Same leaf names, a tool nobody registered.
        ("custom_indicator_tool", "indicators.rsi_14", False),
        ("custom_indicator_tool", "indicators.macd.histogram", False),
    ],
)
def test_only_registered_metric_leaves_are_readings(
    tool: str, path: str, registered: bool
) -> None:
    assert _is_registered_metric_indicator(tool, path) is registered


def test_every_metric_registration_prefix_matches_something_it_names() -> None:
    """A registered prefix must classify the leaf it describes, and only that.

    The mirror of the price-registration check: the prefix itself matches, an
    unregistered tool never matches, and no leaf is both a price level and a
    reading — a registration that overlaps would double-pool the same value.
    """
    for tool, prefixes in _REGISTERED_METRIC_INDICATORS.items():
        for prefix in prefixes:
            assert _is_registered_metric_indicator(tool, prefix) is True, (tool, prefix)
            assert _is_registered_metric_indicator("other_tool", prefix) is False
            assert _is_registered_price_indicator(tool, prefix) is False, (tool, prefix)


# ---------------------------------------------------------------------------
# The replay: twelve observed readings the gate used to reject against prices
# ---------------------------------------------------------------------------


def test_the_replay_indicator_figures_are_grounded(tmp_path: Path) -> None:
    """Eight precise readings and four rounded restatements, all observed.

    Written as the replay wrote them: no call ref on the figure, so the check
    runs the generic pools — and before the registration the metric pool held
    no rsi or macd value, so the price range rejected every one of them.
    """
    ledger = _ledger(tmp_path, SH, HS, KC, CY)
    draft = (
        "000001.SH、000300.SH、000688.SH 与 399006.SZ（tushare，CNY）技术指标如下。\n"
        "000001.SH 的 RSI14 为 36.78，MACD 柱为 -10.88。\n"
        "000300.SH 的 RSI14 为 29.71，MACD 柱为 -15.98。\n"
        "000688.SH 的 RSI14 为 32.21，MACD 柱为 -8.43。\n"
        "399006.SZ 的 RSI14 为 28.68，MACD 柱为 -21.23。\n"
        "000688.SH 的量比为 1.21。\n"
        "000001.SH 的 RSI14 四舍五入为 36.8。"
        "399006.SZ 的 RSI14 四舍五入为 28.7。"
        "000688.SH 的 MACD 柱四舍五入为 -8.4。"
        "399006.SZ 的 MACD 柱四舍五入为 -21.2。"
        + _block(
            "36.78 | observed | RSI14 读数 | 000001.SH",
            "-10.88 | observed | MACD柱 读数 | 000001.SH",
            "29.71 | observed | RSI14 读数 | 000300.SH",
            "-15.98 | observed | MACD柱 读数 | 000300.SH",
            "32.21 | observed | RSI14 读数 | 000688.SH",
            "-8.43 | observed | MACD柱 读数 | 000688.SH",
            "28.68 | observed | RSI14 读数 | 399006.SZ",
            "-21.23 | observed | MACD柱 读数 | 399006.SZ",
            "1.21 | observed | 量比 | 000688.SH",
            "36.8 | observed | RSI14 四舍五入 | 000001.SH",
            "28.7 | observed | RSI14 四舍五入 | 399006.SZ",
            "-8.4 | observed | MACD柱 四舍五入 | 000688.SH",
            "-21.2 | observed | MACD柱 四舍五入 | 399006.SZ",
        )
    )

    result = ledger.validate_final_answer(draft)

    assert result.valid is True, [
        (issue.get("value"), issue.get("reason")) for issue in result.issues
    ]


# ---------------------------------------------------------------------------
# The correction domain: a mistyped reading is answered in its own units
# ---------------------------------------------------------------------------


def test_a_mistyped_reading_is_corrected_in_the_indicator_domain(tmp_path: Path) -> None:
    """46.78 is no RSI this session observed; the correction quotes the RSI pool.

    The price range (3857–3896) tells the model nothing about an oscillator,
    which is what made the replay retract the indicator instead of the typo.
    """
    ledger = _ledger(tmp_path, SH)

    result = ledger.validate_final_answer(
        f"{SH}（tushare，CNY）的 RSI14 为 46.78。"
    )
    issue = next(issue for issue in result.issues if issue.get("value") == "46.78")

    assert issue["reason"] == "value_mismatch"
    assert issue["observed_max"] == pytest.approx(_READINGS[SH]["rsi"])
    assert issue["observed_min"] == pytest.approx(_READINGS[SH]["line"])
    assert issue["observed_nearest"][0] == pytest.approx(_READINGS[SH]["rsi"])
    assert "indicator" in issue["message"]


def test_a_mistyped_close_still_gets_the_price_range(tmp_path: Path) -> None:
    """A decimal inside the instrument's price band keeps the price domain.

    3920 is a plausible close (within ±10% of the range) but not this
    session's: the flat band around 3888.53–3896.31 rejects it, and the
    correction has to quote prices, not the oscillator pool.
    """
    ledger = _ledger(tmp_path, SH)

    result = ledger.validate_final_answer(f"{SH}（tushare，CNY）最新收盘 3920.00。")
    issue = next(issue for issue in result.issues if issue.get("value") == "3920.00")

    assert issue["reason"] == "value_mismatch"
    assert issue["observed_min"] > 3000 and issue["observed_max"] > 3000
    assert issue["observed_nearest"] and all(value > 3000 for value in issue["observed_nearest"])
    assert "indicator" not in issue["message"]


# ---------------------------------------------------------------------------
# The percent pool keeps registered readings out
# ---------------------------------------------------------------------------


def test_an_observed_reading_never_answers_a_percent_claim(tmp_path: Path) -> None:
    """RSI 36.78 must not stand in for 胜率 36.8%: a reading is not a ratio.

    The registration feeds the metric pool the plain path matches against;
    the percent path builds it without the registered leaves, so no percent
    figure is ever grounded by an oscillator that happens to share digits.
    """
    ledger = _ledger(tmp_path, SH)

    result = ledger.validate_final_answer(f"{SH}（tushare，CNY）策略胜率 36.8%。")

    assert _codes(result) == ["numeric_claim_unavailable"]
