# Copyright 2015-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tools for representing raw BSON documents.

Inserting and Retrieving RawBSONDocuments
=========================================

Example: Moving a document between different databases/collections

.. doctest::

  >>> import bson
  >>> from pymongo import MongoClient
  >>> from bson.raw_bson import RawBSONDocument
  >>> client = MongoClient(document_class=RawBSONDocument)
  >>> client.drop_database("db")
  >>> client.drop_database("replica_db")
  >>> db = client.db
  >>> result = db.test.insert_many(
  ...     [{"_id": 1, "a": 1}, {"_id": 2, "b": 1}, {"_id": 3, "c": 1}, {"_id": 4, "d": 1}]
  ... )
  >>> replica_db = client.replica_db
  >>> for doc in db.test.find():
  ...     print(f"raw document: {doc.raw}")
  ...     print(f"decoded document: {bson.decode(doc.raw)}")
  ...     result = replica_db.test.insert_one(doc)
  ...
  raw document: b'...'
  decoded document: {'_id': 1, 'a': 1}
  raw document: b'...'
  decoded document: {'_id': 2, 'b': 1}
  raw document: b'...'
  decoded document: {'_id': 3, 'c': 1}
  raw document: b'...'
  decoded document: {'_id': 4, 'd': 1}

For use cases like moving documents across different databases or writing binary
blobs to disk, using raw BSON documents provides better speed and avoids the
overhead of decoding or encoding BSON.
"""

from __future__ import annotations

import copyreg
import struct
from collections.abc import ItemsView, Iterable, Iterator, Mapping, Sequence
from typing import Any

from bson import _element_to_dict, _get_object_size, _raw_as_bytes, _raw_to_dict
from bson.codec_options import _RAW_BSON_DOCUMENT_MARKER, CodecOptions
from bson.codec_options import DEFAULT_CODEC_OPTIONS as DEFAULT
from bson.errors import BSONPathNotFound, BSONPathTypeError, InvalidBSONPath

_Path = tuple[str | int, ...]

# BSON element type bytes used by the selective path walker.
_TYPE_DOUBLE = 0x01
_TYPE_STRING = 0x02
_TYPE_DOCUMENT = 0x03
_TYPE_ARRAY = 0x04
_TYPE_BINARY = 0x05
_TYPE_UNDEFINED = 0x06
_TYPE_OBJECTID = 0x07
_TYPE_BOOLEAN = 0x08
_TYPE_DATETIME = 0x09
_TYPE_NULL = 0x0A
_TYPE_REGEX = 0x0B
_TYPE_DBPOINTER = 0x0C
_TYPE_CODE = 0x0D
_TYPE_SYMBOL = 0x0E
_TYPE_CODE_W_SCOPE = 0x0F
_TYPE_INT32 = 0x10
_TYPE_TIMESTAMP = 0x11
_TYPE_INT64 = 0x12
_TYPE_DECIMAL128 = 0x13
_TYPE_MINKEY = 0xFF
_TYPE_MAXKEY = 0x7F

_FIXED_SIZES = {
    _TYPE_DOUBLE: 8,
    _TYPE_OBJECTID: 12,
    _TYPE_BOOLEAN: 1,
    _TYPE_DATETIME: 8,
    _TYPE_UNDEFINED: 0,
    _TYPE_NULL: 0,
    _TYPE_INT32: 4,
    _TYPE_TIMESTAMP: 8,
    _TYPE_INT64: 8,
    _TYPE_DECIMAL128: 16,
    _TYPE_MINKEY: 0,
    _TYPE_MAXKEY: 0,
}

_TYPE_NAMES = {
    _TYPE_DOUBLE: "double",
    _TYPE_STRING: "string",
    _TYPE_DOCUMENT: "document",
    _TYPE_ARRAY: "array",
    _TYPE_BINARY: "binary",
    _TYPE_UNDEFINED: "undefined",
    _TYPE_OBJECTID: "objectId",
    _TYPE_BOOLEAN: "boolean",
    _TYPE_DATETIME: "datetime",
    _TYPE_NULL: "null",
    _TYPE_REGEX: "regex",
    _TYPE_DBPOINTER: "dbpointer",
    _TYPE_CODE: "code",
    _TYPE_SYMBOL: "symbol",
    _TYPE_CODE_W_SCOPE: "code_w_scope",
    _TYPE_INT32: "int32",
    _TYPE_TIMESTAMP: "timestamp",
    _TYPE_INT64: "int64",
    _TYPE_DECIMAL128: "decimal128",
    _TYPE_MINKEY: "minKey",
    _TYPE_MAXKEY: "maxKey",
}

_UNPACK_INT = struct.Struct("<i").unpack_from


def _as_view(raw: bytes | memoryview) -> tuple[Any, memoryview]:
    """Return a buffer accepted by the path walker together with its view."""
    if isinstance(raw, (bytes, bytearray)):
        return raw, memoryview(raw)
    view = memoryview(raw)
    if not view.c_contiguous:
        data = view.tobytes()
        return data, memoryview(data)
    if view.format != "B" or view.itemsize != 1:
        view = view.cast("B")
        return view, view
    return raw, view


def _path_text(path: _Path) -> str:
    return ".".join(str(segment) for segment in path) or "<root>"


def _segment_at(path: _Path, segment_index: int) -> str | int | None:
    if 0 <= segment_index < len(path):
        return path[segment_index]
    return None


def _segment_index_at(path: _Path, segment_index: int) -> int | None:
    if 0 <= segment_index < len(path):
        return segment_index
    return None


def _normalize_path(path: str | int | Sequence[str | int]) -> _Path:
    if isinstance(path, (str, int)):
        segments: tuple[str | int, ...] = (path,)
    else:
        segments = tuple(path)
    if not segments:
        raise ValueError("path must contain at least one segment")
    for segment in segments:
        if isinstance(segment, bool) or not isinstance(segment, (str, int)):
            raise TypeError(f"path segments must be str or int, got {segment!r}")
    return segments


def _iter_requested_paths(paths: Any) -> Iterator[Any]:
    """Yield individual paths from either a single path or a batch."""
    if isinstance(paths, (str, int)):
        yield paths
        return
    items: list[Any] = list(paths)
    if not items:
        return
    if all(isinstance(item, (str, int)) for item in items):
        # A single path given directly as a segment sequence.
        yield items
        return
    yield from items


def _find_nul(view: Any, start: int, limit: int, path: _Path, segment_index: int) -> int:
    finder = getattr(view, "find", None)
    if finder is not None:
        found = finder(b"\x00", start, limit)
    else:
        found = -1
        for index in range(start, limit):
            if view[index] == 0:
                found = index
                break
    if found < 0:
        raise InvalidBSONPath(
            f"cannot locate the end of a field name for path {_path_text(path)!r} "
            f"at byte {start}: no NUL before byte {limit}",
            path=path,
            segment=_segment_at(path, segment_index),
            segment_index=_segment_index_at(path, segment_index),
            byte_offset=start,
            reason="field-name boundary is not within the document",
        )
    return found


def _int32_at(data: Any, position: int, path: _Path, segment_index: int) -> int:
    try:
        value = _UNPACK_INT(data, position)[0]
    except struct.error as exc:
        raise InvalidBSONPath(
            f"cannot read a length for path {_path_text(path)!r} at byte {position}: {exc}",
            path=path,
            segment=_segment_at(path, segment_index),
            segment_index=_segment_index_at(path, segment_index),
            byte_offset=position,
            reason="insufficient bytes for a length field",
        ) from None
    if value < 0:
        raise InvalidBSONPath(
            f"negative length for path {_path_text(path)!r} at byte {position}",
            path=path,
            segment=_segment_at(path, segment_index),
            segment_index=_segment_index_at(path, segment_index),
            byte_offset=position,
            reason="negative length",
        )
    return value


def _skip_value(
    data: Any,
    view: Any,
    type_byte: int,
    position: int,
    container_end: int,
    path: _Path,
    segment_index: int,
) -> int:
    """Return the end of an element's value without constructing the value.

    ``container_end`` is the offset of the enclosing document's terminator.
    The returned offset is exclusive and must be at most ``container_end``.
    Only the information needed to find the next element is validated.
    """
    fixed = _FIXED_SIZES.get(type_byte)
    if fixed is not None:
        end = position + fixed
    elif type_byte in (_TYPE_STRING, _TYPE_SYMBOL, _TYPE_CODE):
        end = position + 4 + _int32_at(data, position, path, segment_index)
    elif type_byte in (_TYPE_DOCUMENT, _TYPE_ARRAY):
        size = _int32_at(data, position, path, segment_index)
        if size < 5:
            raise InvalidBSONPath(
                f"invalid {_TYPE_NAMES[type_byte]} size {size} for path "
                f"{_path_text(path)!r} at byte {position}",
                path=path,
                segment=_segment_at(path, segment_index),
                segment_index=_segment_index_at(path, segment_index),
                byte_offset=position,
                reason="invalid container size",
            )
        end = position + size
    elif type_byte == _TYPE_BINARY:
        length = _int32_at(data, position, path, segment_index)
        end = position + 5 + length
    elif type_byte == _TYPE_REGEX:
        first = _find_nul(view, position, container_end, path, segment_index)
        second = _find_nul(view, first + 1, container_end, path, segment_index)
        end = second + 1
    elif type_byte == _TYPE_DBPOINTER:
        length = _int32_at(data, position, path, segment_index)
        end = position + 4 + length + 12
    elif type_byte == _TYPE_CODE_W_SCOPE:
        size = _int32_at(data, position, path, segment_index)
        if size < 14:
            raise InvalidBSONPath(
                f"invalid code_w_scope size {size} for path {_path_text(path)!r} "
                f"at byte {position}",
                path=path,
                segment=_segment_at(path, segment_index),
                segment_index=_segment_index_at(path, segment_index),
                byte_offset=position,
                reason="invalid code_w_scope size",
            )
        end = position + size
    else:
        raise InvalidBSONPath(
            f"cannot determine the length of BSON type 0x{type_byte:02x} for path "
            f"{_path_text(path)!r} at byte {position}",
            path=path,
            segment=_segment_at(path, segment_index),
            segment_index=_segment_index_at(path, segment_index),
            byte_offset=position,
            reason="unknown BSON type",
        )

    if end > container_end:
        raise InvalidBSONPath(
            f"the enclosing size for path {_path_text(path)!r} ends at byte "
            f"{container_end}, but the field at byte {position} ends at byte {end}",
            path=path,
            segment=_segment_at(path, segment_index),
            segment_index=_segment_index_at(path, segment_index),
            byte_offset=position,
            reason="insufficient enclosing bounds to locate the next field",
        )
    return end


def _iter_elements(
    data: Any,
    view: Any,
    start: int,
    container_end: int,
    path: _Path,
    segment_index: int,
) -> Iterator[tuple[int, int, int, int, int]]:
    """Yield (type, type_offset, key_start, key_end, value_end) for a document."""
    position = start
    while position < container_end:
        type_offset = position
        type_byte = data[position]
        key_start = position + 1
        key_end = _find_nul(view, key_start, container_end, path, segment_index)
        value_start = key_end + 1
        value_end = _skip_value(
            data, view, type_byte, value_start, container_end, path, segment_index
        )
        yield type_byte, type_offset, key_start, key_end, value_end
        position = value_end
    if position != container_end:
        raise InvalidBSONPath(
            f"field boundaries for path {_path_text(path)!r} end at byte {position}, "
            f"not at the enclosing terminator {container_end}",
            path=path,
            segment=_segment_at(path, segment_index),
            segment_index=_segment_index_at(path, segment_index),
            byte_offset=position,
            reason="field boundaries do not reach the document terminator",
        )


def _array_elements(
    data: Any,
    view: Any,
    start: int,
    container_end: int,
    path: _Path,
    segment_index: int,
) -> Iterator[tuple[int, int, int]]:
    """Yield (type, type_offset, value_end) for array elements by position."""
    position = start
    while position < container_end:
        type_offset = position
        type_byte = data[position]
        key_start = position + 1
        key_end = _find_nul(view, key_start, container_end, path, segment_index)
        value_start = key_end + 1
        value_end = _skip_value(
            data, view, type_byte, value_start, container_end, path, segment_index
        )
        yield type_byte, type_offset, value_end
        position = value_end
    if position != container_end:
        raise InvalidBSONPath(
            f"array boundaries for path {_path_text(path)!r} end at byte {position}, "
            f"not at the enclosing terminator {container_end}",
            path=path,
            segment=_segment_at(path, segment_index),
            segment_index=_segment_index_at(path, segment_index),
            byte_offset=position,
            reason="array boundaries do not reach the terminator",
        )


def _decode_element(
    data: Any,
    view: Any,
    type_offset: int,
    value_end: int,
    codec_options: CodecOptions[Any],
) -> Any:
    """Decode one already located element with the document's codec options."""
    if isinstance(data, bytes):
        return _element_to_dict(data, view, type_offset, value_end, codec_options)[1]
    # The C implementation of _element_to_dict only accepts bytes. Copy only
    # the selected element instead of the whole surrounding document.
    element = bytes(data[type_offset:value_end])
    element_view = memoryview(element)
    return _element_to_dict(element, element_view, 0, len(element), codec_options)[1]


