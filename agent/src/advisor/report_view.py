"""Signal-first, lightweight public projection from the same frozen evidence."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
from typing import Any

_NAMES={'000001.SH':'上证指数','000300.SH':'沪深300','000688.SH':'科创50','399006.SZ':'创业板指'}
_LABELS={'BLOCK_ADD':'禁新增','REDUCE_TRIGGER':'减仓','PERMIT_ADD':'增仓'}

def serial(value):
    if hasattr(value,'to_dict'): return value.to_dict()
    if isinstance(value,dict): return {str(k):serial(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [serial(v) for v in value]
    return value

def digest(value):
    return hashlib.sha256(json.dumps(serial(value),ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':')).encode()).hexdigest()

@dataclass(frozen=True)
class AdvisorReportView:
    data: dict[str,Any]
    def to_dict(self): return json.loads(json.dumps(self.data,ensure_ascii=False))
    def __getitem__(self,key): return self.data[key]

def _kind(row): return row.get('effect',row.get('kind',row.get('signal_type',row.get('action',''))))
def _state(row): return str(row.get('state','UNKNOWN')).upper()
def _scope(row):
    value=row.get('subject_id') or row.get('subject') or row.get('symbol') or row.get('scope') or '账户'
    if isinstance(value,dict): value=value.get('id') or value.get('symbol') or str(value)
    return str(value)
def _status(rows):
    if any(_state(r)=='TRUE' for r in rows): return '已触发'
    configured=[r for r in rows if str(r.get('config_status','configured')).lower() not in {'not_configured','unconfigured'}]
    if not configured: return '未配置'
    if any(_state(r)=='UNKNOWN' for r in configured): return '未知，关键条件不足'
    return '未触发'

def build_report_view(report, signals, actions):
    signals,actions=serial(list(signals)),serial(list(actions))
    grouped={kind:[r for r in signals if _kind(r)==kind] for kind in _LABELS}
    add,reduce,blocked=(_status(grouped[k]) for k in ('PERMIT_ADD','REDUCE_TRIGGER','BLOCK_ADD'))
    hard_risk = [r for r in signals if _kind(r)=='HARD_RISK']
    if any(_state(r)=='TRUE' for r in hard_risk): reduce='已触发'
    scan=report.get('opportunity_scan') or {'coverage_status':'INCOMPLETE','candidates':[]}
    reviewed=report.get('reviewed_research') or {}
    from src.advisor.expert import approved
    judgments=approved(report)
    rotations=[a for a in actions if a.get('action')=='ROTATE' and a.get('origin')=='RESEARCH_JUDGMENT']
    eligible_rotation=any(a.get('eligibility') in {'ELIGIBLE','SUPPORTED','CONDITIONAL'} for a in rotations)
    rotate='有条件候选，详见执行缺口' if eligible_rotation else '暂无已审查的完整换仓依据'
    headline=f'规则增仓：{add}；减仓：{reduce}；换仓：{rotate}。'
    if blocked=='已触发': headline+='已记录指数禁新增条件成立。'
    research_actions = [a for a in actions if a.get('origin')=='RESEARCH_JUDGMENT']
    names_by_action = {'ADD':'增仓','REDUCE':'减仓','ROTATE':'换仓','HOLD_REVIEW':'维持观察'}
    active_research = [a for a in research_actions if a.get('eligibility')=='ELIGIBLE']
    if active_research:
        labels = list(dict.fromkeys(names_by_action.get(a['action'],a['action']) for a in active_research))
        headline += '研究建议'+'、'.join(labels)+'；金额与交易条件另核。'
    elif research_actions: headline += '研究候选尚有约束或证据缺口。'
    dates=sorted({str(r.get('evidence_as_of') or r.get('as_of')) for r in signals if r.get('evidence_as_of') or r.get('as_of')})
    date_note='、'.join(dates) if dates else '待核验'
    reasons=[]
    affected=[r for r in grouped['REDUCE_TRIGGER']+hard_risk if _state(r)=='TRUE']
    if affected: reasons.append('减仓审查对象：'+'、'.join(_scope(r) for r in affected[:3])+'。')
    elif report.get('holdings') and len(report['holdings']) <= 2:
        reasons.append('本轮对象：'+'、'.join(str(r.get('symbol') or r.get('id')) for r in report['holdings'][:2])+'；单位净值及逐项证据见完整报告。')
    if report.get('portfolio',{}).get('analysis_scope'): reasons.append('仅分析指定子组合，不能外推全账户。')
    if grouped['BLOCK_ADD'] and blocked=='已触发':
        reasons.append('趋势约束仍生效；不把禁新增直接解释为需要清仓。')
    if reduce=='未触发' and not judgments: reasons.append('已配置的技术减仓条件未同时满足；未定义的动能和个券规则另列未知或未配置。')
    if judgments:
        reasons.append('独立研判：'+str(judgments[0].get('summary','')).strip())
    else: reasons.append('模型分析未完成或独立研究未通过审查；规则结果仍可使用。')
    coverage=scan.get('coverage_status',scan.get('status','INCOMPLETE'))
    candidates=scan.get('candidates') or []
    names=[str(c.get('name') or c.get('subject',{}).get('name') or c.get('direction') or c.get('id','')) for c in candidates if isinstance(c,dict)]
    if coverage=='INCOMPLETE': opportunity='持仓外扫描未完成，不能据此认定没有机会。'
    elif coverage=='NO_QUALIFIED_CANDIDATE': opportunity='已扫描持仓外方向，暂未发现证据合格的候选。'
    elif names: opportunity='持仓外观察：'+'、'.join(names[:2])+'；先核验反证与准入。'
    else: opportunity='持仓外比较尚无已审查建议。'
    execution='份额、现金时效及费用缺口未齐时，只判断方向，不给交易金额。'
    checks=[str(judgments[0]['invalidation']) if judgments else '下一已确认收盘后，逐指数复算均线与已定义的确认条件。','若出现推翻研究前提的新原文，重新比较原仓、现金与候选方向。']
    next_check = ('改判条件：'+checks[0]) if judgments else '下一次检查：已确认收盘或关键反证更新。'
    summary='**'+headline+'**\n\n'+' '.join((reasons[:2]+reasons[-1:]) if len(reasons)>3 else reasons)+'\n\n'+opportunity+' '+execution+f'\n\n依据收盘：{date_note}；{next_check}'
    signal_status='complete' if signals and all(_state(r)!='UNKNOWN' for r in signals if str(r.get('config_status','configured')).lower()=='configured') else 'partial'
    research_status=report.get('expert_analysis',{}).get('status','unavailable')
    account_complete=bool(report.get('portfolio',{}).get('valuation_exact'))
    evidence = report.get('research_evidence', {})
    light_evidence = {k:v for k,v in evidence.items() if k != 'items'}
    light_evidence['items'] = [{k:v for k,v in row.items() if k != 'metadata'} for row in evidence.get('items',[])]
    view={'schema_version':2,'method_version':'advisor-view-v2','snapshot_sha256':report['snapshot_sha256'],
        'input_sha256':report.get('assessment_input_sha256'),
        'headline':headline,'signal_cards':signals,'action_cards':actions,
        'opportunity_summary':scan,'forecast_summary':reviewed.get('forecasts',[]),'next_checks':checks,
        'coverage':{'signals':signal_status,'research':research_status,'account':'complete' if account_complete else 'partial','execution_readiness':'unknown' if not account_complete else 'conditional','opportunities':coverage},
        'evidence_groups':{'market':report.get('regime',[]),'holdings':report.get('holdings',[]),
            'news':report.get('news',{}),'outside':report.get('external_market',[]),'research':light_evidence,'comparisons':reviewed.get('comparisons',[]),'identity_candidates':report.get('identity_candidate_collection',{})},
        'summary_text':summary}
    full=[summary,'','**全部规则结果（各作用域独立）**','','| 对象 | 规则 | 状态 | 配置 | 收盘日期 |','|---|---|---|---|---|']
    for row in signals:
        scope=_scope(row)
        full.append(f"| {_NAMES.get(scope,scope)} | {_LABELS.get(_kind(row),_kind(row))} | {_state(row)} | {row.get('config_status','configured')} | {row.get('evidence_as_of') or row.get('as_of') or '未知'} |")
    full += ['', '**逐项条件与适用范围**', '']
    def show_conditions(nodes, depth=0):
        for node in nodes:
            state = {'TRUE':'成立','FALSE':'不成立','UNKNOWN':'未知'}.get(node.get('state'),'未知')
            comparison = f"；观测{node.get('actual')}，阈值{node.get('threshold')}" if node.get('actual') is not None else ''
            full.append('  '*depth + f"- {node.get('display') or node.get('condition_id')}：{state}{comparison}。")
            show_conditions(node.get('children',[]),depth+1)
    for row in signals:
        full.append(f"\n{_NAMES.get(_scope(row),_scope(row))} · {_LABELS.get(_kind(row),_kind(row))} · {row.get('scope')}：")
        show_conditions(row.get('conditions',[]))
    full += ['', '**独立研究与反证**', '']
    for opinion in judgments:
        full += [f"- {opinion.get('summary','')} {opinion.get('reasoning','')}",f"  反证或待验证条件：{opinion.get('counterargument','')}；改判条件：{opinion.get('invalidation','')}。"]
    for comparison in reviewed.get('comparisons',[]):
        full += [f"- 比较期限：{comparison.get('horizon_start')} 至 {comparison.get('horizon_end')}；{comparison.get('reasoning')}。"]
        for option in comparison.get('alternatives',[]): full.append(f"  - {option.get('action')}：{option.get('tradeoff')}。")
    full += ['', '**已审查研究：情景与预判**', '']
    for scenario in reviewed.get('scenarios',[]):
        full += [f"- 情景 {scenario.get('id')}：{scenario.get('path')}；影响：{scenario.get('impact')}；前提：{'、'.join(scenario.get('assumptions',[]))}；期限 {scenario.get('horizon_start')} 至 {scenario.get('horizon_end')}；失效：{scenario.get('invalidation')}。"]
    for forecast in reviewed.get('forecasts',[]):
        full += [f"- 预判 {forecast.get('id')}：{forecast.get('proposition')}；期限 {forecast.get('horizon_start')} 至 {forecast.get('horizon_end')}；解析规则：{forecast.get('resolution_rule')}；解析来源：{forecast.get('resolution_source')}；失效：{forecast.get('invalidation')}。"]
    full += ['', '**行动资格与执行阻挡**', '', '| 方向与对象 | 来源 | 方向资格 | 金额资料 | 执行检查 |','|---|---|---|---|---|']
    for action in actions:
        checks='；'.join(f"{c.get('check_id')}={c.get('state')}" for c in action.get('execution_checks',[]))
        full.append(f"| {names_by_action.get(action.get('action'),action.get('action'))} {'、'.join(action.get('subject_ids',[]))} | {action.get('origin')} {action.get('proposal_id') or ''} | {action.get('eligibility')} | {action.get('sizing_status')} | {checks or '待核验'} |")
    full += ['', '**可行方向与覆盖**', '', opportunity]
    for candidate in candidates:
        subject=candidate.get('subject',{})
        full.append(f"- {subject.get('name')}：{candidate.get('maturity')}；{candidate.get('why_now')}；仍缺：{'、'.join(candidate.get('key_gaps',[]))}。")
    identity_collection=report.get('identity_candidate_collection',{})
    identity_reasons={'future_publication':'来源日期晚于本轮截止日，不能用于当前判断','publication_time_unknown':'原文发布时间未知，抓取时间不能代替发布日期','relationship_conflict':'关系声明与候选来源冲突，尚未裁定','secondary_profile_unverified':'第三方概况线索尚未由原始材料核验','holding_limit_reached':'本轮基金覆盖上限，尚未查询','symbol_limit_reached':'本轮代码覆盖上限，尚未查询','shared_budget_exhausted':'共享取证预算已用尽','readonly_profile_unavailable':'只读基金概况来源不可用','benchmark_is_not_tracking_relationship':'业绩比较基准不能当作跟踪指数','canonical_tracking_index_unconfirmed':'尚未确认标准跟踪指数代码','stale_profile':'概况缓存需重新核对','truncated_profile':'概况不完整','profile_source_failed_or_timeout':'概况来源失败或超时'}
    if identity_collection:
        full += ['', '**基金与指数关系候选（不作为执行依据）**','']
        for candidate in identity_collection.get('candidates',[]):
            full.append(f"- {candidate.get('symbol') or candidate.get('fund_symbol')}：{candidate.get('claim_text') or candidate.get('relationship_kind')}；来源：{candidate.get('source_ref') or candidate.get('source')}；原文日期：{candidate.get('published_at') or candidate.get('as_of') or '未知'}；状态为待核验声明，不能扩展为全账户技术许可。")
        if not identity_collection.get('candidates'):full.append('本轮未取得可核验的关系候选；保留市场参考结果，缺映射不等于技术信号未触发。')
        for gap in identity_collection.get('gaps',[]):
            full.append(f"- 关系核验缺口 {gap.get('symbol') or '本轮范围'}：{identity_reasons.get(gap.get('reason'),gap.get('reason'))}（{gap.get('reason')}）。")
        for candidate in identity_collection.get('candidates',[]):
            if candidate.get('conflicts'):
                full.append(f"- {candidate.get('symbol')} 关系存在冲突，尚未裁定；原始/候选声明：{json.dumps(candidate['conflicts'],ensure_ascii=False)}；不能据此映射信号。")
            if candidate.get('fetched_at'):full.append(f"- {candidate.get('symbol')} 抓取时间：{candidate['fetched_at']}；抓取时间不替代原文日期。")
            for reason in candidate.get('gaps',[]):full.append(f"- {candidate.get('symbol')}：{identity_reasons.get(reason,reason)}（{reason}）。")
        for covered in identity_collection.get('coverage',[]):
            full.append(f"- 关系采集覆盖：{covered.get('symbol')}，{covered.get('status')}；该轮只覆盖有限基金，不能认定全部映射已核验。")
    full += ['', '**净值、现金和执行资料**', '', '| 持仓 | 单位净值 | 日期 | 可赎回份额 |','|---|---|---|---|']
    for holding in report.get('holdings',[]):
        full.append(f"| {holding.get('name') or holding['id']} {holding['id']} | {holding.get('price') or '未知'} | {holding.get('price_as_of') or '未知'} | {holding.get('available_quantity') if holding.get('available_quantity') is not None else '未知'} |")
    full += ['', '历史金额仅供条件估值，不冒充当前账户余额；未知费用、现金时效与在途交易在执行前分别核对。', '', '**原文与证据**','']
    for item in light_evidence.get('items',[]):
        url=item.get('url')
        source=f"[{item.get('source')}]({url})" if url else str(item.get('source') or '未知来源')
        full.append(f"- {item.get('subject')} · {item.get('claim')}：{source}；发布/披露{item.get('published_at') or '未知'}；可用性{'已核验' if item.get('usable') else '线索或缺口'}；证据ID `{item.get('evidence_id')}`。")
    full += ['', f"规则：{report.get('rule_version') or '未配置'}；快照：{report['snapshot_sha256']}；冻结输入：{report.get('assessment_input_sha256') or '旧版'}。"]
    view['full_text']='\n'.join(full)
    view['view_sha256']=digest(view)
    return AdvisorReportView(view)

def validate_view(view,report,signals,actions):
    try: return serial(view)==build_report_view(report,signals,actions).to_dict()
    except (KeyError,TypeError,ValueError): return False
