# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import pytest

from thrift.protocol.TBinaryProtocol import TBinaryProtocol
from thrift.transport import TTransport

from impala._thrift_gen.Metrics.ttypes import TUnit
from impala._thrift_gen.RuntimeProfile.ttypes import (
    TAggCounter, TAggregatedRuntimeProfileNode, TCounter,
    TRuntimeProfileNode, TRuntimeProfileNodeMetadata, TRuntimeProfileTree)
from impala.profile_diff import (
    INSTANCE_INFO_KEY, NodeIdentity, ObservationStatus, diff_scan_counters,
    parse_profile)

BYTES_READ = "BytesRead"
ROWS_READ = "RowsRead"
NODE_A = NodeIdentity("plan_node", 10)
NODE_B = NodeIdentity("plan_node", 11)
NODE_C = NodeIdentity("plan_node", 12)
FIRST_INSTANCES = ["i-0", "i-1", "i-2"]
SWAPPED_INSTANCES = ["i-2", "i-0", "i-3", "i-1"]
COUNTER_UNITS = {BYTES_READ: TUnit.BYTES, ROWS_READ: TUnit.UNIT}


def _agg_counter(name, unit, ordered_values):
    has_value = [value is not None for value in ordered_values]
    return TAggCounter(
        name=name, unit=unit, has_value=has_value,
        values=[value if value is not None else 0 for value in ordered_values])


def _aggregated_node(plan_node_id, name, input_profiles, agg_counters,
                     averaged_counters=None):
    return TRuntimeProfileNode(
        name=name, num_children=0, counters=averaged_counters or [],
        metadata=-1, indent=False, info_strings={},
        info_strings_display_order=[], child_counters_map={},
        node_metadata=TRuntimeProfileNodeMetadata(plan_node_id=plan_node_id),
        aggregated=TAggregatedRuntimeProfileNode(
            num_instances=len(input_profiles) if input_profiles else 0,
            input_profiles=input_profiles, counters=agg_counters))


def _instance_node(plan_node_id, name, instance, counters):
    return TRuntimeProfileNode(
        name=name, num_children=0, counters=counters, metadata=-1,
        indent=False, info_strings={INSTANCE_INFO_KEY: instance},
        info_strings_display_order=[INSTANCE_INFO_KEY],
        child_counters_map={},
        node_metadata=TRuntimeProfileNodeMetadata(plan_node_id=plan_node_id))


def _tree(nodes):
    return TRuntimeProfileTree(nodes=nodes, profile_version=2)


def _serialize(tree):
    transport = TTransport.TMemoryBuffer()
    tree.write(TBinaryProtocol(transport))
    return transport.getvalue()


def _first_snapshot():
    node_a = _aggregated_node(
        NODE_A.identifier, "HDFS_SCAN_NODE(a)", FIRST_INSTANCES,
        [_agg_counter(BYTES_READ, TUnit.BYTES, [100, 200, 300]),
         _agg_counter(ROWS_READ, TUnit.UNIT, [10, None, None])],
        averaged_counters=[TCounter(BYTES_READ, TUnit.BYTES, 9999)])
    node_b = _aggregated_node(
        NODE_B.identifier, "HDFS_SCAN_NODE(b)", ["i-0"],
        [_agg_counter(BYTES_READ, TUnit.BYTES, [50])])
    return _tree([node_a, node_b])


def _second_snapshot_aggregated():
    # Instance positions are deliberately permuted compared with the first
    # snapshot; i-2 has no value this time and i-3 is seen for the first time.
    node_a = _aggregated_node(
        NODE_A.identifier, "HDFS_SCAN_NODE(a)", SWAPPED_INSTANCES,
        [_agg_counter(BYTES_READ, TUnit.BYTES,
                      [None, 150, 400, 150]),
         _agg_counter(ROWS_READ, TUnit.UNIT, [None, 25, None, None])])
    # Consistent standalone representation of the same (node, instance) pair.
    standalone_i0 = _instance_node(
        NODE_A.identifier, "HDFS_SCAN_NODE(a)", "i-0",
        [TCounter(BYTES_READ, TUnit.BYTES, 150),
         TCounter(ROWS_READ, TUnit.UNIT, 25)])
    node_b = _aggregated_node(
        NODE_B.identifier, "HDFS_SCAN_NODE(b)", ["i-0"],
        [_agg_counter(BYTES_READ, TUnit.BYTES, [70])])
    return _tree([node_a, standalone_i0, node_b])


