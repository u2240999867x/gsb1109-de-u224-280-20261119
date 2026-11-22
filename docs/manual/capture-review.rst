.. _capture-review:

Reviewing the nearest capture on a UTC day
==========================================

Replay answers "nearest capture" from index timestamps.  An index row is only
a promise: the record it points at may be unreadable, may live on another
volume, or may carry a ``WARC-Date`` on a different UTC day.  When the result
must be *verifiable* before it is adopted, use
:mod:`pywb.warcserver.review`: it pages index sources, opens the records
actually needed, and refuses to call a day empty until every source has been
exhausted or has bounded itself.

Model
-----

A review is fixed by three values:

* ``url`` — the exact URL to review.
* ``day`` — a ``YYYYMMDD`` UTC day.
* ``at`` — the 14+ digit instant whose nearest capture is wanted.  It must
  fall on ``day``.

The *capture time* of a candidate is the ``WARC-Date`` of the record the index
row points at.  The *payload* of a revisit is resolved through the revisit
headers; a payload dated on the previous day is reported as the body source
and does not move the capture to that day.

Each round returns one of:

* ``selected`` — the best eligible capture is reported under ``capture``.
* ``pending`` — a source has not been exhausted and has not bounded away the
  remaining range.  ``resume_token`` continues the review.
* ``empty`` — every source was read to its end (or bounded itself), and no
  capture on ``day`` is eligible.
* ``restricted`` — no eligible capture was found, but access control blocked
  at least one source.  This is explicitly not the same as ``empty``.

Ranking is ``(distance, enumeration_order)``: on equal distance the candidate
enumerated first wins, including a revisit whose payload is from the previous
day.

Budgets
-------

Every round takes at most ``page_turns`` index pages and ``warc_opens`` WARC
records.  Both are hard limits: a round that needs more returns a resume
credential instead of overrunning.  A zero budget is therefore a pure "look,
do not touch" round and can never produce a terminal answer.

Only the records that can change the decision are opened.  A source may offer
a *boundary*: a claim about the true capture times in its not-yet-paged range.
The bound is used only when

* it is marked ``attested`` and its ``kind`` is trusted (``warc-date`` by
  default);
* any ``probe`` record named by the bound was opened and its ``WARC-Date``
  falls inside the claimed range; and
* the nearest point of the range is strictly farther from ``at`` than the
  current best.

A source that only knows CDX order returns ``attested=False``; its range stays
pending even if its CDX timestamps look unpromising.

Resume credentials
------------------

``result['resume_token']`` is a signed, self-contained snapshot of the review:
source cursors, the candidates already seen and verified, withdrawals, and the
spent budget.  Pass it back to ``run()``.  The token is HMAC-signed; tampering
raises :class:`pywb.warcserver.review.ResumeError`.  Set a stable key with the
``credential_key`` argument (or ``PYWB_REVIEW_KEY``) when tokens must survive a
process restart.

When an index changes between rounds, call
:meth:`ReviewEngine.invalidate_changed_sources`.  It compares each source's
snapshot and re-pages only the changed ones, discarding their old candidates;
unchanged sources keep their cursor and opened evidence.  A withdrawn candidate
leaves a tombstone and cannot be resurrected by re-paging its source.

Example
-------

.. code-block:: python

    from pywb.warcserver.review import (IndexSourcePages, ReviewEngine,
                                        WarcVerifier)

    engine = ReviewEngine(
        {'a': IndexSourcePages('a', source_a, url),
         'b': IndexSourcePages('b', source_b, url)},
        verifier=WarcVerifier(path_resolver=resolve_path),
        url=url, day='20261119', at='20261119120000',
        page_turns=3, warc_opens=2)

    result = engine.run(engine.new_state())
    while result['status'] == 'pending':
        result = engine.run(result['resume_token'],
                            page_turns=3, warc_opens=2)

    capture = result['capture']
    # capture['timestamp']   WARC-Date that the decision uses
    # capture['header']      record that supplied it
    # capture['body']        payload record (a revisit may point to another day)
