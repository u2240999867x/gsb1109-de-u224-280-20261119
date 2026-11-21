"""Reading logic: deliver only manifest-proven, digest-matching records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from archive_reader.state import DeliveredRecord, PendingRecord, ProgressState

if TYPE_CHECKING:
    from archive_reader.manifest import Manifest


class FetchMismatchError(Exception):
    """Raised when credentials for one fetch are used to read another fetch."""


@dataclass(frozen=True)
class Fetch:
    """One fetched copy of the archive bytes.

    :param fetch_id: identity of this fetch. A legitimate append keeps the
        same ``fetch_id`` and extends the same byte prefix. A different
        version of the file is a different fetch, even under the same name.
    :param name: file name the fetch was obtained under.
    :param data: the bytes received so far.
    """

    fetch_id: str
    name: str
    data: bytes


@dataclass(frozen=True)
class Conflict:
    """A contradiction between the credentials and a re-read.

    ``kind`` is one of:

    - ``"content_changed"``: bytes in a confirmed range no longer hash to
      the digest recorded in the credentials.
    - ``"proof_withdrawn"``: the manifest no longer vouches for a confirmed
      record's sequence number, byte range, and digest.
    """

    seq: int
    kind: str
    detail: str


@dataclass(frozen=True)
class SkippedRecord:
    """A manifest entry that cannot be delivered in this read.

    ``reason`` is one of ``"missing_digest"``, ``"digest_mismatch"``, or
    ``"invalid_range"``. Skipped records stay undelivered and may be
    retried under a corrected manifest.
    """

    seq: int
    reason: str
    offset: int
    length: int
    manifest_id: str
    fetch_id: str


@dataclass
class ReadResult:
    """Outcome of one read pass.

    :param delivered: records newly delivered by this read.
    :param already_delivered: records confirmed by earlier reads (not
        re-delivered).
    :param pending: records waiting for more bytes of the same fetch.
    :param skipped: manifest entries that cannot be delivered (see
        :class:`SkippedRecord`).
    :param conflicts: contradictions with the credentials; when non-empty,
        nothing new was delivered and the state was left untouched.
    :param state: the updated credentials (the input state, unchanged, when
        conflicts were detected).
    """

    fetch_id: str
    manifest_id: str
    delivered: list[DeliveredRecord] = field(default_factory=list)
    already_delivered: list[DeliveredRecord] = field(default_factory=list)
    pending: list[PendingRecord] = field(default_factory=list)
    skipped: list[SkippedRecord] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    state: ProgressState | None = None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_archive(
    fetch: Fetch,
    manifest: Manifest,
    state: ProgressState | None = None,
) -> ReadResult:
    """Read *fetch* against *manifest*, optionally resuming from *state*.

    Only boundaries published by the manifest are considered; record
    boundaries are never derived from the BSON bytes themselves. A record
    is delivered only if its full byte range is present and hashes to the
    published digest.

    When *state* is given it must belong to the same fetch (same
    ``fetch_id``); otherwise :class:`FetchMismatchError` is raised. Every
    previously delivered record is re-verified against the fetch bytes and
    the manifest. Any contradiction is reported as a :class:`Conflict`
    naming the sequence number and the change, and the read aborts without
    delivering anything new, without continuing old pending positions, and
    without treating earlier confirmations as proof of the new version.
    """
    if state is not None and state.fetch_id != fetch.fetch_id:
        raise FetchMismatchError(
            f"credentials belong to fetch {state.fetch_id!r} "
            f"({state.fetch_name!r}), cannot use them to read fetch "
            f"{fetch.fetch_id!r} ({fetch.name!r})"
        )

    already: list[DeliveredRecord] = []
    conflicts: list[Conflict] = []
    if state is not None:
        for record in sorted(state.delivered.values(), key=lambda r: r.seq):
            already.append(record)
            chunk = fetch.data[record.offset : record.offset + record.length]
            if len(chunk) != record.length or _sha256(chunk) != record.sha256:
                conflicts.append(
                    Conflict(
                        seq=record.seq,
                        kind="content_changed",
                        detail=(
                            f"confirmed range [{record.offset}, "
                            f"{record.offset + record.length}) of fetch "
                            f"{fetch.fetch_id!r} no longer hashes to the "
                            f"digest recorded under manifest "
                            f"{record.manifest_id!r}"
                        ),
                    )
                )
            if not manifest.proves(record.seq, record.offset, record.length, record.sha256):
                conflicts.append(
                    Conflict(
                        seq=record.seq,
                        kind="proof_withdrawn",
                        detail=(
                            f"manifest {manifest.manifest_id!r} no longer "
                            f"vouches for seq {record.seq} at range "
                            f"[{record.offset}, {record.offset + record.length}) "
                            f"with the digest recorded under manifest "
                            f"{record.manifest_id!r}"
                        ),
                    )
                )
        if conflicts:
            return ReadResult(
                fetch_id=fetch.fetch_id,
                manifest_id=manifest.manifest_id,
                already_delivered=already,
                conflicts=conflicts,
                state=state,
            )

    delivered: list[DeliveredRecord] = []
    pending: list[PendingRecord] = []
    skipped: list[SkippedRecord] = []
    for entry in sorted(manifest.entries, key=lambda e: e.seq):
        if state is not None and entry.seq in state.delivered:
            continue  # already delivered; never re-deliver or replace
        if entry.offset < 0 or entry.length <= 0:
            skipped.append(
                SkippedRecord(
                    entry.seq, "invalid_range", entry.offset, entry.length,
                    manifest.manifest_id, fetch.fetch_id,
                )
            )
            continue
        if entry.sha256 is None:
            skipped.append(
                SkippedRecord(
                    entry.seq, "missing_digest", entry.offset, entry.length,
                    manifest.manifest_id, fetch.fetch_id,
                )
            )
            continue
        received = max(0, min(entry.length, len(fetch.data) - entry.offset))
        if received < entry.length:
            pending.append(
                PendingRecord(
                    entry.seq, entry.offset, entry.length, received,
                    manifest.manifest_id, fetch.fetch_id,
                )
            )
            continue
        chunk = fetch.data[entry.offset : entry.end]
        digest = _sha256(chunk)
        if digest != entry.sha256:
            skipped.append(
                SkippedRecord(
                    entry.seq, "digest_mismatch", entry.offset, entry.length,
                    manifest.manifest_id, fetch.fetch_id,
                )
            )
            continue
        delivered.append(
            DeliveredRecord(
                entry.seq, entry.offset, entry.length, digest,
                manifest.manifest_id, fetch.fetch_id,
            )
        )

    merged_delivered = dict(state.delivered) if state is not None else {}
    for record in delivered:
        merged_delivered[record.seq] = record
    new_state = ProgressState(
        fetch_id=fetch.fetch_id,
        fetch_name=fetch.name if state is None else state.fetch_name,
        delivered=merged_delivered,
        pending={record.seq: record for record in pending},
    )
    return ReadResult(
        fetch_id=fetch.fetch_id,
        manifest_id=manifest.manifest_id,
        delivered=delivered,
        already_delivered=already,
        pending=pending,
        skipped=skipped,
        state=new_state,
    )