def _second_snapshot_per_instance():
    # Equivalent information, but every value is reported through per-instance
    # unaggregated nodes instead of aggregated arrays.
    nodes = [
        _instance_node(NODE_A.identifier, "HDFS_SCAN_NODE(a)", "i-0",
                       [TCounter(BYTES_READ, TUnit.BYTES, 150),
                        TCounter(ROWS_READ, TUnit.UNIT, 25)]),
        _instance_node(NODE_A.identifier, "HDFS_SCAN_NODE(a)", "i-1",
                       [TCounter(BYTES_READ, TUnit.BYTES, 150)]),
        _instance_node(NODE_A.identifier, "HDFS_SCAN_NODE(a)", "i-3",
                       [TCounter(BYTES_READ, TUnit.BYTES, 400)]),
        _instance_node(NODE_B.identifier, "HDFS_SCAN_NODE(b)", "i-0",
                       [TCounter(BYTES_READ, TUnit.BYTES, 70)]),
    ]
    return _tree(nodes)


def _entry_map(report):
    return {(entry.node, entry.counter_name, entry.instance): entry
            for entry in report.entries}


def test_permutation_duplicate_and_lifecycle_statuses():
    report = diff_scan_counters(
        _first_snapshot(), _second_snapshot_aggregated(), COUNTER_UNITS)
    entries = _entry_map(report)

    growth = entries[(NODE_A, BYTES_READ, "i-0")]
    assert growth.status is ObservationStatus.GROWTH
    assert growth.previous_value == 100
    assert growth.current_value == 150
    assert growth.delta == 50
    assert growth.unit == TUnit.BYTES

    decrease = entries[(NODE_A, BYTES_READ, "i-1")]
    assert decrease.status is ObservationStatus.DECREASE
    assert decrease.delta == -50
    assert decrease.delta not in report.totals_by_unit.values()

    unobserved = entries[(NODE_A, BYTES_READ, "i-2")]
    assert unobserved.status is ObservationStatus.UNOBSERVED
    assert unobserved.previous_value == 300
    assert unobserved.current_value is None
    assert unobserved.delta is None

    new_instance = entries[(NODE_A, BYTES_READ, "i-3")]
    assert new_instance.status is ObservationStatus.NEW_INSTANCE
    assert new_instance.current_value == 400
    assert new_instance.delta is None

    rows = entries[(NODE_A, ROWS_READ, "i-0")]
    assert rows.status is ObservationStatus.GROWTH
    assert rows.delta == 15
    assert rows.unit == TUnit.UNIT

    other_node = entries[(NODE_B, BYTES_READ, "i-0")]
    assert other_node.status is ObservationStatus.GROWTH
    assert other_node.delta == 20

    # One (node, counter, instance) triple contributes exactly once even though
    # the current snapshot carries two consistent representations.
    assert sum(1 for entry in report.entries
               if entry.node == NODE_A and entry.instance == "i-0") == 2
    current, unverified = parse_profile(
        _second_snapshot_aggregated(), COUNTER_UNITS, "current")
    bytes_counter = current[(NODE_A, BYTES_READ)]
    assert bytes_counter.representation_counts["i-0"] == 2
    assert bytes_counter.conflicting_values == []
    assert bytes_counter.values["i-0"] == 150
    assert unverified == []

    # Only comparable positive growth feeds the summary, per unit, with the
    # contributing entries exposed for review.
    assert report.totals_by_unit == {TUnit.BYTES: 70, TUnit.UNIT: 15}
    assert report.total_delta is None  # mixed units cannot share one total
    growth_deltas = sorted(
        entry.delta for entry in report.contributions)
    assert growth_deltas == [15, 20, 50]
    assert all(entry.status is ObservationStatus.GROWTH
               for entry in report.contributions)


def test_per_instance_representation_gives_identical_increments():
    aggregated_report = diff_scan_counters(
        _first_snapshot(), _second_snapshot_aggregated(), COUNTER_UNITS)
    per_instance_report = diff_scan_counters(
        _first_snapshot(), _second_snapshot_per_instance(), COUNTER_UNITS)

    assert _entry_map(per_instance_report).keys() == \
        _entry_map(aggregated_report).keys()
    for key, entry in _entry_map(per_instance_report).items():
        reference = _entry_map(aggregated_report)[key]
        assert entry.status is reference.status
        assert entry.delta == reference.delta
        assert entry.previous_value == reference.previous_value
        assert entry.current_value == reference.current_value
        assert entry.unit == reference.unit
    assert per_instance_report.totals_by_unit == \
        aggregated_report.totals_by_unit == {TUnit.BYTES: 70, TUnit.UNIT: 15}


