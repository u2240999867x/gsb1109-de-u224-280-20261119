"""Executable offline diagnostic for JSON vs TEXT decoding in PyMySQL.

No MySQL server is required.  A fake socket feeds the wire packets defined in
``capture.py`` into the driver's *original* packet read flow
(``Connection._read_packet`` -> ``MySQLResult`` -> field descriptors and
``_read_row_from_packet``); only the transport is replaced.

For every scenario the script runs both ``use_unicode=True`` and
``use_unicode=False`` and reads the results twice:

* a streaming cursor (``SSCursor``): fetch one row first, then the rest, then
  move to the next physical result group;
* a buffered cursor (``Cursor``): read everything from the start.

The JSON and TEXT decoders are recording wrappers.  They log the converter
stage (argument Python type, original value, result group, column position)
while returning the same business type (``TextValue``) for every non-NULL
value.  SQL NULL never reaches a converter; real zero-length values do and
stay observable.

In the ``passthrough_json`` rerun the JSON decoder is swapped to identity
passthrough at the exact ``next_result`` boundary before group 2, whose sole
column is JSON, so the visible difference is pinned to the business
conversion stage and to the second physical result group.

Run::

    python offline_diag/diagnose_json_text.py

A deterministic console report is printed and the same data is written to
``offline_diag/diagnostic_report.json``.  The process exits non-zero if any
observed value diverges from the expectations captured in ``capture.py``.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pymysql  # noqa: E402
from pymysql import converters  # noqa: E402
from pymysql.connections import MySQLResult  # noqa: E402
from pymysql.constants import FIELD_TYPE  # noqa: E402
from pymysql.cursors import Cursor, SSCursor  # noqa: E402
from pymysql.protocol import MysqlPacket  # noqa: E402

from offline_diag import capture  # noqa: E402


class TextValue:
    """Single business type returned by every recording converter."""

    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return f"TextValue({self.value!r})"


class FakeSocket:
    """Transport stub: framed server packets in, command bytes out."""

    def __init__(self, framed_packets):
        stream = b"".join(bytes.fromhex(item["hex"]) for item in framed_packets)
        self._rfile = io.BytesIO(stream)
        self.sent = bytearray()

    def makefile(self, mode="rb"):
        return self._rfile

    def sendall(self, data):
        self.sent.extend(data)

    def settimeout(self, timeout):
        pass

    def close(self):
        pass


class Recorder:
    """Per-connection sink for packet taps and converter calls."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.packets = []
        self.calls = []
        self.group = 0

    def note_packet(self, role, packet):
        self.packets.append(
            {
                "role": role,
                "packet_class": packet.__class__.__name__,
                "payload_hex": packet.get_all_data().hex(),
            }
        )

    def note_call(self, label, group, column, value, output):
        self.calls.append(
            {
                "group": group,
                "column": column,
                "converter": label,
                "input_type": type(value).__name__,
                "input_repr": repr(value),
                "output_type": type(output).__name__,
                "output_repr": repr(output),
            }
        )


def business_decoder(label):
    """Decoder that normalises every non-NULL value to TextValue."""

    def decoder(value):
        return TextValue(value)

    decoder.diag_label = label
    decoder.__name__ = f"recording_{label}"
    return decoder


def passthrough_decoder():
    """Identity decoder used to prove the business-conversion stage."""

    def decoder(value):
        return value

    decoder.diag_label = capture.LABEL_JSON_PASSTHROUGH
    decoder.__name__ = capture.LABEL_JSON_PASSTHROUGH
    return decoder


def make_conversion_dict():
    conv = converters.conversions.copy()
    conv[FIELD_TYPE.JSON] = business_decoder(capture.LABEL_JSON)
    conv[FIELD_TYPE.BLOB] = business_decoder(capture.LABEL_BLOB)
    conv[FIELD_TYPE.VAR_STRING] = business_decoder(capture.LABEL_TEXT)
    return conv


_ORIGINAL_GET_DESCRIPTIONS = MySQLResult._get_descriptions


def _tracked_get_descriptions(self):
    """Driver-original field parsing, then position-aware converter wrappers."""
    _ORIGINAL_GET_DESCRIPTIONS(self)
    recorder = getattr(self.connection, "_diag_recorder", None)
    if recorder is None:
        return
    recorder.group += 1
    group = recorder.group
    wrapped = []
    for position, (encoding, converter) in enumerate(self.converters):
        label = getattr(converter, "diag_label", None)
        if label is None:
            wrapped.append((encoding, converter))
            continue

        def make_wrapper(position, converter, label):
            def wrapped_converter(value):
                output = converter(value)
                recorder.note_call(label, group, position, value, output)
                return output

            wrapped_converter.diag_label = label
            wrapped_converter.__name__ = converter.__name__
            return wrapped_converter

        wrapped.append((encoding, make_wrapper(position, converter, label)))
    self.converters = wrapped


