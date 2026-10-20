"""Compare only committed, comparable closing-session rule assessments."""
from dataclasses import is_dataclass, replace
from src.advisor.report_view import serial, digest

def _key(row):
    subject=row.get('subject') or {}
    return (row.get('rule_id'),subject.get('id') if isinstance(subject,dict) else str(subject),row.get('scope'),row.get('effect',row.get('kind')))
def _day(row): return row.get('evidence_as_of') or row.get('as_of')
def _values(value):
    if isinstance(value,dict):
        return {k:_values(v) for k,v in value.items() if k not in {'fetched_at','input_sha256','evidence_refs','snapshot_sha256'}}
    if isinstance(value,list): return [_values(v) for v in value]
    return value

def compare_signals(current,previous):
    baseline={_key(serial(s)):serial(s) for s in previous or []}
    output=[]
    for signal in current:
        row=serial(signal); old=baseline.get(_key(row)); transition='NO_BASELINE'
        if old:
            same_version=all(row.get(k)==old.get(k) for k in ('rule_version','method_version','scope'))
            known=row.get('state') in {'TRUE','FALSE'} and old.get('state') in {'TRUE','FALSE'}
            closed=row.get('finality')=='closed_session' and old.get('finality')=='closed_session'
            ordered=bool(_day(row) and _day(old) and _day(row)>=_day(old))
            revision=_day(row)==_day(old) and digest(_values(row.get('conditions')))!=digest(_values(old.get('conditions')))
            if not same_version or not known or not closed or not ordered or revision:
                transition='NOT_COMPARABLE'
            elif row['state']=='TRUE': transition='STILL_TRIGGERED' if old['state']=='TRUE' else 'NEW_TRIGGER' if _day(row)>_day(old) else 'NOT_COMPARABLE'
            else: transition='CLEARED' if old['state']=='TRUE' and _day(row)>_day(old) else 'UNCHANGED_FALSE' if old['state']=='FALSE' else 'NOT_COMPARABLE'
        if is_dataclass(signal):
            output.append(replace(signal,transition=transition,previous_signal_ref=old.get('signal_id') if old else None))
        else: output.append({**row,'transition':transition,'previous_signal_ref':old.get('signal_id') if old else None})
    return tuple(output)
