"""Read-only Parquet footer loading and canonical schema parsing."""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass
from typing import Optional

from parquet_tools.gen_py.parquet.ttypes import FileMetaData
from parquet_tools.parquet.reader import _read_footer
from parquet_tools.schema_diff.model import SchemaTree, parse_schema

PARQUET_MAGIC = b"PAR1"


class FooterReadError(Exception):
    """Raised when a file is not a readable Parquet footer."""


@dataclass
class FooterDocument:
    path: str
    metadata: FileMetaData
    footer_bytes: bytes
    footer_size: int
    footer_sha256: str
    file_size: int
    tree: SchemaTree

    def summary(self) -> dict:
        metadata = self.metadata
        return {
            "path": self.path,
            "file_size": self.file_size,
            "footer_size": self.footer_size,
            "footer_sha256": self.footer_sha256,
            "num_rows": metadata.num_rows,
            "num_row_groups": len(metadata.row_groups or []),
            "created_by": metadata.created_by,
            "format_version": metadata.version,
            "schema_root": metadata.schema[0].name if metadata.schema else None,
            "logical_field_count": len(self.tree.nodes),
            "logical_leaf_count": len(self.tree.leaves),
        }


def _read_footer_bytes(path: str) -> bytes:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if handle.read(4) != PARQUET_MAGIC:
                raise FooterReadError(
                    f"{path}: missing PAR1 header; not a Parquet file"
                )
            if size < 12:
                raise FooterReadError(f"{path}: file is too small to contain a footer")
            handle.seek(-8, os.SEEK_END)
            footer_length = struct.unpack("<i", handle.read(4))[0]
            trailer = handle.read(4)
            if trailer != PARQUET_MAGIC:
                raise FooterReadError(
                    f"{path}: missing PAR1 trailer; not a Parquet file"
                )
            if footer_length < 0 or footer_length > size - 12:
                raise FooterReadError(f"{path}: invalid footer length {footer_length}")
            handle.seek(-(8 + footer_length), os.SEEK_END)
            data = handle.read(footer_length)
            if len(data) != footer_length:
                raise FooterReadError(f"{path}: truncated footer")
            return data
    except OSError as error:
        raise FooterReadError(f"{path}: cannot read file: {error}") from error


def load_footer(path: str) -> FooterDocument:
    """Load only the Parquet footer bytes and decode its schema.

    Data pages are never opened or scanned.
    """
    footer_bytes = _read_footer_bytes(path)
    try:
        with open(path, "rb") as handle:
            metadata = _read_footer(handle)
    except Exception as error:  # thrift raises several unrelated exception types
        raise FooterReadError(f"{path}: cannot decode Parquet footer: {error}") from error
    try:
        tree = parse_schema(metadata.schema or [])
    except Exception as error:
        raise FooterReadError(f"{path}: invalid Parquet schema: {error}") from error
    return FooterDocument(
        path=path,
        metadata=metadata,
        footer_bytes=footer_bytes,
        footer_size=len(footer_bytes),
        footer_sha256=hashlib.sha256(footer_bytes).hexdigest(),
        file_size=os.path.getsize(path),
        tree=tree,
    )


def try_load_footer(path: str) -> tuple[Optional[FooterDocument], Optional[str]]:
    try:
        return load_footer(path), None
    except FooterReadError as error:
        return None, str(error)
