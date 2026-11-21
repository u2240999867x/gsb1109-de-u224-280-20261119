"""Scenario tests for the offline BSON archive reader.

Scenario: an on-duty operator receives a first manifest plus a partially
transferred fetch, delivers what is provable, saves the credentials, then
resumes after a legitimate append. Later a tampered same-name version and
two revised manifests arrive; delivered records must never be silently
replaced, and only fixes to undelivered records may be retried.
"""

from __future__ import annotations

import hashlib

import pytest

from archive_reader import (
    Fetch,
    FetchMismatchError,
    Manifest,
    ManifestEntry,
    ProgressState,
    read_archive,
)
from bson import encode as bson_encode


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- The archive -----------------------------------------------------------
# Record 3 embeds a full BSON document as a binary value, so its bytes
# contain what looks like an inner BSON header. A content-based scanner
# would mis-split it; the reader must trust only manifest boundaries.

DOC1 = {"_id": 1, "kind": "alpha", "n": 7}
DOC2 = {"_id": 2, "kind": "beta", "n": 8}
INNER = {"_id": "inner", "value": 99}
DOC3 = {"_id": 3, "payload": bson_encode(INNER)}
DOC4 = {"_id": 4, "kind": "omega", "n": 9}

B1 = bson_encode(DOC1)
B2 = bson_encode(DOC2)
B3 = bson_encode(DOC3)
B4 = bson_encode(DOC4)
ARCHIVE = B1 + B2 + B3 + B4

O1 = 0
O2 = O1 + len(B1)
O3 = O2 + len(B2)
O4 = O3 + len(B3)

E1 = ManifestEntry(1, O1, len(B1), _sha256(B1))
# Record 2's outer length is untrustworthy in v1: the published range is
# too long, so the digest cannot match and the record stays undelivered.
E2_BROKEN = ManifestEntry(2, O2, len(B2) + 8, _sha256(B2))
E2_FIXED = ManifestEntry(2, O2, len(B2), _sha256(B2))
E3 = ManifestEntry(3, O3, len(B3), _sha256(B3))
E4 = ManifestEntry(4, O4, len(B4), _sha256(B4))

MANIFEST_V1 = Manifest("manifest-v1", (E1, E2_BROKEN, E3, E4))
MANIFEST_FIX = Manifest("manifest-v2-fix", (E1, E2_FIXED, E3, E4))

# First fetch: only the first half of record 4's 4-byte length field arrived.
FETCH_V1 = Fetch("fetch-1", "dump.bson", ARCHIVE[: O4 + 2])
# Legitimate append to the same fetch: same id, same prefix, more bytes.
FETCH_FULL = Fetch("fetch-1", "dump.bson", ARCHIVE)


def _first_read_state(tmp_path):
    result = read_archive(FETCH_V1, MANIFEST_V1)
    creds = tmp_path / "credentials.json"
    result.state.save(creds)
    return result, ProgressState.load(creds)


def test_first_delivery_and_credentials(tmp_path):
    result, loaded = _first_read_state(tmp_path)

    # Records 1 and 3 are complete and digest-matching: delivered.
    assert [r.seq for r in result.delivered] == [1, 3]
    # Record 4: only half of its length field received -> pending.
    assert [p.seq for p in result.pending] == [4]
    assert result.pending[0].received == 2
    # Record 2: published range is wrong -> digest mismatch, not delivered.
    assert [(s.seq, s.reason) for s in result.skipped] == [(2, "digest_mismatch")]
    assert not result.conflicts

    # The persisted credentials carry seq, byte range, manifest, and fetch.
    assert loaded.to_dict() == result.state.to_dict()
    rec3 = loaded.delivered[3]
    assert (rec3.offset, rec3.length) == (O3, len(B3))
    assert rec3.sha256 == _sha256(B3)
    assert rec3.manifest_id == "manifest-v1"
    assert rec3.fetch_id == "fetch-1"
    assert loaded.pending[4].offset == O4


