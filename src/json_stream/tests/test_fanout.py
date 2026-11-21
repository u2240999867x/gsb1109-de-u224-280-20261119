import io
import unittest
from unittest import TestCase

import json_stream
from json_stream.fanout import (
    EventKind,
    FanoutBlocked,
    FanoutClosed,
    FanoutParseError,
    FanoutTruncated,
    open_fanout,
)
from json_stream.select_tokenizer import default_tokenizer

ORDER = (
    b'{"order":"SO-1","status":"open","0":"not-index",'
    b'"details":[],'
    b'"details":[{"sku":"A","qty":1},{"sku":"B","qty":2},{"sku":"C","qty":3}],'
    b'"total":6}'
)

TRUNCATED = (
    b'{"order":"SO-2","status":"open",'
    b'"details":[],'
    b'"details":[{"sku":"A","qty":1},{"sku":"B"'
)


def drain(branch):
    events = []
    while True:
        try:
            events.append(branch.pull())
        except StopIteration:
            return events


class TestFanoutStructure(TestCase):
    def test_audit_is_structural_and_empty_array_has_matching_pair(self):
        fanout = open_fanout(io.BytesIO(ORDER), report_paths=[])
        events = drain(fanout.audit)

        self.assertFalse([event for event in events if event.kind == EventKind.VALUE])
        self.assertEqual(events[-1].kind, EventKind.OBJECT_END)
        self.assertEqual(events[-1].path, ())
        self.assertEqual(fanout.audit.last_event, events[-1])

        field_events = [event for event in events if event.kind == EventKind.FIELD]
        details_fields = [event for event in field_events if event.key == "details"]
        self.assertEqual(len(details_fields), 2)
        self.assertNotEqual(details_fields[0].id, details_fields[1].id)

        first_details_start = events[events.index(details_fields[0]) + 1]
        self.assertEqual(first_details_start.kind, EventKind.ARRAY_START)
        self.assertEqual(first_details_start.path, ("details",))
        first_details_end = events[events.index(details_fields[1]) - 1]
        self.assertEqual(first_details_end.kind, EventKind.ARRAY_END)
        self.assertEqual(first_details_end.container_id, first_details_start.container_id)
        self.assertNotEqual(first_details_end.id, first_details_start.id)

    def test_numeric_text_key_is_distinct_from_array_index(self):
        fanout = open_fanout(io.BytesIO(ORDER), report_paths=[("0",), ("details", 0)])
        values = [event for event in drain(fanout.report) if event.kind == EventKind.VALUE]
        self.assertEqual([(event.path, event.value) for event in values], [(("0",), "not-index")])

    def test_every_event_id_is_unique_and_container_ids_match(self):
        fanout = open_fanout(io.BytesIO(ORDER), report_paths=[])
        events = drain(fanout.audit)
        ids = [event.id for event in events]
        self.assertEqual(len(ids), len(set(ids)))

        starts = {}
        for event in events:
            if event.kind in (EventKind.OBJECT_START, EventKind.ARRAY_START):
                starts[event.container_id] = event
            elif event.kind in (EventKind.OBJECT_END, EventKind.ARRAY_END):
                self.assertEqual(starts[event.container_id].path, event.path)
                self.assertNotEqual(starts[event.container_id].id, event.id)

    def test_report_receives_only_configured_values(self):
        fanout = open_fanout(
            io.BytesIO(ORDER),
            report_paths=[("order",), ("details", "*", "qty"), ("total",)],
        )
        values = [event for event in drain(fanout.report) if event.kind == EventKind.VALUE]
        self.assertEqual(
            [(event.path, event.value) for event in values],
            [
                (("order",), "SO-1"),
                (("details", 0, "qty"), 1),
                (("details", 1, "qty"), 2),
                (("details", 2, "qty"), 3),
                (("total",), 6),
            ],
        )

    def test_completion_requires_closing_root(self):
        fanout = open_fanout(io.BytesIO(ORDER), report_paths=[])
        events = drain(fanout.audit)
        self.assertEqual(events[-1].kind, EventKind.OBJECT_END)
        self.assertTrue(fanout.complete)
        self.assertTrue(fanout.audit.complete)

        truncated = open_fanout(io.BytesIO(TRUNCATED), report_paths=[])
        with self.assertRaises(FanoutTruncated):
            drain(truncated.audit)
        self.assertFalse(truncated.complete)
        self.assertTrue(truncated.truncated)


