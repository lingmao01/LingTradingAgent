"""Compact source catalogue for bounded financial interpretation."""
import hashlib
import json

from src.advisor.cash import actionable_cash


ACTION_CONDITIONS = {
    'NO_ADD': {'trigger': '所引用的禁新增依据与适用范围仍成立，且没有得到可核验的解除依据', 'invalidation': '所引用的行情、约束或适用范围发生实质变化时重新评估；单一指数变化不等于全账户获准加仓'},
    'REVIEW_REDUCTION': {'trigger': '所引用的集中、约束或持仓风险仍需复核；复核不表示已经满足卖出执行条件', 'invalidation': '所引用的风险、持仓、在途交易或适用约束改变时重新评估，不从复核意见自动推导交易金额'},
    'LIMITED_JUDGMENT': {'trigger': '所引用的资料与关键缺口仍是本轮判断的证据边界', 'invalidation': '关键缺口补齐、数据修订或反证出现时重新评估；此前缺资料不证明持有或买入最优'},
    'VERIFY_SOURCE': {'trigger': '所引用的资料仍有影响判断的缺失、时效或口径冲突', 'invalidation': '取得可核验且口径一致的新证据后重算，不把修复数据源视为交易许可'},
    'MAINTAIN_WITH_REVIEW': {'trigger': '未发现足以支持改变现有敞口的已核验依据，且所引用的约束与风险仍须持续复核', 'invalidation': '出现可执行的风险处置条件或新的有效证据时重新评估；暂缓改变不代表已证明持有最优'},
    'CONDITIONAL_ADD': {'trigger': '所引用的加仓许可在明确适用范围内有效，账户资金与风险约束满足；技术许可本身不证明买入优势', 'invalidation': '任一必要前提或资料口径改变时重评；许可不自动变成买入指令或确定金额'},
}


