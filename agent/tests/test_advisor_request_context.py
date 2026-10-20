"""Explicit user book changes and scope precede the frozen advice snapshot."""
import json
import pytest
from src.agent.tools import ToolRegistry
from src.position_book import PositionBookStore
from src.tools.position_book_tool import PositionBookTool
pytestmark=pytest.mark.unit


def test_completed_exit_cash_change_and_requested_scope_are_applied_before_analysis(tmp_path):
    from src.advisor.request_context import prepare_request
    book=PositionBookStore(tmp_path/'position_book.json')
    book.set_position('001236.OF', quantity=100)
    book.set_position('001237.OF', quantity=100)
    registry=ToolRegistry();registry.register(PositionBookTool(book))
    result=prepare_request(registry, '我已经全部赎回001236.OF，现金更新为500元。今天只分析001237.OF，给出持仓操作建议。', as_of='2026-10-16')
    assert [r['symbol'] for r in book.load_account()['positions']]==['001237.OF']
    assert book.load_account()['cash']=={'amount':500.0,'currency':'CNY','as_of':'2026-10-16','source_ref':'current explicit user statement'}
    assert result['scope_symbols']==['001237.OF'] and not result['gaps']


def test_hypothetical_or_submitted_exit_is_never_treated_as_a_completed_exit(tmp_path):
    from src.advisor.request_context import prepare_request
    book=PositionBookStore(tmp_path/'position_book.json');book.set_position('001236.OF',quantity=100)
    registry=ToolRegistry();registry.register(PositionBookTool(book))
    for text in ['如果我全部赎回001236.OF，现金会变成500元，给出建议。','我已提交001236.OF全部赎回申请，分析一下。']:
        prepare_request(registry,text,as_of='2026-10-16')
        assert book.load_account()['positions'][0]['quantity']==100


def test_unsupported_completed_change_is_retained_as_an_unreconciled_event(tmp_path):
    from src.advisor.request_context import prepare_request
    book=PositionBookStore(tmp_path/'position_book.json');book.set_position('001236.OF', reported_value=1000, value_as_of='2026-10-15')
    registry=ToolRegistry();registry.register(PositionBookTool(book))
    result=prepare_request(registry,'我已赎回001236.OF一部分，给出持仓操作建议。',as_of='2026-10-16')
    assert result['gaps']
    assert book.load_account()['unreconciled_events'][0]['symbol']=='001236.OF'


def test_hypothetical_context_spans_comma_separated_cash_and_trade_clauses(tmp_path):
    from src.advisor.request_context import prepare_request
    book=PositionBookStore(tmp_path/'position_book.json');book.set_position('001236.OF',quantity=100)
    book.set_account(cash={'amount':100,'currency':'CNY','as_of':'2026-10-15'})
    registry=ToolRegistry();registry.register(PositionBookTool(book))
    prepare_request(registry,'假如我已经全部赎回001236.OF，现金更新为500元，应该怎样操作？',as_of='2026-10-16')
    assert book.load_account()['cash']['amount']==100
    assert book.load_account()['positions'][0]['quantity']==100

@pytest.mark.parametrize('text',[
    '不要将现金更新为500元，仍按我的持仓给出建议。',
    '我尚未确认完成001236.OF全部赎回，给出持仓建议。',
])
def test_negated_or_uncompleted_statements_never_authorize_a_book_write(tmp_path,text):
    from src.advisor.request_context import prepare_request
    book=PositionBookStore(tmp_path/'position_book.json');book.set_position('001236.OF',quantity=100)
    book.set_account(cash={'amount':100,'currency':'CNY','as_of':'2026-10-15'})
    registry=ToolRegistry();registry.register(PositionBookTool(book))
    prepare_request(registry,text,as_of='2026-10-16')
    assert book.load_account()['cash']['amount']==100
    assert book.load_account()['positions'][0]['quantity']==100


def test_scope_exclusion_is_not_folded_into_the_requested_inclusion(tmp_path):
    from src.advisor.request_context import prepare_request
    book=PositionBookStore(tmp_path/'position_book.json')
    for code in ['001236.OF','001237.OF']:book.set_position(code,quantity=100)
    registry=ToolRegistry();registry.register(PositionBookTool(book))
    result=prepare_request(registry,'今天只分析001237.OF，别分析001236.OF，给出持仓操作建议。',as_of='2026-10-16')
    assert result['scope_symbols']==['001237.OF']

@pytest.mark.parametrize('text',[
 '我朋友已经全部赎回001236.OF，给我的持仓一些建议。',
 '你之前说我的现金为500元，这个说法不对，请分析我的持仓。',
])
def test_other_person_or_rejected_quotation_cannot_mutate_my_book(tmp_path,text):
    from src.advisor.request_context import prepare_request
    book=PositionBookStore(tmp_path/'position_book.json');book.set_position('001236.OF',quantity=100)
    book.set_account(cash={'amount':100,'currency':'CNY','as_of':'2026-10-16'})
    registry=ToolRegistry();registry.register(PositionBookTool(book))
    prepare_request(registry,text,as_of='2026-10-16')
    assert book.load_account()['cash']['amount']==100
    assert book.load_account()['positions'][0]['quantity']==100
    assert not book.load_account().get('unreconciled_events')


def test_thousands_separator_and_explicit_unparsed_cash_change(tmp_path):
    from src.advisor.request_context import prepare_request
    book=PositionBookStore(tmp_path/'position_book.json');book.set_position('001236.OF',quantity=100)
    registry=ToolRegistry();registry.register(PositionBookTool(book))
    result=prepare_request(registry,'现金已更新为1,000元，分析我的持仓。',as_of='2026-10-16')
    assert book.load_account()['cash']['amount']==1000 and not result['gaps']
    result=prepare_request(registry,'现金更新为数百元，分析我的持仓。',as_of='2026-10-16')
    assert result['gaps'] and book.load_account()['available_cash'] is None