def test_serialized_thrift_input_is_accepted():
    previous_blob = _serialize(_first_snapshot())
    current_blob = _serialize(_second_snapshot_aggregated())
    report = diff_scan_counters(
        previous_blob, current_blob, COUNTER_UNITS)
    assert report.totals_by_unit == {TUnit.BYTES: 70, TUnit.UNIT: 15}


def test_averaged_aggregated_counter_is_never_used():
    # The averaged TCounter (9999) on the aggregated node must not leak into
    # any per-instance observation.
    observations, unverified = parse_profile(
        _first_snapshot(), [BYTES_READ], "previous")
    parsed = observations[(NODE_A, BYTES_READ)]
    assert parsed.values == {"i-0": 100, "i-1": 200, "i-2": 300}
    assert 9999 not in parsed.values.values()
    assert unverified == []


def _unreliable_snapshot():
    # Conflict: standalone representation disagrees with the aggregated one.
    conflicting_i0 = _instance_node(
        NODE_A.identifier, "HDFS_SCAN_NODE(a)", "i-0",
        [TCounter(BYTES_READ, TUnit.BYTES, 999)])
    # Unit change for RowsRead (UNIT -> BYTES).
    node_a = _aggregated_node(
        NODE_A.identifier, "HDFS_SCAN_NODE(a)", SWAPPED_INSTANCES,
        [_agg_counter(BYTES_READ, TUnit.BYTES, [None, 150, 400, 150]),
         _agg_counter(ROWS_READ, TUnit.BYTES, [None, 25, None, None])])
    node_b = _aggregated_node(
        NODE_B.identifier, "HDFS_SCAN_NODE(b)", ["i-0"],
        [_agg_counter(BYTES_READ, TUnit.BYTES, [70])])
    # Node without a stable identity.
    anonymous_node = TRuntimeProfileNode(
        name="HDFS_SCAN_NODE(?)", num_children=0,
        counters=[TCounter(BYTES_READ, TUnit.BYTES, 123)], metadata=-1,
        indent=False, info_strings={INSTANCE_INFO_KEY: "i-0"},
        info_strings_display_order=[INSTANCE_INFO_KEY],
        child_counters_map={})
    return _tree([node_a, conflicting_i0, node_b, anonymous_node])


def _nameless_snapshot():
    # Aggregated node without input_profiles, in a tree that has no instance
    # names anywhere: instance identity cannot be verified.
    nameless_node = _aggregated_node(
        NODE_C.identifier, "HDFS_SCAN_NODE(c)", None,
        [_agg_counter(BYTES_READ, TUnit.BYTES, [88])])
    return _tree([nameless_node])


def test_conflict_unit_change_and_unverified_are_excluded_but_visible():
    report = diff_scan_counters(
        _first_snapshot(), _unreliable_snapshot(), COUNTER_UNITS)
    entries = _entry_map(report)

    conflict = entries[(NODE_A, BYTES_READ, "i-0")]
    assert conflict.status is ObservationStatus.CONFLICT
    assert conflict.delta is None

    unit_changed = entries[(NODE_A, ROWS_READ, "i-0")]
    assert unit_changed.status is ObservationStatus.UNIT_CHANGED
    assert unit_changed.previous_unit == TUnit.UNIT
    assert unit_changed.current_unit == TUnit.BYTES
    assert unit_changed.delta is None

    # The unaffected, healthy instance still produces a verifiable increment.
    healthy = entries[(NODE_B, BYTES_READ, "i-0")]
    assert healthy.status is ObservationStatus.GROWTH
    assert healthy.delta == 20

    assert report.totals_by_unit == {TUnit.BYTES: 20}
    assert [entry.delta for entry in report.contributions] == [20]

    reasons = {item.reason for item in report.unverified}
    assert "node_without_stable_identity" in reasons
    assert all(item.snapshot == "current" for item in report.unverified)