def build_catalog(report):
    active_discipline = {key: value for key, value in report.get('recorded_discipline', {}).items() if key not in {'note', 'redemption_fees'}}
    stress = [{**row, 'fund_decline_magnitude_percent': abs(row['fund_change_percent'])} for row in report.get('stress_scenarios', [])]
    facts = {'account': {'kind': 'calculation_and_user_statement', 'data': {
        'portfolio': report['portfolio'], 'personal_parameters': report.get('personal_parameters', {}),
        'stress_scenarios': stress, 'user_fee_cautions_not_verified_product_terms': {'terms': report.get('recorded_discipline', {}).get('redemption_fees', []), 'verified_product_contract': False}}},
        'discipline': {'kind': 'user_recorded_rules', 'data': {**active_discipline, 'unheld_rule_assessments': report.get('rule_assessments', [])}}}
    for row in report.get('holdings', []):
        data = dict(row)
        for key in ('fund_weight', 'account_weight', 'fund_weight_low', 'fund_weight_high'):
            if row.get(key) is not None:
                data[key + '_percent'] = row[key] * 100
        facts['holding:' + row['id']] = {'kind': 'holding_assessment', 'data': data}
    for row in report.get('clusters', []):
        facts['cluster:' + row['label']] = {'kind': 'user_defined_exposure', 'data': row}
    for row in report.get('regime', []):
        data = dict(row)
        labels = {'000001.SH': '上证指数', '000300.SH': '沪深300', '000688.SH': '科创50', '399006.SZ': '创业板指'}
        if row['symbol'] in labels:
            data['canonical_index_name'] = labels[row['symbol']]
        close, short = row.get('close'), row.get('sma_short')
        if close and short:
            data['close_vs_short_percent'] = (close / short - 1) * 100
            data['distance_to_short_percent'] = (short / close - 1) * 100
        facts['market:' + row['symbol']] = {'kind': 'dated_market_calculation', 'data': data}
    for row in report.get('external_market', []):
        facts['external:' + row['symbol']] = {'kind': 'outside_observation', 'data': row}
    for row in report.get('news', {}).get('primary', []):
        if row.get('verified') is True and row.get('content_sha256'):
            content = str(row.get('content') or '').split('Markdown Content:', 1)[-1]
            facts['news:' + row['content_sha256']] = {'kind': 'retrieved_original',
                'data': {key: row.get(key) for key in ('title', 'url', 'published_at', 'reference_period', 'facts', 'content_sha256')},
                'text': content[:6000], 'excerpt_only': len(content) > 6000}
    groups = []
    for label, words in [('人工智能', ('人工智能', 'AI')), ('科创50', ('科创50',)), ('机器人', ('机器人',)), ('半导体芯片', ('半导体', '芯片')), ('云计算', ('云计算',))]:
        members = [row for row in report.get('holdings', []) if any(word.casefold() in row.get('name', '').casefold() for word in words)]
        if not members:
            continue
        low = sum(row['fund_weight_low'] for row in members) * 100 if all(row.get('fund_weight_low') is not None for row in members) else None
        high = sum(row['fund_weight_high'] for row in members) * 100 if all(row.get('fund_weight_high') is not None for row in members) else None
        groups.append({'label': label, 'members': [row['id'] for row in members], 'fund_weight_low_percent': low, 'fund_weight_high_percent': high})
    facts['concentration'] = {'kind': 'descriptive_name_grouping_not_lookthrough_or_measured_correlation',
        'data': {'groups': sorted(groups, key=lambda row: row['fund_weight_low_percent'] if row['fund_weight_low_percent'] is not None else -1, reverse=True),
                 'basis': 'account product names; each combined holding counted once; no new user limits; percentage bounds retain original conditional-estimate assumptions'}}
    no_add = any(gate.get('state') == 'OPEN' and gate.get('action') == 'NO ADD'
                 for row in report.get('regime', []) for gate in row.get('gates', []))
    add_allowed = any(gate.get('state') == 'OPEN' and gate.get('action') == 'ADD ALLOWED'
                      for row in report.get('regime', []) for gate in row.get('gates', []))
    hard_limit = (any(row.get('state') == 'OVER CAP' for row in report.get('clusters', []))
                  or any(rule.get('state') in {'BREACHED', 'OVER CAP', 'REACHED'}
                         for row in report.get('holdings', []) for rule in row.get('rules', {}).values()))
    floor, available = report.get('personal_parameters', {}).get('minimum_cash'), actionable_cash(report)
    if floor is not None and available is not None and available < floor:
        hard_limit = True
    actions = ['NO_ADD', 'REVIEW_REDUCTION', 'LIMITED_JUDGMENT', 'VERIFY_SOURCE']
    if not hard_limit:
        actions.append('MAINTAIN_WITH_REVIEW')
    if not report['portfolio'].get('analysis_scope') and add_allowed and not no_add and not hard_limit and available is not None and available > 0 and report['portfolio'].get('coverage_complete'):
        actions.append('CONDITIONAL_ADD')
    catalog = {'as_of': report['as_of'], 'snapshot_sha256': report['snapshot_sha256'],
        'facts': facts, 'allowed_actions': actions,
        'scopes': ['PORTFOLIO', 'MACRO', *[row['id'] for row in report.get('holdings', [])],
                   *['CLUSTER:' + row['label'] for row in report.get('clusters', [])]],
        'action_conditions': {action: dict(ACTION_CONDITIONS[action]) for action in actions},
        'gaps': report.get('data_gaps', []) + report.get('external_gaps', []) + report.get('news', {}).get('gaps', [])}
    if report.get('assessment_schema_version') == 2:
        for key,fact in facts.items():
            if key.startswith('market:'):fact['data'].pop('gates',None)
        catalog['schema_version'] = 2
        catalog['input_sha256'] = report.get('assessment_input_sha256')
        catalog['subjects'] = report.get('research_subjects', {})
        catalog['action_assessments'] = report.get('rule_action_assessments', [])
        catalog['conditions'] = {}
        def condition_nodes(nodes, signal):
            for node in nodes:
                catalog['conditions'][node['condition_id']] = {**node, 'signal_id':signal['signal_id'],
                    'subject_id':signal['subject']['id'], 'effect':signal['effect']}
                condition_nodes(node.get('children', []), signal)
        for signal in report.get('signal_assessments', []):
            condition_nodes(signal.get('conditions', []), signal)
            facts['signal:'+signal['signal_id']] = {'kind':'deterministic_signal', 'data':signal}
        for item in report.get('research_evidence', {}).get('items', []):
            if item.get('usable'):
                facts[item['evidence_id']] = {'kind':'retrieved_evidence', 'data':{k:v for k,v in item.items() if k != 'metadata'},
                    'text': str(item.get('value') or ''), 'as_of':item.get('published_at'),
                    'usable':True,'verified':item.get('verified'), 'vintage':item.get('vintage')}
        # Research opinions can propose a different direction; these are not
        # technical permissions. Eligibility is computed only by assess_actions.
        catalog['allowed_actions'] = ['NO_ADD','REVIEW_REDUCTION','LIMITED_JUDGMENT','VERIFY_SOURCE','MAINTAIN_WITH_REVIEW','CONDITIONAL_ADD']
        catalog['scopes'] = list(dict.fromkeys(catalog['scopes'] + list(catalog['subjects'])))
        catalog['action_conditions'] = {}
        catalog['gaps'] += report.get('research_evidence', {}).get('gaps', [])
        catalog['research_context'] = report.get('personal_context', {})
        catalog['opportunities'] = report.get('opportunity_scan', {})
    catalog['sha256'] = hashlib.sha256(json.dumps(catalog, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return catalog
