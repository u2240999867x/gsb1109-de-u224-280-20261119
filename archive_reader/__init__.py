"""Offline BSON archive reader with resumable, credential-checked progress.

The archive writer publishes a *manifest* that gives every BSON document in
the archive a sequence number, an exact byte range, and a SHA-256 digest.
This package reads such archives offline:

- Only boundaries published by a manifest are accepted; boundaries are never
  guessed from the BSON bytes themselves.
- A record is delivered only when its full byte range has been received and
  the bytes hash to the digest published in the manifest.
- Progress (the *credentials*) can be persisted to disk and resumed later,
  e.g. after a legitimate append to the same fetch.
- Already-delivered records are never silently replaced: if a re-read
  conflicts with the credentials, the conflicting sequence numbers and the
  nature of each change are reported instead.
"""

from __future__ import annotations

from archive_reader.manifest import Manifest, ManifestEntry
from archive_reader.reader import (
    Conflict,
    Fetch,
    FetchMismatchError,
    ReadResult,
    SkippedRecord,
    read_archive,
)
from archive_reader.state import DeliveredRecord, PendingRecord, ProgressState

__all__ = [
    "Conflict",
    "DeliveredRecord",
    "Fetch",
    "FetchMismatchError",
    "Manifest",
    "ManifestEntry",
    "PendingRecord",
    "ProgressState",
    "ReadResult",
    "SkippedRecord",
    "read_archive",
]
