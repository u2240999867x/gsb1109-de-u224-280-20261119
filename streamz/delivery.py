"""Sealed, content-addressed deliveries for :class:`streamz.sources.filenames`.

A delivery is an immutable description of a file.  A producer may seal a file
once, then have later deliveries refer to whole segments of an earlier sealed
delivery instead of transmitting the same bytes again.  Declared segments are
read in declaration order and may be repeated or listed out of order.

Content is stored by SHA-256, never by the original path.  Once a declaration
has been accepted its source file may be truncated, replaced or deleted.  A
reference always resolves through the sealed manifest it names; a newer file
with the same name is never silently substituted for an older delivery.

The main objects are:

``DeliveryStore``
    Durable block store with capacity accounting, acknowledgements,
    reference takeover and reclamation.

``DeliveryNotification``
    A ready delivery.  It is mapping-like (``n["sha256"]``) and exposes
    ``open()``, ``read()`` and ``acknowledge()``.

``DeliveryReader``
    Seekable, read-only random access over a delivery.  Reads are resolved
    lazily block by block; no complete synthetic copy is created.

``PendingDelivery``
    A declaration that has taken over its references but still has one or
    more fragments to be supplied.  It is not a success notification.

The module deliberately does not depend on the Streamz event loop so the store
can be used and tested directly.
"""

from __future__ import annotations

import base64
import binascii
import bisect
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Mapping

__all__ = [
    "CapacityError",
    "Declaration",
    "DeliveryConflict",
    "DeliveryError",
    "DeliveryNotification",
    "DeliveryReader",
    "DeliveryStore",
    "DigestMismatch",
    "ManifestError",
    "IncompleteDeclaration",
    "MissingContent",
    "PendingDelivery",
    "SegmentSpec",
    "build_manifest",
    "literal",
    "ref",
]


class DeliveryError(Exception):
    """Base class for delivery errors."""


class ManifestError(DeliveryError, ValueError):
    """The declaration is not a complete or valid delivery manifest."""


class DeliveryConflict(ManifestError):
    """A delivery identifier was reused for different content."""


class IncompleteDeclaration(ManifestError):
    """The declaration file is still being written (incomplete JSON)."""


class MissingContent(ManifestError):
    """Referenced sealed content is not available.

    ``missing`` is a list of mappings.  ``delivery_id`` names the delivery,
    ``segment`` is one-based (matching the task's prose) and ``index`` is the
    zero-based segment position.  ``reason`` explains whether the delivery is
    unknown, not sealed, or its content was reclaimed.
    """

    def __init__(self, missing, message=None):
        self.missing = [dict(item) for item in missing]
        if message is None:
            parts = []
            for item in self.missing:
                label = item.get("delivery_id") or item.get("block") or "?"
                position = item.get("segment")
                if position is None:
                    position = item.get("index")
                if position is None:
                    parts.append(str(label))
                else:
                    parts.append("%s[%s]" % (label, position))
            message = "missing sealed content: " + ", ".join(parts)
        super().__init__(message)

    def to_dict(self):
        return {"missing": [dict(item) for item in self.missing]}


class CapacityError(DeliveryError):
    """Not enough content capacity for a declaration.

    ``deficit_blocks`` / ``deficit_bytes`` say how much additional content
    capacity is required.  Manifest, digest and receipt metadata is not part
    of this accounting.
    """

    def __init__(self, required_blocks, available_blocks, required_bytes=0,
                 available_bytes=0, delivery_id=None):
        self.required_blocks = int(required_blocks)
        self.available_blocks = max(0, int(available_blocks))
        self.deficit_blocks = max(0, self.required_blocks - self.available_blocks)
        self.required_bytes = int(required_bytes)
        self.available_bytes = max(0, int(available_bytes))
        self.deficit_bytes = max(0, self.required_bytes - self.available_bytes)
        self.delivery_id = delivery_id
        super().__init__(
            "insufficient content capacity for delivery %s: need %d more "
            "block(s), %d more byte(s)" % (
                delivery_id, self.deficit_blocks, self.deficit_bytes)
        )


class DigestMismatch(ManifestError):
    """The assembled content does not match the declared final SHA-256."""

    def __init__(self, delivery_id, expected, actual):
        self.delivery_id = delivery_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            "content digest mismatch for delivery %s: expected %s, got %s"
            % (delivery_id, expected, actual)
        )


_HEX_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _normalize_digest(value):
    if not isinstance(value, str):
        raise ManifestError("manifest must declare a SHA-256 digest")
    digest = value.strip().lower()
    for prefix in ("sha256:", "sha-256:"):
        if digest.startswith(prefix):
            digest = digest[len(prefix):]
    if not _HEX_DIGEST.match(digest):
        raise ManifestError("invalid SHA-256 digest: %r" % (value,))
    return digest


def _as_bytes(value, encoding="utf-8"):
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if not isinstance(value, str):
        raise ManifestError("literal segment must be text or bytes")
    return value.encode(encoding or "utf-8")


# ---------------------------------------------------------------------------
# Segment declarations


@dataclass(frozen=True)
class SegmentSpec:
    """One segment in a declaration.

    ``kind`` is ``"literal"`` or ``"reference"``.  A literal may carry inline
    bytes, name a byte range of a file, or be a pending fragment.  A reference
    names a sealed delivery and a zero-based segment index.
    """

    kind: str
    data: bytes | None = None
    source: str | None = None
    index: int | None = None
    file: str | None = None
    offset: int = 0
    length: int | None = None
    pending: bool = False
    unit: str = "segment"

    @property
    def size(self):
        if self.data is not None:
            return len(self.data)
        return self.length


class Declaration(dict):
    """A manifest mapping accepted by :meth:`DeliveryStore.declare`."""


def literal(data, **kwargs):
    """Build a literal segment from text or bytes."""
    if isinstance(data, str):
        data = data.encode(kwargs.pop("encoding", "utf-8"))
    return SegmentSpec("literal", data=bytes(data), **kwargs)


def ref(delivery_id, index, **kwargs):
    """Build a reference to a zero-based segment of a sealed delivery."""
    return SegmentSpec("reference", source=str(delivery_id), index=int(index),
                       **kwargs)


def build_manifest(delivery_id, segments, sha256=None, path=None):
    """Build a canonical manifest mapping.

    ``segments`` may contain :class:`SegmentSpec` values, strings (literal
    UTF-8 text) or segment mappings.  When ``sha256`` is omitted every segment
    must already contain literal bytes; the final digest is then calculated.
    """
    specs = []
    chunks = []
    complete = True
    for item in segments:
        if isinstance(item, SegmentSpec):
            spec = item
        elif isinstance(item, str):
            spec = literal(item)
        elif isinstance(item, (bytes, bytearray)):
            spec = literal(bytes(item))
        elif isinstance(item, Mapping):
            spec = _segment_from_mapping(item, "utf-8")
        else:
            raise ManifestError("unsupported segment: %r" % (item,))
        if isinstance(spec, list):
            for expanded in spec:
                specs.append(_segment_to_mapping(expanded))
                if expanded.kind == "literal" and expanded.data is not None:
                    chunks.append(expanded.data)
                else:
                    complete = False
            continue
        specs.append(_segment_to_mapping(spec))
        if spec.kind == "literal" and spec.data is not None:
            chunks.append(spec.data)
        else:
            complete = False
    if sha256 is None:
        if not complete:
            raise ManifestError("sha256 is required when segments are not all literal")
        sha256 = _sha256(b"".join(chunks))
    manifest = {
        "delivery_id": delivery_id,
        "sha256": sha256,
        "segments": specs,
    }
    if path is not None:
        manifest["path"] = os.fspath(path)
    return manifest


# ---------------------------------------------------------------------------
# Manifest parsing


def _pick(mapping, *names, default=None):
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return default


def _segment_to_mapping(spec):
    result = {"kind": spec.kind}
    if spec.kind == "reference":
        result["source"] = spec.source
        result["index"] = spec.index
        if spec.unit == "block":
            result["unit"] = "block"
        if spec.offset:
            result["offset"] = spec.offset
        if spec.length is not None:
            result["length"] = spec.length
        return result
    if spec.data is not None:
        result["data"] = base64.b64encode(spec.data).decode("ascii")
        result["encoding"] = "base64"
        return result
    result["pending"] = bool(spec.pending)
    if spec.file is not None:
        result["file"] = spec.file
        if spec.offset:
            result["offset"] = spec.offset
    if spec.length is not None:
        result["length"] = spec.length
    return result


