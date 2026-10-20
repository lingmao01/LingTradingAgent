"""User-facing explanations; raw diagnostic receipts stay in the snapshot."""
import re

QUALITY = {'current': '已对齐交易日', 'latest_disclosed': '最新已披露', 'stale': '旧数据',
           'unknown': '时效待核', 'unavailable': '未取到', 'historical_requested': '所查历史区间'}
STATE = {'UNKNOWN': '待核验', 'OVER CAP': '已超上限', 'AT CAP': '已到上限', 'CLEAR': '上限内'}
METRICS = {'cpi_yoy': '美国CPI同比', 'cpi_mom': '美国CPI环比（季调）', 'cpi_previous_yoy': '美国上期CPI同比'}


def explain(reason):
    text = str(reason or '')
    replacements = {
        'original publication date unavailable; search date is not substituted': '原文发布日期尚未核验',
        'directory has no eligible original article link': '检索结果为目录页，尚未取得合格原文',
        'no timely original-page evidence verified in this bounded research': '本轮未核验到近期原文',
        'original URL could not be confirmed by the reader': '原始来源地址尚未核验',
        'original page unavailable or explicitly cached': '原文读取失败或只返回缓存页',
        'publication is future-dated or outside the seven-day event window': '原文日期不在本轮近期信息范围内',
        'registered search/reader unavailable': '搜索或原文读取能力暂不可用',
        'Historical user-reported amounts are not current market values; share quantities and confirmation dates were not supplied.': '台账来自用户历史金额记录，份额与确认日期尚未提供',
        'A combined-code holding remains one unresolved group, not two separately funded positions.': '合并代码持仓仍按一笔资产处理',
        'share quantity or a dated historical value is not recorded': '该项尚缺份额或注明日期的金额',
        'mark freshness is not confirmed for this decision': '该项报价的当前时效尚未确认',
        'pending redemptions exceed recorded shares': '在途赎回份额超过台账份额，需核对订单状态',
        'short holdings require a separate signed-exposure risk model': '该项包含负向敞口，当前多头风险模型不适用',
    }
    if text in replacements:
        return replacements[text]
    if 'collection deadline' in text or 'timed out' in text or 'timeout' in text.lower():
        return '本轮采集达到时限，该项尚未完成'
    if 'NAV' in text and ('missing' in text or 'unavailable' in text):
        stamp = re.search(r'\d{4}-\d{2}-\d{2}', text)
        return f'估值基准日{stamp[0]}的净值尚未取到' if stamp else '该基金的净值资料尚未取到'
    if re.search('[\u4e00-\u9fff]', text):
        return text
    return '该项证据尚未完成核验，原始原因已保存在本轮依据中'
