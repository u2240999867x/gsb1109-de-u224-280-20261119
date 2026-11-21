"""Offline protocol capture for the JSON / TEXT decoding diagnostic.

This module is the executable, server-free "sample": it defines the raw
MySQL wire packets (column count, field descriptor packets, row packets and
EOF packets) for two multi-result-set scenarios, the original row bytes and
the explicitly enumerated expectations for both ``use_unicode`` settings.

The two scenarios share the same first physical result group:

* ``v_text``  -- ordinary multibyte TEXT (VAR_STRING, utf8mb4 charsetnr=45)
* ``v_bin``   -- TEXT/BLOB flagged binary (charsetnr=63)
* ``v_json``  -- JSON also advertised with charsetnr=63
* ``v_empty`` -- ordinary TEXT used for SQL NULL vs real zero-length values

The second group reuses every column name and column order.

* ``swapped_types``: the protocol type codes of the first JSON and one TEXT
  are exchanged (v_text is now JSON, v_json is now VAR_STRING).
* ``passthrough_json``: the second group is a single JSON column and the
  JSON decoder is switched to passthrough immediately before that group.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

FIELD_TYPE_BLOB = 252
FIELD_TYPE_VAR_STRING = 253
FIELD_TYPE_JSON = 245

CHARSETNR_UTF8MB4 = 45
CHARSETNR_BINARY = 63
NULL_MARKER = 251
SERVER_MORE_RESULTS_EXISTS = 8

MB_STR = "多字节A"
MB_BYTES = MB_STR.encode("utf-8")
BIN_BYTES = b"\xff\x00bin"
JSON_STR = '{"k":"值"}'
JSON_BYTES = JSON_STR.encode("utf-8")
EMPTY_STR = ""
EMPTY_BYTES = b""

LABEL_TEXT = "text"
LABEL_BLOB = "blob"
LABEL_JSON = "json"
LABEL_JSON_PASSTHROUGH = "json_passthrough"

# Name exposed by Connection.encoding for utf8mb4 in this driver.
DRIVER_ENCODING = "utf8"


def _lenenc_int(value):
    if value < 0xFB:
        return bytes([value])
    if value < (1 << 16):
        return b"\xfc" + struct.pack("<H", value)
    raise AssertionError("fixture values never need more than two length bytes")


def _lenenc_str(value):
    return _lenenc_int(len(value)) + value


def field_payload(name, type_code, charsetnr, length, table="diag_t"):
    """Build the payload of a column-definition (field descriptor) packet."""
    return b"".join(
        [
            _lenenc_str(b"def"),  # catalog
            _lenenc_str(b"diag_db"),  # schema
            _lenenc_str(table.encode()),  # table
            _lenenc_str(name.encode()),  # org_table
            _lenenc_str(name.encode()),  # name
            _lenenc_str(name.encode()),  # org_name
            b"\x0c" + struct.pack("<HIBHBxx", charsetnr, length, type_code, 0, 0),
        ]
    )


def row_payload(cells):
    payload = b""
    for cell in cells:
        if cell is None:
            payload += bytes([NULL_MARKER])
        else:
            payload += _lenenc_str(cell)
    return payload


def eof_payload(more_results):
    status = SERVER_MORE_RESULTS_EXISTS if more_results else 2
    return b"\xfe" + struct.pack("<HH", 0, status)


def frame(seq, role, payload):
    header = struct.pack("<HBB", len(payload) & 0xFFFF, len(payload) >> 16, seq)
    return {"seq": seq, "role": role, "hex": (header + payload).hex()}


def field_descriptor(name, type_code, charsetnr, length):
    return {
        "name": name,
        "type_code": type_code,
        "charsetnr": charsetnr,
        "length": length,
        "flags": 0,
        "scale": 0,
        "description": [name, type_code, None, length, length, 0, True],
    }


GROUP1_FIELDS = [
    field_descriptor("v_text", FIELD_TYPE_VAR_STRING, CHARSETNR_UTF8MB4, 4096),
    field_descriptor("v_bin", FIELD_TYPE_BLOB, CHARSETNR_BINARY, 4096),
    field_descriptor("v_json", FIELD_TYPE_JSON, CHARSETNR_BINARY, 4096),
    field_descriptor("v_empty", FIELD_TYPE_VAR_STRING, CHARSETNR_UTF8MB4, 4096),
]

GROUP1_RAW_ROWS = [
    [MB_BYTES, None, None, None],
    [None, BIN_BYTES, None, None],
    [None, None, JSON_BYTES, None],
    [None, None, None, None],
    [EMPTY_BYTES, EMPTY_BYTES, EMPTY_BYTES, EMPTY_BYTES],
]

SWAP_GROUP2_FIELDS = [
    field_descriptor("v_text", FIELD_TYPE_JSON, CHARSETNR_BINARY, 4096),
    field_descriptor("v_bin", FIELD_TYPE_BLOB, CHARSETNR_BINARY, 4096),
    field_descriptor("v_json", FIELD_TYPE_VAR_STRING, CHARSETNR_UTF8MB4, 4096),
    field_descriptor("v_empty", FIELD_TYPE_VAR_STRING, CHARSETNR_UTF8MB4, 4096),
]
SWAP_GROUP2_RAW_ROWS = [[JSON_BYTES, None, MB_BYTES, None]]

PASSTHROUGH_GROUP2_FIELDS = [
    field_descriptor("v_json", FIELD_TYPE_JSON, CHARSETNR_BINARY, 4096)
]
PASSTHROUGH_GROUP2_RAW_ROWS = [[JSON_BYTES]]


def _raw_cell(value):
    return None if value is None else {"raw_hex": value.hex()}


def _raw_rows(rows):
    return [[_raw_cell(value) for value in row] for row in rows]


def value_cell(label, value, raw, passthrough=False):
    """Expected record for a non-NULL cell."""
    if passthrough:
        public_type = type(value).__name__
        public_repr = repr(value)
    else:
        public_type = "TextValue"
        public_repr = f"TextValue({value!r})"
    return {
        "raw_hex": raw.hex(),
        "converter_called": True,
        "converter": label,
        "input_type": type(value).__name__,
        "input_repr": repr(value),
        "public_type": public_type,
        "public_repr": public_repr,
    }


def null_cell():
    return {
        "raw_hex": None,
        "converter_called": False,
        "converter": None,
        "input_type": None,
        "input_repr": None,
        "public_type": "NoneType",
        "public_repr": "None",
    }


def _group1_expected(use_unicode):
    def stage(raw, position):
        if raw is None:
            return None
        if not use_unicode:
            return raw
        if position == 1:  # v_bin is charsetnr=63, always raw bytes
            return raw
        return raw.decode("utf-8")

    labels = [LABEL_TEXT, LABEL_BLOB, LABEL_JSON, LABEL_TEXT]
    rows = []
    for raw_row in GROUP1_RAW_ROWS:
        expected_row = []
        for position, raw in enumerate(raw_row):
            if raw is None:
                expected_row.append(null_cell())
            else:
                expected_row.append(
                    value_cell(labels[position], stage(raw, position), raw)
                )
        rows.append(expected_row)

    if use_unicode:
        plan = [
            (DRIVER_ENCODING, LABEL_TEXT),
            (None, LABEL_BLOB),
            (DRIVER_ENCODING, LABEL_JSON),
            (DRIVER_ENCODING, LABEL_TEXT),
        ]
    else:
        plan = [
            (None, LABEL_TEXT),
            (None, LABEL_BLOB),
            (None, LABEL_JSON),
            (None, LABEL_TEXT),
        ]
    return {"plan": plan, "rows": rows}


def _swap_group2_expected(use_unicode):
    raw_row = SWAP_GROUP2_RAW_ROWS[0]
    if use_unicode:
        plan = [
            (DRIVER_ENCODING, LABEL_JSON),
            (None, LABEL_BLOB),
            (DRIVER_ENCODING, LABEL_TEXT),
            (DRIVER_ENCODING, LABEL_TEXT),
        ]
        staged = [JSON_STR, None, MB_STR, None]
    else:
        plan = [
            (None, LABEL_JSON),
            (None, LABEL_BLOB),
            (None, LABEL_TEXT),
            (None, LABEL_TEXT),
        ]
        staged = [JSON_BYTES, None, MB_BYTES, None]
    labels = [LABEL_JSON, LABEL_BLOB, LABEL_TEXT, LABEL_TEXT]
    row = []
    for position, raw in enumerate(raw_row):
        if raw is None:
            row.append(null_cell())
        else:
            row.append(value_cell(labels[position], staged[position], raw))
    return {"plan": plan, "rows": [row]}


def _passthrough_group2_expected(use_unicode):
    raw = PASSTHROUGH_GROUP2_RAW_ROWS[0][0]
    value = JSON_STR if use_unicode else JSON_BYTES
    encoding = DRIVER_ENCODING if use_unicode else None
    return {
        "plan": [(encoding, LABEL_JSON_PASSTHROUGH)],
        "rows": [[value_cell(LABEL_JSON_PASSTHROUGH, value, raw, passthrough=True)]],
    }


def _group1_packets(seq):
    packets = [frame(seq, "result1.column_count", bytes([4]))]
    seq += 1
    for field in GROUP1_FIELDS:
        packets.append(
            frame(
                seq,
                f"result1.field.{field['name']}",
                field_payload(
                    field["name"],
                    field["type_code"],
                    field["charsetnr"],
                    field["length"],
                ),
            )
        )
        seq += 1
    packets.append(frame(seq, "result1.fields_eof", eof_payload(False)))
    seq += 1
    for index, raw_row in enumerate(GROUP1_RAW_ROWS):
        packets.append(frame(seq, f"result1.row{index}", row_payload(raw_row)))
        seq += 1
    packets.append(frame(seq, "result1.rows_eof", eof_payload(True)))
    seq += 1
    return packets, seq


def _swap_packets():
    packets, seq = _group1_packets(1)
    packets.append(frame(seq, "result2.column_count", bytes([4])))
    seq += 1
    for field in SWAP_GROUP2_FIELDS:
        packets.append(
            frame(
                seq,
                f"result2.field.{field['name']}",
                field_payload(
                    field["name"],
                    field["type_code"],
                    field["charsetnr"],
                    field["length"],
                ),
            )
        )
        seq += 1
    packets.append(frame(seq, "result2.fields_eof", eof_payload(False)))
    seq += 1
    packets.append(frame(seq, "result2.row0", row_payload(SWAP_GROUP2_RAW_ROWS[0])))
    seq += 1
    packets.append(frame(seq, "result2.rows_eof", eof_payload(False)))
    return packets


def _passthrough_packets():
    packets, seq = _group1_packets(1)
    packets.append(frame(seq, "result2.column_count", bytes([1])))
    seq += 1
    field = PASSTHROUGH_GROUP2_FIELDS[0]
    packets.append(
        frame(
            seq,
            "result2.field.v_json",
            field_payload(
                field["name"], field["type_code"], field["charsetnr"], field["length"]
            ),
        )
    )
    seq += 1
    packets.append(frame(seq, "result2.fields_eof", eof_payload(False)))
    seq += 1
    packets.append(
        frame(
            seq,
            "result2.row0",
            row_payload(PASSTHROUGH_GROUP2_RAW_ROWS[0]),
        )
    )
    seq += 1
    packets.append(frame(seq, "result2.rows_eof", eof_payload(False)))
    return packets


def build_capture():
    return {
        "constants": {
            "FIELD_TYPE_JSON": FIELD_TYPE_JSON,
            "FIELD_TYPE_BLOB": FIELD_TYPE_BLOB,
            "FIELD_TYPE_VAR_STRING": FIELD_TYPE_VAR_STRING,
            "CHARSETNR_BINARY": CHARSETNR_BINARY,
            "CHARSETNR_UTF8MB4": CHARSETNR_UTF8MB4,
            "NULL_MARKER": NULL_MARKER,
        },
        "scenarios": {
            "swapped_types": {
                "description": (
                    "Group 2 reuses group 1 column names and order but swaps "
                    "the JSON and TEXT protocol type codes."
                ),
                "wire_packets": _swap_packets(),
                "groups": [
                    {
                        "index": 1,
                        "fields": GROUP1_FIELDS,
                        "raw_rows": _raw_rows(GROUP1_RAW_ROWS),
                    },
                    {
                        "index": 2,
                        "fields": SWAP_GROUP2_FIELDS,
                        "raw_rows": _raw_rows(SWAP_GROUP2_RAW_ROWS),
                    },
                ],
                "expected": {
                    "true": [
                        _group1_expected(True),
                        _swap_group2_expected(True),
                    ],
                    "false": [
                        _group1_expected(False),
                        _swap_group2_expected(False),
                    ],
                },
            },
            "passthrough_json": {
                "description": (
                    "Group 2 is one JSON column; the JSON decoder becomes "
                    "passthrough immediately before group 2 starts."
                ),
                "wire_packets": _passthrough_packets(),
                "groups": [
                    {
                        "index": 1,
                        "fields": GROUP1_FIELDS,
                        "raw_rows": _raw_rows(GROUP1_RAW_ROWS),
                    },
                    {
                        "index": 2,
                        "fields": PASSTHROUGH_GROUP2_FIELDS,
                        "raw_rows": _raw_rows(PASSTHROUGH_GROUP2_RAW_ROWS),
                    },
                ],
                "expected": {
                    "true": [
                        _group1_expected(True),
                        _passthrough_group2_expected(True),
                    ],
                    "false": [
                        _group1_expected(False),
                        _passthrough_group2_expected(False),
                    ],
                },
            },
        },
    }


CAPTURE = build_capture()


if __name__ == "__main__":
    out = Path(__file__).with_name("offline_json_text_capture.json")
    out.write_text(json.dumps(CAPTURE, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out}")
