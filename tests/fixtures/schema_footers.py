"""Helpers that build legal, data-free Parquet files for footer-diff tests.

Only the footer is meaningful; row_groups is empty, so these files contain no
data pages.  The schema elements are written exactly as supplied, which lets
tests exercise both legal LIST layouts and deliberately awkward identities.
"""

from __future__ import annotations

import struct
from typing import List, Optional, Sequence

from parquet_tools.gen_py.parquet.ttypes import (
    FileMetaData,
    SchemaElement,
)
from thrift.protocol import TCompactProtocol
from thrift.transport import TTransport


def element(
    name: str,
    *,
    physical_type: Optional[int] = None,
    repetition: Optional[int] = None,
    num_children: Optional[int] = None,
    converted_type: Optional[int] = None,
    logical_list: bool = False,
    field_id: Optional[int] = None,
    type_length: Optional[int] = None,
    scale: Optional[int] = None,
    precision: Optional[int] = None,
) -> SchemaElement:
    from parquet_tools.gen_py.parquet.ttypes import ListType, LogicalType

    logical = LogicalType(LIST=ListType()) if logical_list else None
    return SchemaElement(
        type=physical_type,
        type_length=type_length,
        repetition_type=repetition,
        name=name,
        num_children=num_children,
        converted_type=converted_type,
        scale=scale,
        precision=precision,
        field_id=field_id,
        logicalType=logical,
    )


def root(name: str, children: int) -> SchemaElement:
    return SchemaElement(
        type=None,
        type_length=None,
        repetition_type=None,
        name=name,
        num_children=children,
        converted_type=None,
        scale=None,
        precision=None,
        field_id=None,
        logicalType=None,
    )


def write_footer(path: str, schema: Sequence[SchemaElement]) -> str:
    metadata = FileMetaData(
        version=2,
        schema=list(schema),
        num_rows=0,
        row_groups=[],
        key_value_metadata=None,
        created_by="footer-diff-fixture",
        column_orders=None,
        encryption_algorithm=None,
        footer_signing_key_metadata=None,
    )
    buffer = TTransport.TMemoryBuffer()
    metadata.write(TCompactProtocol.TCompactProtocol(buffer))
    payload = buffer.getvalue()
    with open(path, "wb") as handle:
        handle.write(b"PAR1")
        handle.write(payload)
        handle.write(struct.pack("<i", len(payload)))
        handle.write(b"PAR1")
    return path


def list_three_level(
    name: str,
    element_name: str = "element",
    *,
    element_type: int,
    element_repetition: int,
    list_field_id: Optional[int] = None,
    element_field_id: Optional[int] = None,
    wrapper_name: str = "list",
    wrapper_field_id: Optional[int] = None,
    list_repetition: int = 1,
) -> List[SchemaElement]:
    return [
        element(
            name,
            repetition=list_repetition,
            num_children=1,
            converted_type=3,
            logical_list=True,
            field_id=list_field_id,
        ),
        element(
            wrapper_name,
            repetition=2,
            num_children=1,
            field_id=wrapper_field_id,
        ),
        element(
            element_name,
            physical_type=element_type,
            repetition=element_repetition,
            converted_type=0 if element_type == 6 else None,
            field_id=element_field_id,
        ),
    ]


def list_two_level(
    name: str,
    element_name: str = "element",
    *,
    element_type: int,
    list_field_id: Optional[int] = None,
    element_field_id: Optional[int] = None,
    list_repetition: int = 1,
) -> List[SchemaElement]:
    return [
        element(
            name,
            repetition=list_repetition,
            num_children=1,
            converted_type=3,
            logical_list=True,
            field_id=list_field_id,
        ),
        element(
            element_name,
            physical_type=element_type,
            repetition=2,
            converted_type=0 if element_type == 6 else None,
            field_id=element_field_id,
        ),
    ]
