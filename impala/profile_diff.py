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

"""Reviewable scan-volume increments between two runtime profile samples.

The sync task periodically receives a Thrift ``TRuntimeProfileTree`` of a
running query.  This module turns the per-instance scan-node counters of two
consecutive samples into increments that can be audited afterwards.

Design rules, enforced on purpose:

* A value only participates in a diff when both its scan-node identity (plan
  node id / data sink id) and its execution-instance identity (the stable
  names in ``TAggregatedRuntimeProfileNode.input_profiles``) can be verified.
  Anything else is retained as an *unverified* observation, never guessed.
* A counter missing from one sample is never treated as zero.  Missing values
  become ``UNOBSERVED`` / ``NEW_INSTANCE`` entries, never ``0 - value``.
* The averaged ``TCounter`` values on aggregated nodes are never mixed into
  per-instance totals.
* One ``(node, counter, instance)`` triple contributes at most once per
  sample.  Consistent duplicate representations collapse into one
  observation; inconsistent ones become a ``CONFLICT`` and are excluded.
* A unit change between samples makes the pair non-comparable
  (``UNIT_CHANGED``); a newly seen instance has no baseline, so its
  cumulative value is reported but never counted as the interval increment.
* The summary only adds comparable positive growth, per unit, and lists
  every contribution behind each total.
"""

from dataclasses import dataclass, field
from enum import Enum

from thrift.protocol.TBinaryProtocol import TBinaryProtocol
from thrift.transport import TTransport

from impala._thrift_gen.Metrics.ttypes import TUnit
from impala._thrift_gen.RuntimeProfile.ttypes import TRuntimeProfileTree

# Info-string key used by unaggregated, per-instance profile nodes to state
# which entry of the aggregated ``input_profiles`` list they belong to.
INSTANCE_INFO_KEY = "Instance Name"


class ObservationStatus(Enum):
    # Comparable positive growth; the only status feeding summary totals.
    GROWTH = "growth"
    # Comparable, but the cumulative counter did not move between samples.
    UNCHANGED = "unchanged"
    # Comparable identity, but the current value is smaller than before.
    DECREASE = "decrease"
    # The instance reported the counter in the previous sample only.
    UNOBSERVED = "unobserved"
    # The instance is seen for the first time; no baseline to diff against.
    NEW_INSTANCE = "new_instance"
    # Two valid representations of the same identity disagree in a sample.
    CONFLICT = "conflict"
    # The unit attached to the counter changed between the two samples.
    UNIT_CHANGED = "unit_changed"


@dataclass(frozen=True)
class NodeIdentity:
    """Stable identity of a scan node inside a query plan."""

    kind: str  # "plan_node" or "data_sink"
    identifier: int

    def __str__(self):
        return "%s#%s" % (self.kind, self.identifier)


@dataclass
class CounterDelta:
    """The reviewable result for one (node, counter, instance) triple."""

    node: NodeIdentity
    counter_name: str
    instance: str
    status: ObservationStatus
    previous_value: int = None
    current_value: int = None
    delta: int = None
    previous_unit: int = None
    current_unit: int = None
    expected_unit: int = None
    detail: str = ""

    @property
    def unit(self):
        """Unit in force when the pair is comparable, else ``None``."""
        if self.previous_unit is not None \
                and self.previous_unit == self.current_unit:
            return self.previous_unit
        return None


@dataclass
class UnverifiedObservation:
    """An observation whose source identity could not be confirmed."""

    snapshot: str  # "previous" or "current"
    reason: str
    node_name: str
    counter_name: str
    instance: str = None
    value: int = None
    unit: int = None


@dataclass
class ScanDeltaReport:
    entries: list = field(default_factory=list)
    unverified: list = field(default_factory=list)
    totals_by_unit: dict = field(default_factory=dict)

    @property
    def contributions(self):
        """Comparable positive-growth entries behind ``totals_by_unit``."""
        return [entry for entry in self.entries
                if entry.status is ObservationStatus.GROWTH]

    @property
    def total_delta(self):
        """Comparable-growth sum when all contributions share one unit.

        Returns ``None`` if contributions use different units, in which case
        ``totals_by_unit`` holds the per-unit sums.
        """
        if not self.contributions:
            return 0
        units = {entry.unit for entry in self.contributions}
        if len(units) != 1:
            return None
        return sum(entry.delta for entry in self.contributions)

    def find(self, node, counter_name, instance):
        for entry in self.entries:
            if (entry.node == node
                    and entry.counter_name == counter_name
                    and entry.instance == instance):
                return entry
        return None


