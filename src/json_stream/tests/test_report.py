from io import StringIO
from unittest import TestCase

from json_stream import load, ArraySummary, QualityReport
from json_stream.base import (
    TransientAccessException,
    TransientStreamingJSONList,
    TransientStreamingJSONObject,
)


class _Collector:
    """Record the report callbacks in delivery order."""

    def __init__(self):
        self.staged = []
        self.confirmed = []
        self.retracted = []
        self.report = QualityReport(
            on_stage=lambda summary: self.staged.append(summary),
            on_confirm=lambda path, summaries: self.confirmed.append((path, summaries)),
            on_retract=lambda path, summaries: self.retracted.append((path, summaries)),
        )

    def summary(self, path):
        for summary in self.staged:
            if summary.path == path:
                return summary
        raise KeyError(path)


def _consume_transiently(data, read_first_only=()):
    """Mimic a business consumer that only reads what it asks for."""
    for key, value in data.items():
        if isinstance(value, TransientStreamingJSONList):
            if key in read_first_only:
                next(iter(value))  # read just the first item, then move on
            else:
                list(value)  # explicitly drain the whole array


class TestQualityReport(TestCase):
    def test_accepted_device_stages_then_confirms(self):
        src = '{"id": 1, "readings": [1, 2, "x", true, null], "quality": "accepted"}'
        collector = _Collector()
        load(StringIO(src), report=collector.report).read_all()

        self.assertEqual(1, len(collector.staged))
        summary = collector.staged[0]
        self.assertEqual(("readings",), summary.path)
        self.assertEqual(5, summary.count)
        self.assertEqual({"int": 2, "str": 1, "bool": 1, "NoneType": 1}, dict(summary.types))
        self.assertEqual(3, summary.number_sum)
        self.assertIsInstance(summary.identity, int)

        self.assertEqual([((), [summary])], collector.confirmed)
        self.assertEqual([], collector.retracted)
        # once decided the summary is no longer held as pending
        self.assertEqual([], collector.report.staged)

    def test_nested_array_is_summarised_separately(self):
        src = '{"readings": [1, 2, [3, 4], {"a": 5}], "quality": "accepted"}'
        collector = _Collector()
        load(StringIO(src), report=collector.report).read_all()

        outer = collector.summary(("readings",))
        inner = collector.summary(("readings", 2))

        # the inner array's own numbers never leak into the outer total
        self.assertEqual(4, outer.count)
        self.assertEqual({"int": 2, "list": 1, "dict": 1}, dict(outer.types))
        self.assertEqual(3, outer.number_sum)

        self.assertEqual(2, inner.count)
        self.assertEqual({"int": 2}, dict(inner.types))
        self.assertEqual(7, inner.number_sum)

        self.assertEqual([((), [inner, outer])], collector.confirmed)

    def test_duplicate_readings_keys_count_twice(self):
        src = '{"readings": [1], "readings": [2, 3], "quality": "accepted"}'
        collector = _Collector()
        load(StringIO(src), report=collector.report).read_all()

        self.assertEqual(2, len(collector.staged))
        self.assertNotEqual(collector.staged[0].identity, collector.staged[1].identity)
        self.assertEqual([1], [s.number_sum for s in collector.staged[:1]])
        self.assertEqual(5, collector.staged[1].number_sum)
        self.assertEqual([((), collector.staged)], collector.confirmed)

    def test_quality_at_start_matches_quality_at_end(self):
        end = StringIO('{"readings": [1, 2, [3]], "id": 7, "quality": "accepted"}')
        start = StringIO('{"quality": "accepted", "readings": [1, 2, [3]], "id": 7}')

        end_collector = _Collector()
        load(end, report=end_collector.report).read_all()
        start_collector = _Collector()
        load(start, report=start_collector.report).read_all()

        self.assertEqual(
            [s.as_dict() for s in end_collector.staged],
            [s.as_dict() for s in start_collector.staged],
        )
        self.assertEqual(1, len(end_collector.confirmed))
        self.assertEqual(1, len(start_collector.confirmed))

    def test_rejected_device_retracts_staged_summaries(self):
        src = '{"readings": [1, 2], "quality": "rejected"}'
        collector = _Collector()
        load(StringIO(src), report=collector.report).read_all()

        self.assertEqual(1, len(collector.staged))  # the report did appear first
        self.assertEqual([], collector.confirmed)
        self.assertEqual([((), collector.staged)], collector.retracted)
        self.assertEqual([], collector.report.staged)

    def test_missing_quality_is_retracted(self):
        collector = _Collector()
        load(StringIO('{"readings": [1]}'), report=collector.report).read_all()
        self.assertEqual([((), collector.staged)], collector.retracted)
        self.assertEqual([], collector.confirmed)

    def test_rejected_second_device_after_first_confirmed(self):
        src = (
            '{"devices": ['
            '{"id": 1, "readings": [1], "quality": "accepted"},'
            '{"id": 2, "readings": [2], "quality": "rejected"}'
            ']}'
        )
        collector = _Collector()
        load(StringIO(src), report=collector.report).read_all()

        first, second = collector.staged
        self.assertEqual([(("devices", 0), [first])], collector.confirmed)
        self.assertEqual([(("devices", 1), [second])], collector.retracted)

    def test_truncated_device_retracts_already_staged_summary(self):
        # quality is shown as accepted early, a completed readings array is
        # staged, then the device is cut off before closing
        src = '{"quality": "accepted", "readings": [1], "readings": [2, 3'
        collector = _Collector()
        with self.assertRaisesRegex(ValueError, "Unterminated list"):
            load(StringIO(src), report=collector.report).read_all()

        self.assertEqual(1, len(collector.staged))
        self.assertEqual([], collector.confirmed)
        self.assertEqual([((), collector.staged)], collector.retracted)

    def test_truncation_inside_nested_device(self):
        # the second readings array completes (and is staged) but its device
        # object is cut off before it can close
        src = '[{"id": 1, "readings": [1], "quality": "accepted"}, {"id": 2, "readings": [2]'
        collector = _Collector()
        with self.assertRaises(ValueError):
            load(StringIO(src), report=collector.report).read_all()

        first, second = collector.staged
        self.assertEqual([((0,), [first])], collector.confirmed)
        self.assertEqual([((1,), [second])], collector.retracted)

    def test_tokenizer_level_truncation_also_retracts(self):
        # the array closes and is staged, then an unterminated string cuts the
        # device before its quality decision can be reached
        src = '{"readings": [1], "name": "cut'
        collector = _Collector()
        with self.assertRaises(ValueError):
            load(StringIO(src), report=collector.report).read_all()
        self.assertEqual([], collector.confirmed)
        self.assertEqual([((), collector.staged)], collector.retracted)
        self.assertEqual([], collector.report.staged)

    def test_implicit_skip_matches_explicit_iteration(self):
        src = '{"readings": [10, 20, [30], 40], "other": 7, "quality": "accepted"}'

        explicit = _Collector()
        load(StringIO(src), report=explicit.report).read_all()

        implicit = _Collector()
        data = load(StringIO(src), report=implicit.report)
        _consume_transiently(data, read_first_only={"readings"})
        data.read_all()

        self.assertEqual(
            [s.as_dict() for s in explicit.staged],
            [s.as_dict() for s in implicit.staged],
        )
        outer = implicit.summary(("readings",))
        # item 0 was handed to the business code once and must not be double counted
        self.assertEqual(4, outer.count)
        self.assertEqual(70, outer.number_sum)

    def test_explicitly_drained_array_matches_full_read(self):
        src = '{"readings": [1, 2, 3], "quality": "accepted"}'

        full = _Collector()
        load(StringIO(src), report=full.report).read_all()

        drained = _Collector()
        data = load(StringIO(src), report=drained.report)
        for key, value in data.items():
            if isinstance(value, TransientStreamingJSONList):
                self.assertEqual([1, 2, 3], list(value))
        data.read_all()

        self.assertEqual(full.staged[0].as_dict(), drained.staged[0].as_dict())

    def test_no_read_ahead_before_array_closes(self):
        stage_points = []
        report = QualityReport(on_stage=lambda summary: stage_points.append(summary.path))
        data = load(StringIO('{"readings": [1, 2, 3], "quality": "accepted"}'), report=report)

        readings = data["readings"]
        iterator = iter(readings)
        next(iterator)
        self.assertEqual([], stage_points)  # nothing reported while still open
        readings.read_all()
        self.assertEqual([("readings",)], stage_points)

    def test_identities_are_stable_and_unique_across_consumption_styles(self):
        src = '{"readings": [1, [2]], "readings": [3], "quality": "accepted"}'

        def identities():
            collector = _Collector()
            data = load(StringIO(src), report=collector.report)
            _consume_transiently(data, read_first_only={"readings"})
            data.read_all()
            return [s.identity for s in collector.staged]

        first_run = identities()
        second_run = identities()
        self.assertEqual(first_run, second_run)
        self.assertEqual(len(first_run), len(set(first_run)))

    def test_reporting_preserves_transient_semantics(self):
        data = load(
            StringIO('{"a": 1, "readings": [1, 2, 3], "b": 2}'),
            report=QualityReport(),
        )
        self.assertIsInstance(data, TransientStreamingJSONObject)
        readings = data["readings"]
        self.assertIsInstance(readings, TransientStreamingJSONList)

        self.assertEqual(2, readings[1])
        with self.assertRaises(TransientAccessException):
            _ = readings[0]

        with self.assertRaises(TransientAccessException):
            iter(readings)

    def test_without_report_uses_original_transient_objects(self):
        data = load(StringIO('{"readings": [1, 2]}'))
        self.assertIs(type(data), TransientStreamingJSONObject)
        readings = data["readings"]
        self.assertIs(type(readings), TransientStreamingJSONList)

    def test_reporting_rejects_persistent_mode(self):
        with self.assertRaisesRegex(ValueError, "transient mode"):
            load(StringIO('{}'), persistent=True, report=QualityReport())

    def test_empty_readings_array(self):
        collector = _Collector()
        load(StringIO('{"readings": [], "quality": "accepted"}'),
             report=collector.report).read_all()
        summary = collector.staged[0]
        self.assertEqual(0, summary.count)
        self.assertEqual({}, dict(summary.types))
        self.assertEqual(0, summary.number_sum)
        self.assertEqual(1, len(collector.confirmed))

    def test_summary_equality_uses_identity(self):
        first = ArraySummary(0, ("readings",), ())
        duplicate = ArraySummary(0, ("readings",), ())
        other = ArraySummary(1, ("readings",), ())
        self.assertEqual(first, duplicate)
        self.assertNotEqual(first, other)
        self.assertEqual({first, duplicate}, {first})
