"""Render typed observations/calculations as useful, auditable advice."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from src.advisor.messages import QUALITY, STATE, METRICS, explain

from src.advisor.decisions import decide

LABELS = {
    'REDUCE_RISK': '优先降低已触发的约束风险', 'NO_ADD': '暂停新增风险，分别审查减仓条件',
    'REVIEW_REDUCTION': '已触发减仓审查', 'CONDITIONAL_ADD': '仅具备条件性加仓许可',
    'MAINTAIN_WITH_REVIEW': '暂不主动改变敞口，按条件复核', 'LIMITED_JUDGMENT': '证据不足以给出确定交易金额，先处理可核验风险',
    'REDUCE_CLUSTER': '减仓恢复主题上限', 'RESTORE_CASH': '恢复现金约束', 'RESOLVE_GATE': '补全纪律定义', 'VERIFY_HOLDING': '核对单项持仓缺口',
    'MACRO_ASSESSMENT': '外围机制判断（推论）', 'RETRY_SOURCE': '补采净值资料',
}


def validate_delivery(report: dict[str, Any], decision: dict[str, Any]) -> bool:
    """Bind constraint cards and reviewed judgments to this exact source catalogue."""
    try:
        json.dumps(report, allow_nan=False)
        if decision != decide(report) or not decision['cards']:
            return False
        if report.get('assessment_schema_version') == 2:
            from src.advisor.intelligence import frozen_inputs
            from src.advisor.signals import assess_signals, RuleSet
            from src.advisor.report_view import serial, validate_view
            inputs = frozen_inputs(report['assessment_frozen_payload'])
            if inputs.input_sha256 != report.get('assessment_input_sha256'): return False
            import copy
            from src.advisor.intelligence import initialize_assessment
            check_payload = copy.deepcopy(report['assessment_frozen_payload'])
            check_payload['data'] = copy.deepcopy(report)
            initialize_assessment(check_payload)
            computed = check_payload['data']['signal_assessments']
            # Applicability and publication transitions are separately derived;
            # the truth and complete conditions must match frozen inputs.
            for a,b in zip(computed,report['signal_assessments'],strict=True):
                if any(a.get(k)!=b.get(k) for k in a if k not in {'transition','previous_signal_ref'}): return False
            from src.advisor.applicability import resolve_applicability
            from src.advisor.actions import assess_actions
            from src.advisor.research_evidence import EvidenceBundle
            applicability = resolve_applicability({**inputs.account,'analysis_as_of':inputs.as_of},inputs.identities,RuleSet.from_discipline(inputs.discipline,account_rules=inputs.account_rules))
            if report.get('reviewed_research'):
                import copy
                from src.advisor.comparisons import ReviewedResearch
                from src.advisor.intelligence import reassess_reviewed
                bundle = EvidenceBundle.from_dict(report['research_evidence'])
                reviewed = ReviewedResearch.from_dict(report['reviewed_research'])
                if reviewed.input_sha256 != inputs.input_sha256 or reviewed.evidence_sha256 != bundle.bundle_hash:return False
                if set(reviewed.accepted_judgment_ids) != {j['id'] for j in report.get('expert_analysis',{}).get('accepted',[])}:return False
                candidate = copy.deepcopy(report['assessment_frozen_payload'])
                candidate['data'] = copy.deepcopy(report)
                reassess_reviewed(candidate)
                expected_actions = candidate['data']['action_assessments']
            else:
                expected_actions = serial(assess_actions(inputs,report,assess_signals(inputs,rules=RuleSet.from_discipline(inputs.discipline,account_rules=inputs.account_rules)),applicability))
            if expected_actions != report['action_assessments']:return False
            if report.get('advisor_report') and not validate_view(report['advisor_report'],report,report['signal_assessments'],report['action_assessments']):return False
        return True
    except (ValueError, TypeError, KeyError):
        return False


def _n(value, digits=2):
    return '未知' if value is None else f'{value:,.{digits}f}'


def _cell(value):
    return ' '.join(str(value or '').split()).replace('|', '／')


def render_advice(report, decision=None, *, model_error=None, detail='summary') -> str:
    from src.advisor.report_view import AdvisorReportView
    if isinstance(report, AdvisorReportView):
        if detail not in {'summary','full'}:
            raise ValueError('detail must be summary or full')
        return report['summary_text' if detail == 'summary' else 'full_text']
    return render_legacy_advice(report, decision, model_error=model_error)


def render_legacy_advice(report: dict[str, Any], decision: dict[str, Any], *, model_error: str | None = None) -> str:
    if not validate_delivery(report, decision):
        raise ValueError('decision does not match the trusted assessment snapshot')
    p = report['portfolio']
    lines = [f"**判断：{LABELS[decision['stance']]}。**", '', f"信息截止：{report['as_of']}（上海时间）；基金估值对齐日：{p.get('valuation_as_of') or '尚未取得共同披露日'}。"]
    if p.get('analysis_scope'):
        lines += ['', '本轮只评估指定持仓；所列权重以所选子组合为分母，不能作为全账户权重或完整交易许可。']
    if model_error:
        lines += ['', '本轮模型分析未完成，以下为已获取证据与预先记录约束的确定性评估；外部信息综合判断仍有缺口。']
    judgments = decision.get('expert_judgments') or []
    if judgments:
        from src.advisor.facts import build_catalog
        facts = build_catalog(report)['facts']
        def prose(value):
            # Source links come only from the trusted catalogue; prose stays text.
            import re
            return re.sub(r'([\\`*_[\]<>|])', r'\\\1', str(value)).replace('\n', ' ')
        def reference(ref):
            fact = facts[ref]
            if fact['kind'] == 'retrieved_original':
                return f"[{_cell(fact['data']['title'])}]({fact['data']['url']})"
            labels = {'account': '账户与条件估值', 'discipline': '用户已记录纪律'}
            return labels.get(ref, f"`{_cell(ref)}`")
        lines += ['', '**独立研判（基于本轮证据的条件推论）**', '']
        for item in judgments:
            lines += [f"- **{prose(item['summary'])}** {prose(item['reasoning'])}",
                      f"  依据：{'、'.join(reference(ref) for ref in item['evidence_refs'])}。",
                      f"  反证与保留：{prose(item['counterargument'])}" + (f"（{'、'.join(reference(ref) for ref in item['counter_evidence_refs'])}）" if item['counter_evidence_refs'] else ''),
                      f"  触发：{prose(item['trigger'])}；失效：{prose(item['invalidation'])}。"]
    elif not model_error:
        lines += ['', '本轮独立综合研判未通过证据与论证审查，以下仍给出已核验的风险、约束和操作条件；不将这部分标为完整专家分析。']
    lines += ['', '**操作与条件**', '']
    for card in decision['cards']:
        if card['action'] in {'VERIFY_HOLDING', 'RETRY_SOURCE'}:
            continue
        lines += [f"- **{_cell(card['scope'])}：{LABELS.get(card['action'], card['action'])}。** {card['rationale']}"]
        if card.get('cash_redemption_estimate') is not None:
            lines += [f"  按当前披露净值估算：主题赎回留现金需约{_n(card['cash_redemption_estimate'])}元；同额转到非主题资产需约{_n(card['reallocation_estimate'])}元。在途主题赎回估值约{_n(card['pending_redemption_estimate'])}元，额外赎回测算约{_n(card['additional_cash_redemption_estimate'])}元。{card['limits']}"]
        if card.get('additional_quantity_reduction') is not None:
            lines += [f"  按已记录份额上限、扣除在途赎回预留后，额外需减份额约{_n(card['additional_quantity_reduction'])}份。"]
        lines += [f"  触发：{card['trigger']}；失效条件：{card['invalidation']}。下一次检查：{card['next_check']}。"]
    lines += ['', '**市场证据**', '', '| 指数 | 收盘（点） | 短均线 | 长均线 | 数据截至／来源／质量 |', '|---|---:|---:|---:|---|']
    for row in report.get('regime', []):
        lines += [f"| {_cell(row['symbol'])} | {_n(row.get('close'))} | {_n(row.get('sma_short'))} | {_n(row.get('sma_long'))} | {_cell(row.get('as_of'))}／{_cell(row.get('source'))}／{QUALITY.get(row.get('quality', 'unavailable'), '时效待核')} |"]
    intraday = [row for row in report.get('regime', []) if row.get('intraday_observation')]
    if intraday:
        lines += ['', '盘中观察单列，收盘纪律使用上一已收盘交易日：' + '；'.join(f"{row['symbol']}：{_n(row['intraday_observation']['value'])}点（{row['intraday_observation']['as_of']}，尚未收盘）" for row in intraday) + '。']
    if report.get('external_market'):
        lines += ['', '**外围行情**', '', '| 标的（原始代码） | 报价／单位 | 价格涨跌 | 数据截至／来源 |', '|---|---:|---:|---|']
        for row in report['external_market']:
            unit = '点' if row.get('quote_unit') == 'points' else row.get('currency') or '原始单位未标注'
            change = '未取得相邻两次观测' if row.get('price_change_percent') is None else f"{row['price_change_percent']:+.2f}%"
            lines += [f"| {_cell(row['symbol'])} | {_n(row['value'])} {_cell(unit)} | {change} | {row['as_of']}／{_cell(row['source'])} |"]
        lines += ['', '涨跌按各标的相邻两次已取得报价计算，日期各自保留；ETF用于场内观察，油金为期货报价、包含合约滚动影响，均不替代联接净值。收益率指数保留原始点位，不擅自换算国债收益率；价格回报未还原分红总收益。']
    for gap in report.get('external_gaps', []):
        lines += [f"- 外围行情待补：{_cell(gap.get('scope'))}（{'、'.join(gap.get('symbols', []))}）；{explain(gap.get('reason'))}。"]
    news = report.get('news') or {}
    lines += ['', '**外围信息与证据覆盖**', '']
    for item in news.get('primary', []):
        title = _cell(item.get('title')).replace('[', '（').replace(']', '）')
        lines += [f"- [{title}]({item['url']})：原文发布日期{item.get('published_at')}；经阅读服务核对原始URL与正文，事件发生时间仍需按原文区分。"]
        for fact in item.get('facts', []):
            lines += [f"  原文事实：{METRICS.get(fact['metric'], fact['metric'])}＝{_n(fact['value'])}%（统计期间{('上一个统计月' if fact.get('reference_period') == 'previous_month_year_on_year' else fact.get('reference_period') or item.get('reference_period') or '原文所述期间')}）。"]
    if not news.get('primary'):
        lines += ['本轮尚未核验到符合时效条件的外围原文。搜索摘要仅作线索，资金行为、政策影响和事件事实仍需原文核验；已取得的行情与持仓约束判断仍有效。']
    if news.get('coverage'):
        lines += ['已检索范围：' + '、'.join(row['category'] for row in news['coverage']) + '。有限检索不能声称覆盖全部外围信息，新闻本身也不证明后续价格反应。']
    for gap in news.get('gaps', []):
        scope = gap.get('scope') or urlsplit(str(gap.get('url') or '')).hostname or '原文核验'
        institutions = {'www.pbc.gov.cn': '中国人民银行', 'www.federalreserve.gov': '美联储', 'www.stats.gov.cn': '国家统计局', 'www.bls.gov': '美国劳工统计局', 'www.csrc.gov.cn': '中国证监会'}
        label = institutions.get(scope, f'`{scope}`' if '.' in scope else scope)
        lines += [f"- 外围缺口：{_cell(label)}；{explain(gap.get('reason'))}。"]
    lines += ['', '**持仓与现金**', '']
    if p.get('fund_value') is not None:
        lines += [f"本轮所选基金估值合计{_n(p['fund_value'])}元。" if p.get("analysis_scope") else f"基金市值合计{_n(p['fund_value'])}元；含已记录现金的账户金额{_n(p.get('account_value'))}元。"]
    elif p.get('fund_value_low') is not None:
        lines += [f"历史金额在无交易、无分红/再投资变化假设下的基金估值区间：{_n(p['fund_value_low'])}—{_n(p['fund_value_high'])}元。该区间是条件测算，不是当前份额或账户余额。"]
    else:
        lines += ['持仓总估值覆盖不完整，未将缺失资产当作零，也未据局部数据推算全账户仓位。']
    lines += [f"现金记录余额（扣除已记录在途申购）：{_n(p.get('available_cash'))} {_cell(p.get('available_cash_currency'))}；现金记录日期{p.get('cash_as_of') or '未知'}。旧现金记录不能保证当前可用余额。", '', '| 持仓 | 单位净值／日期 | 估值或条件区间（元） | 份额／可赎回份额 |', '|---|---|---:|---|']
    for row in report.get('holdings', []):
        value = _n(row.get('value')) if row.get('value') is not None else f"{_n(row.get('value_low'))}—{_n(row.get('value_high'))}" if row.get('value_low') is not None else '覆盖缺失'
        quote = f"{_n(row.get('price'), 4)}／{row.get('price_as_of') or '未知'}"
        if not row.get('symbol') and row.get('candidate_marks'):
            quote = '；'.join(f"{mark['symbol']}：{_n(mark['value'], 4)}／{mark['as_of']}" for mark in row['candidate_marks'])
        sources = '、'.join(sorted({str(item.get('source') or 'unknown') for item in row.get('evidence_refs', [])}))
        lines += [f"| {_cell(row['id'])} {_cell(row.get('name'))} | {quote}{('（' + _cell(sources) + '）') if sources else ''} | {value} | {_n(row.get('quantity'))}／{_n(row.get('available_quantity'))} |"]
    for cluster in report.get('clusters', []):
        weight = _n(cluster.get('fund_weight_percent')) if cluster.get('fund_weight_percent') is not None else f"条件区间{_n(cluster.get('fund_weight_low_percent'))}—{_n(cluster.get('fund_weight_high_percent'))}"
        lines += ['', f"{_cell(cluster['label'])}：占基金市值{weight}%，用户上限{cluster['max_percent']:g}%，状态{STATE.get(cluster['state'], '待核验')}{'；无交易等估值假设成立时未超限' if cluster.get('conditional_state') == 'WITHIN_CAP' and cluster['state'] == 'UNKNOWN' else ''}。成员按用户定义分类，不能当作底层穿透持仓。"]
    for row in report.get('rule_assessments', []):
        lines += ['', f"规则跟踪标的`{row['symbol']}`未有已确认持仓或方向，规则状态保留未知；不据此推断已触发可执行交易。"]
    risk = report.get('risk', {})
    lines += ['', '**风险与执行边界**', '']
    full = risk.get('full_portfolio') or risk.get('full_fund_sleeve') or risk.get('available_subportfolio')
    if full:
        lines += [f"历史风险计算范围：{full.get('scope')}；仅作历史描述，价格/净值回报未自动还原分红总收益，不能作为未来最大损失界限。"]
    else:
        lines += ['当前未给出完整账户的历史波动/回撤估计：精确权重或足够共同历史尚未满足。']
    if report.get('stress_scenarios'):
        lines += ['', '**压力情景（假设测算）**', '', '假设所有基金净值统一下跌、期间无交易且已记录现金不变，不计税费与分红；这是检验承受能力的情景参数，不是行情预测、发生概率或未来损失上界。', '', '| 假设基金跌幅 | 基金金额损失区间（元） | 含所记现金的账户损失比例 |', '|---|---:|---:|']
        for row in report['stress_scenarios']:
            ratio = f"{_n(row['account_loss_percent_low'])}—{_n(row['account_loss_percent_high'])}%" if row['account_loss_percent_low'] is not None else '现金口径不足，未推算全账户'
            lines += [f"| {abs(row['fund_change_percent']):g}% | {_n(row['fund_loss_low'])}—{_n(row['fund_loss_high'])} | {ratio} |"]
    lines += ['', '联接基金按披露净值估值，场内ETF价格不能代替联接净值；申赎按产品交易规则与确认净值结算。份额确认日期、适用费率或确认日缺失时，不编造赎回费用；必要风险处置仍需单独衡量。']
    unknown_quantities = any(row.get('quantity') is None for row in report.get('holdings', []))
    if unknown_quantities:
        lines += ['', '份额尚未提供的持仓继续沿用金额台账作条件测算；期间有交易时仅更新变动，不要求重报整份持仓。']
    rows = {row['id']: row for row in report.get('holdings', [])}
    gaps = [card for card in decision['cards'] if card['action'] in {'VERIFY_HOLDING', 'RETRY_SOURCE'} and (rows[card['scope']].get('gaps') or not rows[card['scope']].get('symbol'))]
    if gaps:
        lines += ['', '**需处理的单项缺口**', ''] + [f"- {_cell(card['scope'])}：{card['rationale']}。{card['next_check']}。" for card in gaps]
    if report.get('account_warnings'):
        lines += ['', '台账状态：' + '；'.join(explain(warning) for warning in report['account_warnings'])]
    lines += ['', f"依据快照：`{report['snapshot_sha256']}`；方法：{report['method_version']}／{decision['method_version']}。"]
    return '\n'.join(lines)