@dataclass
class _ParsedCounter:
    unit: int
    # Verified instance name -> cumulative value (one entry per instance).
    values: dict = field(default_factory=dict)
    # Instance name -> number of representations seen in this snapshot.
    representation_counts: dict = field(default_factory=dict)
    # (instance, first value, conflicting value, conflicting unit) tuples.
    conflicting_values: list = field(default_factory=list)


def _node_identity(node):
    metadata = node.node_metadata
    if metadata is not None:
        if metadata.plan_node_id is not None:
            return NodeIdentity("plan_node", metadata.plan_node_id)
        if metadata.data_sink_id is not None:
            return NodeIdentity("data_sink", metadata.data_sink_id)
    return None


def _root_instance_names(tree):
    for node in tree.nodes or []:
        aggregated = node.aggregated
        if aggregated is not None and aggregated.input_profiles:
            return list(aggregated.input_profiles)
    return None


def _deserialize_tree(blob):
    transport = TTransport.TMemoryBuffer(blob)
    protocol = TBinaryProtocol(transport)
    tree = TRuntimeProfileTree()
    tree.read(protocol)
    return tree


def _coerce_tree(profile):
    if isinstance(profile, TRuntimeProfileTree):
        return profile
    if isinstance(profile, (bytes, bytearray)):
        return _deserialize_tree(bytes(profile))
    raise TypeError(
        "expected a TRuntimeProfileTree or serialized TBinary bytes, got %s"
        % type(profile).__name__)


def parse_profile(profile, counter_names, snapshot_label):
    """Extract selected counters keyed by verified node/instance identity.

    Returns ``(observations, unverified)`` where ``observations`` maps
    ``(NodeIdentity, counter_name)`` to a :class:`_ParsedCounter` and
    ``unverified`` lists every observation lacking a verifiable identity.
    ``snapshot_label`` ("previous" or "current") stamps each unverified item.
    """
    tree = _coerce_tree(profile)
    selected = set(counter_names)
    root_names = _root_instance_names(tree)
    observations = {}
    unverified = []

    def record(node_identity, node_name, counter_name, instance, unit, value):
        if counter_name not in selected:
            return
        if node_identity is None:
            unverified.append(UnverifiedObservation(
                snapshot=snapshot_label,
                reason="node_without_stable_identity",
                node_name=node_name, counter_name=counter_name,
                instance=instance, value=value, unit=unit))
            return
        if not instance:
            unverified.append(UnverifiedObservation(
                snapshot=snapshot_label,
                reason="instance_without_stable_identity",
                node_name=node_name, counter_name=counter_name,
                instance=instance, value=value, unit=unit))
            return
        key = (node_identity, counter_name)
        parsed = observations.setdefault(key, _ParsedCounter(unit=unit))
        parsed.representation_counts[instance] = \
            parsed.representation_counts.get(instance, 0) + 1
        existing = parsed.values.get(instance)
        if existing is not None:
            if existing != value or parsed.unit != unit:
                parsed.conflicting_values.append(
                    (instance, existing, value, unit))
            return
        parsed.values[instance] = value

    for node in tree.nodes or []:
        node_identity = _node_identity(node)
        aggregated = node.aggregated
        if aggregated is not None:
            names = aggregated.input_profiles or root_names
            for agg_counter in aggregated.counters or []:
                if agg_counter.name not in selected:
                    continue
                values = agg_counter.values or []
                for index, has_value in enumerate(
                        agg_counter.has_value or []):
                    if not has_value or index >= len(values):
                        continue
                    if names and index < len(names):
                        instance = names[index]
                    else:
                        instance = None
                    record(node_identity, node.name, agg_counter.name,
                           instance, agg_counter.unit, values[index])
            # The node-level TCounter list only holds averaged values on
            # aggregated nodes; never use them as per-instance observations.
            continue

        instance = (node.info_strings or {}).get(INSTANCE_INFO_KEY)
        for counter in node.counters or []:
            record(node_identity, node.name, counter.name, instance,
                   counter.unit, counter.value)

    return observations, unverified


def _conflict_entry(node, counter_name, instance, previous, current,
                    expected_unit, snapshot, conflicts):
    base = dict(node=node, counter_name=counter_name, instance=instance,
                expected_unit=expected_unit)
    relevant = [item for item in conflicts if item[0] == instance]
    if snapshot == "previous":
        return CounterDelta(
            status=ObservationStatus.CONFLICT,
            previous_value=None,
            current_value=current.values.get(instance) if current else None,
            previous_unit=previous.unit,
            current_unit=current.unit if current else None,
            detail="conflicting representations in previous sample: %s"
                   % relevant, **base)
    return CounterDelta(
        status=ObservationStatus.CONFLICT,
        previous_value=previous.values.get(instance) if previous else None,
        current_value=None,
        previous_unit=previous.unit if previous else None,
        current_unit=current.unit,
        detail="conflicting representations in current sample: %s"
               % relevant, **base)


