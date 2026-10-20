"""Recorded cash can fund current CNY actions only with a matching cutoff."""
def actionable_cash(report):
    p = report.get('portfolio') or {}
    if p.get('available_cash_currency') != 'CNY' or p.get('cash_as_of') != report.get('as_of'):
        return None
    return p.get('available_cash')