MySQLResult._get_descriptions = _tracked_get_descriptions


def open_connection(recorder, use_unicode):
    conn = pymysql.connect(
        user="diag",
        password="",
        ssl_disabled=True,
        defer_connect=True,
        use_unicode=use_unicode,
        conv=make_conversion_dict(),
    )
    sock = FakeSocket(recorder.scenario["wire_packets"])
    conn._sock = sock
    conn._rfile = sock.makefile("rb")
    conn._next_seq_id = 1
    conn._current_timeout = None
    conn._diag_recorder = recorder

    wire_packets = recorder.scenario["wire_packets"]
    role_by_packet_index = {
        index: item["role"] for index, item in enumerate(wire_packets)
    }
    original_read_packet = conn._read_packet
    packet_index = 0

    def tapped_read_packet(packet_type=MysqlPacket):
        nonlocal packet_index
        packet = original_read_packet(packet_type)
        role = role_by_packet_index[packet_index]
        packet_index += 1
        recorder.note_packet(role, packet)
        return packet

    conn._read_packet = tapped_read_packet
    return conn


def install_passthrough_at_group2_boundary(conn):
    """Flip the JSON decoder only when the driver starts the second group."""
    original_next_result = conn.next_result
    state = {"switched": False}

    def switching_next_result(unbuffered=False):
        if not state["switched"]:
            conn.decoders[FIELD_TYPE.JSON] = passthrough_decoder()
            state["switched"] = True
        return original_next_result(unbuffered=unbuffered)

    conn.next_result = switching_next_result
    return state


def parse_row_packet(payload_hex, column_count):
    """Re-parse an observed row packet with the driver's own packet class."""
    packet = MysqlPacket(bytes.fromhex(payload_hex), "utf-8")
    return [packet.read_length_coded_string() for _ in range(column_count)]


def field_view(field):
    return {
        "name": field.name,
        "type_code": field.type_code,
        "charsetnr": field.charsetnr,
        "length": field.length,
        "flags": field.flags,
        "description": list(field.description()),
    }


def result_plan(result):
    plan = []
    for position, (encoding, converter) in enumerate(result.converters):
        field = result.fields[position]
        plan.append(
            {
                "column": position,
                "name": field.name,
                "type_code": field.type_code,
                "charsetnr": field.charsetnr,
                "decode_encoding": encoding,
                "converter": getattr(converter, "diag_label", None),
            }
        )
    return plan


def row_payload_index(recorder):
    index = {}
    row_role = re.compile(r"^result(\d+)\.row(\d+)$")
    for observed in recorder.packets:
        match = row_role.match(observed["role"])
        if match is None:
            continue
        group, row = (int(part) for part in match.groups())
        index[(group, row)] = observed["payload_hex"]
    return index


def collect_group(recorder, result, group_decl, observed_rows):
    group_index = group_decl["index"]
    width = len(group_decl["fields"])
    row_index = row_payload_index(recorder)
    calls = [call for call in recorder.calls if call["group"] == group_index]
    rendered_rows = []
    call_cursor = 0
    for row_number, public_row in enumerate(observed_rows):
        raw_cells = parse_row_packet(row_index[(group_index, row_number)], width)
        rendered_row = []
        for position, public_value in enumerate(public_row):
            raw = raw_cells[position]
            entry = {
                "column": position,
                "name": group_decl["fields"][position]["name"],
                "raw_hex": None if raw is None else raw.hex(),
                "raw_is_null": raw is None,
                "raw_is_empty": raw == b"",
            }
            if raw is None:
                entry.update(
                    {
                        "converter_called": False,
                        "converter": None,
                        "input_type": None,
                        "input_repr": None,
                        "public_type": type(public_value).__name__,
                        "public_repr": repr(public_value),
                    }
                )
            else:
                call = calls[call_cursor]
                call_cursor += 1
                entry.update(
                    {
                        "converter_called": True,
                        "converter": call["converter"],
                        "input_type": call["input_type"],
                        "input_repr": call["input_repr"],
                        "public_type": type(public_value).__name__,
                        "public_repr": repr(public_value),
                    }
                )
            rendered_row.append(entry)
        rendered_rows.append(rendered_row)
    return {
        "group_index": group_index,
        "fields": [field_view(field) for field in result.fields],
        "plan": result_plan(result),
        "rows": rendered_rows,
    }


