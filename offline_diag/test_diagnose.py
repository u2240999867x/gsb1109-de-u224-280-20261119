"""Offline pytest coverage for the JSON/TEXT decoding diagnostic.

These tests need no MySQL server: they drive the same fake-socket capture and
driver-original read flow as ``diagnose_json_text.py``.
"""

import pytest

from offline_diag import capture
from offline_diag.diagnose_json_text import (
    check_against_expected,
    run_scenario,
)


@pytest.fixture(
    params=["swapped_types", "passthrough_json"],
    ids=["swap", "passthrough"],
)
def scenario_run(request):
    scenario_name = request.param
    passthrough = scenario_name == "passthrough_json"
    runs = {}
    for use_unicode in (True, False):
        for cursor_kind in ("streaming", "buffered"):
            runs[(use_unicode, cursor_kind)] = run_scenario(
                scenario_name, use_unicode, cursor_kind, passthrough
            )
    return runs


def _cells(run, group_index):
    group = run["groups"][group_index - 1]
    return [cell for row in group["rows"] for cell in row]


def _plan(run, group_index):
    return run["groups"][group_index - 1]["plan"]


def test_all_runs_match_captured_expectations(scenario_run):
    failures = []
    for run in scenario_run.values():
        check_against_expected(run, failures)
    assert failures == []


def test_every_wire_packet_flows_through_original_reader(scenario_run):
    for run in scenario_run.values():
        assert len(run["observed_packets"]) == len(run["wire_packets"])
        observed_hex = [item["payload_hex"] for item in run["observed_packets"]]
        expected_hex = [
            bytes.fromhex(item["hex"])[4:].hex() for item in run["wire_packets"]
        ]
        assert observed_hex == expected_hex


def test_streaming_and_buffered_render_identical_groups(scenario_run):
    for use_unicode in (True, False):
        streaming = scenario_run[(use_unicode, "streaming")]
        buffered = scenario_run[(use_unicode, "buffered")]
        assert streaming["groups"] == buffered["groups"]
        assert streaming["converter_calls"] == buffered["converter_calls"]


def test_null_never_reaches_a_converter(scenario_run):
    for run in scenario_run.values():
        null_cells = [
            cell
            for group in run["groups"]
            for row in group["rows"]
            for cell in row
            if cell["raw_is_null"]
        ]
        assert null_cells, "fixture must include SQL NULL cells"
        assert all(not cell["converter_called"] for cell in null_cells)
        assert all(cell["public_type"] == "NoneType" for cell in null_cells)
        non_null = [
            cell
            for group in run["groups"]
            for row in group["rows"]
            for cell in row
            if not cell["raw_is_null"]
        ]
        assert all(cell["converter_called"] for cell in non_null)
        assert len(run["converter_calls"]) == len(non_null)


def test_zero_length_values_remain_observable(scenario_run):
    run = scenario_run[(True, "streaming")]
    empty = [cell for cell in _cells(run, 1) if cell["raw_is_empty"]]
    assert len(empty) == 4
    assert all(cell["converter_called"] for cell in empty)
    assert all(cell["raw_hex"] == "" for cell in empty)
    assert all(cell["public_repr"].startswith("TextValue(") for cell in empty)


def test_json_and_binary_text_diverge_only_in_charset_handling():
    run = run_scenario("swapped_types", True, "buffered", False)
    plan = _plan(run, 1)
    by_name = {item["name"]: item for item in plan}
    # Both advertise charsetnr=63, but JSON is force-decoded as text while
    # the binary TEXT/BLOB stays raw bytes.
    assert by_name["v_json"]["charsetnr"] == capture.CHARSETNR_BINARY
    assert by_name["v_bin"]["charsetnr"] == capture.CHARSETNR_BINARY
    assert by_name["v_json"]["decode_encoding"] == capture.DRIVER_ENCODING
    assert by_name["v_bin"]["decode_encoding"] is None

    json_cell = next(
        cell
        for cell in _cells(run, 1)
        if cell["name"] == "v_json" and not cell["raw_is_null"]
    )
    blob_cell = next(
        cell
        for cell in _cells(run, 1)
        if cell["name"] == "v_bin" and not cell["raw_is_null"]
    )
    assert json_cell["input_type"] == "str"
    assert blob_cell["input_type"] == "bytes"
    assert json_cell["public_type"] == blob_cell["public_type"] == "TextValue"


def test_use_unicode_flips_converter_input_types_not_business_type():
    unicode_run = run_scenario("swapped_types", True, "buffered", False)
    bytes_run = run_scenario("swapped_types", False, "buffered", False)
    for group_index in (1, 2):
        plan = _plan(unicode_run, group_index)
        binary_columns = {
            item["column"] for item in plan if item["decode_encoding"] is None
        }
        decoded_columns = {
            item["column"] for item in plan if item["decode_encoding"] is not None
        }
        for run, expected_decoded in (
            (unicode_run, "str"),
            (bytes_run, "bytes"),
        ):
            cells = [
                cell for cell in _cells(run, group_index) if cell["converter_called"]
            ]
            assert all(cell["public_type"] == "TextValue" for cell in cells)
            assert all(
                cell["input_type"] == "bytes"
                for cell in cells
                if cell["column"] in binary_columns
            )
            assert all(
                cell["input_type"] == expected_decoded
                for cell in cells
                if cell["column"] in decoded_columns
            )


def test_swapped_type_codes_follow_column_position_in_group2():
    run = run_scenario("swapped_types", True, "streaming", False)
    plan = _plan(run, 2)
    assert [item["name"] for item in plan] == [
        "v_text",
        "v_bin",
        "v_json",
        "v_empty",
    ]
    assert plan[0]["type_code"] == capture.FIELD_TYPE_JSON
    assert plan[0]["converter"] == capture.LABEL_JSON
    assert plan[2]["type_code"] == capture.FIELD_TYPE_VAR_STRING
    assert plan[2]["converter"] == capture.LABEL_TEXT

    row = run["groups"][1]["rows"][0]
    assert row[0]["converter"] == capture.LABEL_JSON
    assert row[0]["input_type"] == "str"
    assert row[2]["converter"] == capture.LABEL_TEXT
    assert row[2]["input_type"] == "str"


def test_passthrough_change_is_scoped_to_group2_and_json():
    unicode_run = run_scenario("passthrough_json", True, "streaming", True)
    bytes_run = run_scenario("passthrough_json", False, "streaming", True)

    assert unicode_run["passthrough_switched_before_group2"] is True
    group1 = [cell for cell in _cells(unicode_run, 1) if cell["converter_called"]]
    assert all(cell["public_type"] == "TextValue" for cell in group1)

    group2_unicode = unicode_run["groups"][1]
    assert len(group2_unicode["fields"]) == 1
    assert group2_unicode["fields"][0]["type_code"] == capture.FIELD_TYPE_JSON
    only_cell = group2_unicode["rows"][0][0]
    assert only_cell["converter"] == capture.LABEL_JSON_PASSTHROUGH
    assert only_cell["input_type"] == "str"
    assert only_cell["public_type"] == "str"

    group2_bytes = bytes_run["groups"][1]["rows"][0][0]
    assert group2_bytes["input_type"] == "bytes"
    assert group2_bytes["public_type"] == "bytes"


def test_business_run_without_passthrough_keeps_text_value_in_group2():
    control = run_scenario("swapped_types", True, "streaming", False)
    group2_cells = [cell for cell in _cells(control, 2) if cell["converter_called"]]
    assert group2_cells
    assert all(cell["public_type"] == "TextValue" for cell in group2_cells)