def _locate_in_document(
    data: Any,
    view: Any,
    start: int,
    container_end: int,
    key: bytes,
    path: _Path,
    segment_index: int,
) -> tuple[int, int, int] | None:
    for type_byte, type_offset, key_start, key_end, value_end in _iter_elements(
        data, view, start, container_end, path, segment_index
    ):
        if data[key_start:key_end] == key:
            return type_byte, type_offset, value_end
    return None


def _locate_in_array(
    data: Any,
    view: Any,
    start: int,
    container_end: int,
    index: int,
    path: _Path,
    segment_index: int,
) -> tuple[int, int, int] | None:
    for ordinal, (type_byte, type_offset, value_end) in enumerate(
        _array_elements(data, view, start, container_end, path, segment_index)
    ):
        if ordinal == index:
            return type_byte, type_offset, value_end
    return None


def _validate_container(data: Any, value_end: int, path: _Path, segment_index: int) -> None:
    """Confirm a traversed container ends with its terminator."""
    if data[value_end - 1] != 0:
        raise InvalidBSONPath(
            f"the container for path {_path_text(path)!r} does not end with a "
            f"terminator at byte {value_end - 1}",
            path=path,
            segment=_segment_at(path, segment_index),
            segment_index=_segment_index_at(path, segment_index),
            byte_offset=value_end - 1,
            reason="container terminator is not within its declared bounds",
        )


