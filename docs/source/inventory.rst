S3 archive inventory
====================

The :class:`~pyopensky.inventory.Inventory` class keeps a persistent,
version-aware inventory of the objects published in the OpenSky S3 hourly
archive.

A first scan of an hour prefix often runs before all objects are published,
and the publisher may replace an existing key with a new version. The
inventory records, for each hour prefix:

- **scans**: every listing attempt, whether it completed, and which object
  versions it returned. A failed pagination keeps the objects already seen
  but is never marked complete, and unseen objects are not declared
  nonexistent;
- **discovered**: all object versions ever seen, identified by
  ``(key, etag, version_id)`` so a replaced key yields a new entry;
- **confirmations**: versions that have been downloaded and confirmed;
- **pending**: discovered versions still worth entering the download queue.

.. code-block:: python

    from pyopensky.inventory import Inventory
    from pyopensky.s3 import S3Client

    client = S3Client()
    inventory = Inventory("inventory.json")

    for hour in ["2022-07-07 18:00Z", "2022-07-07 19:00Z"]:
        inventory.scan(client, hour)

    # Download what is still worth fetching
    report = inventory.fetch_pending(client, "2022-07-07 18:00Z", "data/")
    for version in report.missing:
        print(f"no longer available: {version.fingerprint}")

When a queued version is no longer obtainable (replaced or deleted by the
publisher), it is reported in :attr:`~pyopensky.inventory.FetchReport.missing`;
no other version is silently archived under the same key.

.. autoclass:: pyopensky.inventory.Inventory
    :members:
    :no-undoc-members:

.. autoclass:: pyopensky.inventory.ObjectVersion
    :members:
    :no-undoc-members:

.. autoclass:: pyopensky.inventory.ScanRecord
    :members:
    :no-undoc-members:

.. autoclass:: pyopensky.inventory.Confirmation
    :members:
    :no-undoc-members:

.. autoclass:: pyopensky.inventory.FetchReport
    :members:
    :no-undoc-members:
