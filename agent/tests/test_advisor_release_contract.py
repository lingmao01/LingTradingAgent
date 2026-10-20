"""Local report failures cannot erase unrelated validated analysis."""
from __future__ import annotations

import json
import asyncio

import pytest

from tests.test_grounding_release import _ledger, _run, _ScriptedLLM, _Response, HDR
from tests.test_session_service_lifecycle import _service, _stub_agent

pytestmark = pytest.mark.unit


def test_many_bad_rows_leave_the_verified_row_and_unaffected_analysis(tmp_path):
    ledger = _ledger(tmp_path)
    rows = "\n".join(f"| {n} | {8 + n / 100:.2f} |" for n in range(1, 14))
    draft = (
        HDR + "\n\n行情证据仅用于观察，实际申请须遵循持仓基金的业务规则。\n\n"
        "| 档位 | 买入价 |\n|---|---|\n| 有效观察 | 1.171 |\n" + rows
        + "\n\n这些档位全部保证上涨，立即买入。"
    )
    validation = ledger.validate_final_answer(draft)
    assert not validation.valid
    released = ledger.redacted_release(draft, validation)
    assert released is not None
    assert "有效观察" in released and "1.171" in released
    assert "业务规则" in released
    assert "8.01" not in released and "8.13" not in released
    assert "保证上涨" not in released and "立即买入" not in released
    assert "本会话已核验的各标的最新收盘价" not in released
    assert ledger.revalidate(released).valid
    assert ledger._release_outcome["path"] == "partial"


def test_invalid_premise_removes_its_whole_paragraph_and_action(tmp_path):
    ledger = _ledger(tmp_path)
    invalid = "虚构回撤12%、夏普2.5、胜率55%、年化收益30%，据此立即加仓。"
    draft = HDR + "\n\n" + invalid + "\n\n基金申请价格仍以适用净值为准。"
    released = ledger.redacted_release(draft, ledger.validate_final_answer(draft))
    assert released is not None
    assert "立即加仓" not in released
    assert "基金申请价格" in released


def test_partial_release_does_not_bless_a_wrong_identity(tmp_path):
    ledger = _ledger(tmp_path)
    draft = "000999.SZ（Yahoo，CNY）最新收盘价 1.171 元。"
    verdict = ledger.validate_final_answer(draft)
    assert not verdict.valid
    released = ledger.redacted_release(draft, verdict)
    assert released is None or "000999.SZ" not in released


def test_execution_success_does_not_report_fallback_as_completed_advice(tmp_path):
    llm = _ScriptedLLM([_Response(content="机器人ETF 最新价9.999元。建议立即买入。")])
    result, _events, _agent = _run(tmp_path, llm, max_iterations=1)
    assert result["status"] == "success"
    assert result["completion_status"] == "incomplete"
    assert result["actionability_status"] == "unavailable"
    state = json.loads((tmp_path / "run/state.json").read_text())
    assert state["completion_status"] == "incomplete"
    end = next(e for e in result["react_trace"] if e.get("type") == "end") if any(
        e.get("type") == "end" for e in result["react_trace"]
    ) else None
    if end:
        assert end["completion_status"] == "incomplete"


def test_business_delivery_survives_session_storage_and_completed_event(tmp_path, monkeypatch):
    async def scenario():
        service = _service(tmp_path, monkeypatch)
        session = service.create_session(title="partial advice")
        _stub_agent(service, monkeypatch, {
            "status": "success", "content": "已核对部分持仓，其他基金缺少净值。",
            "completion_status": "partial", "evidence_status": "partial",
            "actionability_status": "limited", "release_path": "partial", "degraded": True,
        })
        reply = await service.send_message(session.session_id, "给持仓操作建议")
        await service._active_tasks[session.session_id]
        stored = service.store.get_messages(session.session_id)[-1]
        assert stored.metadata["completion_status"] == "partial"
        assert stored.metadata["actionability_status"] == "limited"
        attempt = service.store.get_attempt(session.session_id, reply["attempt_id"])
        assert attempt.delivery["completion_status"] == "partial"
    asyncio.run(scenario())


def test_rejected_paragraph_cannot_leave_its_unnumbered_causal_trade_instruction(tmp_path):
    from tests.test_grounding_release import _ledger, HDR
    ledger = _ledger(tmp_path)
    text = HDR + '\n\n该基金历史胜率' + '、'.join(str(x) + '%' for x in range(81, 94)) + '，预期收益优势明显。\n\n因此建议立即满仓买入该基金。'
    validation = ledger.revalidate(text)
    assert not validation.valid
    released = ledger.redacted_release(text, validation)
    assert released is None or '满仓买入' not in released