def _resolve_path(
    data: Any,
    view: Any,
    root_start: int,
    root_end: int,
    path: _Path,
    codec_options: CodecOptions[Any],
    checked: tuple[_Path, ...],
) -> Any:
    container_type = _TYPE_DOCUMENT
    start = root_start
    container_end = root_end
    for index, segment in enumerate(path):
        if container_type == _TYPE_DOCUMENT:
            if not isinstance(segment, str):
                raise BSONPathTypeError(
                    f"path {_path_text(path)!r} uses index {segment!r} at segment "
                    f"{index} for an embedded document",
                    path=path,
                    checked_paths=checked,
                    segment=segment,
                    segment_index=index,
                    byte_offset=start,
                    reason="array index used for an embedded document",
                )
            found = _locate_in_document(
                data,
                view,
                start,
                container_end,
                segment.encode("utf-8"),
                path,
                index,
            )
        else:
            if not isinstance(segment, int) or isinstance(segment, bool):
                raise BSONPathTypeError(
                    f"path {_path_text(path)!r} uses key {segment!r} at segment "
                    f"{index} for an array",
                    path=path,
                    checked_paths=checked,
                    segment=segment,
                    segment_index=index,
                    byte_offset=start,
                    reason="document key used for an array",
                )
            found = _locate_in_array(data, view, start, container_end, segment, path, index)

        if found is None:
            raise BSONPathNotFound(
                f"path {_path_text(path)!r} was not found at segment {index} ({segment!r})",
                path=path,
                checked_paths=checked,
                segment=segment,
                segment_index=index,
            )
        type_byte, type_offset, value_end = found
        if index == len(path) - 1:
            return _decode_element(
                data,
                view,
                type_offset,
                value_end,
                codec_options,
            )
        if type_byte not in (_TYPE_DOCUMENT, _TYPE_ARRAY):
            raise BSONPathTypeError(
                f"path {_path_text(path)!r} cannot descend through segment "
                f"{index} ({segment!r}) because its value is "
                f"{_TYPE_NAMES.get(type_byte, f'0x{type_byte:02x}')}",
                path=path,
                checked_paths=checked,
                segment=segment,
                segment_index=index,
                byte_offset=type_offset,
                reason="value is not a document or array",
            )
        key_start = type_offset + 1
        key_end = _find_nul(view, key_start, container_end, path, index)
        child_start = key_end + 1
        _validate_container(data, value_end, path, index)
        # Skip the child's own int32 length prefix.
        start = child_start + 4
        container_end = value_end - 1
        container_type = type_byte

    raise AssertionError("unreachable")


