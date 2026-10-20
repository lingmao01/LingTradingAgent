"""The availability-claim check and the ledger digest for withheld-tool turns.

The distortion these pin (2026-10-08 run): layer-1 compaction had emptied the
model's working context, the forced-text turn wrote without tools, and the
released answer told the user that data the session really observed was
"未取到/未保留". Two halves of the fix live here: the digest a withheld-tool
turn quotes its observations from (``verified_evidence_digest``), and the check
that rejects a draft denying an instrument the ledger holds
(``_validate_availability_claims``).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent.grounding import GroundingLedger, ValidationResult
from src.agent.grounding.evidence import EvidenceRecord
from src.agent.loop import AgentLoop
from src.agent.tools import BaseTool, ToolRegistry
from tests.message_roles_helpers import assert_system_messages_only_lead

pytestmark = pytest.mark.unit

SYMBOL = "000001.SH"
ETF_SYMBOL = "588000.SH"
DIGEST_CLOSE = "close=3811.9 (prices::close)"


def _market_payload() -> str:
    return json.dumps(
        {
            SYMBOL: [
                {
                    "trade_date": "2026-10-08",
                    "open": 3800.0,
                    "high": 3825.0,
                    "low": 3790.0,
                    "close": 3811.9,
                    "volume": 1,
                }
            ],
            "_provenance": {
                SYMBOL: {
                    "source": "tushare",
                    "requested_source": "auto",
                    "detected_source": "tushare",
                    "fallback_used": False,
                    "currency_conversion": "none",
                }
            },
        }
    )


def _ledger(tmp_path: Path) -> GroundingLedger:
    ledger = GroundingLedger(
        run_dir=tmp_path, user_message=f"请分析 {SYMBOL} 的走势并给出建议"
    )
    ledger.ingest_tool_result(
        tool_name="get_market_data",
        arguments={"codes": [SYMBOL]},
        result=_market_payload(),
        call_id="prices",
        success=True,
    )
    return ledger


def _carry_etf_close(ledger: GroundingLedger) -> None:
    """Seed one quote an earlier run of this session observed."""
    ledger._evidence.append(
        EvidenceRecord(
            call_id="prices",
            tool="get_market_data",
            symbol=ETF_SYMBOL,
            source="tushare",
            timestamp="2026-09-30",
            field="close",
            value=1.616,
            status="observed",
            carried_from="run-earlier",
        )
    )


def _observe_many(ledger: GroundingLedger, *symbols: str) -> None:
    """Seed one observed close per symbol via a single ``get_market_data`` call."""
    payload: dict[str, object] = {
        symbol: [{"trade_date": "2026-10-12", "close": 100.0}]
        for symbol in symbols
    }
    payload["_provenance"] = {
        symbol: {
            "source": "tencent",
            "requested_source": "auto",
            "detected_source": "tencent",
            "fallback_used": False,
            "currency_conversion": "none",
        }
        for symbol in symbols
    }
    ledger.ingest_tool_result(
        tool_name="get_market_data",
        arguments={"codes": list(symbols)},
        result=json.dumps(payload),
        call_id="prices",
        success=True,
    )


def _codes(result: ValidationResult) -> list[str]:
    return [str(issue.get("code")) for issue in result.issues]


def _availability_issues(validation: ValidationResult) -> list[dict]:
    return [
        issue
        for issue in validation.issues
        if issue.get("code") == "availability_claim_contradicts_evidence"
    ]


# ---------------------------------------------------------------------------
# The ledger digest a withheld-tool turn quotes from
# ---------------------------------------------------------------------------


def test_digest_quotes_latest_values_with_refs_and_as_of(tmp_path):
    ledger = _ledger(tmp_path)
    digest = ledger.verified_evidence_digest()

    assert digest.startswith(f"- {SYMBOL}: ")
    assert DIGEST_CLOSE in digest
    assert "[as of 2026-10-08]" in digest


def test_digest_is_empty_without_observed_values(tmp_path):
    ledger = GroundingLedger(run_dir=tmp_path, user_message="你好")
    assert ledger.verified_evidence_digest() == ""


def test_digest_marks_evidence_inherited_from_an_earlier_run(tmp_path):
    ledger = GroundingLedger(run_dir=tmp_path, user_message=f"请分析 {ETF_SYMBOL}")
    _carry_etf_close(ledger)
    digest = ledger.verified_evidence_digest()

    assert "close=1.616 (prices::close)" in digest
    assert "[as of 2026-09-30]" in digest
    assert "[inherited from an earlier run of this session]" in digest


# ---------------------------------------------------------------------------
# Denying an instrument the ledger holds is rejected, with its real values
# ---------------------------------------------------------------------------


def test_a_denial_of_observed_data_is_rejected_with_the_real_values(tmp_path):
    ledger = _ledger(tmp_path)
    validation = ledger.validate_final_answer(
        f"{SYMBOL} 的指标数值未保留到可引用状态，未取到。"
    )

    issue = next(iter(_availability_issues(validation)))
    assert issue["symbol"] == SYMBOL
    assert issue["reason"] == "claim_contradicts_ledger"
    # The message must quote the ledger's own values and refs: a denied
    # instrument is answered with its observed numbers, not another denial.
    assert "prices::close = 3811.9" in issue["message"]
    assert "(as of 2026-10-08)" in issue["message"]
    assert "Remove the claim or state the observed values instead." in issue["message"]


def test_the_rejection_quotes_the_latest_calls_indicator_fields(tmp_path):
    """The old message quoted the session's first records — an early OHLC print
    — while the draft's own sma_50 came from the latest call, so the model read
    the rejection as "the data really is gone" (2026-10-10 run)."""
    ledger = GroundingLedger(
        run_dir=tmp_path, user_message=f"请分析 {SYMBOL} 的走势并给出建议"
    )
    ledger.ingest_tool_result(
        tool_name="get_market_data",
        arguments={"codes": [SYMBOL]},
        result=_market_payload(),
        call_id="old_prices",
        success=True,
    )
    ledger.ingest_tool_result(
        tool_name="technical_indicators",
        arguments={"symbol": SYMBOL},
        result=json.dumps(
            {
                "ok": True,
                "symbol": SYMBOL,
                "latest_close": 3813.79,
                "latest_date": "2026-10-09",
                "indicators": {
                    "sma_20": 3892.75,
                    "sma_50": 3899.5414,
                    "rsi_14": 31.7,
                    "macd": {"macd_line": -0.0187, "signal_line": -0.0134},
                },
            }
        ),
        call_id="new_indicators",
        success=True,
    )

    validation = ledger.validate_final_answer(f"{SYMBOL} 的指标数值未取到。")

    issue = next(iter(_availability_issues(validation)))
    # The latest call's fields are the rewrite material — indicator leaves
    # first, and never the superseded first call's OHLC print.
    assert "new_indicators::indicators.sma_50 = 3899.5414" in issue["message"]
    assert "old_prices::open" not in issue["message"]
    assert issue["message"].index("indicators.sma_20") < issue["message"].index(
        "latest_close"
    )
    assert "(as of 2026-10-09)" in issue["message"]


def test_only_the_denied_instrument_the_ledger_holds_is_flagged(tmp_path):
    ledger = _ledger(tmp_path)
    validation = ledger.validate_final_answer(
        f"{SYMBOL} 的行情未取到；399006.SZ 的行情也未取到。"
    )

    flagged = [issue["symbol"] for issue in _availability_issues(validation)]
    assert flagged == [SYMBOL]


def test_a_line_naming_several_instruments_denies_only_the_nearest(tmp_path):
    """The 2026-10-12 run: four indices on one line, three really observed.

    The line denies only ``399006.SZ`` ("…三只已核实到位…（399006.SZ 本轮未取到）"),
    yet the line-scoped check flagged the three the ledger held. A denial now
    binds to the nearest code beside it, and the truly-denied one is not in the
    ledger, so nothing is flagged.
    """
    ledger = GroundingLedger(run_dir=tmp_path, user_message="分析四宽基")
    _observe_many(ledger, "000001.SH", "000300.SH", "000688.SH")
    validation = ledger.validate_final_answer(
        "四宽基中的三只（上证综指 `000001.SH`、沪深300 `000300.SH`、"
        "科创50 `000688.SH`）已核实到位（`399006.SZ` 创业板指本轮未取到）。"
    )

    assert not _availability_issues(validation)


def test_a_denial_leading_its_line_binds_to_the_following_code(tmp_path):
    """A line that opens with the phrase still attributes it to the code after."""
    ledger = _ledger(tmp_path)
    validation = ledger.validate_final_answer("未取到：000001.SH 的收盘数据。")

    assert [i["symbol"] for i in _availability_issues(validation)] == [SYMBOL]


def test_a_substitution_stand_in_is_not_denied(tmp_path):
    """「改用 X 代替」的 X 是替代品，不是被否定的标的。"""
    ledger = GroundingLedger(run_dir=tmp_path, user_message="分析")
    _observe_many(ledger, "513010.SH")
    validation = ledger.validate_final_answer("该标的行情未取到，改用 513010.SH 代替。")

    assert not _availability_issues(validation)


def test_the_denial_and_the_code_must_share_a_line(tmp_path):
    ledger = _ledger(tmp_path)
    validation = ledger.validate_final_answer(
        f"下面这几项数据未取到。\n{SYMBOL} 的收盘价为 3811.9。"
    )

    assert not _availability_issues(validation)


def test_a_denial_without_a_code_is_not_rejected(tmp_path):
    ledger = _ledger(tmp_path)
    validation = ledger.validate_final_answer("板块排名数据未取到，北向资金未保留。")

    assert not _availability_issues(validation)


def test_a_denial_of_an_instrument_the_ledger_lacks_stays_free(tmp_path):
    ledger = _ledger(tmp_path)
    validation = ledger.validate_final_answer("399006.SZ 的行情未取到。")

    assert not _availability_issues(validation)


def test_uncomputed_statements_are_not_retrieval_claims(tmp_path):
    ledger = _ledger(tmp_path)
    validation = ledger.validate_final_answer(
        f"{SYMBOL} 的回撤熔断峰值未计算，无法给出当前档位。"
    )

    assert not _availability_issues(validation)


def test_a_denial_of_an_instruments_earlier_run_evidence_stands(tmp_path):
    """The denial reads "本轮未取到" (not retrieved *this run*). Evidence the
    ledger carries from an earlier run of the session says nothing about what
    this run fetched, so denying an instrument this run never observed is
    honest and must pass (2026-10-12 run, ``399006.SZ``)."""
    ledger = GroundingLedger(run_dir=tmp_path, user_message=f"请分析 {ETF_SYMBOL}")
    _carry_etf_close(ledger)
    validation = ledger.validate_final_answer(f"{ETF_SYMBOL} 的行情未取到。")

    assert not _availability_issues(validation)


def test_a_denial_is_still_rejected_once_this_run_observed_the_instrument(tmp_path):
    """The flip side of the pair: the same denial about an instrument THIS run
    observed is the compaction distortion the check exists to stop, so it fires
    and quotes the run's own value."""
    ledger = GroundingLedger(run_dir=tmp_path, user_message=f"请分析 {SYMBOL}")
    ledger.ingest_tool_result(
        tool_name="get_market_data",
        arguments={"codes": [SYMBOL]},
        result=_market_payload(),
        call_id="prices",
        success=True,
    )
    validation = ledger.validate_final_answer(f"{SYMBOL} 的行情未取到。")

    issue = next(iter(_availability_issues(validation)))
    assert issue["symbol"] == SYMBOL
    assert "prices::close = 3811.9" in issue["message"]


