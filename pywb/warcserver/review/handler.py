"""HTTP entry point for a budgeted UTC-day capture review.

The handler is intentionally thin: it parses the review request, converts the
JSON body back into a :class:`~pywb.warcserver.review.model.ReviewState`, runs
one round, and returns the JSON result.  A ``pending`` result carries a
``resume_token`` the caller posts back in the next request.

Parameters
----------
url
    Exact URL to review (required).
day
    ``YYYYMMDD`` UTC day (required).
at
    Review instant, 14+ digits, on ``day`` (required).
pageTurns
    Page turns allowed in this request (default from the handler).
warcOpens
    WARC opens allowed in this request (default from the handler).
resumeToken
    Credential from a previous ``pending`` result.
"""

from pywb.utils.wbexception import BadRequestException
from pywb.warcserver.review.engine import ReviewEngine
from pywb.warcserver.review.credential import ResumeError


def to_bool(value):
    return str(value).lower() in ('1', 'true', 'yes')


class ReviewHandler(object):
    """Expose :class:`ReviewEngine` as a warcserver handler."""

    DEFAULT_PAGE_TURNS = 3
    DEFAULT_WARC_OPENS = 3

    def __init__(self, sources, verifier, credential_key=None, **kwargs):
        self.engine = ReviewEngine(sources, verifier=verifier,
                                   page_turns=self.DEFAULT_PAGE_TURNS,
                                   warc_opens=self.DEFAULT_WARC_OPENS,
                                   credential_key=credential_key)

    def get_supported_modes(self):
        return dict(modes=['review'])

    def __call__(self, params):
        mode = params.get('mode', 'review')
        if mode != 'review':
            errs = dict(last_exc=BadRequestException(
                'mode {0} not supported'.format(mode)))
            return None, None, errs

        url = params.get('url')
        day = params.get('day')
        at = params.get('at')

        try:
            page_turns = self._int(params.get('pageTurns'),
                                   self.DEFAULT_PAGE_TURNS)
            warc_opens = self._int(params.get('warcOpens'),
                                   self.DEFAULT_WARC_OPENS)

            token = params.get('resumeToken')
            if token:
                state = self.engine.decode(token)
            else:
                state = self.engine.new_state(url=url, day=day, at=at)

            changed = params.get('invalidateChanged')
            if changed and to_bool(changed):
                self.engine.invalidate_changed_sources(state)

            result = self.engine.run(state, page_turns=page_turns,
                                     warc_opens=warc_opens)
        except ResumeError as e:
            errs = dict(last_exc=BadRequestException(str(e)))
            return None, None, errs
        except ValueError as e:
            errs = dict(last_exc=BadRequestException(str(e)))
            return None, None, errs
        except Exception as e:
            errs = dict(last_exc=e)
            return None, None, errs

        return {'Content-Type': 'application/json'}, result, {}

    @staticmethod
    def _int(value, default):
        if value is None or value == '':
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            raise BadRequestException('invalid integer: {0!r}'.format(value))
