"""Apply explicit current user statements before freezing the advisory account.

This performs bookkeeping only. It never submits an investment transaction.
Ambiguous changes remain durable, unresolved events rather than guessed shares.
"""
import json
import re
from datetime import date, timedelta

_CODE = re.compile(r'(?<!\d)\d{6}(?:\.(?:OF|SH|SZ|BJ))?(?!\d)', re.I)
_NUMBER = r'(\d+(?:\.\d+)?)\s*(万|千)?'


def _amount(match):
    return float(match.group(1)) * {'万': 10000, '千': 1000, None: 1}[match.group(2)]


def prepare_request(registry, request, *, as_of, executor=None):
    request = re.sub(r'(?<![\d,.])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d,.])', lambda match: match.group().replace(',', ''), request)
    result = {'scope_symbols': None, 'applied': [], 'gaps': []}
    tool = registry.get('position_book')
    if tool is None:
        return result
    store = tool._store
    account = store.load_account()
    existing = [row['symbol'] for row in account.get('positions', [])]
    existing += [symbol for row in account.get('holding_groups', []) for symbol in row.get('candidate_symbols', [])]
    def symbols(text):
        found = []
        for match in _CODE.finditer(text):
            code = match.group().upper()
            matches = [item for item in existing if item == code or '.' not in code and item.split('.')[0] == code]
            if len(set(matches)) == 1:
                found.append(matches[0])
            elif '.' in code:
                found.append(code)
        return sorted(set(found))
    scope = re.search(r'(?:只|仅)(?:分析|看|评估|讨论)([^。；;\n]+)', request)
    if scope:
        positive_scope = re.split(r'[，,](?=\s*(?:别|不|其余|给出|然后|请|其他))', scope.group(1))[0]
        selected = symbols(positive_scope)
        if not selected:
            rows = account.get('positions', []) + account.get('holding_groups', [])
            themes = [word for word in ('机器人', '人工智能', '半导体', '云计算', '医药', '沪深300', '科创50') if word in positive_scope]
            matched = [row for row in rows if row.get('name') and (row['name'] in positive_scope or any(word in row['name'] for word in themes))]
            selected = sorted({symbol for row in matched for symbol in ([row['symbol']] if row.get('symbol') else row.get('candidate_symbols', []))})
        if selected:
            result['scope_symbols'] = selected
        else:
            result['scope_symbols'] = []
            result['gaps'].append('本轮指定的分析范围尚不能与账本代码对应；未擅自改为全账户分析')
    exclusions = re.findall(r'(?:别|不要|不)(?:再)?分析([^。；;\n]+)', request)
    excluded = {symbol for clause in exclusions for symbol in symbols(clause)}
    if excluded:
        result['scope_symbols'] = sorted(set(result['scope_symbols'] if result['scope_symbols'] is not None else existing) - excluded)
    execute = executor or registry.execute
    clauses = []
    for sentence in re.split(r'[。；;？?\n]', request):
        if re.search(r'如果|假如|假设|假定|例如|示例|他说|她说|引用|复现', sentence):
            continue
        clauses.extend(re.split(r'[，,]', sentence))
    for clause in clauses:
        if re.search(r'如果|假如|假设|假定|例如|示例|他说|她说|引用|复现|不要|不得|尚未|还未|未确认|并未|没有|没完成|未完成|别|取消|撤销|否认|不是|不对|不属实|错误|假的|不确定|不能确定|计划|打算|准备|考虑|是否|能否|拟|提交|申请|会变成', clause):
            continue
        codes = symbols(clause)
        day = re.search(r'\d{4}-\d{2}-\d{2}', clause)
        stamp = day.group() if day else (date.fromisoformat(as_of) - timedelta(days=1)).isoformat() if '昨天' in clause else as_of
        args = None
        direct = re.sub(r'^(?:(?:今天|目前|现在|昨天)|\d{4}-\d{2}-\d{2})\s*', '', clause.strip())
        own_cash = bool(re.match(r'^(?:我(?:的)?(?:账户(?:的)?)?\s*)?(?:(?:目前|现在|现有|当前)\s*)?现金', direct))
        own_holding = bool(re.match(r'^(?:我(?=已经|已|确认|现|还剩|剩余)|已经|已|确认完成|(?:我的)?\d{6}(?:\.[A-Z]{2})?)', direct, re.I))
        if not own_cash and not own_holding:
            continue
        cash = re.search(r'(?:现金(?:余额)?(?:已更新为|更新为|改为|现为|现在为|为)|(?:目前|现在|现有)现金)\s*'+_NUMBER+r'\s*(人民币|美元|元|CNY|USD)', clause, re.I)
        if cash and own_cash:
            currency = 'USD' if cash.group(3).upper() in {'美元', 'USD'} else 'CNY'
            args = {'action': 'set_account', 'cash': {'amount': _amount(cash), 'currency': currency,
                'as_of': stamp, 'source_ref': 'current explicit user statement'}}
        elif own_holding and len(codes) == 1 and re.search(r'已(?:经)?|确认完成', clause) and (re.search(r'全部|清仓|卖光|赎回完', clause)) and re.search(r'赎回|卖出|清仓|卖光', clause):
            args = {'action': 'remove', 'symbol': codes[0]}
        elif own_holding and len(codes) == 1:
            quantity = re.search(r'(?:现持有|现在持有|还剩|剩余|份额更新为|持仓数量更新为)\s*'+_NUMBER+r'\s*份', clause)
            value = re.search(r'(?:当前市值|现持仓金额|持仓金额更新为)\s*'+_NUMBER+r'\s*(?:元|人民币)', clause)
            if quantity:
                args = {'action': 'set', 'symbol': codes[0], 'quantity': _amount(quantity)}
            elif value:
                args = {'action': 'set', 'symbol': codes[0], 'reported_value': _amount(value), 'value_as_of': stamp}
        if args:
            try:
                response = json.loads(execute('position_book', args))
                if response.get('status') != 'ok':
                    raise ValueError(response.get('error') or 'book update unavailable')
                result['applied'].append(args)
                continue
            except (ValueError, TypeError, OSError) as exc:
                result['gaps'].append(f'本轮明确账本更新尚未成功：{exc}')
        if own_cash and re.search(r'现金.*(?:更新|改为|现为|现在为|余额|为)', direct):
            try:
                store.record_unreconciled_event('__CASH__', clause.strip(), as_of=stamp)
            except (ValueError, TypeError, OSError) as exc:
                result['gaps'].append(f'未能保存现金变更线索：{exc}')
            result['gaps'].append('本轮现金变更尚不能确定金额，旧余额暂停用于当前资金动作')
        if codes and re.search(r'已(?:经)?.{0,12}(?:赎回|卖出|买入|加仓|减仓)|(?:赎回|卖出|买入).{0,12}(?:已确认|完成)|份额更新|持仓金额更新', clause):
            for symbol in codes:
                try:
                    store.record_unreconciled_event(symbol, clause.strip(), as_of=stamp)
                except (ValueError, TypeError, OSError) as exc:
                    result['gaps'].append(f'未能保存本轮变更线索：{exc}')
            result['gaps'].append('已保存本轮持仓变动；缺少可确定的剩余份额/当期金额或批次信息，相关旧估值暂不可用于当前动作')
    return result
