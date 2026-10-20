"""Registered, dated-source review of previously published personal advice."""
import json

from src.advisor.review import ReviewService
from src.agent.tools import BaseTool
from src.market_observation import market_now


class AdvisorReviewTool(BaseTool):
    name = 'review_published_advice'
    description = 'Review a validated personal-advice publication from advice_journal published/due. Fetch each fund own NAV and benchmark; compare predeclared 5/20 observation paper hold, periodic-invest and rebalance baselines with cost sensitivity. Records computed review evidence only; unknown execution remains unknown, no success probabilities or realized P&L are invented. Missing exact initial weights explicitly prevents portfolio evaluation.'
    parameters = {'type': 'object', 'additionalProperties': False, 'properties': {
        'publication_id': {'type': 'string'},
        'as_of': {'type': 'string', 'description': 'Review cutoff YYYY-MM-DD, default today in Shanghai.'},
    }, 'required': ['publication_id']}
    is_readonly = False  # persists the computed ex-post review, never an order
    repeatable = True

    def __init__(self, service=None):
        self.service = service or ReviewService()

    def execute(self, **kwargs):
        try:
            if set(kwargs) - {'publication_id', 'as_of', 'run_dir'}:
                raise ValueError('unsupported review parameters')
            identifier = kwargs.get('publication_id')
            if not isinstance(identifier, str) or len(identifier) != 64:
                raise ValueError('publication_id must name a published snapshot')
            result = self.service.review(identifier, as_of=kwargs.get('as_of') or market_now().date().isoformat())
            return json.dumps({'ok': True, 'data': result}, ensure_ascii=False, allow_nan=False)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            return json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False)
