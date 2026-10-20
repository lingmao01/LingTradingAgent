"""Typed feeder-report routing and state-only scheduled Verdicts."""
from __future__ import annotations


def assessment_playbook(config):
    if config.get('playbook') == 'daily-risk-watch':
        return 'daily-risk-watch'
    if config.get('playbook') == 'portfolio-checkup' and config.get('holdings_source') == 'position_book':
        return 'portfolio-checkup'
    return None


def feeder_book_available():
    # Preserve ordinary equity/crypto checkups and explicitly supplied books.
    from src.position_book import PositionBookStore
    try:
        book = PositionBookStore().load_account()
        rows = book.get('positions', []) + book.get('holding_groups', [])
        symbols = [symbol for row in rows for symbol in ([row['symbol']] if row.get('symbol') else row.get('candidate_symbols', []))]
        return bool(symbols) and all(symbol.endswith('.OF') for symbol in symbols)
    except (ValueError, TypeError, OSError):
        return False


def verdict_tail(report):
    lines = ['## Verdict', '']
    for row in [*report.get('holdings', []), *report.get('rule_assessments', [])]:
        symbol = row.get('symbol')
        if not symbol:
            continue  # ambiguous holding IDs are not valid instrument symbols
        for name, rule in row.get('rules', {}).items():
            state = rule.get('state', 'UNKNOWN')
            if state not in {'BREACHED', 'REACHED', 'OVER CAP', 'CLEAR', 'UNKNOWN'}:
                state = 'UNKNOWN'
            lines.append(f"- {symbol}: {state} - {name}; dated own-fund observation; {rule.get('reason') or 'recorded rule comparison'}")
    if report.get('signal_assessments'):
        for signal in report['signal_assessments']:
            subject = signal.get('subject') or {}
            symbol = subject.get('symbol') or subject.get('id') or 'PORTFOLIO'
            state = ({'BLOCK_ADD': 'NO ADD', 'REDUCE_TRIGGER': 'REDUCE', 'PERMIT_ADD': 'ADD ALLOWED'}.get(signal.get('effect'), 'UNKNOWN')
                     if signal.get('state') == 'TRUE' else 'CLOSED' if signal.get('state') == 'FALSE' else 'UNKNOWN')
            lines.append(f"- {symbol}: {state} - {signal.get('signal_id')}; {signal.get('scope')}; {signal.get('evidence_as_of') or 'unknown date'}")
    for row in ([] if report.get('signal_assessments') else report.get('regime', [])):
        for gate in row.get('gates', []):
            state = gate.get('action') if gate.get('state') == 'OPEN' else 'CLOSED' if gate.get('state') == 'CLOSED' else 'UNKNOWN'
            lines.append(f"- PORTFOLIO: {state} - {row['symbol']}; {gate.get('action')}; {gate.get('reason') or 'recorded condition'}")
    for row in report.get('clusters', []):
        if row.get('state') in {'OVER CAP', 'AT CAP', 'UNKNOWN'}:
            lines.append(f"- PORTFOLIO: {row['state']} - user-defined theme cap; exact weight state")
    # A raw account-balance decline is not a cash-flow-adjusted investment
    # drawdown, so it cannot truthfully trip the portfolio return breaker.
    if report.get('recorded_discipline', {}).get('max_drawdown_percent') is not None:
        lines.append('- PORTFOLIO: UNKNOWN - cash-flow-reconciled account drawdown unavailable')
    return '\n'.join(lines)