class _RawBSONSelection(Mapping[_Path, Any]):
    """Values read by :meth:`RawBSONDocument.select_paths`.

    ``checked_paths`` lists only the requested paths that were resolved.
    ``complete`` is always ``False``: values were obtained without decoding
    or validating fields that are not on one of those paths.

    .. versionadded:: 4.21
    """

    __slots__ = ("_checked_paths", "_values")

    complete = False
    """Whether the entire document was decoded and validated. Always ``False``."""

    document_complete = False
    """Alias for :attr:`complete`."""

    def __init__(self, values: dict[_Path, Any], checked_paths: tuple[_Path, ...]) -> None:
        self._values = values
        self._checked_paths = checked_paths

    @property
    def checked_paths(self) -> tuple[_Path, ...]:
        """The requested paths that were resolved, in request order."""
        return self._checked_paths

    @property
    def values_by_path(self) -> dict[_Path, Any]:
        """A copy of the resolved values keyed by their path tuple."""
        return dict(self._values)

    def as_dict(self) -> dict[_Path, Any]:
        """Return the resolved values keyed by their path tuple."""
        return dict(self._values)

    def __getitem__(self, path: str | int | Sequence[str | int]) -> Any:
        return self._values[_normalize_path(path)]

    def __iter__(self) -> Iterator[_Path]:
        return iter(self._checked_paths)

    def __len__(self) -> int:
        return len(self._checked_paths)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(checked_paths={self._checked_paths!r}, complete=False)"


