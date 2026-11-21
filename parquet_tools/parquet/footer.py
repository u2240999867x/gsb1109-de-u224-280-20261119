"""Byte-range planning for parquet footers.

Parquet stores a plaintext footer as a compact-thrift struct followed by an
8 byte trailer (``footer_length`` as little-endian int32 and ``PAR1``).  To
render the simple report without downloading data pages we only need the
leading part of the footer up through ``created_by`` (thrift field 6); the
detailed report needs the whole footer.

This module scans the thrift field boundaries of whatever footer prefix is
available.  It reads thrift primitives into bytes rather than strings so a
partially received footer never raises decode errors.
"""

import struct
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from thrift.protocol import TCompactProtocol
from thrift.protocol.TProtocol import TProtocolException
from thrift.Thrift import TType
from thrift.transport import TTransport

MAGIC = b'PAR1'
TRAILER_SIZE = 8

# thrift field ids in parquet's FileMetaData
FIELD_VERSION = 1
FIELD_SCHEMA = 2
FIELD_NUM_ROWS = 3
FIELD_ROW_GROUPS = 4
FIELD_KEY_VALUE_METADATA = 5
FIELD_CREATED_BY = 6
FIELD_COLUMN_ORDERS = 7

SIMPLE_FIELDS = (FIELD_VERSION, FIELD_SCHEMA, FIELD_NUM_ROWS, FIELD_ROW_GROUPS)


class FooterError(Exception):
    pass


@dataclass(frozen=True)
class FooterLayout:
    prefix_end: int
    have_simple_fields: bool
    complete: bool
    field_offsets: Dict[int, Tuple[int, int]]
    consumed: int = 0

    @property
    def needed_end(self) -> int:
        """Bytes of footer required for the simple report."""
        if not self.have_simple_fields:
            return 0
        return self.prefix_end

    def is_complete_footer(self, footer_length: int) -> bool:
        """A complete footer consumes exactly its declared length."""
        return self.complete and self.consumed == footer_length


class _TrackingBuffer(TTransport.TMemoryBuffer):
    def __init__(self, value: bytes):
        super().__init__(value)
        self.offset = 0

    def read(self, size):
        data = super().read(size)
        self.offset += len(data)
        return data


def _skip(protocol, ttype) -> None:
    if ttype == TType.BOOL:
        protocol.readBool()
    elif ttype == TType.BYTE:
        protocol.readByte()
    elif ttype == TType.I16:
        protocol.readI16()
    elif ttype == TType.I32:
        protocol.readI32()
    elif ttype == TType.I64:
        protocol.readI64()
    elif ttype == TType.DOUBLE:
        protocol.readDouble()
    elif ttype == TType.STRING:
        protocol.readBinary()
    elif ttype == TType.STRUCT:
        protocol.readStructBegin()
        while True:
            _, field_type, _ = protocol.readFieldBegin()
            if field_type == TType.STOP:
                break
            _skip(protocol, field_type)
            protocol.readFieldEnd()
        protocol.readStructEnd()
    elif ttype == TType.MAP:
        key_type, value_type, size = protocol.readMapBegin()
        for _ in range(size):
            _skip(protocol, key_type)
            _skip(protocol, value_type)
        protocol.readMapEnd()
    elif ttype == TType.SET:
        element_type, size = protocol.readSetBegin()
        for _ in range(size):
            _skip(protocol, element_type)
        protocol.readSetEnd()
    elif ttype == TType.LIST:
        element_type, size = protocol.readListBegin()
        for _ in range(size):
            _skip(protocol, element_type)
        protocol.readListEnd()
    else:
        raise FooterError(f'unsupported thrift type {ttype}')


def scan_footer(footer_prefix: bytes, footer_length: Optional[int] = None) -> FooterLayout:
    """Scan as many top-level fields as ``footer_prefix`` fully contains.

    ``footer_prefix`` starts at the first footer byte (immediately after the
    data pages).  The scan never decodes binary fields as UTF-8, so binary
    thrift payloads are safe.  It stops at the first truncated field.
    """
    transport = _TrackingBuffer(footer_prefix)
    protocol = TCompactProtocol.TCompactProtocol(transport)
    offsets: Dict[int, Tuple[int, int]] = {}
    complete = False
    try:
        protocol.readStructBegin()
        while True:
            start = transport.offset
            _, field_type, field_id = protocol.readFieldBegin()
            if field_type == TType.STOP:
                complete = True
                break
            _skip(protocol, field_type)
            protocol.readFieldEnd()
            offsets[field_id] = (start, transport.offset)
    except (EOFError, TProtocolException, FooterError, struct.error, ValueError, AssertionError):
        pass

    have_simple = all(field_id in offsets for field_id in SIMPLE_FIELDS)
    if FIELD_CREATED_BY in offsets:
        prefix_end = offsets[FIELD_CREATED_BY][1]
    elif have_simple:
        prefix_end = offsets[FIELD_ROW_GROUPS][1]
    else:
        prefix_end = 0
    return FooterLayout(
        prefix_end=prefix_end,
        have_simple_fields=have_simple,
        complete=complete,
        field_offsets=offsets,
        consumed=transport.offset,
    )


def footer_length_from_trailer(trailer: bytes) -> int:
    if len(trailer) != TRAILER_SIZE:
        raise FooterError('trailer must be 8 bytes')
    if trailer[4:] != MAGIC:
        raise FooterError('missing parquet magic in trailer')
    length = struct.unpack('<i', trailer[:4])[0]
    if length < 0:
        raise FooterError('negative footer length')
    return length


def trailer_for(footer_length: int) -> bytes:
    return struct.pack('<i', footer_length) + MAGIC


def verify_header(header: bytes) -> None:
    if header != MAGIC:
        raise FooterError('missing parquet magic at start of object')
