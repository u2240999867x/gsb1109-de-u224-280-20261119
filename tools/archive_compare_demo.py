"""Build the audit fixture archives and run every comparison scenario.

Scenarios (see the module docstring of tools/archive_compare.py for the
policy model):

A. default strict comparison (key order ignored, arrays positional)
B. business pairing of ``devices`` by ``device_id``, no width relaxation
C. combined policy: pairing + width relaxation only for
   ``devices[device_id=int32:7].meter.value``
D. archive pair with duplicate device ids in both integer widths
E. key-order stability: reordering object keys changes neither the diff
   set nor its stable ordering
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from archive_compare import Comparator, parse_archive, render_text

import bson
from bson.int64 import Int64

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

OLD_DOC = {
    "name": "site-a",
    "devices": [
        # device 7: nested meter.value AND a literal dotted key "meter.value"
        {"device_id": 7, "meter": {"value": 105}, "meter.value": 9, "switch": True},
        {"device_id": 8, "meter": {"value": 200}, "switch": True},
    ],
    "readings": [1, 2, 3],
}

NEW_DOC = {
    # top-level keys reordered; devices swapped; readings swapped
    "readings": [3, 2, 1],
    "devices": [
        # device 8 now first: nested width change + switch true -> int32 1
        {"device_id": 8, "switch": 1, "meter": {"value": Int64(200)}},
        # device 7: both nested meter.value and literal "meter.value" widened
        {
            "device_id": 7,
            "meter.value": Int64(9),
            "meter": {"value": Int64(105)},
            "switch": True,
        },
    ],
    "name": "site-a",
}

# Duplicate device ids: int32 7 appears twice per side; int64 7 is a
# different typed identity and pairs cleanly.
DUP_OLD_DOC = {
    "devices": [
        {"device_id": 7, "tag": "a"},
        {"device_id": Int64(7), "meter": {"value": 50}},
        {"device_id": 7, "tag": "c"},
    ]
}
DUP_NEW_DOC = {
    "devices": [
        {"device_id": 7, "tag": "a2"},
        {"device_id": 7, "tag": "c2"},
        {"device_id": Int64(7), "meter": {"value": Int64(50)}},
    ]
}


def reorder_keys(obj):
    """Recursively reverse object key order; arrays keep their order."""
    if isinstance(obj, dict):
        return {key: reorder_keys(obj[key]) for key in reversed(list(obj))}
    if isinstance(obj, list):
        return [reorder_keys(item) for item in obj]
    return obj


def write_archive(directory, name, doc):
    path = Path(directory) / name
    path.write_bytes(bson.encode(doc))
    return str(path)


def run(old_path, new_path, pair_arrays=None, relaxed_paths=None):
    comparator = Comparator(pair_arrays, relaxed_paths)
    with open(old_path, "rb") as fh:
        left_docs = parse_archive(fh.read())
    with open(new_path, "rb") as fh:
        right_docs = parse_archive(fh.read())
    comparator.compare(left_docs, right_docs)
    return comparator.finalize()


def signature(report):
    """Order-independent-of-key-order signature: (kind, path) sequence."""
    return [(d.kind, d.path) for d in report.diffs]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="bson-archive-compare-")
    old_path = write_archive(tmp, "old.bson", OLD_DOC)
    new_path = write_archive(tmp, "new.bson", NEW_DOC)
    dup_old_path = write_archive(tmp, "dup_old.bson", DUP_OLD_DOC)
    dup_new_path = write_archive(tmp, "dup_new.bson", DUP_NEW_DOC)
    reordered_new_path = write_archive(tmp, "new_reordered.bson", reorder_keys(NEW_DOC))
    reordered_old_path = write_archive(tmp, "old_reordered.bson", reorder_keys(OLD_DOC))

    pair = {"devices": "device_id"}
    relax = {"devices[device_id=int32:7].meter.value"}

    print("=" * 78)
    print("Scenario A: default strict comparison (no auditor policy)")
    print("=" * 78)
    report_a = run(old_path, new_path)
    print(render_text(report_a))
    assert {p for _, p in signature(report_a)} == {
        "devices[0].device_id",
        "devices[0].meter.value",
        "devices[0].switch",
        'devices[0]["meter.value"]',
        "devices[1].device_id",
        "devices[1].meter.value",
        'devices[1]["meter.value"]',
        "readings[0]",
        "readings[2]",
    }, signature(report_a)
    assert not report_a.conflicts
    assert not report_a.suppressed

    print()
    print("=" * 78)
    print("Scenario B: pair devices by device_id, no width relaxation")
    print("=" * 78)
    report_b = run(old_path, new_path, pair_arrays=pair)
    print(render_text(report_b))
    assert {p for _, p in signature(report_b)} == {
        "devices[device_id=int32:7].meter.value",
        'devices[device_id=int32:7]["meter.value"]',
        "devices[device_id=int32:8].meter.value",
        "devices[device_id=int32:8].switch",
        "readings[0]",
        "readings[2]",
    }, signature(report_b)
    # Each side reports its own real array position; no cross-mapping.
    dev7 = next(d for d in report_b.diffs if d.path == "devices[device_id=int32:7].meter.value")
    assert dev7.left.real_path == "devices[0].meter.value", dev7.left
    assert dev7.right.real_path == "devices[1].meter.value", dev7.right
    assert dev7.left.bson_type == "int32"
    assert dev7.right.bson_type == "int64"
    assert dev7.left.value_end - dev7.left.value_start == 4
    assert dev7.right.value_end - dev7.right.value_start == 8
    dev8 = next(d for d in report_b.diffs if d.path == "devices[device_id=int32:8].switch")
    assert dev8.left.real_path == "devices[1].switch"
    assert dev8.right.real_path == "devices[0].switch"
    assert dev8.left.bson_type == "bool"
    assert dev8.right.bson_type == "int32"

    print()
    print("=" * 78)
    print("Scenario C: pairing + relax width only for device int32:7 nested meter.value")
    print("=" * 78)
    report_c = run(old_path, new_path, pair_arrays=pair, relaxed_paths=relax)
    print(render_text(report_c))
    assert {p for _, p in signature(report_c)} == {
        'devices[device_id=int32:7]["meter.value"]',  # literal dotted key: reported
        "devices[device_id=int32:8].meter.value",  # other element: not relaxed
        "devices[device_id=int32:8].switch",  # bool -> int32 1: reported
        "readings[0]",  # undeclared array: positional diffs reported
        "readings[2]",
    }, signature(report_c)
    assert [s.path for s in report_c.suppressed] == ["devices[device_id=int32:7].meter.value"]

    print()
    print("=" * 78)
    print("Scenario D: duplicate device ids; typed caller identifier int64:7")
    print("=" * 78)
    report_d = run(
        dup_old_path,
        dup_new_path,
        pair_arrays=pair,
        relaxed_paths={"devices[device_id=int64:7].meter.value"},
    )
    print(render_text(report_d))
    assert len(report_d.conflicts) == 1
    conflict = report_d.conflicts[0]
    assert conflict.path == "devices"
    assert conflict.identity == "int32:7"
    assert conflict.left_positions == [0, 2]
    assert conflict.right_positions == [0, 1]
    # int64:7 is a distinct typed identity: pairs old[1] with new[2], and the
    # caller's int64:7-scoped relaxation suppresses its width change only.
    assert not report_d.diffs
    assert [s.path for s in report_d.suppressed] == ["devices[device_id=int64:7].meter.value"]
    sup = report_d.suppressed[0]
    assert sup.left.real_path == "devices[1].meter.value"
    assert sup.right.real_path == "devices[2].meter.value"

    print()
    print("=" * 78)
    print("Scenario E: key-order stability")
    print("=" * 78)
    report_c2 = run(old_path, reordered_new_path, pair_arrays=pair, relaxed_paths=relax)
    assert signature(report_c2) == signature(report_c), (
        signature(report_c2),
        signature(report_c),
    )
    report_self = run(old_path, reordered_old_path, pair_arrays=pair, relaxed_paths=relax)
    assert not report_self.diffs
    assert not report_self.conflicts
    print(
        "reordered archive yields identical (kind, path) sequence; "
        "key-reorder-only pair yields zero differences"
    )

    print()
    print(f"All scenario assertions passed. Fixtures kept in {tmp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
