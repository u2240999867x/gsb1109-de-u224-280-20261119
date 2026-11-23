"""Regression tests for the local calibration-switch diagnosis."""

import json

from diagnostics.calibration_switch.model import (
    earliest_field_consequence,
    field_divergence,
    run_all,
    run_control,
    run_merged,
    run_reversed,
    run_transit,
    summarize,
)
from diagnostics.calibration_switch.report import render_json, render_markdown


def _summary(result):
    return summarize(result)


def _record(summary, rid):
    return next(row for row in summary["records"] if row["rid"] == rid)


def test_pre_switch_field_prefix_is_identical_across_rounds():
    transit = _summary(run_transit())
    merged_continue = _summary(run_merged("continue"))
    merged_abort = _summary(run_merged("abort"))

    expected = [
        {"scope": "field", "kind": "source_read", "source": "A", "rid": "A1"},
        {"scope": "field", "kind": "source_read", "source": "B", "rid": "B1"},
    ]
    assert transit["field_prefix_before_switch"] == expected
    assert (
        transit["field_prefix_before_switch"]
        == merged_continue["field_prefix_before_switch"]
        == merged_abort["field_prefix_before_switch"]
    )


def test_transit_round_is_a_legal_wait():
    summary = _summary(run_transit())
    switch = summary["switch"]

    assert switch["ok"] is True
    assert summary["topology"]["consistent"] is True
    b1 = _record(summary, "B1")
    assert b1["state"] == "settled"
    assert b1["ack"] is True
    assert b1["left_gate"] is True
    # The late B1 was released with no downstream; its confirmation proves
    # release, not admission into the merge node.
    assert b1["ack_without_merge"] is True
    assert summary["earliest_a2_write"] == {"rids": ["A2", "C1"], "arity": 2}


def test_merged_round_is_a_half_completed_switch():
    summary = _summary(run_merged("continue"))
    switch = summary["switch"]
    state = summary["switch_state"]

    assert switch["ok"] is False
    assert switch["stage"] == "disconnect"
    assert "KeyError" in switch["error"]
    assert summary["topology"]["consistent"] is False
    assert state["delta"]["consistent"] == {"before": True, "after": False}
    assert state["delta"]["last_present"] == {
        "before": [True, True],
        "after": [True, False],
    }

    b1 = _record(summary, "B1")
    assert b1["state"] == "orphaned_ref"
    assert b1["ref_count_local"] > 0
    assert b1["ack"] is False
    assert b1["merge_slots_local"] == []
    # Acknowledgement would have required a downstream, which no longer exists.
    assert b1["left_gate"] is True


def test_merged_continue_has_a_second_independent_failure():
    summary = _summary(run_merged("continue"))
    c1 = _record(summary, "C1")

    assert c1["state"] == "merge_rejected"
    assert c1["merge_attempted"] is True
    assert c1["merge_rejected"] is True
    assert c1["ref_count_local"] == 2
    assert c1["ack"] is False
    assert summary["db"] == []
    assert summary["earliest_a2_write"] is None


def test_merged_abort_leaves_c1_without_usable_reference():
    summary = _summary(run_merged("abort"))
    c1 = _record(summary, "C1")

    assert c1["state"] == "unadmitted_zero_ref"
    assert c1["ref_count_local"] == 0
    assert c1["ack"] is False
    assert c1["merge_attempted"] is False
    assert summary["earliest_a2_write"] == {"rids": ["A2"], "arity": 1}


def test_control_run_replaces_old_calibration_value():
    summary = _summary(run_control())

    assert summary["switch"] is None
    assert summary["topology"]["consistent"] is True
    b1 = _record(summary, "B1")
    b2 = _record(summary, "B2")
    assert b1["state"] == "settled"
    assert b1["ack"] is True and b1["ack_without_merge"] is False
    assert b2["state"] == "merge_slot"
    assert summary["earliest_a2_write"] == {"rids": ["A2", "B2"], "arity": 2}


def test_reversed_arrival_can_write_before_the_switch():
    summary = _summary(run_reversed())

    assert summary["earliest_a2_write"] is None
    assert summary["db"][0]["rids"] == ["A1", "B1"]
    b1 = _record(summary, "B1")
    assert b1["db_rows"] == [0]
    # A row exists for B1, yet the same half-completed switch leaks its ref.
    assert b1["state"] == "orphaned_ref"
    assert b1["ref_count_local"] > 0
    assert b1["ack"] is False


def test_field_divergence_ignores_callback_order():
    transit = run_transit()
    merged_continue = run_merged("continue")
    merged_abort = run_merged("abort")

    continue_diff = field_divergence(transit, merged_continue)
    abort_diff = field_divergence(transit, merged_abort)

    # Reads are identical in both rounds; only acknowledgements and writes
    # differ, and acknowledgements are compared as sets, not as event order.
    assert "reads" not in continue_diff
    assert "reads" not in abort_diff
    assert set(continue_diff["acks"]["transit"]) == {"A1", "B1"}
    assert set(continue_diff["acks"]["merged"]) == {"A1"}
    assert continue_diff["writes"] == {
        "transit": [{"rids": ["A2", "C1"], "arity": 2}],
        "merged": [],
    }
    assert abort_diff["writes"] == {
        "transit": [{"rids": ["A2", "C1"], "arity": 2}],
        "merged": [{"rids": ["A2"], "arity": 1}],
    }
    # B2 only exists in the merged rounds and must not pollute the comparison.
    assert "B2" not in json.dumps(continue_diff)


def test_earliest_a2_consequence_is_shape_not_ack_order():
    transit = run_transit()
    merged_abort = run_merged("abort")

    assert earliest_field_consequence(transit["bench"]) == {
        "rids": ["A2", "C1"], "arity": 2
    }
    assert earliest_field_consequence(merged_abort["bench"]) == {
        "rids": ["A2"], "arity": 1
    }


def test_reports_render_and_default_json_hides_local_values():
    results = run_all()

    markdown = render_markdown(results)
    payload = json.loads(render_json(results))

    for label in ("T1", "T2a", "T2b", "对照", "顺序颠倒"):
        assert label in markdown
    assert "现场可见" in markdown
    assert "仅本地复现" in markdown

    assert payload["same_field_prefix_before_switch"] is True
    assert set(payload["cases"]) == {
        "transit", "merged_continue", "merged_abort", "control", "reversed"
    }
    dumped = json.dumps(payload, ensure_ascii=False)
    assert '"value":' not in dumped
    assert '"values":' not in dumped


def test_json_can_include_local_values_explicitly():
    results = run_all()
    payload = json.loads(render_json(results, include_values=True))

    write = next(
        event
        for event in payload["cases"]["transit"]["timeline"]
        if event["kind"] == "db_write_local"
    )
    assert write["detail"]["values"] == [20, 7]
