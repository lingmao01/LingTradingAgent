import pytest

from src.advisor.decisions import advice_requested

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('message', ['为什么现在不能加仓？', '那我应该卖多少？', '如果午后反弹，怎么分批？', '具体解释一下。'])
def test_financial_followups_retain_the_personal_advisor_delivery_contract(message):
    history = [{'role': 'user', 'content': '搜集今日最新行情，给出我的持仓操作建议。'}]
    assert advice_requested(message, history)


@pytest.mark.parametrize('message', ['帮我修复一个Python函数', '翻译下面的文字', '请写一个网页', '今天北京天气怎样？'])
def test_unrelated_work_does_not_become_a_portfolio_report(message):
    history = [{'role': 'user', 'content': '给出我的持仓操作建议。'}]
    assert not advice_requested(message, history)
