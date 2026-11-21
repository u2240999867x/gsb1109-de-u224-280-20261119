"""Byte-level comparison of two BSON archives.

The comparator walks the raw BSON bytes of two archives (each archive is a
sequence of concatenated BSON documents) and reports differences using each
side's own real array positions, value byte ranges, and BSON types.  It never
infers BSON types from Python numeric equality: element identity and equality
are decided from the BSON type byte plus the raw value bytes.

Default policy:

* Object key order is ignored (objects are matched by key).
* Array elements are compared positionally.

Auditor-declared policies:

* ``pair_arrays``: maps an array policy path (e.g. ``devices``) to a business
  key field (e.g. ``device_id``).  Elements of that array are paired by the
  typed identity ``(BSON type, raw value bytes)`` of the key field instead of
  by position.  Non-unique identities on either side produce a conflict
  listing every candidate position; nothing is paired arbitrarily.
* ``relaxed_paths``: exact policy paths where a same-value int32/int64 width
  change is ignored, e.g. ``devices[device_id=int32:7].meter.value``.  The
  predicate carries the caller-declared BSON type, so ``int32:7`` and
  ``int64:7`` are different identifiers.  A relaxation declared for one
  business element never leaks onto another element, onto a literal dotted
  key such as ``["meter.value"]``, or onto undeclared arrays.

Policy path rendering:

* Simple object keys are dotted: ``meter.value`` means nested key ``meter``
  then key ``value``.
* Keys that are not simple identifiers are quoted: ``["meter.value"]`` means
  one literal key containing a dot.
* Positional array elements use ``[i]``; paired elements use
  ``[key_field=type:value]`` with the element's own typed identity.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TYPE_NAMES = {
    0x01: "double",
    0x02: "string",
    0x03: "document",
    0x04: "array",
    0x05: "binary",
    0x06: "undefined",
    0x07: "objectid",
    0x08: "bool",
    0x09: "datetime",
    0x0A: "null",
    0x0B: "regex",
    0x0C: "dbpointer",
    0x0D: "code",
    0x0E: "symbol",
    0x0F: "code_w_scope",
    0x10: "int32",
    0x11: "timestamp",
    0x12: "int64",
    0x13: "decimal128",
    0x7F: "maxkey",
    0xFF: "minkey",
}

_DOCUMENT = 0x03
_ARRAY = 0x04
_CONTAINER_TYPES = (_DOCUMENT, _ARRAY)
_INT_TYPES = (0x10, 0x12)


class BsonParseError(ValueError):
    """Raised when an archive does not contain well-formed BSON."""


@dataclass
class Element:
    """One BSON element with its byte-level provenance."""

    key: str
    type_byte: int
    doc_index: int
    elem_start: int  # offset of the type byte
    value_start: int  # offset of the first value byte
    value_end: int  # one past the last value byte
    raw: bytes  # the value bytes
    scalar: Any  # decoded scalar value (None for containers)
    children: list[Element] = field(default_factory=list)

    @property
    def type_name(self) -> str:
        return _TYPE_NAMES.get(self.type_byte, f"unknown(0x{self.type_byte:02x})")

    @property
    def is_container(self) -> bool:
        return self.type_byte in _CONTAINER_TYPES


# ---------------------------------------------------------------------------
# BSON walking
# ---------------------------------------------------------------------------


def _cstring(buf: bytes, pos: int) -> tuple[str, int]:
    end = buf.find(b"\x00", pos)
    if end < 0:
        raise BsonParseError(f"unterminated cstring at offset {pos}")
    return buf[pos:end].decode("utf-8", "replace"), end + 1


def _parse_value(
    buf: bytes, pos: int, tbyte: int, doc_index: int
) -> tuple[int, Any, list[Element]]:
    """Parse one value at ``pos``; return (next_pos, scalar, children)."""
    try:
        if tbyte == 0x01:
            return pos + 8, struct.unpack_from("<d", buf, pos)[0], []
        if tbyte in (0x02, 0x0D, 0x0E):
            (n,) = struct.unpack_from("<i", buf, pos)
            text = buf[pos + 4 : pos + 4 + max(n - 1, 0)].decode("utf-8", "replace")
            return pos + 4 + n, text, []
        if tbyte in _CONTAINER_TYPES:
            children, end = _parse_document_elements(buf, pos, doc_index)
            return end, None, children
        if tbyte == 0x05:
            (n,) = struct.unpack_from("<i", buf, pos)
            subtype = buf[pos + 4]
            return pos + 5 + n, (subtype, bytes(buf[pos + 5 : pos + 5 + n])), []
        if tbyte == 0x06:
            return pos, "undefined", []
        if tbyte == 0x07:
            return pos + 12, buf[pos : pos + 12].hex(), []
        if tbyte == 0x08:
            return pos + 1, buf[pos] != 0, []
        if tbyte == 0x09:
            return pos + 8, struct.unpack_from("<q", buf, pos)[0], []
        if tbyte == 0x0A:
            return pos, None, []
        if tbyte == 0x0B:
            pattern, pos = _cstring(buf, pos)
            options, pos = _cstring(buf, pos)
            return pos, (pattern, options), []
        if tbyte == 0x0C:
            (n,) = struct.unpack_from("<i", buf, pos)
            return pos + 4 + n + 12, None, []
        if tbyte == 0x0F:
            (n,) = struct.unpack_from("<i", buf, pos)
            return pos + n, None, []
        if tbyte == 0x10:
            return pos + 4, struct.unpack_from("<i", buf, pos)[0], []
        if tbyte == 0x11:
            inc, ts = struct.unpack_from("<II", buf, pos)
            return pos + 8, (ts, inc), []
        if tbyte == 0x12:
            return pos + 8, struct.unpack_from("<q", buf, pos)[0], []
        if tbyte == 0x13:
            return pos + 16, bytes(buf[pos : pos + 16]), []
        if tbyte in (0x7F, 0xFF):
            return pos, _TYPE_NAMES[tbyte], []
    except (struct.error, IndexError) as exc:
        raise BsonParseError(f"truncated value of type 0x{tbyte:02x} at offset {pos}") from exc
    raise BsonParseError(f"unsupported BSON type 0x{tbyte:02x} at offset {pos}")


def _parse_document_elements(buf: bytes, start: int, doc_index: int) -> tuple[list[Element], int]:
    """Parse the document starting at ``start``; return (children, end)."""
    try:
        (length,) = struct.unpack_from("<i", buf, start)
    except struct.error as exc:
        raise BsonParseError(f"truncated document header at offset {start}") from exc
    if length < 5 or start + length > len(buf):
        raise BsonParseError(f"invalid document length {length} at offset {start}")
    if buf[start + length - 1] != 0:
        raise BsonParseError(f"document at offset {start} is not NUL terminated")
    pos = start + 4
    end_of_elements = start + length - 1
    children: list[Element] = []
    while pos < end_of_elements:
        elem_start = pos
        tbyte = buf[pos]
        pos += 1
        key, pos = _cstring(buf, pos)
        value_start = pos
        pos, scalar, grandchildren = _parse_value(buf, pos, tbyte, doc_index)
        if pos > end_of_elements + 1:
            raise BsonParseError(f"element {key!r} overruns document at offset {start}")
        children.append(
            Element(
                key=key,
                type_byte=tbyte,
                doc_index=doc_index,
                elem_start=elem_start,
                value_start=value_start,
                value_end=pos,
                raw=bytes(buf[value_start:pos]),
                scalar=scalar,
                children=grandchildren,
            )
        )
    return children, start + length


def parse_archive(data: bytes) -> list[Element]:
    """Parse an archive (concatenated BSON documents) into top-level documents."""
    docs: list[Element] = []
    pos = 0
    doc_index = 0
    while pos < len(data):
        children, end = _parse_document_elements(data, pos, doc_index)
        docs.append(
            Element(
                key="",
                type_byte=_DOCUMENT,
                doc_index=doc_index,
                elem_start=pos,
                value_start=pos,
                value_end=end,
                raw=bytes(data[pos:end]),
                scalar=None,
                children=children,
            )
        )
        pos = end
        doc_index += 1
    return docs


# ---------------------------------------------------------------------------
# Policy path rendering
# ---------------------------------------------------------------------------

_SIMPLE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _join_field(path: str, key: str) -> str:
    if _SIMPLE_KEY.match(key):
        return f"{path}.{key}" if path else key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'{path}["{escaped}"]'


def _join_index(path: str, index: int) -> str:
    return f"{path}[{index}]"


def _join_pair(path: str, key_field: str, identity: str) -> str:
    return f"{path}[{key_field}={identity}]"


def _display(elem: Element) -> str:
    """Human-readable rendering of an element's value."""
    tbyte = elem.type_byte
    if tbyte == _DOCUMENT:
        return f"<document: {len(elem.children)} keys>"
    if tbyte == _ARRAY:
        return f"<array: {len(elem.children)} elements>"
    if tbyte == 0x08:
        return "true" if elem.scalar else "false"
    if tbyte == 0x02:
        return json.dumps(elem.scalar, ensure_ascii=False)
    if tbyte == 0x0A:
        return "null"
    if tbyte == 0x01:
        return repr(elem.scalar)
    if tbyte in (0x09, 0x10, 0x12):
        return str(elem.scalar)
    if tbyte == 0x07:
        return f"ObjectId({elem.scalar})"
    if tbyte == 0x05:
        return f"<binary subtype {elem.scalar[0]}: {len(elem.scalar[1])} bytes>"
    if tbyte == 0x0B:
        return f"/{elem.scalar[0]}/{elem.scalar[1]}"
    if tbyte == 0x11:
        return f"Timestamp({elem.scalar[0]}, {elem.scalar[1]})"
    if tbyte == 0x13:
        return f"<decimal128 {elem.raw.hex()}>"
    return str(elem.scalar)