class TestFanoutBackpressure(TestCase):
    REPORT_PATHS = [("order",), ("details", "*", "qty"), ("total",)]

    def _reference_audit(self):
        fanout = open_fanout(io.BytesIO(ORDER), report_paths=[], pending_limit=None)
        return drain(fanout.audit)

    def test_audit_reports_blocking_report_and_loses_nothing(self):
        fanout = open_fanout(
            io.BytesIO(ORDER), report_paths=self.REPORT_PATHS, pending_limit=4
        )
        expected = self._reference_audit()
        first_audit = fanout.audit.pull()
        first_report = fanout.report.pull()
        self.assertEqual(first_audit.kind, EventKind.OBJECT_START)
        self.assertEqual(first_report.value, "SO-1")

        observed = [first_audit]
        blocked = None
        while True:
            try:
                observed.append(fanout.audit.pull())
            except FanoutBlocked as exc:
                blocked = exc
                break

        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.blocking, ("report",))
        self.assertEqual(fanout.blocked_on, ("report",))
        self.assertEqual(fanout.report.pending, 4)
        tokens_at_block = fanout.tokens_read

        # A blocked pull neither consumes source tokens nor discards the head
        # event that report still has to accept.
        with self.assertRaises(FanoutBlocked):
            fanout.audit.pull()
        self.assertEqual(fanout.tokens_read, tokens_at_block)

        fanout.report.pull()
        observed.append(fanout.audit.pull())
        fanout.report.close()
        observed.extend(drain(fanout.audit))

        self.assertEqual(observed, expected)
        self.assertTrue(fanout.audit.complete)
        with self.assertRaises(FanoutClosed):
            fanout.report.pull()

    def test_report_reports_blocking_audit_and_survives_close(self):
        fanout = open_fanout(
            io.BytesIO(ORDER), report_paths=self.REPORT_PATHS, pending_limit=4
        )
        reference = [
            event
            for event in drain(
                open_fanout(
                    io.BytesIO(ORDER), report_paths=self.REPORT_PATHS, pending_limit=None
                ).report
            )
            if event.kind == EventKind.VALUE
        ]

        first_audit = fanout.audit.pull()
        first_report = fanout.report.pull()
        self.assertEqual(first_audit.kind, EventKind.OBJECT_START)

        observed = [first_report]
        blocked = None
        while True:
            try:
                observed.append(fanout.report.pull())
            except FanoutBlocked as exc:
                blocked = exc
                break

        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.blocking, ("audit",))
        self.assertEqual(fanout.blocked_on, ("audit",))
        self.assertEqual(fanout.audit.pending, 4)
        tokens_at_block = fanout.tokens_read

        with self.assertRaises(FanoutBlocked):
            fanout.report.pull()
        self.assertEqual(fanout.tokens_read, tokens_at_block)

        fanout.audit.pull()
        fanout.audit.close()
        observed.extend(
            event for event in drain(fanout.report) if event.kind == EventKind.VALUE
        )

        self.assertEqual(observed, reference)
        self.assertTrue(fanout.report.complete)
        with self.assertRaises(FanoutClosed):
            fanout.audit.pull()


class TestFanoutTruncation(TestCase):
    REPORT_PATHS = [("order",), ("details", "*", "sku"), ("details", "*", "qty")]

    def test_truncation_keeps_delivered_data_and_reports_open_levels(self):
        fanout = open_fanout(
            io.BytesIO(TRUNCATED), report_paths=self.REPORT_PATHS, pending_limit=None
        )
        audit_events = []
        report_values = []
        audit_error = None
        report_error = None

        while True:
            try:
                audit_events.append(fanout.audit.pull())
            except FanoutTruncated as exc:
                audit_error = exc
                break
        while True:
            try:
                event = fanout.report.pull()
            except FanoutTruncated as exc:
                report_error = exc
                break
            if event.kind == EventKind.VALUE:
                report_values.append(event)

        self.assertIsNotNone(audit_error)
        self.assertIsNotNone(report_error)
        self.assertFalse(fanout.complete)
        self.assertTrue(fanout.truncated)
        self.assertEqual(audit_events[-1].kind, EventKind.FIELD)
        self.assertFalse(
            [
                event
                for event in audit_events
                if event.kind in (EventKind.OBJECT_END, EventKind.ARRAY_END)
                and event.path == ()
            ]
        )

        self.assertEqual(
            [(event.path, event.value) for event in report_values],
            [
                (("order",), "SO-2"),
                (("details", 0, "sku"), "A"),
                (("details", 0, "qty"), 1),
                (("details", 1, "sku"), "B"),
            ],
        )
        self.assertEqual(report_error.last_value, "B")
        self.assertEqual(report_error.last_event.id, report_values[-1].id)
        self.assertEqual(audit_error.last_event.id, audit_events[-1].id)

        self.assertEqual(
            [(level.kind, level.path) for level in audit_error.unfinished],
            [("object", ()), ("array", ("details",)), ("object", ("details", 1))],
        )
        self.assertEqual(report_error.unfinished, audit_error.unfinished)

    def test_truncation_with_report_as_survivor(self):
        fanout = open_fanout(
            io.BytesIO(TRUNCATED), report_paths=self.REPORT_PATHS, pending_limit=2
        )
        self.assertEqual(fanout.audit.pull().kind, EventKind.OBJECT_START)
        first_report = fanout.report.pull()
        self.assertEqual(first_report.value, "SO-2")
        fanout.audit.close()

        values = [first_report.value]
        error = None
        while True:
            try:
                event = fanout.report.pull()
            except FanoutTruncated as exc:
                error = exc
                break
            if event.kind == EventKind.VALUE:
                values.append(event.value)

        self.assertIsNotNone(error)
        self.assertEqual(values, ["SO-2", "A", 1, "B"])
        self.assertEqual(error.last_value, "B")
        self.assertEqual(
            [level.path for level in error.unfinished],
            [(), ("details",), ("details", 1)],
        )
        with self.assertRaises(FanoutClosed):
            fanout.audit.pull()