def test_record3_boundary_comes_from_manifest_not_content():
    # Record 3's bytes contain an inner BSON header; a content scanner would
    # see a plausible document boundary inside it. The reader delivers the
    # exact manifest range instead.
    inner_header = bson_encode(INNER)[:4]
    assert inner_header in B3[4:]
    result = read_archive(FETCH_V1, MANIFEST_V1)
    rec3 = next(r for r in result.delivered if r.seq == 3)
    assert rec3.length == len(B3)
    assert FETCH_V1.data[rec3.offset : rec3.offset + rec3.length] == B3


def test_resume_after_append_matches_fresh_rescan(tmp_path):
    _, loaded = _first_read_state(tmp_path)

    # Resume from the saved credentials against the completed fetch.
    resumed = read_archive(FETCH_FULL, MANIFEST_V1, loaded)
    assert [r.seq for r in resumed.delivered] == [4]  # only the gap is filled
    assert [r.seq for r in resumed.already_delivered] == [1, 3]  # not re-delivered
    assert not resumed.pending
    assert not resumed.conflicts

    # A fresh rescan of a clean copy must converge to the same records.
    fresh = read_archive(FETCH_FULL, MANIFEST_V1)
    assert [r.seq for r in fresh.delivered] == [1, 3, 4]
    assert resumed.state.delivered == fresh.state.delivered


def test_tampered_same_name_version_is_rejected(tmp_path):
    _, loaded = _first_read_state(tmp_path)

    # Another version under the same fetch identity: record 1's range was
    # replaced with same-length content, and the revised manifest no longer
    # recognizes record 3's original boundary (the attacker even "updates"
    # record 1's digest to legitimize the swap).
    evil = bytearray(ARCHIVE)
    evil[O1 + 10] ^= 0xFF
    fetch_evil = Fetch("fetch-1", "dump.bson", bytes(evil))
    e1_evil = ManifestEntry(1, O1, len(B1), _sha256(bytes(evil[O1 : O1 + len(B1)])))
    manifest_evil = Manifest("manifest-v2-evil", (e1_evil, E2_FIXED, E4))

    result = read_archive(fetch_evil, manifest_evil, loaded)

    # The exact sequence numbers and changes are called out.
    by_seq = {}
    for conflict in result.conflicts:
        by_seq.setdefault(conflict.seq, set()).add(conflict.kind)
    assert by_seq[1] == {"content_changed", "proof_withdrawn"}
    assert by_seq[3] == {"proof_withdrawn"}

    # Nothing new is delivered: the old pending position for record 4 is not
    # continued, and earlier confirmations are not treated as proof of the
    # new version. The credentials are left untouched.
    assert result.delivered == []
    assert result.pending == []
    assert result.state is loaded
    assert sorted(loaded.delivered) == [1, 3]


def test_fixed_manifest_retries_only_undelivered_record(tmp_path):
    _, loaded = _first_read_state(tmp_path)
    resumed = read_archive(FETCH_FULL, MANIFEST_V1, loaded)

    # This manifest only corrects record 2's range; the confirmed prefix and
    # record 3's proof are fully preserved.
    result = read_archive(FETCH_FULL, MANIFEST_FIX, resumed.state)

    assert not result.conflicts
    assert [r.seq for r in result.delivered] == [2]
    assert [r.seq for r in result.already_delivered] == [1, 3, 4]
    rec2 = result.state.delivered[2]
    assert (rec2.offset, rec2.length, rec2.sha256) == (O2, len(B2), _sha256(B2))
    assert rec2.manifest_id == "manifest-v2-fix"
    assert sorted(result.state.delivered) == [1, 2, 3, 4]


def test_missing_digest_cannot_be_delivered():
    manifest = Manifest(
        "manifest-nodigest",
        (ManifestEntry(2, O2, len(B2), None),),
    )
    result = read_archive(FETCH_FULL, manifest)
    assert result.delivered == []
    assert [(s.seq, s.reason) for s in result.skipped] == [(2, "missing_digest")]


def test_credentials_of_another_fetch_are_refused(tmp_path):
    _, loaded = _first_read_state(tmp_path)
    other_fetch = Fetch("fetch-2", "dump.bson", ARCHIVE)  # same name, new fetch
    with pytest.raises(FetchMismatchError):
        read_archive(other_fetch, MANIFEST_V1, loaded)