def _inflate_bson(
    bson_bytes: bytes | memoryview,
    codec_options: CodecOptions[RawBSONDocument],
    raw_array: bool = False,
) -> dict[str, Any]:
    """Inflates the top level fields of a BSON document.

    :param bson_bytes: the BSON bytes that compose this document
    :param codec_options: An instance of
        :class:`~bson.codec_options.CodecOptions` whose ``document_class``
        must be :class:`RawBSONDocument`.
    """
    return _raw_to_dict(bson_bytes, 4, len(bson_bytes) - 1, codec_options, {}, raw_array=raw_array)


class RawBSONDocument(Mapping[str, Any]):
    """Representation of a MongoDB document that exposes its raw BSON bytes.

    Only when a field is accessed or modified within the document does
    RawBSONDocument decode its bytes.
    """

    __slots__ = ("__codec_options", "__inflated_doc", "__raw")
    _type_marker = _RAW_BSON_DOCUMENT_MARKER
    __codec_options: CodecOptions[RawBSONDocument]

    def __init__(
        self,
        bson_bytes: bytes | memoryview,
        codec_options: CodecOptions[RawBSONDocument] | None = None,
    ) -> None:
        """Create a new :class:`RawBSONDocument`.

        :class:`RawBSONDocument` is a representation of a BSON document that
        provides access to the underlying raw BSON bytes. Only when a field is
        accessed or modified within the document does RawBSONDocument decode
        its bytes.

        :class:`RawBSONDocument` implements the ``Mapping`` abstract base
        class from the standard library so it can be used like a read-only
        ``dict``::

            >>> from bson import encode
            >>> raw_doc = RawBSONDocument(encode({'_id': 'my_doc'}))
            >>> raw_doc.raw
            b'...'
            >>> raw_doc['_id']
            'my_doc'

        :param bson_bytes: the BSON bytes that compose this document
        :param codec_options: An instance of
            :class:`~bson.codec_options.CodecOptions` whose ``document_class``
            must be :class:`RawBSONDocument`. The default is
            :attr:`DEFAULT_RAW_BSON_OPTIONS`.

        .. versionchanged:: 3.8
          :class:`RawBSONDocument` now validates that the ``bson_bytes``
          passed in represent a single bson document.

        .. versionchanged:: 3.5
          If a :class:`~bson.codec_options.CodecOptions` is passed in, its
          `document_class` must be :class:`RawBSONDocument`.
        """
        self.__raw = bson_bytes
        self.__inflated_doc: Mapping[str, Any] | None = None
        # Can't default codec_options to DEFAULT_RAW_BSON_OPTIONS in signature,
        # it refers to this class RawBSONDocument.
        if codec_options is None:
            codec_options = DEFAULT_RAW_BSON_OPTIONS
        elif not issubclass(codec_options.document_class, RawBSONDocument):
            raise TypeError(
                "RawBSONDocument cannot use CodecOptions with document "
                f"class {codec_options.document_class}"
            )
        self.__codec_options = codec_options
        # Validate the bson object size.
        _get_object_size(bson_bytes, 0, len(bson_bytes))

    @property
    def raw(self) -> bytes | memoryview:
        """The raw BSON bytes composing this document.

        .. versionchanged:: 4.18
           Documents and subdocuments 4KB and larger decoded from an
           immutable buffer are returned as read-only :class:`memoryview`
           slices of that buffer instead of :class:`bytes` copies. Documents
           decoded from mutable buffers such as :class:`bytearray` are
           always :class:`bytes` copies. Call ``bytes(doc.raw)``
           to get an independent copy.
        """
        return self.__raw

    def items(self) -> ItemsView[str, Any]:
        """Lazily decode and iterate elements in this document."""
        return self.__inflated.items()

    @property
    def __inflated(self) -> Mapping[str, Any]:
        if self.__inflated_doc is None:
            # We already validated the object's size when this document was
            # created, so no need to do that again.
            self.__inflated_doc = self._inflate_bson(self.__raw, self.__codec_options)
        return self.__inflated_doc

    @staticmethod
    def _inflate_bson(
        bson_bytes: bytes | memoryview, codec_options: CodecOptions[RawBSONDocument]
    ) -> Mapping[str, Any]:
        return _inflate_bson(bson_bytes, codec_options)

    def __getitem__(self, item: str) -> Any:
        return self.__inflated[item]

    def select_paths(
        self, paths: Iterable[Sequence[str | int] | str | int] | Sequence[str | int] | str | int
    ) -> _RawBSONSelection:
        """Read values at explicit paths without decoding other fields.

        ``paths`` may be a batch of paths (each a sequence of segments) or a
        single path. A :class:`str` segment selects a document key by its
        exact, literal bytes; an :class:`int` segment selects an array element
        by position. The document's own ``codec_options`` decode the selected
        values, including any custom type decoders.

        Fields outside the requested paths are skipped using only their
        declared outer lengths. Their values are never constructed, so an
        unselected value that is invalid under the normal full-read rules
        does not prevent the lookup from succeeding. A successful result is
        therefore a partial view: :attr:`_RawBSONSelection.checked_paths`
        lists the paths that were resolved and
        :attr:`_RawBSONSelection.complete` is always ``False``.

        The full mapping interface is not affected. Accessing this document
        through a mapping operation still inflates and validates every field
        using the normal rules; cached mapping state is never populated from
        a selective lookup.

        :param paths: Paths to resolve.
        :raises BSONPathNotFound: If a path does not exist.
        :raises BSONPathTypeError: If a segment does not match the container
            encountered (for example an array index against an embedded
            document).
        :raises InvalidBSONPath: If the enclosing size does not safely bound
            the next element. The exception carries ``path``,
            ``checked_paths``, ``segment``, ``segment_index`` and
            ``byte_offset``.
        :raises InvalidBSON: If a selected value is itself invalid under the
            normal rules.

        .. versionadded:: 4.21
        """
        raw, view = _as_view(self.__raw)
        root_end = len(raw) - 1
        values: dict[_Path, Any] = {}
        checked: list[_Path] = []
        for path in _iter_requested_paths(paths):
            normalized = _normalize_path(path)
            value = _resolve_path(
                raw,
                view,
                4,
                root_end,
                normalized,
                self.__codec_options,
                tuple(checked),
            )
            values[normalized] = value
            checked.append(normalized)
        return _RawBSONSelection(values, tuple(checked))

    def get_path(self, path: Sequence[str | int] | str | int) -> Any:
        """Return the value at a single explicit path.

        Shorthand for :meth:`select_paths` with one path.

        .. versionadded:: 4.21
        """
        return self.select_paths((path,)).as_dict()[_normalize_path(path)]

    def __iter__(self) -> Iterator[str]:
        return iter(self.__inflated)

    def __len__(self) -> int:
        return len(self.__inflated)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, RawBSONDocument):
            return self.__raw == other.raw
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def __getstate__(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        slots_state: dict[str, Any] = {
            name: getattr(self, name)
            for name in copyreg._slotnames(type(self))  # type: ignore[attr-defined]
            if hasattr(self, name)
        }
        slots_state["_RawBSONDocument__raw"] = _raw_as_bytes(self.__raw)
        slots_state["_RawBSONDocument__inflated_doc"] = None
        return getattr(self, "__dict__", None), slots_state

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}({_raw_as_bytes(self.__raw)!r}, "
            f"codec_options={self.__codec_options!r})"
        )


class _RawArrayBSONDocument(RawBSONDocument):
    """A RawBSONDocument that only expands sub-documents and arrays when accessed."""

    @staticmethod
    def _inflate_bson(
        bson_bytes: bytes | memoryview, codec_options: CodecOptions[RawBSONDocument]
    ) -> Mapping[str, Any]:
        return _inflate_bson(bson_bytes, codec_options, raw_array=True)


DEFAULT_RAW_BSON_OPTIONS: CodecOptions[RawBSONDocument] = DEFAULT.with_options(
    document_class=RawBSONDocument
)
_RAW_ARRAY_BSON_OPTIONS: CodecOptions[_RawArrayBSONDocument] = DEFAULT.with_options(
    document_class=_RawArrayBSONDocument
)
"""The default :class:`~bson.codec_options.CodecOptions` for
:class:`RawBSONDocument`.
"""
