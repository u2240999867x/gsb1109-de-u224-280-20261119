.. _recorder:

Recorder
========

The recorder component acts a proxy component, intercepting requests to and response from the :ref:`warcserver` and recording them
to a WARC file on disk.

The recorder uses the :class:`pywb.recorder.multifilewarcwriter.MultiFileWARCWriter` which extends the base :class:`warcio.warcwriter.WARCWriter` from :mod:`warcio` and provides support for:

* appending to multiple WARC files at once

* WARC 'rollover' based on maximum size idle time

* indexing (CDXJ) on write


Many of the features of the Recorder are created for use with Webrecorder project, although the core recorder is used to provide
a basic recording via ``/record/`` endpoint. (See: :ref:`recording-mode`)


Deduplication Filters
---------------------

The core recorder class provides for optional deduplication using the :class:`pywb.recorder.redisindexer.WritableRedisIndexer` class which requires Redis to store the index, and can be used to either:

* write duplicates responses.

* write ``revisit`` records.

* ignore duplicates and don't write to WARC.


Custom Filtering
----------------

The recorder filter system also includes a filtering system to allow for not writing certain requests and responses.
Filters include:

* Skipping by regex applied to source (``Warcserver-Source-Coll`` header from Warcserver)

* Skipping if ``Recorder-Skip: 1`` header is provided

* Skipping if ``Range`` request header is provided

* Filtering out certain HTTP headers, for example, http-only cookies

The additional recorder functionality will be enhanced in a future version.

For a more detailed examples, please consult the tests in :mod:`pywb.recorder.test.test_recorder`


Shared-directory continuation
-----------------------------

A capture job can be handed from one machine to another through a shared
directory.  :class:`pywb.recorder.continuation.ContinuationManifest` is the
only coordination point: it is a JSON document guarded by an advisory file
lock and re-read for every operation, so workers keep no in-process state and
a restarted machine sees exactly what the previous machine committed.

Each page records its authorization attempts and its durable state:

* ``pending`` -- no proven capture; available for a new claim.
* ``writing`` -- one owner holds a lease and is producing a capture.
* ``captured`` -- a complete WARC response exists in a named volume and byte
  range, with a verified payload digest.
* ``fenced`` -- the capture is proven but its index entry is not published.
* ``indexed`` -- the capture is proven and published.

Claims are exclusive.  When the control plane re-delivers the same task to a
second machine, the second claim is refused (and the delivery is still
recorded for auditing).  A takeover is only possible for a worker whose lease
has expired, or one that is explicitly declared dead.

A page that was already captured by the old machine is seeded from the legacy
volume with :meth:`ContinuationManifest.bootstrap_from_index`.  Legacy
volumes and the old index are opened read-only and never appended to.  A row
whose offset no longer points at a complete record is not trusted; the volume
is scanned for the real record instead.  A row without any verifiable record
stays pending.

Writing uses :class:`pywb.recorder.continuationwriter.ContinuationWriter`,
which creates a fresh volume, writes to a sibling ``.part`` file, verifies
every gzip member, and only then renames the volume into place.  A name that
already exists (for example a read-only legacy volume) is never opened for
append; a ``-rN`` name is allocated instead.

Recovery after a crash is evidence-driven:

* A complete volume left by a writer that died before recording it is
  *adopted* by the takeover machine and indexed as-is.
* A volume with a torn gzip member can never be verified and is ignored; the
  takeover machine captures the page into a new volume.
* An index message from a superseded writer is rejected with
  :class:`pywb.recorder.continuation.IndexFenced`; it can neither re-index the
  page nor replace the capture that was proven complete.

The command-line driver runs one durable step per process, which makes each
invocation equivalent to a separate machine::

    pywb-continuation ./batch init --url http://example.com/a
    pywb-continuation ./batch bootstrap --index ./old.cdxj
    pywb-continuation ./batch capture --owner machine-a \
        --url http://example.com/a --preferred rec.warc.gz \
        --payload-file ./a.html
    pywb-continuation ./batch plan

See :mod:`pywb.recorder.continuationcli` for the full command set, including
controlled interruption (``--interrupt``), stopping after the volume is
written (``--stop-after write``), adoption and index submission.