def _reference_indices(item):
    """Return ``(zero-based indices, unit)`` for a reference mapping.

    ``index``/``segment``/``position`` address declaration segments.
    ``block``/``blocks``/``first``/``last``/``range`` address content blocks.
    Both units are zero-based internally; the user-facing ``block`` and
    ``first``/``last`` forms are one-based.  Ranges may run in reverse.
    """
    zero_based = bool(_pick(item, "zero_based", default=False))
    unit = str(_pick(item, "unit", "level", default="")).lower()
    if unit in ("block", "blocks", "chunk", "chunks"):
        unit = "block"
    elif unit in ("segment", "segments"):
        unit = "segment"

    explicit = _pick(item, "index", "segment", "segment_index", "position",
                     "part", "part_index")
    if explicit is not None:
        return [int(explicit)], (unit or "segment")

    block = _pick(item, "block", "chunk")
    if block is not None:
        value = int(block)
        return [value if zero_based else value - 1], "block"

    blocks = _pick(item, "blocks", "segment_indices", "parts", "chunks",
                   "segments")
    if blocks is not None:
        if isinstance(blocks, (int, str)):
            blocks = [blocks]
        values = [int(value) for value in blocks]
        if not zero_based:
            values = [value - 1 for value in values]
        return values, (unit or "block")

    first = _pick(item, "first")
    last = _pick(item, "last")
    start = _pick(item, "start", "from_segment", "from_block")
    stop = _pick(item, "stop", "end", "to_segment", "to_block")
    space = _pick(item, "range", "segment_range", "block_range")
    if space is not None:
        if isinstance(space, str):
            bounds = re.split(r"\s*[-:,\.]{1,2}\s*", space.strip())
        else:
            bounds = list(space)
        if len(bounds) != 2:
            raise ManifestError("invalid reference range: %r" % (space,))
        first, last = bounds
    if first is not None:
        start = first
        one_based = True
    elif start is not None:
        one_based = not zero_based
    else:
        raise ManifestError(
            "reference segment needs index, segment, block, blocks, "
            "first/last, range or start/stop")

    if unit == "":
        unit = "block" if (first is not None or last is not None
                           or _pick(item, "block_range") is not None
                           or _pick(item, "first") is not None
                           or _pick(item, "last") is not None) else "segment"

    start = int(start)
    if one_based:
        start -= 1

    if last is not None:
        stop = last
        stop_one_based = True
    elif stop is not None:
        stop_one_based = one_based
    else:
        return [start], unit

    stop = int(stop)
    if stop_one_based:
        stop -= 1

    inclusive = bool(_pick(item, "inclusive", default=True))
    step = -1 if stop < start else 1
    end = stop + step if inclusive else stop
    return list(range(start, end, step)), unit


def _segment_from_mapping(item, default_encoding="utf-8"):
    if isinstance(item, str):
        match = re.match(r"^@([^:]+):(\d+)(?:-(\d+))?$", item.strip())
        if match:
            start = int(match.group(2))
            stop = int(match.group(3) or start)
            step = -1 if stop < start else 1
            return [
                SegmentSpec("reference", source=match.group(1),
                            index=index - 1, unit="block")
                for index in range(start, stop + step, step)
            ]
        return literal(item)
    if not isinstance(item, Mapping):
        raise ManifestError("segment must be a mapping or string: %r" % (item,))

    kind = _pick(item, "kind", "type")
    source = _pick(item, "source", "ref", "reference", "from",
                   "source_delivery", "delivery_id", "deliveryId",
                   "consignment", "consignment_id", "origin")
    # Accept a compact reference form: {"ref": "X", "block": 2} or
    # {"ref": "X", "from_block": 2, "to_block": 3}.  A bare string reference
    # may also be written as "@X:2" or "@X:2-3" by callers building manifests.
    from_block = _pick(item, "from_block", "block_from")
    to_block = _pick(item, "to_block", "block_to", "until_block")
    if from_block is not None or to_block is not None:
        source = source if source is not None else _pick(item, "delivery")
        if source is None:
            raise ManifestError("reference segment has no source delivery")
        start = int(from_block if from_block is not None else to_block) - 1
        stop = int(to_block if to_block is not None else from_block) - 1
        step = -1 if stop < start else 1
        return [
            SegmentSpec("reference", source=str(source), index=index,
                        unit="block")
            for index in range(start, stop + step, step)
        ]
    if kind in ("reference", "ref") or source is not None:
        if source is None:
            raise ManifestError("reference segment has no source delivery")
        if isinstance(source, Mapping):
            raise ManifestError(
                "a reference must name a delivery identifier, not a nested "
                "manifest")
        if isinstance(source, (list, tuple)):
            if len(source) == 2:
                item = dict(item)
                item.setdefault("block", source[1])
            elif len(source) == 3:
                item = dict(item)
                item.setdefault("first", source[1])
                item.setdefault("last", source[2])
            else:
                raise ManifestError("invalid list reference: %r" % (source,))
            source = source[0]
        if isinstance(source, str) and ":" in source:
            # Compact "X:2-3" or "X:2" source.
            name, _, positions = source.partition(":")
            bounds = positions.split("-")
            item = dict(item)
            if len(bounds) == 1:
                item.setdefault("block", bounds[0])
            elif len(bounds) == 2:
                item.setdefault("first", bounds[0])
                item.setdefault("last", bounds[1])
            source = name
        indices, unit = _reference_indices(item)
        byte_offset = int(_pick(item, "byte_offset", "segment_offset", default=0))
        byte_length = _pick(item, "byte_length", "segment_length")
        if byte_length is not None:
            byte_length = int(byte_length)
            if byte_length < 0:
                raise ManifestError("negative reference byte length")
        if byte_offset < 0:
            raise ManifestError("negative reference byte offset")
        return [
            SegmentSpec("reference", source=str(source), index=index,
                        offset=byte_offset, length=byte_length, unit=unit)
            for index in indices
        ]

    if (bool(_pick(item, "pending", "awaiting", "deferred", "to_supply",
                    default=False))
            or kind in ("fragment", "pending", "supply", "transmit")
            or ("data" in item and item.get("data") is None)
            or ("inline" in item and item.get("inline") is None)):
        # A fragment without inline bytes is pending until it is supplied.
        length_value = _pick(item, "length", "size", "count")
        filename = _pick(item, "fragment_file", "supply_file", "file", "path",
                         "filename", "source_file")
        if length_value is not None:
            length_value = int(length_value)
            if length_value < 0:
                raise ManifestError("negative segment length")
        return SegmentSpec(
            "literal", pending=True,
            file=str(filename) if filename is not None else None,
            length=length_value)
    if kind not in (None, "literal", "bytes", "data", "content"):
        raise ManifestError("unknown segment kind: %r" % (kind,))

    encoded = _pick(item, "base64", "b64")
    if isinstance(encoded, str):
        try:
            data = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ManifestError("invalid base64 literal: %s" % (exc,)) from exc
        return literal(data)

    data = _pick(item, "data", "literal", "bytes", "content", "value", "text",
                 "inline", "payload")
    if data is not None:
        encoding = _pick(item, "encoding", default=default_encoding)
        if str(encoding).lower() in ("base64", "b64") and isinstance(data, str):
            try:
                return literal(base64.b64decode(
                    data.encode("ascii"), validate=True))
            except (binascii.Error, ValueError) as exc:
                raise ManifestError(
                    "invalid base64 literal: %s" % (exc,)) from exc
        return literal(_as_bytes(data, encoding))

    length = _pick(item, "length", "size", "count")
    if length is not None:
        length = int(length)
        if length < 0:
            raise ManifestError("negative segment length")

    filename = _pick(item, "file", "path", "filename", "source_file")
    if filename is not None:
        offset = int(_pick(item, "offset", "start", default=0))
        if offset < 0:
            raise ManifestError("negative segment offset")
        span = _pick(item, "range", "byte_range")
        if span is not None:
            bounds = (re.split(r"\s*[-:,.]\s*", span.strip())
                      if isinstance(span, str) else list(span))
            if len(bounds) != 2:
                raise ManifestError("invalid byte range: %r" % (span,))
            offset, end = int(bounds[0]), int(bounds[1])
            length = end - offset + 1
        return SegmentSpec("literal", file=str(filename), offset=offset,
                           length=length)

    if length is not None:
        return SegmentSpec("literal", pending=True, length=length)

    raise ManifestError(
        "segment has no content, file, reference or pending marker")


