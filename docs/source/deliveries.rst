Deliveries
==========

``Stream.filenames`` normally reports each path once.  A busy collection
directory can instead receive several different files under the same name, in
which case a bare path is ambiguous.  The optional ``deliveries`` mode emits
sealed *delivery notifications* instead of paths.  A notification carries the
delivery identifier, the final content SHA-256, and a reader for the exact
bytes that were sealed.

.. code-block:: python

   from streamz import Stream

   source = Stream.filenames(
       'incoming/',
       deliveries='delivery-store',
       content_capacity=7,
   )
   source.sink(lambda delivery: print(delivery.delivery_id,
                                      delivery.sha256,
                                      delivery.read()))

Without ``deliveries`` the source keeps its original path-only behavior and
de-duplicates by path.

Declarations
------------

A declaration is a complete JSON object written into the watched directory,
normally with a ``.delivery`` suffix.  A half-written file is ignored until it
becomes valid JSON.

.. code-block:: json

   {
     "delivery_id": "Y",
     "sha256": "9f2c...e01",
     "path": "bill.csv",
     "segments": [
       {"source": "X", "block": 2, "last": 3},
       {"kind": "literal", "data": "RQ==", "encoding": "base64"}
     ]
   }

Segments are read in declaration order.  A segment may:

- carry literal bytes (``data``; ``base64`` or another ``encoding``), or a byte
  range of a local file (``file`` with ``offset``/``length``);
- refer to an earlier sealed delivery with ``source`` and a position
  (``block``/``blocks``/``first``/``last``/``range`` are one-based block
  positions; ``index``/``segment`` are zero-based declared segments);
- be pending (``{"kind": "pending", "length": N}``) until its bytes arrive.

References may repeat, be listed out of order, or run in reverse.  The order in
the declaration is the order in which readers see the bytes.

A declaration is accepted only when it is complete and the assembled bytes
match ``sha256``.  A malformed declaration, an out-of-range position, or a
digest mismatch raises an error and never produces a notification.  A
declaration whose pending fragment has not arrived returns
``PendingDelivery``, not a success notification.

Lifecycle
---------

Once accepted, content belongs to the store, not to the original file.  The
producer may truncate, replace or remove the source file; readers continue to
see the sealed bytes.  A reference resolves through the sealed manifest of the
named delivery, never through a newer file with the same original name.

``DeliveryNotification.acknowledge(consumer=...)`` confirms that a consumer is
finished with a delivery.  Content is released only when no other delivery
(including a pending declaration that has taken over a reference) and no open
reader needs it.  Re-delivery after a restart is idempotent and keeps the same
delivery identifier.

Capacity
--------

``content_capacity`` limits the number of content blocks retained.  Manifest,
digest and receipt metadata is not counted; content blocks, in-flight pending
fragments and temporary copies are.  ``DeliveryStore.can_accept`` reports the
required and missing block counts without changing the store.  A declaration
that does not fit raises :class:`~streamz.delivery.CapacityError` and is not
taken over.  Already accepted references are never discarded to make room.

Random access
-------------

``notification.open()`` returns a seekable reader.  Reads that cross segment
boundaries fetch only the required blocks; no complete synthetic copy is
built.  Readers retain the blocks they need until they are closed.

.. code-block:: python

   with delivery.open() as reader:
       reader.seek(1)
       two_bytes = reader.read(2)