def _make_entry(node, counter_name, instance, previous, current,
                expected_unit):
    base = dict(node=node, counter_name=counter_name, instance=instance,
                expected_unit=expected_unit)
    previous_conflicts = {item[0] for item in previous.conflicting_values} \
        if previous is not None else set()
    current_conflicts = {item[0] for item in current.conflicting_values} \
        if current is not None else set()
    if instance in previous_conflicts:
        return _conflict_entry(
            node, counter_name, instance, previous, current,
            expected_unit, "previous", previous.conflicting_values)
    if instance in current_conflicts:
        return _conflict_entry(
            node, counter_name, instance, previous, current,
            expected_unit, "current", current.conflicting_values)
    previous_has = previous is not None and instance in previous.values
    current_has = current is not None and instance in current.values
    if not previous_has:
        return CounterDelta(
            status=ObservationStatus.NEW_INSTANCE,
            current_value=current.values[instance],
            previous_unit=None, current_unit=current.unit,
            detail="no baseline; current cumulative value is not an "
                   "interval increment", **base)
    if not current_has:
        return CounterDelta(
            status=ObservationStatus.UNOBSERVED,
            previous_value=previous.values[instance],
            previous_unit=previous.unit, current_unit=None,
            detail="counter absent from current sample; missing is not zero",
            **base)
    previous_value = previous.values[instance]
    current_value = current.values[instance]
    if previous.unit != current.unit:
        return CounterDelta(
            status=ObservationStatus.UNIT_CHANGED,
            previous_value=previous_value, current_value=current_value,
            previous_unit=previous.unit, current_unit=current.unit,
            detail="unit changed %s -> %s; values are not comparable"
                   % (TUnit._VALUES_TO_NAMES.get(previous.unit),
                      TUnit._VALUES_TO_NAMES.get(current.unit)),
            **base)
    if expected_unit is not None and previous.unit != expected_unit:
        return CounterDelta(
            status=ObservationStatus.CONFLICT,
            previous_value=previous_value, current_value=current_value,
            previous_unit=previous.unit, current_unit=current.unit,
            detail="observed unit %s does not match expected unit %s"
                   % (TUnit._VALUES_TO_NAMES.get(previous.unit),
                      TUnit._VALUES_TO_NAMES.get(expected_unit)),
            **base)
    delta = current_value - previous_value
    if delta > 0:
        status = ObservationStatus.GROWTH
        detail = "comparable growth"
    elif delta == 0:
        status = ObservationStatus.UNCHANGED
        detail = "comparable, no movement"
    else:
        status = ObservationStatus.DECREASE
        detail = "cumulative counter decreased; excluded from summary"
    return CounterDelta(
        status=status, previous_value=previous_value,
        current_value=current_value, delta=delta,
        previous_unit=previous.unit, current_unit=current.unit,
        detail=detail, **base)


def diff_scan_counters(previous_profile, current_profile, counter_units):
    """Compare two consecutive Thrift profiles for selected scan counters.

    ``counter_units`` maps a counter name to the ``TUnit`` the caller
    expects (the verifiable unit contract).  Returns a
    :class:`ScanDeltaReport`.
    """
    counter_units = dict(counter_units)
    previous, previous_unverified = parse_profile(
        previous_profile, counter_units.keys(), "previous")
    current, current_unverified = parse_profile(
        current_profile, counter_units.keys(), "current")

    keys = set(previous) | set(current)
    entries = []
    for node, counter_name in sorted(
            keys,
            key=lambda key: (key[0].kind, key[0].identifier, key[1])):
        previous_counter = previous.get((node, counter_name))
        current_counter = current.get((node, counter_name))
        instances = set()
        if previous_counter is not None:
            instances.update(previous_counter.values)
        if current_counter is not None:
            instances.update(current_counter.values)
        for instance in sorted(instances):
            entries.append(_make_entry(
                node, counter_name, instance, previous_counter,
                current_counter, counter_units.get(counter_name)))

    totals_by_unit = {}
    for entry in entries:
        if entry.status is ObservationStatus.GROWTH:
            totals_by_unit[entry.unit] = \
                totals_by_unit.get(entry.unit, 0) + entry.delta

    return ScanDeltaReport(
        entries=entries,
        unverified=previous_unverified + current_unverified,
        totals_by_unit=totals_by_unit)