def run_streaming(recorder, conn):
    cursor = conn.cursor(SSCursor)
    cursor.execute("SELECT /* streaming diagnostic */ * FROM diagnostic_view")
    groups = []

    first_row = cursor.fetchone()
    remaining = cursor.fetchall()
    groups.append(
        collect_group(
            recorder,
            conn._result,
            recorder.scenario["groups"][0],
            [first_row, *remaining],
        )
    )

    group_index = 1
    while cursor.nextset() is not None:
        group_index += 1
        rows = cursor.fetchall()
        groups.append(
            collect_group(
                recorder,
                conn._result,
                recorder.scenario["groups"][group_index - 1],
                rows,
            )
        )
    cursor.close()
    return groups


def run_buffered(recorder, conn):
    cursor = conn.cursor(Cursor)
    cursor.execute("SELECT /* buffered diagnostic */ * FROM diagnostic_view")
    groups = []
    group_index = 1
    while True:
        groups.append(
            collect_group(
                recorder,
                conn._result,
                recorder.scenario["groups"][group_index - 1],
                cursor.fetchall(),
            )
        )
        if cursor.nextset() is None:
            break
        group_index += 1
    cursor.close()
    return groups


def run_scenario(scenario_name, use_unicode, cursor_kind, passthrough):
    scenario = capture.CAPTURE["scenarios"][scenario_name]
    recorder = Recorder(scenario)
    conn = open_connection(recorder, use_unicode)
    switch_state = None
    if passthrough:
        switch_state = install_passthrough_at_group2_boundary(conn)
    if cursor_kind == "streaming":
        groups = run_streaming(recorder, conn)
    else:
        groups = run_buffered(recorder, conn)
    conn.close()
    return {
        "scenario": scenario_name,
        "use_unicode": use_unicode,
        "cursor_kind": cursor_kind,
        "json_passthrough_in_group2": passthrough,
        "passthrough_switched_before_group2": (
            switch_state["switched"] if switch_state else False
        ),
        "wire_packets": scenario["wire_packets"],
        "observed_packets": recorder.packets,
        "converter_calls": recorder.calls,
        "groups": groups,
    }


def expected_groups(scenario_name, use_unicode):
    key = "true" if use_unicode else "false"
    return capture.CAPTURE["scenarios"][scenario_name]["expected"][key]


def check_against_expected(run, failures):
    prefix = f"{run['scenario']} unicode={run['use_unicode']} {run['cursor_kind']}"
    scenario = capture.CAPTURE["scenarios"][run["scenario"]]
    if len(run["observed_packets"]) != len(run["wire_packets"]):
        failures.append(
            f"{prefix}: observed {len(run['observed_packets'])} packets, "
            f"expected {len(run['wire_packets'])}"
        )

    expected = expected_groups(run["scenario"], run["use_unicode"])
    for group_run, group_decl, group_expected in zip(
        run["groups"], scenario["groups"], expected
    ):
        group_index = group_run["group_index"]
        for position, (observed_field, declared_field) in enumerate(
            zip(group_run["fields"], group_decl["fields"])
        ):
            for key_name in ("name", "type_code", "charsetnr", "length", "flags"):
                if observed_field[key_name] != declared_field[key_name]:
                    failures.append(
                        f"{prefix} group{group_index} col{position}: field "
                        f"{key_name} {observed_field[key_name]!r} != "
                        f"{declared_field[key_name]!r}"
                    )
            if observed_field["description"] != declared_field["description"]:
                failures.append(
                    f"{prefix} group{group_index} col{position}: description "
                    f"tuple mismatch"
                )

        for position, plan_item in enumerate(group_run["plan"]):
            expected_encoding, expected_label = group_expected["plan"][position]
            if plan_item["decode_encoding"] != expected_encoding:
                failures.append(
                    f"{prefix} group{group_index} col{position}: encoding "
                    f"{plan_item['decode_encoding']} != {expected_encoding}"
                )
            if plan_item["converter"] != expected_label:
                failures.append(
                    f"{prefix} group{group_index} col{position}: converter "
                    f"{plan_item['converter']} != {expected_label}"
                )

        for row_number, (row_run, row_expected) in enumerate(
            zip(group_run["rows"], group_expected["rows"])
        ):
            for cell_run, cell_expected in zip(row_run, row_expected):
                cell_prefix = (
                    f"{prefix} group{group_index} row{row_number} "
                    f"col{cell_run['column']}({cell_run['name']})"
                )
                if cell_run["raw_hex"] != cell_expected["raw_hex"]:
                    failures.append(f"{cell_prefix}: raw bytes mismatch")
                if cell_run["converter_called"] != cell_expected["converter_called"]:
                    failures.append(
                        f"{cell_prefix}: converter invocation "
                        f"{cell_run['converter_called']} != "
                        f"{cell_expected['converter_called']}"
                    )
                if cell_run["public_repr"] != cell_expected["public_repr"]:
                    failures.append(
                        f"{cell_prefix}: public {cell_run['public_repr']!r} "
                        f"!= {cell_expected['public_repr']!r}"
                    )
                if cell_run["raw_is_null"]:
                    continue
                if cell_run["input_type"] != cell_expected["input_type"]:
                    failures.append(
                        f"{cell_prefix}: input type "
                        f"{cell_run['input_type']} != "
                        f"{cell_expected['input_type']}"
                    )
                if cell_run["input_repr"] != cell_expected["input_repr"]:
                    failures.append(f"{cell_prefix}: converter input mismatch")
                if cell_run["converter"] != cell_expected["converter"]:
                    failures.append(f"{cell_prefix}: converter label mismatch")

    non_null_cells = sum(
        1
        for group in run["groups"]
        for row in group["rows"]
        for cell in row
        if not cell["raw_is_null"]
    )
    if len(run["converter_calls"]) != non_null_cells:
        failures.append(
            f"{prefix}: {len(run['converter_calls'])} converter calls for "
            f"{non_null_cells} non-NULL cells (NULL must skip converters)"
        )