def _identity(elem: Element) -> tuple[int, bytes]:
    """Typed pairing identity: BSON type byte plus raw value bytes."""
    return (elem.type_byte, elem.raw)


def _identity_str(elem: Element) -> str:
    """Canonical typed identity string, e.g. ``int32:7`` or ``string:abc``."""
    if elem.type_byte == 0x02:
        return f"string:{elem.scalar}"
    if elem.type_byte == 0x08:
        return f"bool:{'true' if elem.scalar else 'false'}"
    if elem.type_byte in (0x01, 0x09, 0x10, 0x12):
        return f"{elem.type_name}:{elem.scalar}"
    return f"{elem.type_name}:{elem.raw.hex()}"


# ---------------------------------------------------------------------------
# Comparison records
# ---------------------------------------------------------------------------


@dataclass
class Location:
    """Where a differing element lives in one archive."""

    doc_index: int
    real_path: str
    bson_type: str
    value_start: int
    value_end: int
    display: str


@dataclass
class Diff:
    kind: str  # "value" | "type" | "added" | "removed"
    path: str  # policy path (order-independent)
    left: Location | None
    right: Location | None
    detail: str = ""


@dataclass
class Conflict:
    """A business pairing that is not unique; nothing was paired."""

    path: str  # policy path of the array
    identity: str
    left_positions: list[int]
    right_positions: list[int]
    detail: str = ""


