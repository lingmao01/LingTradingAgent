"""Conditional advice from explicit constraints, not an invented return forecast."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from src.advisor.cash import actionable_cash
from src.advisor.messages import explain
from src.advisor.expert import approved


_UNRELATED_REQUEST = re.compile(r'修复|Python|函数|翻译|(?:写|生成).{0,8}(?:网页|代码|文件)|天气', re.I)
_K_ETF_COMMAND = re.compile(
    r'K\s*[-‐‑‒–—−]\s*ETF\s*(?:[。.!！?？]*|[,，:：;；\n]\s*(?P<extra>.*))', re.I | re.S,
)
_CANCEL_RESEARCH = re.compile(
    r'(?:不要|别|不用|无需|(?:暂时|暂|先)?不)(?:再|继续|要)?\s*(?:执行|运行|启动|分析|研判)'
    r'|(?:停止|取消)\s*(?:本轮|这次|此次|当前|这个|该)?\s*'
    r'(?:执行|运行|启动|分析|研判|研究|任务|流程|指令|K\s*[-‐‑‒–—−]\s*ETF|[。.!！?？]*$)', re.I,
)
_K_ETF_REQUEST = (
    '分析我的持仓并给出增仓、减仓和换仓建议。'
    '搜集我的完整持仓账户的最新基金净值、行情与相关外围信息，'
    '结合已记录的持仓、四宽基纪律和风险规则，评判减仓、减持信号，'
    '主动查询持仓以外的可行方向，比较维持、增仓、减仓留现金及换仓，寻找支持与反证；'
    '给出有证据的独立研究判断、可检验预判与改判条件，区分规则信号与研究建议，结论先行且简短。'
)


def expand_advice_request(message: str) -> str:
    """Resolve the explicit workflow command before any security-name lookup.

    Only a standalone command or a delimited invocation qualifies. Mentions,
    ticker suffixes, questions about the alias and cancellations stay ordinary
    requests. Preserve the original text for those non-command messages.
    """
    match = _K_ETF_COMMAND.fullmatch(unicodedata.normalize('NFKC', message).strip())
    if not match:
        return message
    extra = (match.group('extra') or '').strip()
    if (_UNRELATED_REQUEST.search(extra)
            or re.match(r'(?:是什么|什么意思|什么含义|怎么用|如何使用)', extra)
            or _CANCEL_RESEARCH.search(extra)):
        return message
    return _K_ETF_REQUEST + ('\n本轮补充要求：' + extra if extra else '')


def advice_requested(message: str, history=None) -> bool:
    if _CANCEL_RESEARCH.search(message):
        return False
    expanded = expand_advice_request(message)
    if _K_ETF_COMMAND.fullmatch(unicodedata.normalize('NFKC', message).strip()):
        return expanded != message
    text = expanded.strip()
    unrelated = _UNRELATED_REQUEST.search(text)
    followup = len(text) <= 120 and re.search(r'继续|再看看|为什么|然后|如果|那我|怎么|如何|分批|具体|多少|加仓|减仓|换仓|增仓|未关注|没关注|方向|推翻|反证|信号', text)
    if history and followup and not unrelated:
        recent = [str(row.get('content', '')) for row in history[-8:] if row.get('role') == 'user']
        prior = next((item for item in reversed(recent) if advice_requested(item)), None)
        if prior:
            text += ' ' + expand_advice_request(prior)
    intent = bool(re.search(r'操作建议|投资建议|(?:加仓|减仓|买入|卖出|持仓|基金|ETF).{0,20}(?:建议|分析|怎么办)|(?:建议|分析).{0,20}(?:持仓|基金|ETF)', text, re.I))
    personal = bool(re.search(r'持仓|我(?:的|持有)|账户|今日.*行情', text))
    return intent and personal


def decide(report: dict[str, Any]) -> dict[str, Any]:
    if report.get('assessment_schema_version') == 2:
        return _decide_v2(report)
    cards = []
    def add(action, scope, rationale, *, evidence_refs=(), **extra):
        cards.append({
            'action': action, 'scope': scope, 'rationale': rationale,
            'horizon': '条件有效期间；下一交易时点前按新披露证据重评',
            'trigger': '本卡列出的已记录约束与已核验观测仍成立',
            'invalidation': '净值、行情、在途交易或用户约束改变；数据源修订',
            'next_check': '下一次已披露基金净值及指数收盘更新后重算，交易前核对平台确认与费用',
            'limits': '这是约束下的条件决策，不是上涨概率或收益保证；不自动下单',
            'evidence_refs': list(evidence_refs), **extra,
        })
    holdings = report.get('holdings', [])
    portfolio = report.get('portfolio', {})
    for cluster in report.get('clusters', []):
        if cluster['state'] != 'OVER CAP':
            continue
        if any(row.get('gaps') for row in holdings):
            continue
        cap = cluster['max_percent'] / 100
        total = portfolio.get('fund_value')
        weight = cluster.get('fund_weight_percent')
        if total is None or weight is None or cap >= 1:
            continue
        excess = max(0, total * (weight / 100 - cap))
        redeem = excess / (1 - cap)
        pending = sum(max(0, (row.get('quantity') or 0) - (row.get('available_quantity') or 0)) * (row.get('price') or 0)
                      for row in holdings if row['id'] in cluster['members'])
        add('REDUCE_CLUSTER', cluster['label'], '已核验的主题权重超过用户记录的硬上限，优先恢复约束；反弹许可不能覆盖超限。',
            evidence_refs=[f"clusters:{cluster['label']}", 'portfolio:fund_value'],
            cash_redemption_estimate=redeem, reallocation_estimate=excess,
            pending_redemption_estimate=pending,
            additional_cash_redemption_estimate=max(0, redeem - pending),
            trigger=f"主题占基金市值{weight:.2f}%，上限{cluster['max_percent']:g}%",
            limits='赎回留现金与转到非主题资产的分母不同；金额按披露净值估算、未计费用，最终按确认净值结算；先核对在途订单，不能重复提交')
    hard = bool(cards)
    for row in holdings:
        breaches = [key for key, rule in row.get('rules', {}).items() if rule['state'] in {'BREACHED', 'OVER CAP', 'REACHED'}]
        if breaches:
            hard = True
            quantity_cap = row.get('rules', {}).get('max_quantity', {})
            additional = max(0, row['available_quantity'] - quantity_cap['level']) if quantity_cap.get('state') == 'OVER CAP' and row.get('available_quantity') is not None else None
            add('REVIEW_REDUCTION', row['id'], '已记录的个券风险/止盈约束触发：' + '、'.join(breaches) + '。按原规则审查减仓，费用是执行约束，不能自动否决必要的风险处置。',
                evidence_refs=[f"holdings:{row['id']}:rules"], available_quantity=row.get('available_quantity'),
                additional_quantity_reduction=additional, fees=row.get('redemption_fee'))
    minimum = report.get('personal_parameters', {}).get('minimum_cash')
    available_cash = actionable_cash(report)
    if minimum is not None and available_cash is not None and available_cash < minimum:
        hard = True
        add('RESTORE_CASH', '账户', '可用现金低于用户记录的最低现金要求，暂停新增申购并安排流动性；赎回到账前不能将其作为可用现金。', evidence_refs=['portfolio:available_cash', 'personal_parameters:minimum_cash'])
    no_add, allowed, reduce, unknown = [], [], [], []
    for index in report.get('regime', []):
        for gate in index.get('gates', []):
            if gate['state'] == 'UNKNOWN':
                unknown.append(index['symbol'])
            elif gate['state'] == 'OPEN':
                destination = {'NO ADD': no_add, 'ADD ALLOWED': allowed, 'REDUCE': reduce}.get(gate['action'])
                if destination is not None:
                    destination.append(index['symbol'])
    if no_add:
        add('NO_ADD', '市场纪律', '以下已记录指数禁止加仓条件成立：' + '、'.join(sorted(set(no_add))) + '。暂停新增风险敞口；这不等于已证明应清仓，卖出仍按持仓风险约束分别判断。', evidence_refs=[f'regime:{s}' for s in set(no_add)])
    if reduce:
        add('REVIEW_REDUCTION', '市场纪律', '减仓条件成立：' + '、'.join(sorted(set(reduce))) + '。结合超限、持仓规则和在途交易审查；不从技术信号虚构卖出金额。', evidence_refs=[f'regime:{s}' for s in set(reduce)])
    if allowed:
        add('CONDITIONAL_ADD', '市场纪律', '加仓许可条件成立：' + '、'.join(sorted(set(allowed))) + '。许可只解除一项禁令，不能证明买入有正收益；先满足现金、集中度、持仓完整性和费用条件，再决定是否增加风险。', evidence_refs=[f'regime:{s}' for s in set(allowed)])
    if unknown:
        add('RESOLVE_GATE', '未定义的纪律', '部分条件无法量化或缺少历史。保留未知，不将“动能确认”等未定义条款擅自解释成已触发；其他可计算条件仍独立生效。', evidence_refs=[f'regime:{s}' for s in set(unknown)])
    if hard:
        stance = 'REDUCE_RISK'
    elif no_add:
        stance = 'NO_ADD'
    elif reduce:
        stance = 'REVIEW_REDUCTION'
    elif not holdings or not portfolio.get('coverage_complete') or any(row.get('gaps') for row in holdings):
        stance = 'LIMITED_JUDGMENT'
    elif allowed and not portfolio.get('analysis_scope') and available_cash is not None and available_cash > 0:
        stance = 'CONDITIONAL_ADD'
    elif not unknown and report.get('regime') and all(row.get('sma_long') is not None and row.get('quality') not in {'stale', 'unknown'} for row in report['regime']):
        stance = 'MAINTAIN_WITH_REVIEW'
    else:
        stance = 'LIMITED_JUDGMENT'
    if not cards or stance == 'LIMITED_JUDGMENT':
        add('LIMITED_JUDGMENT' if stance == 'LIMITED_JUDGMENT' else 'MAINTAIN_WITH_REVIEW', '账户',
            '目前没有已核验的规则足以支持改变仓位。现有敞口仍承担损失风险；缺数据不等于持有最优，也不构成机械补仓理由。先处理已知风险和信息缺口，再作金额决策。',
            evidence_refs=['portfolio:coverage_complete'],
            trigger='未发现可执行的完整增减仓触发依据；已核验约束优先')
    for item in report.get('news', {}).get('primary', []):
        if item.get('verified') is not True or not item.get('published_at') or item['published_at'] > report['as_of']:
            continue
        facts = {fact['metric']: fact['value'] for fact in item.get('facts', []) if fact.get('unit') == 'percent'}
        if 'cpi_yoy' not in facts or 'cpi_previous_yoy' not in facts:
            continue
        change = facts['cpi_yoy'] - facts['cpi_previous_yoy']
        if change > 0:
            rationale = '原文显示美国CPI同比增幅高于上期。我的机制判断是：将海外宽松或估值扩张作为成长资产加仓前提，需要更强的新证据；偏防守的约束判断因此仍有理由。利率、汇率和政策实际反应尚需分别核验，不能从通胀方向直接推断股市涨跌。'
        elif change < 0:
            rationale = '原文显示美国CPI同比增幅低于上期。我的机制判断是：通胀约束边际缓和有利于研究流动性改善的可能性，但仍需核对核心通胀、利率与市场预期；这一项改善不能单独覆盖持仓上限或趋势禁令。'
        else:
            rationale = '原文显示美国CPI同比增幅与上期持平。这项事实没有提供通胀方向的新边际证据，当前仓位判断继续以已核验市场条件和账户约束为主。'
        add('MACRO_ASSESSMENT', '外围宏观', rationale, evidence_refs=[item['url']],
            inference_type='economic_mechanism_not_causal_proof', yoy_change_percentage_points=change,
            trigger='所列官方原文与相邻期间同比口径经核对；未取得一致预期数据时不作预期差判断',
            invalidation='数据修订，或核心通胀、政策、利率和汇率证据与这项单因素机制相抵触')
    for row in holdings:
        issues = row.get('gaps', [])
        if row.get('valuation_kind') == 'conditional_estimate':
            issues = ['历史金额可作无交易假设下的区间测算，份额仍未知'] + issues
        if not row.get('symbol'):
            issues = ['合并持仓不拆成多笔重复资产，代码/份额分配仍未明确'] + issues
        if issues:
            source_failure = any('NAV' in issue and ('missing' in issue or 'unavailable' in issue) for issue in row.get('gaps', []))
            add('RETRY_SOURCE' if source_failure else 'VERIFY_HOLDING', row['id'], '；'.join(explain(issue) for issue in issues), evidence_refs=[f"holdings:{row['id']}"],
                next_check='重查该基金自己的净值数据源，保留现有台账' if source_failure else '沿用已保存台账，仅更新缺少的份额、日期或交易变动')
    return {'schema_version': 1, 'method_version': 'constraint-decisions-v1', 'snapshot_sha256': report['snapshot_sha256'], 'stance': stance, 'cards': cards, 'expert_judgments': approved(report)}


def _decide_v2(report):
    """Compatibility projection from authoritative v2 results, never old gates."""
    cards=[]
    signals=report.get('signal_assessments',[])
    true=[s for s in signals if s.get('state')=='TRUE']
    for signal in signals:
        effect,state=signal.get('effect'),signal.get('state')
        if state=='TRUE':
            action={'BLOCK_ADD':'NO_ADD','REDUCE_TRIGGER':'REVIEW_REDUCTION','HARD_RISK':'REVIEW_REDUCTION'}.get(effect)
        elif state=='UNKNOWN' and signal.get('config_status')!='NOT_CONFIGURED':action='RESOLVE_GATE'
        else:action=None
        if not action:continue
        subject=signal.get('subject') or {}
        conditions=signal.get('conditions',[])
        cards.append({'action':action,'scope':subject.get('id') or '账户','origin':'RECORDED_RULE',
            'rationale':'；'.join(c.get('display','') for c in conditions) or '该项条件尚未完整定义或缺少依赖',
            'trigger':'；'.join(c.get('display','') for c in conditions),
            'invalidation':'所引用条件或已核验适用关系改变时复算；不从单一指数推定全账户许可',
            'next_check':signal.get('next_check') or '补齐该项依赖后以已确认收盘复算',
            'horizon':'所列证据日期的条件判断','limits':'方向资格、份额和资金费用条件分别核验',
            'signal_refs':[signal['signal_id']],'condition_refs':[c['condition_id'] for c in conditions if c.get('condition_id')],
            'evidence_refs':list(signal.get('evidence_refs',[]))})
    qualified=[a for a in report.get('action_assessments',[]) if a.get('action')=='ADD' and a.get('eligibility')=='ELIGIBLE' and a.get('origin')=='RECORDED_RULE']
    for action in qualified:
        cards.append({'action':'CONDITIONAL_ADD','scope':'、'.join(action['subject_ids']),
            'origin':'RECORDED_RULE','rationale':'所列对象的规则资格成立；实际金额仍受执行资料限制',
            'trigger':'、'.join(action.get('signal_refs',[])),'invalidation':'任一必要条件或约束改变时重新评估',
            'next_check':'核对当前现金、在途交易与费用','horizon':'同一冻结快照','limits':'技术许可不代表买入优势',
            'signal_refs':action.get('signal_refs',[]),'evidence_refs':action.get('evidence_refs',[])})
    risk=any(s.get('effect')=='HARD_RISK' or s.get('effect')=='REDUCE_TRIGGER' and s.get('subject',{}).get('kind')=='fund' for s in true)
    stance='REDUCE_RISK' if risk else 'REVIEW_REDUCTION' if any(s.get('effect')=='REDUCE_TRIGGER' for s in true) else 'NO_ADD' if any(s.get('effect')=='BLOCK_ADD' for s in true) else 'CONDITIONAL_ADD' if qualified else 'LIMITED_JUDGMENT'
    if not cards:cards=[{'action':'LIMITED_JUDGMENT','scope':'账户','rationale':'没有可核验的完整规则足以支持改变仓位；不把未知解释为维持最优','trigger':'规则或适用资料尚未齐备','invalidation':'取得有效新证据后重新评估','next_check':'补齐会改变结论的依赖','horizon':'本轮证据时点','limits':'不自动交易','evidence_refs':[]}]
    return {'schema_version':2,'method_version':'constraint-decisions-v2','snapshot_sha256':report['snapshot_sha256'],
        'stance':stance,'cards':cards,'expert_judgments':approved(report)}