def render_console(report, failures):
    lines = []
    for run in report["runs"]:
        lines.append("=" * 78)
        lines.append(
            f"scenario={run['scenario']} use_unicode={run['use_unicode']} "
            f"cursor={run['cursor_kind']} "
            f"group2_json_passthrough={run['json_passthrough_in_group2']}"
        )
        lines.append(
            f"wire packets observed through Connection._read_packet: "
            f"{len(run['observed_packets'])} / {len(run['wire_packets'])}"
        )
        if run["json_passthrough_in_group2"]:
            lines.append(
                "JSON decoder switched to passthrough at the group2 boundary: "
                f"{run['passthrough_switched_before_group2']}"
            )
        for group in run["groups"]:
            lines.append("")
            lines.append(f"-- physical result group {group['group_index']} --")
            lines.append("field descriptors (replayed in column order):")
            for position, field in enumerate(group["fields"]):
                lines.append(
                    f"  col{position} {field['name']}: type_code="
                    f"{field['type_code']} charsetnr={field['charsetnr']} "
                    f"length={field['length']} flags={field['flags']}"
                )
                lines.append(f"      description={field['description']}")
            lines.append("driver decode plan (stage before business converter):")
            for item in group["plan"]:
                lines.append(
                    f"  col{item['column']} {item['name']}: "
                    f"type_code={item['type_code']} "
                    f"charsetnr={item['charsetnr']} "
                    f"decode_encoding={item['decode_encoding']} "
                    f"converter={item['converter']}"
                )
            for row_number, row in enumerate(group["rows"]):
                lines.append(f"row {row_number}:")
                for cell in row:
                    if cell["raw_is_null"]:
                        lines.append(
                            f"  col{cell['column']} {cell['name']}: "
                            "raw=NULL(0xfb) converter=not-called "
                            f"public={cell['public_type']} {cell['public_repr']}"
                        )
                    else:
                        empty = " [zero-length]" if cell["raw_is_empty"] else ""
                        lines.append(
                            f"  col{cell['column']} {cell['name']}: "
                            f"raw={cell['raw_hex']}{empty} -> "
                            f"{cell['converter']}("
                            f"{cell['input_type']}: {cell['input_repr']}) -> "
                            f"public={cell['public_type']} {cell['public_repr']}"
                        )
        lines.append("")
        lines.append("converter call log:")
        for call in run["converter_calls"]:
            lines.append(
                f"  group{call['group']} col{call['column']} "
                f"{call['converter']}: {call['input_type']} "
                f"{call['input_repr']} -> {call['output_type']} "
                f"{call['output_repr']}"
            )
    lines.append("=" * 78)
    if failures:
        lines.append(f"CHECKS FAILED: {len(failures)}")
        lines.extend(f"  - {failure}" for failure in failures)
    else:
        lines.append(
            "CHECKS OK: field descriptors, raw rows, converter stages and "
            "public results match the offline expectations"
        )
    return "\n".join(lines)


def main():
    report = {"runs": []}
    failures = []
    for scenario_name in ("swapped_types", "passthrough_json"):
        for use_unicode in (True, False):
            for cursor_kind in ("streaming", "buffered"):
                passthrough = scenario_name == "passthrough_json"
                run = run_scenario(scenario_name, use_unicode, cursor_kind, passthrough)
                check_against_expected(run, failures)
                report["runs"].append(run)

    out_path = Path(__file__).with_name("diagnostic_report.json")
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(render_console(report, failures))
    print(f"\nmachine-readable report: {out_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