@dataclass
class Suppressed:
    """A difference ignored because an auditor-declared relaxation applied."""

    path: str
    left: Location
    right: Location
    reason: str


@dataclass
class Report:
    diffs: list[Diff]
    conflicts: list[Conflict]
    suppressed: list[Suppressed]


def _location(elem: Element, real_path: str) -> Location:
    return Location(
        doc_index=elem.doc_index,
        real_path=real_path,
        bson_type=elem.type_name,
        value_start=elem.value_start,
        value_end=elem.value_end,
        display=_display(elem),
    )


def _group_by_key(children: list[Element]) -> dict[str, list[Element]]:
    groups: dict[str, list[Element]] = {}
    for child in children:
        groups.setdefault(child.key, []).append(child)
    return groups


def _array_children(elem: Element) -> list[Element]:
    def order(child: Element) -> tuple[int, Any]:
        try:
            return (0, int(child.key))
        except ValueError:
            return (1, child.key)

    return sorted(elem.children, key=order)


class Comparator:
    """Compare two parsed archives under auditor-declared policies."""

    def __init__(
        self,
        pair_arrays: dict[str, str] | None = None,
        relaxed_paths: set[str] | None = None,
    ) -> None:
        self.pair_arrays = dict(pair_arrays or {})
        self.relaxed_paths = set(relaxed_paths or ())
        self.diffs: list[Diff] = []
        self.conflicts: list[Conflict] = []
        self.suppressed: list[Suppressed] = []

    def compare(self, left_docs: list[Element], right_docs: list[Element]) -> None:
        count = max(len(left_docs), len(right_docs))
        for i in range(count):
            if i >= len(left_docs):
                self.diffs.append(
                    Diff("added", "<archive>", None, _location(right_docs[i], f"<doc {i}>"))
                )
            elif i >= len(right_docs):
                self.diffs.append(
                    Diff("removed", "<archive>", _location(left_docs[i], f"<doc {i}>"), None)
                )
            else:
                self._compare_document_children(
                    left_docs[i].children, right_docs[i].children, "", "", ""
                )

    def finalize(self) -> Report:
        """Sort records by policy path so key order never affects ordering."""
        diffs = sorted(
            self.diffs,
            key=lambda d: (
                d.path,
                d.kind,
                d.left.real_path if d.left else "",
                d.right.real_path if d.right else "",
            ),
        )
        conflicts = sorted(self.conflicts, key=lambda c: (c.path, c.identity))
        suppressed = sorted(self.suppressed, key=lambda s: s.path)
        return Report(diffs, conflicts, suppressed)

    # -- recursion ---------------------------------------------------------

    def _compare_element(
        self, left: Element, right: Element, path: str, left_real: str, right_real: str
    ) -> None:
        if left.type_byte != right.type_byte:
            if self._try_relax(left, right, path, left_real, right_real):
                return
            self.diffs.append(
                Diff(
                    "type",
                    path,
                    _location(left, left_real),
                    _location(right, right_real),
                    detail=f"BSON type differs: {left.type_name} vs {right.type_name}",
                )
            )
            return
        if left.type_byte == _DOCUMENT:
            self._compare_document_children(
                left.children, right.children, path, left_real, right_real
            )
        elif left.type_byte == _ARRAY:
            self._compare_array(left, right, path, left_real, right_real)
        elif left.raw != right.raw:
            self.diffs.append(
                Diff(
                    "value",
                    path,
                    _location(left, left_real),
                    _location(right, right_real),
                    detail="same BSON type, different value bytes",
                )
            )

    def _try_relax(
        self, left: Element, right: Element, path: str, left_real: str, right_real: str
    ) -> bool:
        if (
            left.type_byte in _INT_TYPES
            and right.type_byte in _INT_TYPES
            and left.scalar == right.scalar
            and path in self.relaxed_paths
        ):
            self.suppressed.append(
                Suppressed(
                    path,
                    _location(left, left_real),
                    _location(right, right_real),
                    reason=(
                        f"int32/int64 width relaxation declared for {path!r} "
                        f"({left.type_name} {left.scalar} == {right.type_name} {right.scalar})"
                    ),
                )
            )
            return True
        return False

    def _compare_document_children(
        self,
        left_children: list[Element],
        right_children: list[Element],
        path: str,
        left_real: str,
        right_real: str,
    ) -> None:
        left_groups = _group_by_key(left_children)
        right_groups = _group_by_key(right_children)
        for key in sorted(set(left_groups) | set(right_groups)):
            lc = left_groups.get(key, [])
            rc = right_groups.get(key, [])
            child_path = _join_field(path, key)
            child_left_real = _join_field(left_real, key)
            child_right_real = _join_field(right_real, key)
            for k in range(max(len(lc), len(rc))):
                if k >= len(lc):
                    self.diffs.append(
                        Diff("added", child_path, None, _location(rc[k], child_right_real))
                    )
                elif k >= len(rc):
                    self.diffs.append(
                        Diff("removed", child_path, _location(lc[k], child_left_real), None)
                    )
                else:
                    self._compare_element(
                        lc[k], rc[k], child_path, child_left_real, child_right_real
                    )

    def _compare_array(
        self, left: Element, right: Element, path: str, left_real: str, right_real: str
    ) -> None:
        key_field = self.pair_arrays.get(path)
        if key_field is not None:
            self._compare_paired_array(left, right, path, left_real, right_real, key_field)
            return
        lc = _array_children(left)
        rc = _array_children(right)
        for i in range(max(len(lc), len(rc))):
            child_path = _join_index(path, i)
            child_left_real = _join_index(left_real, i)
            child_right_real = _join_index(right_real, i)
            if i >= len(lc):
                self.diffs.append(
                    Diff("added", child_path, None, _location(rc[i], child_right_real))
                )
            elif i >= len(rc):
                self.diffs.append(
                    Diff("removed", child_path, _location(lc[i], child_left_real), None)
                )
            else:
                self._compare_element(lc[i], rc[i], child_path, child_left_real, child_right_real)

    def _compare_paired_array(
        self,
        left: Element,
        right: Element,
        path: str,
        left_real: str,
        right_real: str,
        key_field: str,
    ) -> None:
        left_map, left_unpairable = self._collect_identities(left, key_field)
        right_map, right_unpairable = self._collect_identities(right, key_field)
        for side, indices in (("left", left_unpairable), ("right", right_unpairable)):
            if indices:
                self.conflicts.append(
                    Conflict(
                        path,
                        f"<missing or non-scalar key {key_field!r}>",
                        indices if side == "left" else [],
                        indices if side == "right" else [],
                        detail=f"{side} array has elements that cannot be paired",
                    )
                )
        for identity in sorted(set(left_map) | set(right_map)):
            lc = left_map.get(identity, [])
            rc = right_map.get(identity, [])
            sample = lc[0][2] if lc else rc[0][2]
            id_str = _identity_str(sample)
            if len(lc) > 1 or len(rc) > 1:
                self.conflicts.append(
                    Conflict(
                        path,
                        id_str,
                        [index for index, _, _ in lc],
                        [index for index, _, _ in rc],
                        detail="pairing identity is not unique; no element was paired",
                    )
                )
                continue
            pair_path = _join_pair(path, key_field, id_str)
            if not lc:
                index, elem, _ = rc[0]
                self.diffs.append(
                    Diff("added", pair_path, None, _location(elem, _join_index(right_real, index)))
                )
            elif not rc:
                index, elem, _ = lc[0]
                self.diffs.append(
                    Diff("removed", pair_path, _location(elem, _join_index(left_real, index)), None)
                )
            else:
                (left_index, left_elem, _), (right_index, right_elem, _) = lc[0], rc[0]
                self._compare_element(
                    left_elem,
                    right_elem,
                    pair_path,
                    _join_index(left_real, left_index),
                    _join_index(right_real, right_index),
                )

    @staticmethod
    def _collect_identities(
        array_elem: Element, key_field: str
    ) -> tuple[dict[tuple[int, bytes], list[tuple[int, Element, Element]]], list[int]]:
        """Group array elements by typed identity of their business key."""
        result: dict[tuple[int, bytes], list[tuple[int, Element, Element]]] = {}
        unpairable: list[int] = []
        for index, child in enumerate(_array_children(array_elem)):
            key_elem: Element | None = None
            if child.type_byte == _DOCUMENT:
                for sub in child.children:
                    if sub.key == key_field:
                        key_elem = sub
                        break
            if key_elem is None or key_elem.is_container:
                unpairable.append(index)
                continue
            result.setdefault(_identity(key_elem), []).append((index, child, key_elem))
        return result, unpairable


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _format_location(loc: Location) -> str:
    return (
        f"doc {loc.doc_index}, real path {loc.real_path}, type {loc.bson_type}, "
        f"value bytes [{loc.value_start}, {loc.value_end}), value {loc.display}"
    )


