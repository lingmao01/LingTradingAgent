"""Fund identities and old user statements remain distinct from live observations."""
from __future__ import annotations

import json

import pytest

from src.position_book import PositionBookStore, render_book_block

pytestmark = pytest.mark.unit


def test_typed_bare_fund_is_persisted_as_off_exchange(tmp_path):
    store = PositionBookStore(tmp_path / "position_book.json")
    result = store.set_position("000051", quantity=10, instrument_type="off_exchange_fund")
    assert result.symbol == "000051.OF"
    with pytest.raises(ValueError, match="ambiguous"):
        store.set_position("000052", quantity=10)
    named = store.set_position("000053", quantity=20, name="样本ETF联接C")
    assert named.symbol == "000053.OF"


def _memory(tmp_path):
    folder = tmp_path / "memory/user"
    folder.mkdir(parents=True)
    (folder / "book.md").write_text("""---
type: user
updated_at: 2026-09-29T14:56:13
---
用户持仓台账：均为场外ETF联接基金，表中金额为09-28/09-29口径。
台账逐只（代号/名称/金额元/累计收益/买入日）：
- 001234/001235 样本人工智能ETF联接A / 另一人工智能ETF联接A：1000 / -5% / 2025年8月
- 001236 样本沪深300ETF联接C：500 / -2% / 2026-09-24
- 活钱：200
""", encoding="utf-8")


def test_effective_book_recovers_memory_without_inventing_shares_or_splitting_groups(tmp_path):
    _memory(tmp_path)
    store = PositionBookStore(tmp_path / "position_book.json")
    account = store.load_account()
    assert not store.path.exists()  # read-only; no unrequested state rewrite
    assert len(account["positions"]) == 1
    position = account["positions"][0]
    assert position["symbol"] == "001236.OF"
    assert position["quantity"] is None
    assert position["reported_value"] == 500
    assert position["value_as_of"] is None
    assert position["purchase_date"] == "2026-09-24"
    assert position["reported_return_pct"] == -2
    assert account["valuation_date_range"] == ["2026-09-28", "2026-09-29"]
    group = account["holding_groups"][0]
    assert group["candidate_symbols"] == ["001234.OF", "001235.OF"]
    assert group["reported_value"] == 1000
    assert account["cash"]["amount"] == 200
    assert account["reported_fund_value"] == 1500
    block = render_book_block(store)
    assert "001236.OF" in block and "quantity 500" not in block
    assert "001234.OF" in block and "001235.OF" in block
    assert "2026-09-28" in block and "user-reported" in block


def test_explicit_empty_book_is_not_resurrected_from_memory(tmp_path):
    _memory(tmp_path)
    store = PositionBookStore(tmp_path / "position_book.json")
    store.clear()
    assert store.load_account()["positions"] == []
    assert store.load_account().get("holding_groups", []) == []


def test_lots_cash_and_pending_orders_survive_quantity_update(tmp_path):
    store = PositionBookStore(tmp_path / "position_book.json")
    lots = [
        {"lot_id": "old", "quantity": 10, "confirmed_at": "2026-10-01", "cost": 2},
        {"lot_id": "new", "quantity": 15, "confirmed_at": "2026-10-14", "cost": 3},
    ]
    store.set_position("001236.OF", quantity=25, lots=lots)
    store.set_account(cash={"amount": 1000, "currency": "CNY", "as_of": "2026-10-15"}, pending_orders=[
        {"order_id": "pending-buy", "symbol": "001236.OF", "action": "subscribe", "amount": 200, "state": "pending", "submitted_at": "2026-10-15"},
        {"order_id": "pending-sell", "symbol": "001236.OF", "action": "redeem", "quantity": 5, "state": "pending", "submitted_at": "2026-10-15"},
    ])
    store.set_position("001236.OF", quantity=25, name="样本联接C")
    account = store.load_account()
    assert account["positions"][0]["lots"] == lots
    assert account["cash"]["amount"] == 1000
    assert account["available_cash"] == 800
    assert len(account["pending_orders"]) == 2


def test_conflicting_lot_sum_or_invalid_date_is_rejected_before_write(tmp_path):
    store = PositionBookStore(tmp_path / "position_book.json")
    with pytest.raises(ValueError, match="lot"):
        store.set_position("001236.OF", quantity=25, lots=[
            {"lot_id": "x", "quantity": 10, "confirmed_at": "2026-10-01"},
        ])
    with pytest.raises(ValueError, match="date"):
        store.set_position("001236.OF", quantity=10, lots=[
            {"lot_id": "x", "quantity": 10, "confirmed_at": "2026-02-31"},
        ])
    assert not store.path.exists()