@dataclass(frozen=True)
class ParsedManifest:
    delivery_id: str
    sha256: str
    path: str | None
    segments: tuple
    raw: dict = field(compare=False, repr=False)
    fingerprint: str = ""

    def canonical(self):
        return json.dumps(
            {
                "delivery_id": self.delivery_id,
                "sha256": self.sha256,
                "path": self.path,
                "segments": [_segment_to_mapping(segment)
                             for segment in self.segments],
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def parse_manifest(manifest, base_path=None):
    """Parse a manifest mapping, JSON bytes/string or JSON file.

    Returns ``(ParsedManifest, base_path)``.  Incomplete JSON raises
    :class:`ManifestError`; it never produces a partial declaration.
    """
    source_path = None
    if isinstance(manifest, (bytes, bytearray)):
        try:
            manifest = json.loads(bytes(manifest).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestError(
                "manifest is not complete JSON: %s" % (exc,)) from exc
    elif isinstance(manifest, (str, os.PathLike)):
        value = os.fspath(manifest)
        if isinstance(manifest, str) and os.path.isfile(value):
            source_path = os.path.abspath(value)
            try:
                with open(value, "rb") as handle:
                    manifest = json.loads(handle.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise IncompleteDeclaration(
                    "manifest is not complete JSON: %s" % (exc,)) from exc
        else:
            try:
                manifest = json.loads(value)
            except json.JSONDecodeError as exc:
                raise IncompleteDeclaration(
                    "manifest is not complete JSON: %s" % (exc,)) from exc

    if not isinstance(manifest, Mapping):
        raise ManifestError("manifest must be a JSON object")

    nested = manifest.get("manifest") if "manifest" in manifest else None
    if isinstance(nested, Mapping):
        merged = dict(nested)
        for key in ("path", "filename", "file", "declaration"):
            if key in manifest and key not in merged:
                merged[key] = manifest[key]
        manifest = merged

    delivery_id = _pick(manifest, "delivery_id", "delivery", "id", "name",
                        "consignment", "consignment_id")
    if delivery_id is None:
        raise ManifestError("manifest has no delivery identifier")
    delivery_id = str(delivery_id)
    if not delivery_id:
        raise ManifestError("empty delivery identifier")

    digest = _pick(manifest, "sha256", "final_sha256", "content_sha256",
                   "content_hash", "digest", "checksum", "content")
    if isinstance(digest, Mapping):
        digest = _pick(digest, "sha256", "digest", "hash", "value")
    if digest is None:
        raise ManifestError("manifest has no final SHA-256 digest")
    digest = _normalize_digest(digest)

    path = _pick(manifest, "path", "filename", "file")
    if path is not None:
        path = str(path)

    raw_segments = _pick(manifest, "segments", "parts", "blocks", "layout",
                         "entries")
    if raw_segments is None:
        raise ManifestError("manifest has no segments")
    if not isinstance(raw_segments, (list, tuple)):
        raise ManifestError("segments must be a list")

    encoding = _pick(manifest, "encoding", default="utf-8")
    segments = []
    for item in raw_segments:
        parsed = _segment_from_mapping(item, encoding)
        if isinstance(parsed, list):
            segments.extend(parsed)
        else:
            segments.append(parsed)
    if not segments:
        raise ManifestError("manifest has no segments")

    for position, segment in enumerate(segments):
        if segment.kind == "reference":
            if segment.source is None or segment.index is None:
                raise ManifestError("invalid reference at segment %d" % position)
            if segment.index < 0:
                raise ManifestError(
                    "segment position out of range at segment %d" % position)
        elif segment.kind != "literal":
            raise ManifestError("unknown segment kind at %d" % position)

    if source_path is not None and base_path is None:
        base_path = os.path.dirname(source_path)
    if path is None and source_path is not None:
        path = source_path

    parsed = ParsedManifest(
        delivery_id=delivery_id,
        sha256=digest,
        path=path,
        segments=tuple(segments),
        raw=dict(manifest),
    )
    fingerprint = _sha256(parsed.canonical().encode("utf-8"))
    return replace(parsed, fingerprint=fingerprint), base_path


# ---------------------------------------------------------------------------
# Stored records


def _atomic_write(path, data):
    path = os.fspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _resolve_path(path, base_path):
    if path is None:
        return None
    path = os.fspath(path)
    if os.path.isabs(path) or not base_path:
        return path
    return os.path.join(base_path, path)


def _segments_from_json(value):
    result = []
    for segment in value:
        segment = dict(segment)
        segment["pieces"] = [tuple(piece) for piece in segment.get("pieces", [])]
        if segment.get("origin") is not None:
            segment["origin"] = list(segment["origin"])
        result.append(segment)
    return result


def _segments_to_json(segments):
    result = []
    for segment in segments:
        item = dict(segment)
        item["pieces"] = [list(piece) for piece in segment.get("pieces", [])]
        result.append(item)
    return result


class _Plan:
    """A resolved declaration before it becomes a durable record."""

    def __init__(self):
        self.segments = []
        self.new_data = {}          # digest -> bytes for blocks to write
        self.new_digests = set()
        self.new_bytes = 0
        self.reserved_blocks = 0
        self.reserved_bytes = 0
        self.missing = []


class DeliveryNotification(dict):
    """A ready, sealed delivery.

    It behaves as a mapping of immutable delivery metadata and also exposes
    convenience attributes.  ``open()`` returns a :class:`DeliveryReader`.
    """

    def __init__(self, store, delivery_id, consumer=None):
        self._store = store
        self.delivery_id = delivery_id
        self.consumer = consumer
        record = store._ready_record(delivery_id)
        size = sum(int(segment.get("size", 0)) for segment in record["segments"])
        super().__init__(
            delivery_id=delivery_id,
            id=delivery_id,
            sha256=record["sha256"],
            digest=record["sha256"],
            content_sha256=record["sha256"],
            path=record.get("path"),
            filename=record.get("path"),
            size=size,
            acknowledged=bool(record.get("acked")),
            ready=True,
        )

    @property
    def sha256(self):
        return dict.__getitem__(self, "sha256")

    @property
    def digest(self):
        return dict.__getitem__(self, "sha256")

    @property
    def path(self):
        return dict.__getitem__(self, "path")

    @property
    def size(self):
        return dict.__getitem__(self, "size")

    @property
    def ready(self):
        return True

    @property
    def acknowledged(self):
        return self._store.is_acknowledged(self.delivery_id,
                                           consumer=self.consumer)

    @property
    def segments(self):
        return tuple(
            {
                "index": index,
                "segment": index + 1,
                "size": int(segment.get("size", 0)),
                "kind": segment.get("kind"),
                "sha256": (segment.get("pieces") or [[None]])[0][0],
            }
            for index, segment in enumerate(
                self._store._delivery(self.delivery_id)["segments"])
        )

    def open(self, offset=0, length=None):
        """Open a seekable reader over this delivery."""
        return self._store.open(self.delivery_id, offset=offset, length=length)

    reader = open

    def read(self, offset=0, length=None):
        """Read an arbitrary byte range."""
        return self._store.read(self.delivery_id, offset=offset, length=length)

    read_at = read

    def read_bytes(self):
        return self.read()

    def chunks(self, size=65536):
        return self._store.iter_chunks(self.delivery_id, size=size)

    def verify(self):
        return self._store.verify(self.delivery_id)

    def acknowledge(self, consumer=None):
        return self._store.acknowledge(
            self.delivery_id,
            consumer=self.consumer if consumer is None else consumer)

    ack = acknowledge
    release = acknowledge
    confirm = acknowledge

    def take_over(self):
        """Return a query view for references this delivery has taken over.

        The declaration is already accepted, so this only reports what it
        owns; it never re-reads the current same-name original file.
        """
        return self._store.takeover_report(self.delivery_id)

    def to_dict(self):
        result = dict(self)
        result["segments"] = list(self.segments)
        result["acknowledged"] = self.acknowledged
        return result

    def __repr__(self):
        return "<DeliveryNotification {} sha256={} size={}>".format(
            self.delivery_id, self.sha256[:12], self.size)

    def __str__(self):
        return self.__repr__()


class PendingDelivery:
    """An accepted declaration that still waits for fragments.

    References in this declaration have already been taken over, so the
    referenced blocks stay retained even if the older delivery is
    acknowledged.
    """

    def __init__(self, store, delivery_id, consumer=None):
        self._store = store
        self.delivery_id = delivery_id
        self.consumer = consumer

    @property
    def id(self):
        return self.delivery_id

    @property
    def ready(self):
        return False

    @property
    def acknowledged(self):
        return self._store.is_acknowledged(self.delivery_id,
                                           consumer=self.consumer)

    @property
    def pending_segments(self):
        record = self._store._delivery(self.delivery_id)
        return tuple(
            index for index, segment in enumerate(record["segments"])
            if segment.get("pending"))

    @property
    def missing_blocks(self):
        record = self._store._delivery(self.delivery_id)
        return sum(
            int(segment.get("reserved_blocks", 0))
            for segment in record["segments"] if segment.get("pending"))

    def supply(self, index, data):
        return self._store.supply_fragment(self.delivery_id, index, data)

    supply_fragment = supply

    def supply_file(self, index, filename):
        return self._store.supply_fragment_file(self.delivery_id, index,
                                                filename)

    def read(self, offset=0, length=None):
        raise ManifestError(
            "delivery %s is still pending: %s" % (
                self.delivery_id, self.pending_segments))

    def open(self, offset=0, length=None):
        return self.read(offset, length)

    def acknowledge(self, consumer=None):
        return self._store.acknowledge(self.delivery_id, consumer=consumer)

    ack = acknowledge
    confirm = acknowledge

    def reject(self):
        """Reject an unaccepted pending declaration and free its ownership."""
        return self._store.abandon(self.delivery_id)

    def take_over(self):
        return self._store.takeover_report(self.delivery_id)

    def __repr__(self):
        return "<PendingDelivery {} pending={}>".format(
            self.delivery_id, self.pending_segments)


class DeliveryReader(io.RawIOBase):
    """Seekable, read-only random access over a sealed delivery.

    Reads resolve individual block pieces lazily.  A read crossing reference
    boundaries never constructs a complete synthetic copy.
    """

    def __init__(self, store, delivery_id, offset=0, length=None):
        super().__init__()
        self._store = store
        self.delivery_id = delivery_id
        record = store._ready_record(delivery_id)
        self._pieces = []
        for segment in record["segments"]:
            self._pieces.extend(segment.get("pieces", []))
        self._starts = []
        total = 0
        for digest, start, size in self._pieces:
            self._starts.append(total)
            total += int(size)
        self._size = total
        self._start = int(offset)
        if self._start < 0:
            self._start = max(0, self._size + self._start)
        if self._start > self._size:
            raise ValueError("offset beyond delivery size")
        self._limit = self._size
        if length is not None:
            length = int(length)
            if length < 0:
                raise ValueError("length must be non-negative")
            self._limit = min(self._size, self._start + length)
        self._position = self._start
        store._reader_opened(delivery_id)

    @property
    def sha256(self):
        return self._store._delivery(self.delivery_id)["sha256"]

    @property
    def digest(self):
        return self.sha256

    @property
    def delivery_size(self):
        return self._size

    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return True

    def tell(self):
        return self._position

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self._position + offset
        elif whence == io.SEEK_END:
            position = self._size + offset
        else:
            raise ValueError("invalid whence")
        if position < 0:
            raise ValueError("negative seek position")
        self._position = position
        return position

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def read(self, size=-1):
        if size is None or size < 0:
            size = self._limit - self._position
        size = min(int(size), max(0, self._limit - self._position))
        if size <= 0 or self._position >= self._size:
            return b""
        parts = []
        remaining = size
        position = self._position
        index = bisect.bisect_right(self._starts, position) - 1
        if index < 0:
            index = 0
        while remaining > 0 and index < len(self._pieces):
            digest, block_start, block_size = self._pieces[index]
            segment_start = self._starts[index]
            inside = position - segment_start
            available = int(block_size) - inside
            if available <= 0:
                index += 1
                continue
            take = min(remaining, available)
            parts.append(
                self._store._read_block(digest, int(block_start) + inside, take))
            position += take
            remaining -= take
            index += 1
        data = b"".join(parts)
        self._position += len(data)
        return data

    def readall(self):
        return self.read(-1)

    def close(self):
        if not self.closed:
            try:
                self._store._reader_closed(self.delivery_id)
            finally:
                super().close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def __repr__(self):
        return "<DeliveryReader {} position={} size={}>".format(
            self.delivery_id, self._position, self._size)


# ---------------------------------------------------------------------------
# Durable store


def _safe_name(delivery_id):
    name = re.sub(r"[^A-Za-z0-9_.-]",
                  lambda match: "_%02x" % ord(match.group(0)), delivery_id)
    if name in ("", ".", ".."):
        name = "_"
    if len(name) > 180:
        name = name[:140] + "-" + _sha256(delivery_id.encode("utf-8"))[:20]
    return name


def _slice_pieces(pieces, offset=0, length=None):
    """Slice ``[(digest, start, size), ...]`` by absolute byte offsets."""
    total = sum(int(piece[2]) for piece in pieces)
    offset = int(offset or 0)
    if length is not None:
        length = int(length)
        if length < 0:
            raise ManifestError("negative segment length")
    if offset < 0 or offset > total:
        raise ManifestError("segment position out of range")
    if length is None:
        length = total - offset
    if offset + length > total:
        raise ManifestError("segment position out of range")
    if length == 0:
        return []
    result = []
    position = 0
    end = offset + length
    for digest, start, size in pieces:
        size = int(size)
        piece_end = position + size
        if piece_end <= offset:
            position = piece_end
            continue
        if position >= end:
            break
        local_start = max(0, offset - position)
        local_end = min(size, end - position)
        result.append((digest, int(start) + local_start,
                       local_end - local_start))
        position = piece_end
    return result


class DeliveryStore:
    """Durable content-addressed storage for sealed deliveries.

    Parameters
    ----------
    root: path
        Directory for content blocks, manifests and receipts.
    content_capacity: int, optional
        Maximum number of content blocks retained.  ``None`` is unlimited.
    content_bytes: int, optional
        Optional byte capacity in addition to the block count.
    block_size: int, optional
        Optional maximum size for a literal block.  By default a literal
        segment is one block, which makes capacity easy to reason about.
    """

    def __init__(self, root, content_capacity=None, content_bytes=None,
                 block_size=None, capacity=None, max_blocks=None,
                 **kwargs):
        self.root = os.path.abspath(os.fspath(root))
        self.blocks_dir = os.path.join(self.root, "blocks")
        self.records_dir = os.path.join(self.root, "deliveries")
        self.temp_dir = os.path.join(self.root, "tmp")
        for directory in (self.blocks_dir, self.records_dir, self.temp_dir):
            os.makedirs(directory, exist_ok=True)
        if content_capacity is None:
            content_capacity = capacity
        if content_capacity is None:
            content_capacity = max_blocks
        if content_capacity is not None:
            content_capacity = int(content_capacity)
            if content_capacity < 0:
                raise ValueError("content_capacity must be non-negative")
        self.content_capacity = content_capacity
        self.content_bytes = (int(content_bytes)
                              if content_bytes is not None else None)
        self.block_size = int(block_size) if block_size is not None else None
        self._lock = threading.RLock()
        self._records = {}
        self._owners = defaultdict(set)
        self._readers = defaultdict(int)
        self._load()
        self._reconcile_blocks()

    # -- persistence -----------------------------------------------------

    def _record_path(self, delivery_id):
        return os.path.join(self.records_dir, _safe_name(delivery_id) + ".json")

    def _block_path(self, digest):
        return os.path.join(self.blocks_dir, digest)

    def _load(self):
        try:
            names = sorted(os.listdir(self.records_dir))
        except OSError:
            names = []
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            path = os.path.join(self.records_dir, name)
            try:
                with open(path, "rb") as handle:
                    record = json.loads(handle.read().decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            delivery_id = record.get("delivery_id")
            if not delivery_id:
                continue
            record["segments"] = _segments_from_json(
                record.get("segments", []))
            record.setdefault("consumers", {})
            record.setdefault("acked", False)
            record.setdefault("released", False)
            self._records[delivery_id] = record
        self._owners.clear()
        for delivery_id, record in self._records.items():
            if record.get("released"):
                continue
            for segment in record["segments"]:
                for digest, _, _ in segment.get("pieces", []):
                    self._owners[digest].add(delivery_id)

    def _save(self, delivery_id):
        record = dict(self._records[delivery_id])
        record["segments"] = _segments_to_json(record["segments"])
        _atomic_write(
            self._record_path(delivery_id),
            (json.dumps(record, sort_keys=True, ensure_ascii=False,
                        indent=1) + "\n").encode("utf-8"))

    def _reconcile_blocks(self):
        """Delete content files that no live delivery owns.

        A block owned only by an acknowledged delivery is removed.  A block
        still owned by any pending or ready delivery (and therefore by a
        taken-over reference) is retained.
        """
        referenced = set()
        for record in self._records.values():
            if record.get("released"):
                continue
            for segment in record["segments"]:
                for digest, _, _ in segment.get("pieces", []):
                    referenced.add(digest)
        try:
            names = os.listdir(self.blocks_dir)
        except OSError:
            names = []
        for name in names:
            if name.startswith("."):
                continue
            if name not in referenced:
                try:
                    os.unlink(os.path.join(self.blocks_dir, name))
                except OSError:
                    pass

    # -- inspection ------------------------------------------------------

    def _delivery(self, delivery_id):
        try:
            return self._records[delivery_id]
        except KeyError:
            raise MissingContent([
                {"delivery_id": delivery_id, "segment": None,
                 "index": None, "reason": "unknown delivery"}
            ], "unknown delivery: %s" % delivery_id)

    def _ready_record(self, delivery_id):
        record = self._delivery(delivery_id)
        if record.get("state") != "ready":
            raise ManifestError(
                "delivery %s is not complete" % delivery_id)
        return record

    def has_delivery(self, delivery_id):
        return delivery_id in self._records

    def is_ready(self, delivery_id):
        record = self._records.get(delivery_id)
        return bool(record and record.get("state") == "ready")

    def is_pending(self, delivery_id):
        record = self._records.get(delivery_id)
        return bool(record and record.get("state") == "pending")

    def is_acknowledged(self, delivery_id, consumer=None):
        record = self._records.get(delivery_id)
        if record is None:
            return False
        if record.get("released"):
            return True
        consumers = record.get("consumers") or {}
        if consumer is not None:
            return bool(consumers.get(str(consumer)))
        return bool(record.get("acked"))

    def references_taken_over(self, delivery_id):
        """Whether this declaration has taken over its references."""
        return delivery_id in self._records

    taken_over = references_taken_over

    def status(self, delivery_id):
        record = self._records.get(delivery_id)
        if record is None:
            return {
                "delivery_id": delivery_id,
                "known": False,
                "ready": False,
                "pending": False,
                "taken_over": False,
                "acknowledged": False,
                "missing": [],
            }
        missing = []
        for index, segment in enumerate(record["segments"]):
            for digest, _, _ in segment.get("pieces", []):
                if not os.path.exists(self._block_path(digest)):
                    missing.append({
                        "delivery_id": delivery_id,
                        "segment": index + 1,
                        "index": index,
                        "block": digest,
                        "reason": "content reclaimed",
                    })
        return {
            "delivery_id": delivery_id,
            "known": True,
            "ready": record.get("state") == "ready",
            "pending": record.get("state") == "pending",
            "taken_over": True,
            "acknowledged": self.is_acknowledged(delivery_id),
            "released": bool(record.get("released")),
            "sha256": record.get("sha256"),
            "path": record.get("path"),
            "pending_segments": [
                index + 1 for index, segment in enumerate(record["segments"])
                if segment.get("pending")],
            "missing": missing,
        }

    def get(self, delivery_id, consumer=None):
        record = self._delivery(delivery_id)
        if consumer is not None:
            self.register_consumer(delivery_id, consumer)
        if record.get("state") == "ready":
            return DeliveryNotification(self, delivery_id, consumer=consumer)
        return PendingDelivery(self, delivery_id, consumer=consumer)

    def register_consumer(self, delivery_id, consumer):
        """Register an independent consumer of a delivery.

        Registered consumers each receive their own acknowledgement slot.
        Content is released only after every registered consumer has
        acknowledged (or a global acknowledgement is given).
        """
        if consumer is None:
            return
        with self._lock:
            record = self._delivery(delivery_id)
            consumers = record.setdefault("consumers", {})
            if str(consumer) not in consumers:
                consumers[str(consumer)] = False
                self._save(delivery_id)

    notification = get
    __getitem__ = get

    def __contains__(self, delivery_id):
        return delivery_id in self._records

    def records(self):
        result = []
        for delivery_id, record in sorted(
                self._records.items(),
                key=lambda item: (item[1].get("created_at", 0), item[0])):
            result.append(self.get(delivery_id))
        return result

    def unacknowledged(self):
        """Ready deliveries whose notification has not been acknowledged."""
        result = []
        for delivery_id, record in sorted(
                self._records.items(),
                key=lambda item: (item[1].get("created_at", 0), item[0])):
            if record.get("state") != "ready":
                continue
            if record.get("released") or record.get("acked"):
                continue
            result.append(DeliveryNotification(self, delivery_id))
        return result

    pending_notifications = unacknowledged

    def redeliver(self):
        """Return notifications that still require acknowledgement.

        Retransmission is idempotent: each notification keeps the original
        delivery identifier and digest.  Acknowledged deliveries are omitted.
        Pending declarations are not success notifications and are omitted.
        """
        return self.unacknowledged()

    def pending(self):
        result = []
        for delivery_id, record in sorted(
                self._records.items(),
                key=lambda item: (item[1].get("created_at", 0), item[0])):
            if record.get("state") == "pending" and not record.get("released"):
                result.append(PendingDelivery(self, delivery_id))
        return result

    # -- capacity --------------------------------------------------------

    def _reserved_blocks(self, exclude=None):
        count = 0
        for delivery_id, record in self._records.items():
            if delivery_id == exclude or record.get("released"):
                continue
            for segment in record["segments"]:
                if segment.get("pending"):
                    count += int(segment.get("reserved_blocks", 1))
        return count

    def _reserved_bytes(self, exclude=None):
        count = 0
        for delivery_id, record in self._records.items():
            if delivery_id == exclude or record.get("released"):
                continue
            for segment in record["segments"]:
                if segment.get("pending"):
                    count += int(segment.get("reserved_bytes", 0))
        return count

    def _block_files(self):
        try:
            names = os.listdir(self.blocks_dir)
        except OSError:
            return []
        return [name for name in names
                if not name.startswith(".")
                and os.path.isfile(os.path.join(self.blocks_dir, name))]

    def used_blocks(self):
        return len(self._block_files()) + self._reserved_blocks()

    @property
    def used_content_blocks(self):
        return self.used_blocks()

    def used_bytes(self):
        used = 0
        for name in self._block_files():
            try:
                used += os.path.getsize(os.path.join(self.blocks_dir, name))
            except OSError:
                pass
        return used + self._reserved_bytes()

    @property
    def available_blocks(self):
        if self.content_capacity is None:
            return None
        return max(0, self.content_capacity - self.used_blocks())

    @property
    def available_bytes(self):
        if self.content_bytes is None:
            return None
        return max(0, self.content_bytes - self.used_bytes())

    def capacity(self):
        return {
            "blocks": self.content_capacity,
            "block_capacity": self.content_capacity,
            "bytes": self.content_bytes,
            "byte_capacity": self.content_bytes,
            "used_blocks": self.used_blocks(),
            "used_bytes": self.used_bytes(),
            "reserved_blocks": self._reserved_blocks(),
            "reserved_bytes": self._reserved_bytes(),
            "available_blocks": self.available_blocks,
            "available_bytes": self.available_bytes,
        }

    def shortage(self, required_blocks=1, required_bytes=0):
        """Return the capacity shortfall for a proposed addition.

        ``None`` means the request fits.  Otherwise the mapping contains
        ``deficit_blocks`` and ``deficit_bytes``.  This does not modify the
        store and is useful for reporting how much more content capacity is
        needed before a declaration can be accepted.
        """
        available = self.available_blocks
        available_bytes = self.available_bytes
        deficit_blocks = (0 if available is None
                          else max(0, int(required_blocks) - available))
        deficit_bytes = (0 if available_bytes is None
                         else max(0, int(required_bytes) - available_bytes))
        if deficit_blocks == 0 and deficit_bytes == 0:
            return None
        return {
            "deficit_blocks": deficit_blocks,
            "deficit_bytes": deficit_bytes,
            "available_blocks": available,
            "available_bytes": available_bytes,
        }

    def _check_capacity(self, new_blocks, new_bytes, delivery_id):
        if self.content_capacity is not None:
            used = self.used_blocks()
            available = max(0, self.content_capacity - used)
            if new_blocks > available:
                raise CapacityError(
                    new_blocks, available, new_bytes,
                    self.available_bytes if self.available_bytes is not None
                    else new_bytes,
                    delivery_id=delivery_id)
        if self.content_bytes is not None:
            used = self.used_bytes()
            available = max(0, self.content_bytes - used)
            if new_bytes > available:
                raise CapacityError(
                    new_blocks,
                    self.available_blocks if self.available_blocks is not None
                    else new_blocks,
                    new_bytes, available, delivery_id=delivery_id)
        return True

    def can_accept(self, manifest, base_path=None):
        """Check capacity for a declaration without accepting it.

        Returns a mapping with ``accepted``, ``required_blocks``,
        ``available_blocks``, ``deficit_blocks``, and the byte equivalents.
        Referenced content that is already retained costs no new capacity.
        Missing sealed sources or range errors are reported rather than
        accepted.
        """
        parsed, parsed_base = parse_manifest(manifest, base_path)
        if base_path is None:
            base_path = parsed_base
        with self._lock:
            try:
                plan = self._plan(parsed, base_path)
            except (ManifestError, MissingContent) as exc:
                return {
                    "accepted": False,
                    "delivery_id": parsed.delivery_id,
                    "required_blocks": 0,
                    "available_blocks": self.available_blocks,
                    "deficit_blocks": 0,
                    "required_bytes": 0,
                    "available_bytes": self.available_bytes,
                    "deficit_bytes": 0,
                    "error": str(exc),
                }
            required = len(plan.new_digests) + plan.reserved_blocks
            required_bytes = plan.new_bytes + plan.reserved_bytes
            used = self.used_blocks()
            available = (None if self.content_capacity is None
                         else max(0, self.content_capacity - used))
            used_bytes = self.used_bytes()
            available_bytes = (None if self.content_bytes is None
                               else max(0, self.content_bytes - used_bytes))
            deficit = (0 if available is None
                       else max(0, required - available))
            deficit_bytes = (0 if available_bytes is None
                             else max(0, required_bytes - available_bytes))
            missing = plan.missing
            accepted = not missing and deficit == 0 and deficit_bytes == 0
            return {
                "accepted": accepted,
                "delivery_id": parsed.delivery_id,
                "required_blocks": required,
                "available_blocks": available,
                "deficit_blocks": deficit,
                "required_bytes": required_bytes,
                "available_bytes": available_bytes,
                "deficit_bytes": deficit_bytes,
                "missing": missing,
            }

    capacity_for = can_accept

    # -- block IO --------------------------------------------------------

    def _read_block(self, digest, offset=0, length=None):
        path = self._block_path(digest)
        try:
            with open(path, "rb") as handle:
                if offset:
                    handle.seek(int(offset))
                if length is None:
                    return handle.read()
                return handle.read(int(length))
        except FileNotFoundError:
            raise MissingContent([
                {"delivery_id": None, "segment": None, "block": digest,
                 "reason": "missing content block"}
            ], "missing content block %s" % digest)

    def _store_block(self, digest, data):
        path = self._block_path(digest)
        if not os.path.exists(path):
            _atomic_write(path, data)

    def _reader_opened(self, delivery_id):
        with self._lock:
            record = self._records.get(delivery_id)
            if not record:
                return
            for segment in record["segments"]:
                for digest, _, _ in segment.get("pieces", []):
                    self._readers[digest] += 1

    def _reader_closed(self, delivery_id):
        with self._lock:
            record = self._records.get(delivery_id)
            if not record:
                return
            for segment in record["segments"]:
                for digest, _, _ in segment.get("pieces", []):
                    self._readers[digest] = max(
                        0, self._readers.get(digest, 0) - 1)
            self._reclaim_orphan_blocks()

    def _reclaim_orphan_blocks(self):
        candidates = set(self._owners)
        candidates.update(self._readers)
        for digest in candidates:
            if (not self._owners.get(digest)
                    and self._readers.get(digest, 0) <= 0):
                try:
                    os.unlink(self._block_path(digest))
                except OSError:
                    pass
                self._readers.pop(digest, None)
                self._owners.pop(digest, None)
        for digest in list(self._owners):
            if not self._owners[digest]:
                self._owners.pop(digest, None)
        # Remove block files that were created by a failed declaration but
        # never acquired an owner.
        owned = set(self._owners)
        for name in self._block_files():
            if name not in owned and self._readers.get(name, 0) <= 0:
                try:
                    os.unlink(self._block_path(name))
                except OSError:
                    pass

    # -- declaration -----------------------------------------------------

    def _block_index(self, record):
        """Flat list of ``(segment index, piece index, piece)`` for a record."""
        result = []
        for segment_index, segment in enumerate(record["segments"]):
            for piece_index, piece in enumerate(segment.get("pieces", [])):
                result.append((segment_index, piece_index, piece))
        return result

    def _plan(self, manifest, base_path):
        plan = _Plan()
        for position, spec in enumerate(manifest.segments):
            if spec.kind == "reference":
                source = self._records.get(spec.source)
                if source is None:
                    plan.missing.append({
                        "delivery_id": spec.source,
                        "segment": spec.index + 1,
                        "index": spec.index,
                        "reason": "unknown delivery",
                    })
                    continue
                if spec.unit == "block":
                    blocks = self._block_index(source)
                    if spec.index >= len(blocks):
                        raise ManifestError(
                            "block position out of range: %s block %d has "
                            "only %d block(s)" % (
                                spec.source, spec.index + 1, len(blocks)))
                    source_segment_index, _, piece = blocks[spec.index]
                    source_segment = source["segments"][source_segment_index]
                    if source_segment.get("pending"):
                        plan.missing.append({
                            "delivery_id": spec.source,
                            "segment": source_segment_index + 1,
                            "index": source_segment_index,
                            "block": spec.index + 1,
                            "reason": "source block is still pending",
                        })
                        continue
                    source_pieces = [piece]
                    origin_segment = source_segment_index
                else:
                    segments = source["segments"]
                    if spec.index >= len(segments):
                        raise ManifestError(
                            "segment position out of range: %s segment %d has "
                            "only %d segment(s)" % (
                                spec.source, spec.index + 1, len(segments)))
                    source_segment = segments[spec.index]
                    if source_segment.get("pending"):
                        plan.missing.append({
                            "delivery_id": spec.source,
                            "segment": spec.index + 1,
                            "index": spec.index,
                            "reason": "source segment is still pending",
                        })
                        continue
                    source_pieces = source_segment.get("pieces", [])
                    origin_segment = spec.index
                try:
                    pieces = _slice_pieces(
                        source_pieces, spec.offset, spec.length)
                except ManifestError:
                    raise ManifestError(
                        "%s position out of range: %s[%d]" % (
                            spec.unit, spec.source, spec.index + 1))
                if not pieces:
                    raise ManifestError(
                        "%s position out of range: %s[%d]" % (
                            spec.unit, spec.source, spec.index + 1))
                for digest, _, _ in pieces:
                    if not os.path.exists(self._block_path(digest)):
                        plan.missing.append({
                            "delivery_id": spec.source,
                            "segment": origin_segment + 1,
                            "index": origin_segment,
                            "block": digest,
                            "reason": "content reclaimed",
                        })
                size = sum(int(piece[2]) for piece in pieces)
                plan.segments.append({
                    "kind": "reference",
                    "origin": [spec.source, origin_segment],
                    "pieces": pieces,
                    "size": size,
                    "pending": False,
                })
                continue

            if spec.pending:
                filename = _resolve_path(spec.file, base_path)
                reserved_bytes = int(spec.length or 0)
                plan.segments.append({
                    "kind": "literal",
                    "origin": None,
                    "pieces": [],
                    "size": int(spec.length) if spec.length is not None else None,
                    "pending": True,
                    "reserved_blocks": 1,
                    "reserved_bytes": reserved_bytes,
                    "fragment_file": filename,
                })
                plan.reserved_blocks += 1
                plan.reserved_bytes += reserved_bytes
                continue

            if spec.data is not None:
                data = bytes(spec.data)
            else:
                filename = _resolve_path(spec.file, base_path)
                if filename is None:
                    raise ManifestError(
                        "literal segment %d has no file" % (position + 1))
                data = self._read_file_range(
                    filename, spec.offset, spec.length, missing_ok=False)
            pieces = []
            block_size = self.block_size
            if block_size and len(data) > block_size:
                for start in range(0, len(data), block_size):
                    chunk = data[start:start + block_size]
                    digest = _sha256(chunk)
                    pieces.append((digest, 0, len(chunk)))
                    if (digest not in plan.new_digests
                            and not os.path.exists(self._block_path(digest))):
                        plan.new_data[digest] = chunk
                        plan.new_digests.add(digest)
                        plan.new_bytes += len(chunk)
            else:
                digest = _sha256(data)
                pieces.append((digest, 0, len(data)))
                if (digest not in plan.new_digests
                        and not os.path.exists(self._block_path(digest))):
                    plan.new_data[digest] = data
                    plan.new_digests.add(digest)
                    plan.new_bytes += len(data)
            plan.segments.append({
                "kind": "literal",
                "origin": None,
                "pieces": pieces,
                "size": len(data),
                "pending": False,
            })
        return plan

    @staticmethod
    def _read_file_range(filename, offset, length, missing_ok=False):
        try:
            size = os.path.getsize(filename)
        except OSError:
            if missing_ok:
                return None
            raise ManifestError("literal source file does not exist: %r"
                                % (filename,))
        offset = int(offset or 0)
        if offset < 0 or offset > size:
            raise ManifestError("segment position out of range for %r"
                                % (filename,))
        if length is None:
            length = size - offset
        length = int(length)
        if length < 0 or offset + length > size:
            raise ManifestError("segment position out of range for %r"
                                % (filename,))
        with open(filename, "rb") as handle:
            handle.seek(offset)
            data = handle.read(length)
        if len(data) != length:
            raise ManifestError("short read from literal source %r"
                                % (filename,))
        return data

    def _hash_plan(self, plan):
        hasher = hashlib.sha256()
        for segment in plan.segments:
            if segment.get("pending"):
                continue
            for digest, start, size in segment.get("pieces", []):
                data = plan.new_data.get(digest)
                if data is not None:
                    hasher.update(data[int(start):int(start) + int(size)])
                else:
                    remaining = int(size)
                    position = int(start)
                    while remaining:
                        chunk = self._read_block(
                            digest, position, min(remaining, 65536))
                        if not chunk:
                            break
                        hasher.update(chunk)
                        position += len(chunk)
                        remaining -= len(chunk)
        return hasher.hexdigest()

    def _hash_record(self, record, overrides=None):
        overrides = overrides or {}
        hasher = hashlib.sha256()
        for index, segment in enumerate(record["segments"]):
            if index in overrides:
                hasher.update(overrides[index])
                continue
            if segment.get("pending"):
                continue
            for digest, start, size in segment.get("pieces", []):
                remaining = int(size)
                position = int(start)
                while remaining:
                    chunk = self._read_block(
                        digest, position, min(remaining, 65536))
                    if not chunk:
                        break
                    hasher.update(chunk)
                    position += len(chunk)
                    remaining -= len(chunk)
        return hasher.hexdigest()

    def declare(self, manifest, base_path=None, consumer=None, strict=True):
        """Accept a sealed declaration.

        Returns :class:`DeliveryNotification` when the delivery is complete and
        its final digest matches.  Returns :class:`PendingDelivery` when the
        declaration has taken over its references but still waits for one or
        more fragments.  Errors raise :class:`ManifestError` subclasses and
        never produce a success notification.

        Re-declaring the same identifier is idempotent.  Reusing an identifier
        for a different digest is a :class:`DeliveryConflict`.
        """
        parsed, parsed_base = parse_manifest(manifest, base_path)
        if base_path is None:
            base_path = parsed_base
        with self._lock:
            existing = self._records.get(parsed.delivery_id)
            if existing is not None:
                if existing.get("released"):
                    # Releasing a version must not make a repeated scan of the
                    # same declaration resurrect stale content implicitly.
                    raise DeliveryConflict(
                        "delivery %s was already acknowledged and released"
                        % parsed.delivery_id)
                if existing.get("fingerprint") == parsed.fingerprint:
                    return self.get(parsed.delivery_id)
                if (existing.get("state") == "pending"
                        and existing.get("sha256") == parsed.sha256):
                    return self._complete_pending(
                        existing, parsed, base_path, consumer)
                raise DeliveryConflict(
                    "delivery %s was already declared with different content"
                    % parsed.delivery_id)

            plan = self._plan(parsed, base_path)
            if plan.missing:
                raise MissingContent(plan.missing)
            if not plan.reserved_blocks:
                actual = self._hash_plan(plan)
                if actual != parsed.sha256:
                    raise DigestMismatch(
                        parsed.delivery_id, parsed.sha256, actual)
            # Pending fragments already reserved one block each.  New literal
            # blocks are in addition to those reservations.
            self._check_capacity(
                len(plan.new_digests) + plan.reserved_blocks,
                plan.new_bytes + plan.reserved_bytes, parsed.delivery_id)

            for digest, data in plan.new_data.items():
                self._store_block(digest, data)
            record = {
                "delivery_id": parsed.delivery_id,
                "sha256": parsed.sha256,
                "path": parsed.path,
                "fingerprint": parsed.fingerprint,
                "canonical": parsed.canonical(),
                "state": "pending" if plan.reserved_blocks else "ready",
                "segments": plan.segments,
                "acked": False,
                "released": False,
                "consumers": {},
                "created_at": time.time(),
            }
            self._records[parsed.delivery_id] = record
            for segment in plan.segments:
                for digest, _, _ in segment.get("pieces", []):
                    self._owners[digest].add(parsed.delivery_id)
            self._save(parsed.delivery_id)
            return self.get(parsed.delivery_id)

    accept = declare
    publish = declare
    add_delivery = declare

    def _hash_merged(self, segments, new_data=None):
        new_data = new_data or {}
        hasher = hashlib.sha256()
        for segment in segments:
            if segment.get("pending"):
                continue
            for digest, start, size in segment.get("pieces", []):
                data = new_data.get(digest)
                remaining = int(size)
                position = int(start)
                while remaining:
                    if data is not None:
                        chunk = data[position:position + min(remaining, 65536)]
                    else:
                        chunk = self._read_block(
                            digest, position, min(remaining, 65536))
                    if not chunk:
                        break
                    hasher.update(chunk)
                    position += len(chunk)
                    remaining -= len(chunk)
        return hasher.hexdigest()

    def _complete_pending(self, existing, parsed, base_path, consumer=None):
        """Merge a completion declaration into an existing pending record."""
        plan = self._plan(parsed, base_path)
        if plan.missing:
            raise MissingContent(plan.missing)

        old_segments = existing["segments"]
        new_segments = plan.segments
        if len(new_segments) != len(old_segments):
            raise DeliveryConflict(
                "delivery %s completion has a different segment layout"
                % parsed.delivery_id)

        merged = []
        released_reservations = 0
        released_reservation_bytes = 0
        new_blocks = set()
        new_bytes = 0
        for index, (old, new) in enumerate(zip(old_segments, new_segments)):
            if old.get("pending"):
                if new.get("pending"):
                    merged.append(new)
                    continue
                released_reservations += int(old.get("reserved_blocks", 1))
                released_reservation_bytes += int(
                    old.get("reserved_bytes", 0))
                for digest, _, size in new.get("pieces", []):
                    if (digest in plan.new_data
                            and digest not in new_blocks):
                        new_blocks.add(digest)
                        new_bytes += int(size)
                merged.append(new)
            else:
                if (old.get("kind") != new.get("kind")
                        or old.get("pieces") != new.get("pieces")
                        or int(old.get("size", 0)) != int(new.get("size", 0))):
                    raise DeliveryConflict(
                        "delivery %s completion changes an already sealed "
                        "segment" % parsed.delivery_id)
                merged.append(old)

        if not any(segment.get("pending") for segment in merged):
            actual = self._hash_merged(merged, plan.new_data)
            if actual != parsed.sha256:
                raise DigestMismatch(
                    parsed.delivery_id, parsed.sha256, actual)

        effective_blocks = max(0, len(new_blocks) - released_reservations)
        effective_bytes = max(0, new_bytes - released_reservation_bytes)
        self._check_capacity(effective_blocks, effective_bytes,
                             parsed.delivery_id)

        for digest in new_blocks:
            self._store_block(digest, plan.new_data[digest])

        for segment in merged:
            for digest, _, _ in segment.get("pieces", []):
                self._owners[digest].add(parsed.delivery_id)

        existing["segments"] = merged
        existing["fingerprint"] = parsed.fingerprint
        existing["canonical"] = parsed.canonical()
        existing["path"] = parsed.path or existing.get("path")
        existing["state"] = (
            "pending" if any(segment.get("pending") for segment in merged)
            else "ready")
        self._save(parsed.delivery_id)
        return self.get(parsed.delivery_id)

    # -- fragments -------------------------------------------------------

    def supply_fragment(self, delivery_id, index, data):
        """Supply one pending fragment.

        ``index`` is zero-based.  A fragment is accepted only if the complete
        delivery digest matches; otherwise the pending declaration is left
        unchanged and the fragment is not retained.
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        data = bytes(data)
        with self._lock:
            record = self._delivery(delivery_id)
            if record.get("state") != "pending":
                raise ManifestError(
                    "delivery %s is already complete" % delivery_id)
            segments = record["segments"]
            try:
                segment = segments[index]
            except IndexError:
                raise ManifestError(
                    "segment position out of range: %s has only %d segment(s)"
                    % (delivery_id, len(segments)))
            if not segment.get("pending"):
                raise ManifestError(
                    "segment %d of %s is not pending"
                    % (index + 1, delivery_id))
            expected = segment.get("size")
            if expected is not None and int(expected) != len(data):
                raise ManifestError(
                    "fragment length mismatch for %s segment %d: expected %d, "
                    "got %d" % (delivery_id, index + 1, int(expected),
                                len(data)))
            digest = _sha256(data)
            new_block = not os.path.exists(self._block_path(digest))
            released = int(segment.get("reserved_blocks", 1))
            effective = max(0, int(new_block) - released)
            self._check_capacity(effective, len(data) if new_block else 0,
                                 delivery_id)
            actual = self._hash_record(record, {index: data})
            if actual != record["sha256"]:
                raise DigestMismatch(delivery_id, record["sha256"], actual)
            if new_block:
                self._store_block(digest, data)
            segments[index] = {
                "kind": "literal",
                "origin": None,
                "pieces": [(digest, 0, len(data))],
                "size": len(data),
                "pending": False,
            }
            self._owners[digest].add(delivery_id)
            if not any(item.get("pending") for item in segments):
                record["state"] = "ready"
            self._save(delivery_id)
            return self.get(delivery_id)

    supply = supply_fragment

    def supply_fragment_file(self, delivery_id, index, filename):
        with open(filename, "rb") as handle:
            data = handle.read()
        return self.supply_fragment(delivery_id, index, data)

    def complete_pending(self, delivery_id):
        """Read any declared fragment files that are now available."""
        record = self._records.get(delivery_id)
        if not record or record.get("state") != "pending":
            return self.get(delivery_id) if record else None
        for index, segment in enumerate(record["segments"]):
            if not segment.get("pending"):
                continue
            filename = segment.get("fragment_file")
            if filename and os.path.isfile(filename):
                with open(filename, "rb") as handle:
                    data = handle.read()
                return self.supply_fragment(delivery_id, index, data)
        return PendingDelivery(self, delivery_id)

    # -- acknowledgement and reclamation ---------------------------------

    def acknowledge(self, delivery_id, consumer=None):
        """Acknowledge a delivery and release content no longer referenced.

        ``consumer`` names an independent reader.  When omitted, the delivery
        is globally released.  Content shared with another unacknowledged or
        pending delivery survives; content still needed by an open reader
        survives until that reader closes.
        """
        with self._lock:
            record = self._delivery(delivery_id)
            if consumer is None:
                record["acked"] = True
                record["released"] = True
            else:
                consumers = record.setdefault("consumers", {})
                consumers[str(consumer)] = True
                # Retain content until every registered consumer has
                # acknowledged.  An unregistered consumer is registered by
                # its acknowledgement and becomes the only consumer.
                if consumers and all(consumers.values()):
                    record["acked"] = True
                    record["released"] = True
            if record.get("released"):
                for segment in record["segments"]:
                    for digest, _, _ in segment.get("pieces", []):
                        owners = self._owners.get(digest)
                        if owners is not None:
                            owners.discard(delivery_id)
            self._save(delivery_id)
            self._reclaim_orphan_blocks()
            # Also remove blocks that have no owners and no readers.
            for segment in record["segments"]:
                for digest, _, _ in segment.get("pieces", []):
                    if (not self._owners.get(digest)
                            and self._readers.get(digest, 0) <= 0):
                        try:
                            os.unlink(self._block_path(digest))
                        except OSError:
                            pass
            return True

    release = acknowledge
    ack = acknowledge
    confirm = acknowledge

    def verify(self, delivery_id):
        """Return the SHA-256 of the currently retained content."""
        with self._lock:
            record = self._ready_record(delivery_id)
            return self._hash_record(record)

    def takeover_report(self, delivery_id):
        """Report the references a declaration has taken over.

        For each segment this returns the sealed source it came from (if any),
        the source's one-based segment number, the block digest, and whether
        the block is still retained.  When a referenced delivery is gone the
        report names the missing delivery and segment instead of consulting a
        newer file with the same original name.
        """
        with self._lock:
            record = self._delivery(delivery_id)
            segments = []
            missing = []
            for index, segment in enumerate(record["segments"]):
                item = {
                    "index": index,
                    "segment": index + 1,
                    "kind": segment.get("kind"),
                    "pending": bool(segment.get("pending")),
                    "size": int(segment.get("size") or 0),
                    "origin": (list(segment["origin"])
                               if segment.get("origin") else None),
                    "blocks": [],
                }
                for digest, _, size in segment.get("pieces", []):
                    retained = os.path.exists(self._block_path(digest))
                    item["blocks"].append({
                        "sha256": digest,
                        "size": int(size),
                        "retained": retained,
                    })
                    if not retained:
                        missing.append({
                            "delivery_id": delivery_id,
                            "segment": index + 1,
                            "index": index,
                            "block": digest,
                            "reason": "content reclaimed",
                        })
                segments.append(item)
            return {
                "delivery_id": delivery_id,
                "sha256": record.get("sha256"),
                "state": record.get("state"),
                "taken_over": True,
                "references": segments,
                "missing": missing,
            }

    def source_of(self, delivery_id, index):
        """Return the sealed source of one reference segment, if any."""
        record = self._delivery(delivery_id)
        segment = record["segments"][index]
        origin = segment.get("origin")
        if origin is None:
            return None
        source_id, source_index = origin
        return {
            "delivery_id": source_id,
            "segment": source_index + 1,
            "index": source_index,
        }

    def abandon(self, delivery_id):
        """Drop an unacknowledged pending declaration and its reservations.

        The declaration never produced a success notification, so this only
        frees capacity.  It never removes content referenced by another live
        delivery.
        """
        with self._lock:
            record = self._delivery(delivery_id)
            if record.get("state") != "pending":
                raise ManifestError(
                    "only a pending declaration can be rejected")
            record["released"] = True
            record["acked"] = True
            for segment in record["segments"]:
                for digest, _, _ in segment.get("pieces", []):
                    owners = self._owners.get(digest)
                    if owners is not None:
                        owners.discard(delivery_id)
            self._save(delivery_id)
            self._reclaim_orphan_blocks()
            return True

    reject = abandon

    def accepts(self, delivery_id):
        """Whether this declaration has been accepted (taken over refs)."""
        return delivery_id in self._records

    def is_taken_over(self, delivery_id):
        return delivery_id in self._records

    def delivery_manifest(self, delivery_id):
        """Return the sealed, canonical manifest for diagnostics."""
        record = self._delivery(delivery_id)
        return json.loads(record["canonical"])

    manifest = delivery_manifest

    def missing_references(self, manifest, base_path=None):
        """List missing sealed sources or out-of-range segment positions.

        This checks only reference availability.  It is useful before
        accepting a declaration that may have been delayed until its source
        was reclaimed.  A same-name file is never considered as a substitute.
        """
        parsed, parsed_base = parse_manifest(manifest, base_path)
        if base_path is None:
            base_path = parsed_base
        with self._lock:
            try:
                plan = self._plan(parsed, base_path)
            except ManifestError as exc:
                return [{"delivery_id": parsed.delivery_id, "segment": None,
                         "index": None, "reason": str(exc)}]
            return plan.missing

    def read(self, delivery_id, offset=0, length=None):
        reader = self.open(delivery_id, offset=offset, length=length)
        try:
            return reader.read()
        finally:
            reader.close()

    read_at = read

    def open(self, delivery_id, offset=0, length=None):
        return DeliveryReader(self, delivery_id, offset=offset, length=length)

    def reader(self, delivery_id, offset=0, length=None):
        """Alias for :meth:`open` returning a random-access reader."""
        return self.open(delivery_id, offset=offset, length=length)

    def segments(self, delivery_id):
        """Describe the declared segments in read order."""
        record = self._delivery(delivery_id)
        result = []
        for index, segment in enumerate(record["segments"]):
            result.append({
                "index": index,
                "segment": index + 1,
                "kind": segment.get("kind"),
                "size": int(segment.get("size") or 0),
                "pending": bool(segment.get("pending")),
                "origin": (list(segment["origin"])
                           if segment.get("origin") else None),
                "blocks": [
                    {"sha256": digest, "offset": int(start), "size": int(size)}
                    for digest, start, size in segment.get("pieces", [])
                ],
            })
        return result

    def blocks(self, delivery_id):
        """Describe retained content blocks in read order."""
        record = self._delivery(delivery_id)
        result = []
        for segment_index, segment in enumerate(record["segments"]):
            for digest, start, size in segment.get("pieces", []):
                result.append({
                    "sha256": digest,
                    "delivery_id": delivery_id,
                    "segment": segment_index + 1,
                    "offset": int(start),
                    "size": int(size),
                    "retained": os.path.exists(self._block_path(digest)),
                })
        return result

    def iter_chunks(self, delivery_id, size=65536):
        reader = self.open(delivery_id)
        try:
            while True:
                chunk = reader.read(size)
                if not chunk:
                    break
                yield chunk
        finally:
            reader.close()

    def reclaim(self):
        """Delete every block no live delivery or reader needs."""
        with self._lock:
            self._reclaim_orphan_blocks()
            return self.capacity()