def render_text(report: Report) -> str:
    """Render a report as human-readable text."""
    lines: list[str] = []
    lines.append(f"Differences: {len(report.diffs)}")
    for diff in report.diffs:
        lines.append(f"  [{diff.kind}] {diff.path}")
        if diff.left is not None:
            lines.append(f"    left : {_format_location(diff.left)}")
        if diff.right is not None:
            lines.append(f"    right: {_format_location(diff.right)}")
        if diff.detail:
            lines.append(f"    note : {diff.detail}")
    lines.append(f"Pairing conflicts: {len(report.conflicts)}")
    for conflict in report.conflicts:
        lines.append(f"  {conflict.path} identity {conflict.identity}")
        lines.append(f"    left candidate positions : {conflict.left_positions}")
        lines.append(f"    right candidate positions: {conflict.right_positions}")
        if conflict.detail:
            lines.append(f"    note : {conflict.detail}")
    lines.append(f"Suppressed by policy: {len(report.suppressed)}")
    for sup in report.suppressed:
        lines.append(f"  {sup.path}")
        lines.append(f"    left : {_format_location(sup.left)}")
        lines.append(f"    right: {_format_location(sup.right)}")
        lines.append(f"    note : {sup.reason}")
    return "\n".join(lines)


def render_json(report: Report) -> str:
    """Render a report as JSON."""

    def loc(loc_: Location | None) -> dict[str, Any] | None:
        return None if loc_ is None else loc_.__dict__

    payload = {
        "differences": [
            {
                "kind": d.kind,
                "path": d.path,
                "left": loc(d.left),
                "right": loc(d.right),
                "detail": d.detail,
            }
            for d in report.diffs
        ],
        "conflicts": [c.__dict__ for c in report.conflicts],
        "suppressed": [
            {"path": s.path, "left": loc(s.left), "right": loc(s.right), "reason": s.reason}
            for s in report.suppressed
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def compare_files(
    old_path: str,
    new_path: str,
    pair_arrays: dict[str, str] | None = None,
    relaxed_paths: set[str] | None = None,
) -> Report:
    """Compare two archive files and return the finalized report."""
    left_docs = parse_archive(Path(old_path).read_bytes())
    right_docs = parse_archive(Path(new_path).read_bytes())
    comparator = Comparator(pair_arrays, relaxed_paths)
    comparator.compare(left_docs, right_docs)
    return comparator.finalize()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two BSON archives at the byte/type level.",
        epilog=(
            "Policy paths: object keys are dotted (meter.value), non-simple keys are "
            'quoted (["meter.value"]), positional array elements use [0], paired '
            "elements use [key_field=type:value] (devices[device_id=int32:7])."
        ),
    )
    parser.add_argument("old", help="old archive file (concatenated BSON documents)")
    parser.add_argument("new", help="new archive file")
    parser.add_argument(
        "--pair-array",
        action="append",
        default=[],
        metavar="PATH=KEY_FIELD",
        help="pair elements of the array at PATH by business key field, e.g. "
        "devices=device_id; undeclared arrays stay positional",
    )
    parser.add_argument(
        "--relax-width",
        action="append",
        default=[],
        metavar="POLICY_PATH",
        help="ignore a same-value int32/int64 width change at this exact policy "
        "path, e.g. 'devices[device_id=int32:7].meter.value'",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    pair_arrays: dict[str, str] = {}
    for spec in args.pair_array:
        path, sep, key_field = spec.rpartition("=")
        if not sep or not path or not key_field:
            parser.error(f"--pair-array expects PATH=KEY_FIELD, got {spec!r}")
        pair_arrays[path] = key_field

    try:
        report = compare_files(args.old, args.new, pair_arrays, set(args.relax_width))
    except (BsonParseError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(render_json(report) if args.json else render_text(report))
    return 1 if report.diffs or report.conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