def test_old_book_unknown_fields_survive_an_additive_write(tmp_path):
    path = tmp_path / "position_book.json"
    path.write_text(json.dumps({"version": 1, "custom_account": {"keep": True}, "positions": [
        {"symbol": "001236.OF", "quantity": 10, "cost": 2, "broker_reference": "original"},
    ]}))
    store = PositionBookStore(path)
    store.set_position("001236.OF", quantity=12)
    raw = json.loads(path.read_text())
    assert raw["custom_account"] == {"keep": True}
    assert raw["positions"][0]["broker_reference"] == "original"
    assert raw["positions"][0]["cost"] == 2


def test_fund_relationship_and_product_terms_are_dated_and_never_promoted_to_verified(tmp_path):
    from src.fund_identity import FundIdentityStore
    store = FundIdentityStore(tmp_path / "fund_identities.json")
    record = store.record(
        "000051.OF", name="样本沪深300联接A", share_class="A", target_etf="510330.SH",
        tracking_index="000300.SH", source_ref="user supplied relationship", as_of="2026-10-15",
        terms={"redemption_tiers": [{"holding_days_below": 7, "fee_percent": 1.5}], "effective_from": "2026-01-01"},
    )
    assert record["source_kind"] == "user_declared"
    assert record["verified"] is False
    assert store.lookup("000051")["target_etf"] == "510330.SH"
    store.record("000051.OF", name="更新名称")
    assert store.lookup("000051.OF")["terms"]["effective_from"] == "2026-01-01"
    with pytest.raises(ValueError, match="ETF"):
        store.record("000051.OF", target_etf="000052.OF")
    with pytest.raises(ValueError, match="fee"):
        store.record("000051.OF", terms={"redemption_tiers": [{"holding_days_below": 7, "fee_percent": -1}]})


def test_account_and_identity_are_accessible_through_registered_tools(tmp_path, monkeypatch):
    from src.agent.tools import ToolRegistry
    from src.tools.position_book_tool import PositionBookTool
    from src.tools.fund_data_tool import GetFundDataTool
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    registry = ToolRegistry()
    registry.register(PositionBookTool(PositionBookStore(tmp_path / "position_book.json")))
    result = json.loads(registry.execute("position_book", {
        "action": "set_account", "cash": {"amount": 1000, "currency": "CNY", "as_of": "2026-10-15"},
    }))
    assert result["status"] == "ok"
    assert json.loads(registry.execute("position_book", {"action": "account"}))["account"]["available_cash"] == 1000
    result = json.loads(registry.execute("position_book", {
        "action": "set_identity", "symbol": "000051.OF", "target_etf": "510330.SH",
        "tracking_index": "000300.SH", "source_ref": "user statement", "as_of": "2026-10-15",
    }))
    assert result["identity"]["verified"] is False
    registry.register(GetFundDataTool())
    result = json.loads(registry.execute("get_fund_data", {"codes": ["000051"], "view": "identity"}))
    assert result["000051.OF"]["data"]["target_etf"] == "510330.SH"
    assert result["000051.OF"]["_provenance"]["quality"] == "user_declared"


def test_registered_account_constraints_are_not_silently_ignored(tmp_path):
    import json
    from src.agent.tools import ToolRegistry
    from src.position_book import PositionBookStore
    from src.tools.position_book_tool import PositionBookTool
    registry = ToolRegistry()
    registry.register(PositionBookTool(PositionBookStore(tmp_path / 'position_book.json')))
    result = json.loads(registry.execute('position_book', {'action': 'set_account', 'minimum_cash': 200, 'max_loss_amount': 1000, 'investment_horizon': '按条件退出，不设固定期限'}))
    assert result['status'] == 'ok'
    assert result['account']['minimum_cash'] == 200
    assert result['account']['max_loss_amount'] == 1000
    assert result['account']['investment_horizon'] == '按条件退出，不设固定期限'


def test_unreadable_book_is_unavailable_through_registered_list_and_account(tmp_path):
    import json
    from src.agent.tools import ToolRegistry
    from src.position_book import PositionBookStore
    from src.tools.position_book_tool import PositionBookTool
    path = tmp_path / 'position_book.json'
    path.write_text('{bad')
    registry = ToolRegistry()
    registry.register(PositionBookTool(PositionBookStore(path)))
    for action in ('list', 'account'):
        result = json.loads(registry.execute('position_book', {'action': action}))
        assert result['status'] == 'error'
    assert path.read_text() == '{bad'


@pytest.mark.parametrize('terms', [
    {'effective_from': '2026-10-16', 'effective_until': '2026-10-15'},
    {'otherwise_fee_percent': -1}, {'otherwise_fee_percent': 101}, {'otherwise_fee_percent': True},
])
def test_invalid_product_fee_terms_are_not_recorded(tmp_path, terms):
    from src.fund_identity import FundIdentityStore
    path = tmp_path / 'identity.json'
    with pytest.raises(ValueError):
        FundIdentityStore(path).record('001236.OF', terms=terms)
    assert not path.exists()
