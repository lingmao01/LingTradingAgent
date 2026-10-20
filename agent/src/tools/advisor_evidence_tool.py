"""Read the current run's durable advisor evidence after any compaction."""
import json

from src.advisor.snapshots import evidence_root
from src.agent.tools import BaseTool


class AdvisorEvidenceTool(BaseTool):
    name = 'get_advisor_evidence'
    description = 'Read dated evidence already collected for this advisor run, including every holding, constraints and verified primary news. Use after compaction instead of refetching unchanged sources. Pagination retains more than eight holdings. No path selection is allowed.'
    parameters = {'type': 'object', 'additionalProperties': False, 'properties': {
        'section': {'type': 'string', 'enum': ['holdings', 'portfolio', 'regime', 'constraints', 'news', 'observations', 'gaps']},
        'symbol': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 40},
    }, 'required': ['section']}
    is_readonly = True
    repeatable = True
    replay_after_compaction = True

    def execute(self, **kwargs):
        try:
            if set(kwargs) - {'section', 'symbol', 'offset', 'limit', 'run_dir'}:
                raise ValueError('unsupported evidence parameters')
            section = kwargs.get('section')
            if section not in self.parameters['properties']['section']['enum']:
                raise ValueError('unknown evidence section')
            offset, limit = kwargs.get('offset', 0), kwargs.get('limit', 10)
            if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 40:
                raise ValueError('invalid evidence pagination')
            payload = json.loads((evidence_root() / 'artifacts/advisor_snapshot.json').read_text(encoding='utf-8'))
            report = payload['data']
            key = {'constraints': 'recorded_discipline', 'gaps': 'data_gaps'}.get(section, section)
            data = report.get(key, [])
            symbol = kwargs.get('symbol')
            if symbol and isinstance(data, dict):
                data = {key: value for key, value in data.items() if key == symbol}
            elif symbol and isinstance(data, list):
                data = [row for row in data if isinstance(row, dict) and (row.get('symbol') == symbol or symbol in row.get('candidates', []))]
            total = len(data)
            if isinstance(data, list):
                data = data[offset:offset + limit]
            elif isinstance(data, dict) and section == 'observations':
                data = dict(list(data.items())[offset:offset + limit])
            return json.dumps({'ok': True, 'section': section, 'data': data, 'total': total, 'offset': offset,
                               'snapshot_sha256': report.get('snapshot_sha256')}, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, OSError, KeyError) as exc:
            return json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False)