# ---------------------------------------------------------------------------
# The correction prompt teaches the rewrite the rejection needs
# ---------------------------------------------------------------------------


def test_the_correction_prompt_teaches_rewriting_around_observed_values(tmp_path):
    ledger = _ledger(tmp_path)
    validation = ledger.validate_final_answer(f"{SYMBOL} 的数据未取到。")
    prompt = ledger.correction_prompt(validation)

    assert "the session DID retrieve that data" in prompt


def test_other_rejections_do_not_carry_the_retrieval_instruction(tmp_path):
    ledger = _ledger(tmp_path)
    validation = ledger.validate_final_answer(f"{SYMBOL} 收盘 9999.0。")
    prompt = ledger.correction_prompt(validation)

    assert not _availability_issues(validation)
    assert "the session DID retrieve that data" not in prompt


# ---------------------------------------------------------------------------
# The 2026-10-08 incident, end to end: the denial chain must break
# ---------------------------------------------------------------------------


class _Response:
    def __init__(
        self, *, content: str = "", tool_calls: list[SimpleNamespace] | None = None
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning_content = None
        self.has_tool_calls = bool(self.tool_calls)


class _ScriptedLLM:
    """Plays a script, then keeps returning its last response forever."""

    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.messages_history: list[list[dict]] = []
        self.tools_history: list[list | None] = []

    def stream_chat(
        self,
        messages: list[dict],
        tools: list | None = None,
        on_text_chunk=None,
        on_reasoning_chunk=None,
        timeout=None,
        idle_timeout_s=None,
        should_cancel=None,
    ) -> _Response:
        self.calls += 1
        self.messages_history.append(list(messages))
        self.tools_history.append(tools)
        response = (
            self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        )
        if response.content and on_text_chunk:
            on_text_chunk(response.content)
        return response

    def chat(self, messages: list[dict], **kwargs: object) -> _Response:
        return _Response()


class _MarketTool(BaseTool):
    """Offline ``get_market_data`` double; a required argument keeps tool
    discovery from ever registering it outside this module's tests."""

    name = "get_market_data"
    description = "Fetch OHLCV bars (offline test double)."
    parameters = {
        "type": "object",
        "properties": {"codes": {"type": "array", "items": {"type": "string"}}},
        "required": ["codes"],
    }
    repeatable = True

    def __init__(self, result: str) -> None:
        self.result = result

    def execute(self, **kwargs: object) -> str:
        return self.result


def _tool_call(call_id: str, tool_name: str, **arguments: object) -> SimpleNamespace:
    return SimpleNamespace(id=call_id, name=tool_name, arguments=arguments)


def test_the_2026_10_08_denial_chain_is_broken_end_to_end(tmp_path: Path) -> None:
    """The incident's chain, end to end.

    The model fetched 000001.SH, then wrote the answer denying the retrieval
    ("未保留到可引用状态，未取到"). The gate rejects that draft quoting the
    ledger's own values (P0-②), and the revision turn it receives carries the
    digest alongside the correction prompt (P0-①), so the released answer
    quotes the observations instead of denying them.
    """
    denial = f"{SYMBOL} 的指标数值未保留到可引用状态，未取到。"
    corrected = f"{SYMBOL}（tushare，CNY）最新收盘价 3811.9 元。"
    llm = _ScriptedLLM(
        [
            _Response(
                tool_calls=[_tool_call("prices", "get_market_data", codes=[SYMBOL])]
            ),
            _Response(content=denial),
            _Response(content=corrected),
        ]
    )
    registry = ToolRegistry()
    registry.register(_MarketTool(_market_payload()))
    events: list[tuple[str, dict]] = []
    agent = AgentLoop(
        registry=registry,
        llm=llm,
        max_iterations=8,
        event_callback=lambda name, data: events.append((name, data)),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    agent.memory.run_dir = str(run_dir)

    result = agent.run(f"请分析 {SYMBOL} 的走势并给出建议")

    assert result["status"] == "success"
    assert "未取到" not in result["content"]
    assert "未保留" not in result["content"]
    assert "3811.9" in result["content"]
    # The revision turn the model actually received: the correction prompt
    # quotes the ledger's own ref, and the digest rides with it.
    revision = "\n".join(
        str(message.get("content")) for message in llm.messages_history[2]
    )
    assert "the session DID retrieve that data" in revision
    assert "prices::close = 3811.9" in revision
    assert "[SYSTEM] Verified evidence this session already retrieved" in revision
    assert DIGEST_CLOSE in revision
    assert_system_messages_only_lead(llm.messages_history)


def test_a_stubborn_denial_ships_the_analysis_not_the_bare_fallback(tmp_path: Path) -> None:
    """Revisions exhausted on an unretrieved-data claim, end to end.

    The 2026-10-20 run burned its revision budget on a denial that no figure cut
    can repair, and the reader got the bare-data fallback: closes and ranges,
    every conclusion stripped. The claim's unit is dropped and the verified
    paragraphs ship instead, so the wait buys an answer rather than a refusal.
    """
    denial = f"{SYMBOL} 的行情未取到。\n\n均线空头排列，处于下行趋势。"
    llm = _ScriptedLLM(
        [
            _Response(
                tool_calls=[_tool_call("prices", "get_market_data", codes=[SYMBOL])]
            ),
            _Response(content=denial),
        ]
    )
    registry = ToolRegistry()
    registry.register(_MarketTool(_market_payload()))
    agent = AgentLoop(registry=registry, llm=llm, max_iterations=8)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    agent.memory.run_dir = str(run_dir)

    result = agent.run(f"请分析 {SYMBOL} 的走势并给出建议")

    assert result["status"] == "success"
    assert "未取到" not in result["content"]
    assert "下行趋势" in result["content"]
    assert "部分段落" in result["content"]
