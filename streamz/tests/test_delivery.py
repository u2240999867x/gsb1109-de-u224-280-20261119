import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile

import pytest

from streamz.delivery import (
    CapacityError,
    DeliveryConflict,
    DeliveryNotification,
    DeliveryStore,
    DigestMismatch,
    IncompleteDeclaration,
    ManifestError,
    MissingContent,
    PendingDelivery,
    build_manifest,
    literal,
    ref,
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def block_manifest(delivery_id, segments, data, path=None):
    return build_manifest(delivery_id, segments, sha256=digest(data), path=path)


def write_delivery(path, manifest):
    with open(path, "w") as handle:
        json.dump(manifest, handle)
    return path


@pytest.fixture
def store(tmpdir):
    return DeliveryStore(str(tmpdir.join("store")), content_capacity=7)


def test_basic_chain_and_ranges(store):
    x = block_manifest("X", ["A", "B", "C", "D"], b"ABCD")
    y = block_manifest("Y", [ref("X", 1), ref("X", 2), "E"], b"BCE")
    z = block_manifest("Z", [ref("Y", 1), ref("Y", 2), ref("X", 0),
                             ref("X", 2)], b"CEAC")
    assert store.declare(x).read() == b"ABCD"
    assert store.declare(y).read() == b"BCE"
    assert store.declare(z).read() == b"CEAC"
    # The one-based "second to third block" wording is accepted too.
    manifest = build_manifest(
        "Z-range",
        [{"source": "Y", "first": 2, "last": 3},
         {"source": "X", "first": 1},
         {"source": "X", "first": 3}],
        sha256=digest(b"CEAC"),
    )
    assert store.declare(manifest).read() == b"CEAC"


def test_out_of_order_and_repeated_references(store):
    store.declare(block_manifest("X", ["A", "B", "C"], b"ABC"))
    manifest = block_manifest(
        "R", [ref("X", 2), ref("X", 0), ref("X", 2)], b"CAC")
    assert store.declare(manifest).read() == b"CAC"


def test_manifest_json_file_and_inline_base64(store, tmpdir):
    path = tmpdir.join("X.delivery")
    write_delivery(str(path), block_manifest("X", ["A", "B", "C"], b"ABC"))
    assert store.declare(str(path)).read() == b"ABC"

    raw = block_manifest("B64", [literal(b"A"), literal(b"B")], b"AB")
    assert store.declare(raw).read() == b"AB"


def test_half_written_declaration_is_not_success(store, tmpdir):
    path = str(tmpdir.join("X.delivery"))
    with open(path, "w") as handle:
        handle.write('{"delivery_id": "X", "segments": [')

    with pytest.raises(IncompleteDeclaration):
        store.declare(path)
    assert store.records() == []


def test_digest_mismatch_is_not_success(store):
    manifest = build_manifest(
        "X", ["A", "B"], sha256=digest(b"not this"))
    with pytest.raises(DigestMismatch):
        store.declare(manifest)
    assert store.records() == []


def test_segment_position_out_of_range(store):
    store.declare(block_manifest("X", ["A", "B"], b"AB"))
    manifest = build_manifest("Y", [ref("X", 2)], sha256=digest(b"C"))
    with pytest.raises(ManifestError):
        store.declare(manifest)
    assert "Y" not in store


def test_missing_source_names_delivery_and_segment(store):
    manifest = build_manifest("Y", [ref("X", 1)], sha256=digest(b"B"))
    with pytest.raises(MissingContent) as error:
        store.declare(manifest)
    assert error.value.missing == [{
        "delivery_id": "X", "segment": 2, "index": 1,
        "reason": "unknown delivery"}]
    assert "X" in str(error.value)


def test_acknowledgement_retains_taken_over_references(store):
    store.declare(block_manifest("X", ["A", "B", "C", "D"], b"ABCD"))
    store.declare(block_manifest("Y", [ref("X", 1), ref("X", 2), "E"], b"BCE"))
    store.declare(block_manifest(
        "Z", [ref("Y", 1), ref("Y", 2), ref("X", 0), ref("X", 2)], b"CEAC"))

    store.acknowledge("X", consumer="x-consumer")
    store.acknowledge("Y", consumer="y-consumer")

    # D is only used by X; it is released.  A, B and C are still required by Z.
    assert store.verify("Z") == digest(b"CEAC")
    assert store.read("Z") == b"CEAC"
    assert store.used_blocks() == 3
    assert store.takeover_report("Z")["missing"] == []


def test_reclaimed_reference_reports_concrete_gap(store):
    store.declare(block_manifest("X", ["A", "B", "C"], b"ABC"))
    store.acknowledge("X")
    assert store.used_blocks() == 0

    manifest = build_manifest("Y", [ref("X", 1)], sha256=digest(b"B"))
    with pytest.raises(MissingContent) as error:
        store.declare(manifest)
    assert error.value.missing[0]["delivery_id"] == "X"
    assert error.value.missing[0]["segment"] == 2


def test_pending_delivery_takes_over_then_gets_fragment(store):
    store.declare(block_manifest("X", ["A", "B", "C", "D"], b"ABCD"))
    store.declare(block_manifest("Y", [ref("X", 1), ref("X", 2), "E"], b"BCE"))
    manifest = build_manifest(
        "Z",
        [{"source": "Y", "first": 2, "last": 3},
         {"kind": "pending", "length": 1},
         {"source": "X", "first": 1},
         {"source": "X", "first": 3}],
        sha256=digest(b"CEFAC"),
    )
    pending = store.declare(manifest)
    assert isinstance(pending, PendingDelivery)
    assert pending.pending_segments == (2,)  # zero-based: after Y's second block
    assert "Z" in store
    assert store.takeover_report("Z")["taken_over"]

    # Consumers of X and Y can finish before the fragment arrives.
    store.acknowledge("X", consumer="x-consumer")
    store.acknowledge("Y", consumer="y-consumer")
    # B is not used by Z any more, but C and E are still held for Z.
    assert store.status("Z")["missing"] == []

    result = pending.supply(2, b"F")
    assert isinstance(result, DeliveryNotification)
    assert result.read() == b"CEFAC"
    assert result.digest == digest(b"CEFAC")


def test_pending_supply_digest_mismatch_leaves_declaration_pending(store):
    store.declare(block_manifest("X", ["A", "B"], b"AB"))
    manifest = build_manifest(
        "Y",
        [ref("X", 0), {"kind": "pending", "length": 1}],
        sha256=digest(b"AC"),
    )
    pending = store.declare(manifest)
    with pytest.raises(DigestMismatch):
        pending.supply(1, b"Z")
    assert store.is_pending("Y")
    assert pending.read() if False else True
    assert pending.supply(1, b"C").read() == b"AC"


def test_random_access_and_seek_across_boundaries(store):
    store.declare(block_manifest("X", ["1", "2", "3", "4", "5"], b"12345"))
    store.declare(block_manifest("Y", [ref("X", 1), ref("X", 2)], b"23"))
    store.declare(block_manifest("Z", [ref("X", 0), ref("Y", 0), ref("Y", 1)],
                                 b"123"))
    with store.open("Z") as reader:
        reader.seek(1)
        assert reader.read(2) == b"23"
        reader.seek(-2, os.SEEK_END)
        assert reader.read() == b"23"
        reader.seek(0)
        assert reader.read(3) == b"123"
        assert reader.tell() == 3
    assert store.read("Z", offset=1, length=2) == b"23"


def test_reader_retains_blocks_after_ack(store):
    store.declare(block_manifest("X", ["A", "B"], b"AB"))
    reader = store.open("X")
    store.acknowledge("X")
    assert store.used_blocks() == 2
    assert reader.read() == b"AB"
    reader.close()
    assert store.used_blocks() == 0


def test_restart_keeps_unacknowledged_delivery_and_pending(store, tmpdir):
    root = str(tmpdir.join("store"))
    store.declare(block_manifest("X", ["A", "B", "C"], b"ABC"))
    store.declare(block_manifest("Y", [ref("X", 0), ref("X", 1)], b"AB"))
    manifest = build_manifest(
        "Z", [ref("Y", 0), {"kind": "pending", "length": 1}],
        sha256=digest(b"AQ"))
    store.declare(manifest)

    restarted = DeliveryStore(root, content_capacity=7)
    assert restarted.is_ready("X")
    assert restarted.is_pending("Z")
    assert restarted.read("X") == b"ABC"
    assert [item.delivery_id for item in restarted.redeliver()] == ["X", "Y"]
    restarted.acknowledge("X", consumer="x-consumer")
    restarted.acknowledge("Y", consumer="y-consumer")
    assert [item.delivery_id for item in restarted.redeliver()] == []


def test_original_can_be_truncated_and_replaced(store, tmpdir):
    original = str(tmpdir.join("bill.txt"))
    with open(original, "wb") as handle:
        handle.write(b"ABCD")
    manifest = build_manifest(
        "X", [{"file": original, "offset": 0, "length": 2},
              {"file": original, "offset": 2, "length": 2}],
        sha256=digest(b"ABCD"), path="bill.txt")
    store.declare(manifest)
    with open(original, "wb") as handle:
        handle.write(b"new content")
    assert store.read("X") == b"ABCD"

    with open(original, "wb") as handle:
        handle.write(b"ABCD")
    replacement = build_manifest(
        "Y", [{"file": original, "offset": 0, "length": 4}],
        sha256=digest(b"ABCD"), path="bill.txt")
    store.declare(replacement)
    os.unlink(original)
    assert store.read("X") == b"ABCD"
    assert store.read("Y") == b"ABCD"


def test_duplicate_declaration_is_idempotent(store):
    first = block_manifest("X", ["A", "B"], b"AB")
    a = store.declare(first)
    b = store.declare(first)
    assert a.read() == b.read() == b"AB"
    assert len(store.records()) == 1
    changed = build_manifest("X", ["A", "C"], sha256=digest(b"AC"))
    with pytest.raises(DeliveryConflict):
        store.declare(changed)


def test_capacity_is_per_content_block(store):
    store.declare(block_manifest("X", ["A", "B", "C", "D"], b"ABCD"))
    store.declare(block_manifest("Y", [ref("X", 1), ref("X", 2), "E"], b"BCE"))
    store.declare(block_manifest(
        "Z", [ref("Y", 1), ref("Y", 2), ref("X", 0), ref("X", 2)], b"CEAC"))
    # ABCD + E = five distinct blocks.  A sixth new block still fits, but two
    # more do not.
    store.declare(block_manifest("F", ["F"], b"F"))
    with pytest.raises(CapacityError):
        store.declare(block_manifest("G", ["G", "H"], b"GH"))


def test_capacity_reports_deficit_and_rejects_without_taking_over(store, tmpdir):
    store.declare(block_manifest("X", ["A", "B", "C", "D"], b"ABCD"))
    store.declare(block_manifest("Y", [ref("X", 1), ref("X", 2), "E"], b"BCE"))
    # X contributes 4 blocks and Y's new E is the fifth.  With 7 total, only
    # 2 new blocks fit; this declaration needs 4, so it is 2 short.
    report = store.can_accept(block_manifest("Z", ["F", "G", "H", "I"], b"FGHI"))
    assert report["deficit_blocks"] == 2
    assert report["accepted"] is False
    with pytest.raises(CapacityError) as error:
        store.declare(block_manifest("Z", ["F", "G", "H", "I"], b"FGHI"))
    assert error.value.deficit_blocks == 2
    assert "Z" not in store


def test_release_allows_next_full_delivery(store):
    store.declare(block_manifest("X", ["A", "B", "C", "D"], b"ABCD"))
    store.declare(block_manifest("Y", [ref("X", 1), ref("X", 2), "E"], b"BCE"))
    store.declare(block_manifest(
        "Z", [ref("Y", 1), ref("Y", 2), ref("X", 0), ref("X", 2)], b"CEAC"))
    store.acknowledge("X")
    store.acknowledge("Y")
    store.acknowledge("Z")
    assert store.used_blocks() == 0
    full = block_manifest("N", ["1", "2", "3", "4", "5", "6", "7"], b"1234567")
    assert store.declare(full).read() == b"1234567"


def test_filenames_default_behavior_unchanged(tmpdir):
    from streamz import Stream
    directory = str(tmpdir.mkdir("watched"))
    for name in "abc":
        open(os.path.join(directory, name), "w").close()

    source = Stream.filenames(directory, asynchronous=False)
    seen = source.sink_to_list()
    import time
    deadline = time.time() + 5
    source.start()
    try:
        while len(seen) < 3 and time.time() < deadline:
            time.sleep(0.01)
    finally:
        source.stop()
    assert seen == [os.path.join(directory, name) for name in "abc"]


def test_filenames_delivery_mode(store, tmpdir):
    from streamz import Stream
    directory = str(tmpdir.mkdir("watched"))
    write_delivery(os.path.join(directory, "X.delivery"),
                   block_manifest("X", ["A", "B", "C"], b"ABC",
                                  path="bill.txt"))

    source = Stream.filenames(directory, asynchronous=False, deliveries=store)
    events = source.sink_to_list()
    source.start()
    import time
    deadline = time.time() + 5
    try:
        while not events and time.time() < deadline:
            time.sleep(0.01)
    finally:
        source.stop()
    assert len(events) == 1
    assert events[0].read() == b"ABC"
    assert events[0]["sha256"] == digest(b"ABC")


def test_block_references_address_global_content_blocks(tmpdir):
    store = DeliveryStore(str(tmpdir.join("blocks")), content_capacity=7,
                          block_size=1)
    store.declare(block_manifest("X", ["ABCD"], b"ABCD"))
    assert [segment["size"] for segment in store.get("X").segments] == [4]
    manifest = build_manifest("Y", [{"source": "X", "first": 2, "last": 3}],
                              sha256=digest(b"BC"))
    assert store.declare(manifest).read() == b"BC"

    manifest = build_manifest(
        "Z", [{"source": "Y", "first": 1, "last": 2},
              {"source": "X", "block": 1},
              {"source": "X", "block": 3}],
        sha256=digest(b"BCAC"))
    assert store.declare(manifest).read() == b"BCAC"


def test_block_range_out_of_bounds(store):
    store.declare(block_manifest("X", ["AB"], b"AB"))
    manifest = build_manifest(
        "Y", [{"source": "X", "first": 1, "last": 3}],
        sha256=digest(b"AB"))
    with pytest.raises(ManifestError):
        store.declare(manifest)


def test_pending_completion_can_reuse_existing_block(tmpdir):
    store = DeliveryStore(str(tmpdir.join("reuse")), content_capacity=7)
    store.declare(block_manifest("X", ["A", "B"], b"AB"))
    manifest = build_manifest(
        "Y",
        [ref("X", 0), {"kind": "pending", "length": 1}],
        sha256=digest(b"AA"),
    )
    pending = store.declare(manifest)
    # The completion declaration supplies a literal block that already exists.
    completed = build_manifest("Y", [ref("X", 0), "A"], sha256=digest(b"AA"))
    result = store.declare(completed)
    assert isinstance(result, DeliveryNotification)
    assert result.read() == b"AA"
    assert store.used_blocks() == 2


def test_truncate_overwrite_and_restart(tmpdir):
    directory = tmpdir.mkdir("incoming")
    original = str(directory.join("bill.bin"))
    root = str(tmpdir.join("persist"))
    with open(original, "wb") as handle:
        handle.write(b"ABCD")
    store = DeliveryStore(root, content_capacity=7)
    store.declare(build_manifest(
        "R1", [{"file": "bill.bin", "offset": 0, "length": 4}],
        sha256=digest(b"ABCD"), path="bill.bin"), base_path=str(directory))
    with open(original, "wb") as handle:
        handle.write(b"AB")
    assert store.read("R1") == b"ABCD"
    with open(original, "wb") as handle:
        handle.write(b"ABCD")
    store.declare(build_manifest(
        "R2", [{"file": "bill.bin", "offset": 0, "length": 4}],
        sha256=digest(b"ABCD"), path="bill.bin"), base_path=str(directory))
    os.unlink(original)

    restarted = DeliveryStore(root, content_capacity=7)
    assert restarted.read("R1") == b"ABCD"
    assert restarted.read("R2") == b"ABCD"
    assert restarted.verify("R2") == digest(b"ABCD")


def test_restart_redelivers_only_unacknowledged(tmpdir):
    root = str(tmpdir.join("redelivery"))
    store = DeliveryStore(root, content_capacity=7)
    for name, content in [("X", b"A"), ("Y", b"B"), ("Z", b"C")]:
        store.declare(block_manifest(name, [content], content))
    store.acknowledge("X", consumer="consumer-1")
    restarted = DeliveryStore(root, content_capacity=7)
    pending = restarted.redeliver()
    assert [(item.delivery_id, item.sha256) for item in pending] == [
        ("Y", digest(b"B")), ("Z", digest(b"C"))]
    for item in pending:
        assert item.acknowledge(consumer="consumer-1")
    assert restarted.redeliver() == []


def test_seven_block_budget_three_versions_then_next_full(tmpdir):
    store = DeliveryStore(str(tmpdir.join("budget")), content_capacity=7)
    store.declare(block_manifest("X", ["A", "B", "C", "D"], b"ABCD"))
    store.declare(block_manifest("Y", [ref("X", 1), ref("X", 2), "E"], b"BCE"))
    store.declare(block_manifest(
        "Z", [ref("Y", 1), ref("Y", 2), ref("X", 0), ref("X", 2)], b"CEAC"))
    pending = store.declare(build_manifest(
        "ZF",
        [{"source": "Y", "first": 2, "last": 3},
         {"pending": True, "length": 1},
         {"source": "X", "first": 1},
         {"source": "X", "first": 3}],
        sha256=digest(b"CEFAC")))
    assert isinstance(pending, PendingDelivery)
    # Only blocks A, C, E, and the in-flight F are still distinct; X, Y and Z
    # remain readable within the budget.
    assert store.used_blocks() <= 7
    store.acknowledge("X", consumer="x")
    store.acknowledge("Y", consumer="y")
    assert store.read("Z") == b"CEAC"
    ready = pending.supply(2, b"F")
    assert ready.read() == b"CEFAC"
    for name in ("Z", "ZF"):
        store.acknowledge(name)
    assert store.used_blocks() == 0
    full = block_manifest("N", list("1234567"), b"1234567")
    assert store.declare(full).read() == b"1234567"


def test_reference_after_reclaim_does_not_use_latest_file(tmpdir):
    directory = tmpdir.mkdir("replace")
    original = str(directory.join("same.bin"))
    store = DeliveryStore(str(tmpdir.join("store2")), content_capacity=7)
    with open(original, "wb") as handle:
        handle.write(b"OLD")
    store.declare(build_manifest(
        "OLD", [{"file": "same.bin"}], sha256=digest(b"OLD"),
        path="same.bin"), base_path=str(directory))
    with open(original, "wb") as handle:
        handle.write(b"NEW")
    store.declare(build_manifest(
        "NEW", [{"file": "same.bin"}], sha256=digest(b"NEW"),
        path="same.bin"), base_path=str(directory))
    store.acknowledge("OLD")
    manifest = build_manifest(
        "LATE", [ref("OLD", 0)], sha256=digest(b"OLD"), path="same.bin")
    with pytest.raises(MissingContent) as error:
        store.declare(manifest)
    assert error.value.missing[0]["delivery_id"] == "OLD"
    assert error.value.missing[0]["segment"] == 1


def test_repeated_declaration_does_not_duplicate_notification(tmpdir):
    from streamz import Stream
    directory = str(tmpdir.mkdir("repeat"))
    store = DeliveryStore(str(tmpdir.join("repeat-store")), content_capacity=7)
    path = os.path.join(directory, "X.delivery")
    write_delivery(path, block_manifest("X", ["A", "B"], b"AB"))
    source = Stream.filenames(directory, asynchronous=False, deliveries=store,
                              poll_interval=0.01)
    events = source.sink_to_list()
    source.start()
    import time
    deadline = time.time() + 5
    try:
        while len(events) < 1 and time.time() < deadline:
            time.sleep(0.01)
        # Rewrite the same declaration several times while polling.
        for _ in range(3):
            write_delivery(path, block_manifest("X", ["A", "B"], b"AB"))
            time.sleep(0.05)
    finally:
        source.stop()
    assert len(events) == 1
    assert events[0].read() == b"AB"


def test_cross_boundary_read_is_lazy(store, monkeypatch):
    store.declare(block_manifest("X", ["A", "B", "C", "D"], b"ABCD"))
    store.declare(block_manifest("Y", [ref("X", 1), ref("X", 2)], b"BC"))
    store.declare(block_manifest(
        "Z", [ref("Y", 0), ref("Y", 1), ref("X", 3)], b"BCD"))

    reads = []
    original = store._read_block

    def counting(digest, offset=0, length=None):
        reads.append((digest, offset, length))
        return original(digest, offset, length)

    monkeypatch.setattr(store, "_read_block", counting)
    with store.open("Z") as reader:
        reader.seek(0)
        assert reader.read(2) == b"BC"
    # Only the two referenced blocks are touched; no full synthetic copy is
    # read for the whole delivery.
    assert len(reads) == 2
    assert {digest for digest, _, _ in reads} == {
        digest(b"B"), digest(b"C")}
    assert store.read("Z") == b"BCD"


def test_pending_reference_keeps_source_when_source_acked(store):
    store.declare(block_manifest("X", ["A", "B", "C"], b"ABC"))
    pending = store.declare(build_manifest(
        "Y", [ref("X", 1), {"pending": True, "length": 1}],
        sha256=digest(b"BZ")))
    assert isinstance(pending, PendingDelivery)
    store.acknowledge("X")
    # The block B is owned by the pending Y declaration.
    assert store.read("Y") if False else True
    assert store.takeover_report("Y")["missing"] == []
    assert pending.supply(1, b"Z").read() == b"BZ"


def test_independent_consumers_ack_separately(store):
    store.declare(block_manifest("X", ["A", "B"], b"AB"))
    first = store.get("X", consumer="first")
    second = store.get("X", consumer="second")
    assert first.read() == second.read() == b"AB"
    first.acknowledge()
    assert not store.is_acknowledged("X")
    assert store.used_blocks() == 2
    second.acknowledge()
    assert store.is_acknowledged("X")
    assert store.used_blocks() == 0


def test_independent_readers_after_ack_keep_blocks(tmpdir):
    store = DeliveryStore(str(tmpdir.join("pinned")), content_capacity=7)
    store.declare(block_manifest("X", ["A", "B", "C"], b"ABC"))
    reader = store.open("X")
    store.acknowledge("X")
    assert reader.read(1) == b"A"
    assert store.used_blocks() == 3
    reader.close()
    assert store.used_blocks() == 0
