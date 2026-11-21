"""Build the corrupted-fixture family used by tests/test_corrupt_parquet.py.

The source file is intentionally tiny and written with dictionary encoding off
and compression off so every page starts with an explicit PLAIN data page
header.  Both corruptions are derived from the source bytes:

* ``data_page_corrupt.parquet`` keeps the source footer byte-for-byte and
  changes only the first four bytes of the first BYTE_ARRAY value in row group
  1 / column ``payload`` (the first value length is rewritten from 9 to 5).
* ``wrong_offset_same_page_boundary.parquet`` keeps every source data byte and
  rewrites only the footer's ``data_page_offset`` for row group 1 / column
  ``payload`` so it points at the row group 0 / column ``id`` page header.
* ``wrong_offset_mid_header.parquet`` is the same kind of footer-only edit, but
  the declared offset lands in the middle of that page header.  It is a
  control showing that a wrong offset can also escape the ArrowInvalid catch
  layer and surface as a page-header/thrift error.

The script verifies its own output from bytes and footer fields; it does not
rely on reader errors.  Run from the repository root:

    python tests/fixtures/corrupt/build_fixtures.py
"""

import os
import struct
import sys
import tempfile
from copy import deepcopy

import pyarrow as pa
import pyarrow.parquet as pq
from thrift.protocol import TCompactProtocol
from thrift.transport import TTransport

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from parquet_tools.gen_py.parquet.ttypes import PageHeader  # noqa: E402
from parquet_tools.parquet.reader import get_filemetadata  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "source.parquet")
NEIGHBOR = os.path.join(HERE, "neighbor.parquet")
DATA_PAGE = os.path.join(HERE, "data_page_corrupt.parquet")
WRONG_OFFSET = os.path.join(HERE, "wrong_offset_same_page_boundary.parquet")
WRONG_OFFSET_MID = os.path.join(HERE, "wrong_offset_mid_header.parquet")

SCHEMA = pa.schema([("id", pa.int64()), ("payload", pa.string())])
SOURCE_ROWS = {
    "id": list(range(8)),
    "payload": [f"p{index:02d}-alpha" for index in range(8)],
}
NEIGHBOR_ROWS = {
    "id": list(range(100, 108)),
    "payload": [f"n{index:02d}-omega" for index in range(8)],
}
TARGET_ROW_GROUP = 1
TARGET_COLUMN = 1


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as file:
        return file.read()


def split_footer(data: bytes):
    assert data[:4] == b"PAR1" and data[-4:] == b"PAR1", "missing parquet magic"
    footer_len = struct.unpack("<i", data[-8:-4])[0]
    footer_start = len(data) - 8 - footer_len
    assert 4 <= footer_start < len(data) - 8
    return footer_start, footer_len


def page_header_at(data: bytes, offset: int):
    transport = TTransport.TMemoryBuffer(data[offset:])
    header = PageHeader()
    header.read(TCompactProtocol.TCompactProtocol(transport))
    return header, transport._buffer.tell()


def write_source(path: str, rows: dict) -> None:
    table = pa.table(rows, schema=SCHEMA)
    pq.write_table(
        table,
        path,
        row_group_size=4,
        compression="NONE",
        use_dictionary=False,
        data_page_version="1.0",
    )


def metadata(path: str):
    return get_filemetadata(path)


def target_meta(fmd):
    return fmd.row_groups[TARGET_ROW_GROUP].columns[TARGET_COLUMN].meta_data


def target_layout(source: bytes, fmd):
    target = target_meta(fmd)
    header, header_len = page_header_at(source, target.data_page_offset)
    payload_start = target.data_page_offset + header_len
    definition_len = struct.unpack("<i", source[payload_start:payload_start + 4])[0]
    values_start = payload_start + 4 + definition_len
    first_value_len = struct.unpack("<i", source[values_start:values_start + 4])[0]
    return target, header, definition_len, values_start, first_value_len


def build_data_page_corrupt(source: bytes, fmd) -> bytes:
    target, header, definition_len, values_start, first_value_len = target_layout(source, fmd)
    assert target.path_in_schema == ["payload"]
    assert header.type == 0  # DATA_PAGE
    assert definition_len == 2
    assert first_value_len == len(SOURCE_ROWS["payload"][4])
    assert source[values_start + 4:values_start + 4 + first_value_len] == b"p04-alpha"

    bad = bytearray(source)
    bad[values_start:values_start + 4] = struct.pack("<i", 5)
    footer_start, _ = split_footer(source)
    assert bytes(bad[footer_start:]) == source[footer_start:]
    changed = [index for index, (left, right) in enumerate(zip(bad, source)) if left != right]
    assert changed == [values_start], changed
    return bytes(bad)


def rewrite_footer(source: bytes, fmd, offset: int) -> bytes:
    footer_start, _ = split_footer(source)
    edited = deepcopy(fmd)
    target_meta(edited).data_page_offset = offset
    transport = TTransport.TMemoryBuffer()
    edited.write(TCompactProtocol.TCompactProtocol(transport))
    footer = transport.getvalue()
    bad = source[:footer_start] + footer + struct.pack("<i", len(footer)) + b"PAR1"
    assert bad[:footer_start] == source[:footer_start]
    return bad


def verify_footer_only_change(source: bytes, fmd, bad: bytes, offset: int) -> None:
    footer_start, _ = split_footer(source)
    assert bad[:footer_start] == source[:footer_start]
    expected = deepcopy(fmd)
    target_meta(expected).data_page_offset = offset
    assert metadata_bytes(bad) == expected
    assert metadata_bytes(bad) != fmd


def metadata_bytes(data: bytes):
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as file:
        file.write(data)
        temp_path = file.name
    try:
        return metadata(temp_path)
    finally:
        os.unlink(temp_path)


def main() -> None:
    write_source(SOURCE, SOURCE_ROWS)
    write_source(NEIGHBOR, NEIGHBOR_ROWS)

    source = read_bytes(SOURCE)
    fmd = metadata(SOURCE)
    rg0_id_offset = fmd.row_groups[0].columns[0].meta_data.data_page_offset
    assert target_meta(fmd).data_page_offset != rg0_id_offset

    data_page = build_data_page_corrupt(source, fmd)
    with open(DATA_PAGE, "wb") as file:
        file.write(data_page)

    wrong_offset = rewrite_footer(source, fmd, rg0_id_offset)
    with open(WRONG_OFFSET, "wb") as file:
        file.write(wrong_offset)
    verify_footer_only_change(source, fmd, wrong_offset, rg0_id_offset)

    mid_offset = rg0_id_offset + 5
    mid_header, _ = page_header_at(source, mid_offset)
    assert mid_header.type is None and mid_header.compressed_page_size is None
    wrong_offset_mid = rewrite_footer(source, fmd, mid_offset)
    with open(WRONG_OFFSET_MID, "wb") as file:
        file.write(wrong_offset_mid)
    verify_footer_only_change(source, fmd, wrong_offset_mid, mid_offset)

    print(f"source: {SOURCE} ({len(source)} bytes)")
    print(f"data page corrupt: {DATA_PAGE} ({len(data_page)} bytes); footer preserved")
    print(f"wrong offset: {WRONG_OFFSET} ({len(wrong_offset)} bytes); data pages preserved")
    print(f"wrong offset mid-header: {WRONG_OFFSET_MID} ({len(wrong_offset_mid)} bytes)")


if __name__ == "__main__":
    main()