class TestFanoutErrors(TestCase):
    def _drain_until_error(self, branch):
        events = []
        while True:
            try:
                events.append(branch.pull())
            except StopIteration:
                return events, None
            except Exception as exc:
                return events, exc

    def test_incomplete_tokens_are_reported_as_truncation(self):
        for payload in (b'{"a":"x', b'{"a":1', b'{"a":tru'):
            with self.subTest(payload=payload):
                fanout = open_fanout(io.BytesIO(payload), report_paths=[("a",)])
                events, error = self._drain_until_error(fanout.audit)
                self.assertIsInstance(error, FanoutTruncated)
                self.assertTrue(fanout.truncated)
                self.assertFalse(fanout.failed)
                self.assertEqual(
                    [level.path for level in error.unfinished],
                    [()],
                )
                self.assertTrue(events)

    def test_invalid_json_is_reported_as_parse_error(self):
        fanout = open_fanout(io.BytesIO(b'{"a":1,}'), report_paths=[("a",)])
        events, error = self._drain_until_error(fanout.audit)
        self.assertIsInstance(error, FanoutParseError)
        self.assertEqual(error.branch, "audit")
        self.assertIsNotNone(error.error)
        self.assertTrue(fanout.failed)
        self.assertFalse(fanout.truncated)
        self.assertEqual(events[-1].kind, EventKind.FIELD)

    def test_both_tokenizers_produce_same_audit_stream(self):
        from json_stream.tokenizer import tokenize

        audit_streams = []
        for tokenizer in (default_tokenizer, tokenize):
            fanout = open_fanout(
                io.BytesIO(ORDER), report_paths=[], tokenizer=tokenizer
            )
            audit_streams.append(
                [(event.kind, event.path, event.key) for event in drain(fanout.audit)]
            )
        self.assertEqual(audit_streams[0], audit_streams[1])

    def test_closed_branch_rejects_pull(self):
        fanout = open_fanout(io.BytesIO(ORDER), report_paths=[])
        fanout.report.close()
        with self.assertRaises(FanoutClosed):
            fanout.report.pull()

    def test_branch_is_iterable_and_nextable(self):
        fanout = open_fanout(io.BytesIO(b'{"a":1}'), report_paths=[("a",)])
        self.assertEqual(next(fanout.report).value, 1)
        self.assertEqual(fanout.branch("audit") is fanout.audit, True)
        with self.assertRaises(KeyError):
            fanout.branch("other")
        self.assertEqual(
            [event.kind for event in fanout.audit],
            [EventKind.OBJECT_START, EventKind.FIELD, EventKind.OBJECT_END],
        )

    def test_closing_every_branch_stops_without_further_source_reads(self):
        fanout = open_fanout(io.BytesIO(ORDER), report_paths=[("order",)])
        fanout.audit.pull()
        fanout.close()
        self.assertTrue(fanout.stopped)
        with self.assertRaises(FanoutClosed):
            fanout.audit.pull()
        self.assertEqual(fanout.tokens_read, 1)


class TestFanoutCompatibility(TestCase):
    def test_visitor_still_works_after_fanout_import(self):
        visited = []
        json_stream.visit(
            io.StringIO('{"x": 1, "y": [], "z": {"k": 2}}'),
            lambda item, path: visited.append((item, path)),
        )
        self.assertEqual(
            visited,
            [(1, ("x",)), ([], ("y",)), (2, ("z", "k"))],
        )


if __name__ == "__main__":
    unittest.main()