def test_missing_instance_identity_is_retained_as_unverified():
    report = diff_scan_counters(
        _first_snapshot(), _nameless_snapshot(), COUNTER_UNITS)
    # No current value is trustworthy enough to create a comparable pair.
    assert all(entry.status is ObservationStatus.UNOBSERVED
               for entry in report.entries)
    assert report.totals_by_unit == {}
    assert len(report.unverified) == 1
    unverified = report.unverified[0]
    assert unverified.snapshot == "current"
    assert unverified.reason == "instance_without_stable_identity"
    assert unverified.value == 88


def test_expected_unit_mismatch_is_flagged():
    report = diff_scan_counters(
        _first_snapshot(), _second_snapshot_per_instance(),
        {BYTES_READ: TUnit.BYTES, ROWS_READ: TUnit.BYTES})
    rows = _entry_map(report)[(NODE_A, ROWS_READ, "i-0")]
    assert rows.status is ObservationStatus.CONFLICT
    assert rows.expected_unit == TUnit.BYTES
    assert rows.delta is None
    assert TUnit.UNIT not in report.totals_by_unit


def test_missing_value_is_never_treated_as_zero():
    report = diff_scan_counters(
        _first_snapshot(), _second_snapshot_aggregated(), COUNTER_UNITS)
    unobserved = _entry_map(report)[(NODE_A, BYTES_READ, "i-2")]
    assert unobserved.status is ObservationStatus.UNOBSERVED
    # No fabricated -300 entry and nothing subtracts from the totals.
    assert report.totals_by_unit[TUnit.BYTES] == 70
    new_instance = _entry_map(report)[(NODE_A, BYTES_READ, "i-3")]
    assert new_instance.status is ObservationStatus.NEW_INSTANCE
    assert 400 not in report.totals_by_unit.values()


def test_invalid_input_type_rejected():
    with pytest.raises(TypeError):
        diff_scan_counters(object(), _second_snapshot_aggregated(),
                           COUNTER_UNITS)


def test_unchanged_entry_and_single_unit_total():
    previous = _tree([
        _aggregated_node(
            NODE_A.identifier, "HDFS_SCAN_NODE(a)", ["i-0", "i-1"],
            [_agg_counter(BYTES_READ, TUnit.BYTES, [100, 200])])])
    current = _tree([
        _aggregated_node(
            NODE_A.identifier, "HDFS_SCAN_NODE(a)", ["i-1", "i-0"],
            [_agg_counter(BYTES_READ, TUnit.BYTES, [200, 140])])])
    report = diff_scan_counters(
        previous, current, {BYTES_READ: TUnit.BYTES})
    entries = _entry_map(report)
    assert entries[(NODE_A, BYTES_READ, "i-0")].status \
        is ObservationStatus.GROWTH
    assert entries[(NODE_A, BYTES_READ, "i-1")].status \
        is ObservationStatus.UNCHANGED
    assert entries[(NODE_A, BYTES_READ, "i-1")].delta == 0
    assert report.total_delta == 40
    assert report.totals_by_unit == {TUnit.BYTES: 40}


def test_data_sink_identity_is_supported():
    sink_identity = NodeIdentity("data_sink", 3)
    sink_node = TRuntimeProfileNode(
        name="DATA_STREAM_SINK", num_children=0,
        counters=[TCounter(BYTES_READ, TUnit.BYTES, 60)], metadata=-1,
        indent=False, info_strings={INSTANCE_INFO_KEY: "i-9"},
        info_strings_display_order=[INSTANCE_INFO_KEY],
        child_counters_map={},
        node_metadata=TRuntimeProfileNodeMetadata(data_sink_id=3))
    previous = _tree([sink_node])
    current = _tree([TRuntimeProfileNode(
        name="DATA_STREAM_SINK", num_children=0,
        counters=[TCounter(BYTES_READ, TUnit.BYTES, 75)], metadata=-1,
        indent=False, info_strings={INSTANCE_INFO_KEY: "i-9"},
        info_strings_display_order=[INSTANCE_INFO_KEY],
        child_counters_map={},
        node_metadata=TRuntimeProfileNodeMetadata(data_sink_id=3))])
    report = diff_scan_counters(
        previous, current, {BYTES_READ: TUnit.BYTES})
    entry = report.find(sink_identity, BYTES_READ, "i-9")
    assert entry is not None
    assert entry.status is ObservationStatus.GROWTH
    assert entry.delta == 15
    assert report.total_delta == 15

