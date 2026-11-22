.. _batch-index:

Incremental Batch Index
=======================

Some archive deliveries are split into volumes: the volumes with the
*response* records arrive first, and the volumes with the matching *request*
records arrive later, possibly in a different order.  The request records
carry the HTTP method and request body that make up the POST replay keys.
The response volumes may already have been moved to cold storage by the time
the request volumes arrive, so the index must be updated without reading the
response volumes again.

``wb-batch`` (also available as ``wb-manager batch``) implements such an
incremental index on top of :class:`pywb.indexer.batchindexer.BatchIndex`.
For every added volume it stores a small, durable *evidence* file: the WARC
record ids, target URIs, dates, record offsets and lengths, and the canonical
request body.  Pairing only uses this evidence and the WARC
``WARC-Record-Id`` / ``WARC-Concurrent-To`` relationship.  It never falls back
to record adjacency, timestamps or a shared target URI.

.. code-block:: bash

  # first delivery: the response volume (and a GET pair)
  wb-batch --root ./batch-index add --submission round-1 \
      --volume ./volumes/responses.warc.gz

  # second delivery: the request volume, in reverse order; the response
  # volume is already cold and is not read
  wb-batch --root ./batch-index add --submission round-2 \
      --volume ./volumes/requests.warc.gz

  # inspect the complete view
  wb-batch --root ./batch-index status

The current view is also linked from ``<root>/live``.  Configure a pywb
collection to read it directly::

  collections:
    forms:
      index_paths: ./batch-index/live/
      archive_paths: ./volumes/

Index Views
-----------

A view is a complete, immutable ``index.cdxj`` with a ``conflicts.json`` and
a ``waiting.json``.  Publishing a submission writes a new view directory and
switches ``current`` (and the ``live`` symlink) atomically, so a reader sees
either the previous complete view or the complete new view, never a mix.

A response whose request has not arrived is never given a POST key.  It stays
in ``waiting.json`` when another response shares the same url and second, as
those are exactly the forms whose requests will decide the keys.  A lone
response for its url and second, with no other candidate, is published as the
ordinary plain capture (as the normal indexer does for a response without a
request record), and an unlinked request is listed in ``unpaired.json``.

Conflicting Candidates
----------------------

A supplemental request may carry the same ``WARC-Record-Id`` as an already
linked request but different actual fields; or two request records may refer
to the same response.  Such a response is reported in ``conflicts.json``
instead of being resolved to "last one wins"::

  wb-batch --root ./batch-index conflicts

While the conflict is pending, the previous link remains readable.  Once it
is confirmed, the affected POST key is withdrawn; the other keys (the other
POST form and the ordinary GET) stay available.  The conflict record keeps
the exact volume and record ids of both candidates as evidence.

.. code-block:: bash

  wb-batch --root ./batch-index confirm --submission round-3

Withdrawing the conflicting volume removes the candidate.  The affected POST
key is restored from the original request evidence::

  wb-batch --root ./batch-index withdraw --submission round-4 conflict.warc.gz

Retry After Interruption
------------------------

Every submission is saved under ``<root>/submissions/`` before the new view is
published.  If the process is interrupted after the evidence and submission
were saved but before the view was published, retrying the *same* submission
id completes that one update.  Replaying an already applied submission is a
no-op and returns the same view; reusing a submission id with different
content is rejected.

``add`` also accepts ``--evidence saved.json`` to add evidence exported from
another machine, so a cold response volume never has to be fetched again.

.. code-block:: bash

  # one-shot batch CDX from volumes, in any order
  cdx-indexer -j --batch -o batch.cdxj ./volumes/*.warc.gz

Records that are waiting for a request, conflicts and unpaired records are
reported on stderr by ``cdx-indexer --batch``; the generated index only
contains proven pairs (plus the unambiguous lone responses described above).

Python API
----------

.. code-block:: python

  from pywb.indexer.batchindexer import BatchIndex, BatchReader

  index = BatchIndex('./batch-index')
  index.submit('round-1', volumes=['./volumes/responses.warc.gz'])
  index.submit('round-2', volumes=['./volumes/requests.warc.gz'])

  # pin one complete view: index and conflicts always match
  reader = BatchReader('./batch-index')
  lines = reader.index_lines()
  conflicts = reader.conflicts()

  status = index.status()          # waiting records, conflicts, withdrawn keys
  index.confirm_conflicts('round-3')
  index.withdraw_volume('round-4', 'conflict.warc.gz')

``BatchIndex.submit`` accepts ``evidence=[...]`` (one evidence dict or
:class:`ArchiveEvidence` per item) instead of volume paths, so a batch can be
rebuilt on another machine from saved evidence alone.  ``failpoint`` is a
callback with the stage name (``after-save`` / ``after-publish``); it exists
so tests can interrupt between saving a submission and publishing its view.
