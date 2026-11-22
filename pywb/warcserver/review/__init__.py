"""Review the best replayable capture of one URL on one UTC day.

Typical use::

    from pywb.warcserver.review import ReviewEngine, IndexSourcePages

    engine = ReviewEngine({'a': IndexSourcePages('a', source_a, url),
                           'b': IndexSourcePages('b', source_b, url)},
                          verifier=verifier, url=url, day='20261119',
                          at='20261119120000')
    result = engine.run(engine.new_state(), page_turns=3, warc_opens=2)
    if result['status'] == 'pending':
        state = engine.decode(result['resume_token'])
        result = engine.run(state, page_turns=3, warc_opens=2)

The engine is also usable one round at a time through the state object, which
is what the acceptance tests do.
"""

from .model import (Boundary, Candidate, CandidateState, RecordRef, ReviewState,
                    SourcePage, SourceState, Verification)
from .sources import IndexSourcePages, ReviewSource, SourceError, StaticPages
from .warc import ArchiveReadError, WarcVerifier, make_local_resolver
from .engine import Budget, BudgetExhausted, ReviewEngine, withdraw, withdraw_by_record
from .credential import CredentialCodec, ResumeError
from .handler import ReviewHandler


__all__ = [
    'Boundary', 'Candidate', 'CandidateState', 'RecordRef', 'ReviewState',
    'SourcePage', 'SourceState', 'Verification',
    'IndexSourcePages', 'ReviewSource', 'SourceError', 'StaticPages',
    'ArchiveReadError', 'WarcVerifier', 'make_local_resolver',
    'Budget', 'BudgetExhausted', 'ReviewEngine', 'withdraw', 'withdraw_by_record',
    'CredentialCodec', 'ResumeError', 'ReviewHandler',
]
