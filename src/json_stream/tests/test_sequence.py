import io
import json
from unittest import TestCase

from json_stream.sequence import (
    ACCEPTED,
    NEEDS_REVIEW,
    JSONTextSequenceReader,
    SequenceError,
    SequenceRecord,
    load_sequence,
)
from json_stream.util import to_standard_types

RS = b"\x1e"


def records(payload, chunk_size=1, **kwargs):
    return list(load_sequence(payload, chunk_size=chunk_size, **kwargs))


class TestJSONTextSequenceFraming(TestCase):
    def test_only_real_rs_reframes(self):
        payload = b"".join(
            [
                RS,
                b'{"text": "line one\\nline two"}\n',
                RS,
                b'{"escaped": "\\u001e"}\n',
                RS,
                b'{"escaped slash": "\\\\"}\n',
                RS,
                RS,
                RS,
                b'{"tail": true}',
            ]
        )
        recs = records(payload, chunk_size=1)
        self.assertEqual(4, len(recs))
        self.assertListEqual(
            [
                {"text": "line one\nline two"},
                {"escaped": "\x1e"},
                {"escaped slash": "\\"},
                {"tail": True},
            ],
            [to_standard_types(r.value) for r in recs],
        )
        self.assertTrue(all(r.accepted for r in recs))

    def test_multiline_object_is_not_split(self):
        payload = (
            RS
            + b'{\n  "a": 1,\n  "b": [\n    2,\n    3\n  ]\n}\n'
            + RS
            + b'{"c": 4}\n'
        )
        recs = records(payload, chunk_size=3)
        self.assertEqual(2, len(recs))
        self.assertEqual({"a": 1, "b": [2, 3]}, to_standard_types(recs[0].value))
        self.assertEqual({"c": 4}, to_standard_types(recs[1].value))

    def test_consecutive_rs_does_not_create_empty_record(self):
        recs = records(RS * 10, chunk_size=1)
        self.assertEqual([], recs)

    def test_whitespace_only_fragment_is_reported(self):
        recs = records(RS + b" \n\t " + RS + b'{"a": 1}\n', chunk_size=1)
        self.assertEqual(2, len(recs))
        self.assertFalse(recs[0].accepted)
        self.assertEqual((1, 5), recs[0].span)
        self.assertEqual("No JSON value found in non-empty fragment", recs[0].reason)
        self.assertTrue(recs[1].accepted)
        self.assertEqual((6, 15), recs[1].span)

    def test_leading_rs_is_not_required_to_buffer(self):
        # Fragment after the final RS is still a real received fragment.
        recs = records(b'{"a": 1}\n', chunk_size=1)
        self.assertEqual(1, len(recs))
        self.assertTrue(recs[0].accepted)
        self.assertEqual(0, recs[0].start)


class TestJSONTextSequenceVerdicts(TestCase):
    def test_number_immediately_followed_by_rs(self):
        recs = records(RS + b"123" + RS + b"456 \n" + RS, chunk_size=1)
        self.assertEqual(2, len(recs))
        self.assertFalse(recs[0].accepted)
        self.assertEqual(NEEDS_REVIEW, recs[0].status)
        self.assertEqual("incomplete_number", recs[0].error.kind)
        self.assertEqual((1, 4), recs[0].span)
        self.assertEqual(4, recs[0].error_offset)
        self.assertTrue(recs[1].accepted)
        self.assertEqual(456, recs[1].value)

    def test_number_at_eof_needs_trailing_whitespace(self):
        recs = records(RS + b"123", chunk_size=1)
        self.assertEqual(1, len(recs))
        self.assertFalse(recs[0].accepted)
        self.assertEqual("incomplete_number", recs[0].reason_kind)

        recs = records(RS + b"123\n", chunk_size=1)
        self.assertTrue(recs[0].accepted)
        self.assertEqual(123, recs[0].value)
        self.assertEqual((1, 5), recs[0].span)

    def test_number_in_container_does_not_need_trailing_whitespace(self):
        recs = records(RS + b'{"a": 123}' + RS, chunk_size=1)
        self.assertTrue(recs[0].accepted)

    def test_unclosed_object(self):
        recs = records(RS + b'{"a": [1, 2' + RS, chunk_size=1)
        self.assertEqual(1, len(recs))
        self.assertFalse(recs[0].accepted)
        self.assertEqual("Unterminated list at end of file", recs[0].error.reason)
        self.assertEqual((1, 12), recs[0].span)

    def test_trailing_characters_after_valid_object(self):
        payload = RS + b'{"ok": true} EXTRA' + RS + b'{"next": 1}\n'
        recs = records(payload, chunk_size=1)
        self.assertEqual(2, len(recs))
        self.assertFalse(recs[0].accepted)
        self.assertEqual("trailing_data", recs[0].error.kind)
        self.assertEqual(14, recs[0].error_offset)
        self.assertEqual({"ok": True}, to_standard_types(recs[0].value))
        self.assertEqual({"next": 1}, to_standard_types(recs[1].value))
        self.assertTrue(recs[1].accepted)

    def test_damaged_fragment_does_not_commit_partial_fields(self):
        payload = RS + b'{"a": 1, bad' + RS
        (rec,) = records(payload, chunk_size=1)
        self.assertFalse(rec.accepted)
        self.assertIsNone(rec.committed_value)
        self.assertEqual({"a": 1}, to_standard_types(rec.value))

    def test_recovery_from_next_rs(self):
        payload = (
            RS
            + b'{"broken": [1,'
            + RS
            + b'{"worked": true}\n'
            + RS
            + b"3 "
            + RS
            + b"[4]\n"
        )
        recs = records(payload, chunk_size=1)
        self.assertListEqual(
            [False, True, True, True], [r.accepted for r in recs]
        )
        self.assertEqual({"worked": True}, to_standard_types(recs[1].value))
        self.assertEqual(3, recs[2].value)
        self.assertEqual([4], to_standard_types(recs[3].value))


class TestJSONTextSequenceChunking(TestCase):
    STREAM = b"".join(
        [
            RS + b'{"id": 1,\n "text": "line1\\nline2"}\n',
            RS + b'{"escaped": "\\u001e"}\n',
            RS + b'{"emoji": "\xf0\x9f\x98\x80"}\n',
            RS + RS,
            RS + b"123",
            RS + b"456 \n",
            RS + b'{"open": [1, 2',
            RS + b'{"ok": true} EXTRA',
            RS + b'{"after": "recovered"}\n',
        ]
    )

    def test_chunk_sizes_give_same_results(self):
        baseline = None
        for chunk_size in (1, 2, 3, 4, 5, 7, 11, 64, len(self.STREAM)):
            with self.subTest(chunk_size=chunk_size):
                recs = records(self.STREAM, chunk_size=chunk_size)
                summary = [
                    (r.accepted, r.start, r.end, r.raw, r.reason)
                    for r in recs
                ]
                if baseline is None:
                    baseline = summary
                else:
                    self.assertListEqual(baseline, summary)

    def test_chunk_sizes_give_same_accepted_results(self):
        expected = None
        for chunk_size in (1, 2, 3, 4, 5, 7, 11, 64, len(self.STREAM)):
            with self.subTest(chunk_size=chunk_size):
                recs = records(self.STREAM, chunk_size=chunk_size)
                accepted = [
                    (r.start, r.end, to_standard_types(r.value))
                    for r in recs
                    if r.accepted
                ]
                if expected is None:
                    expected = accepted
                else:
                    self.assertListEqual(expected, accepted)

    def test_two_interrupted_tails(self):
        reader = JSONTextSequenceReader()
        self.assertListEqual([], list(reader.feed(RS + b'{"a": 1')))
        pending = reader.pending
        self.assertIsNotNone(pending)
        self.assertFalse(pending.accepted)
        self.assertEqual({}, to_standard_types(pending.value))
        self.assertEqual((1, 8), pending.span)

        finished = list(reader.feed(b', "b": 2}\n' + RS + b"12"))
        self.assertEqual(1, len(finished))
        self.assertTrue(finished[0].accepted)
        self.assertEqual({"a": 1, "b": 2}, to_standard_types(finished[0].value))
        self.assertIsNotNone(reader.pending)
        self.assertFalse(reader.pending.accepted)

        finished = list(reader.feed(b"3 \n"))
        self.assertEqual([], finished)
        (final,) = list(reader.finish())
        self.assertTrue(final.accepted)
        self.assertEqual(123, final.value)

    def test_pending_scalar_tail_does_not_commit(self):
        reader = JSONTextSequenceReader()
        reader.feed(RS + b"12")
        self.assertEqual(12, reader.pending.value)
        self.assertFalse(reader.pending.accepted)
        finished = list(reader.feed(b"3 \n"))
        self.assertEqual([], finished)
        (final,) = list(reader.finish())
        self.assertEqual(123, final.value)
        self.assertTrue(final.accepted)


class TestJSONTextSequenceOffsets(TestCase):
    def test_offsets_are_actual_byte_positions(self):
        payload = (
            RS
            + b'{"a": "\xf0\x9f\x98\x80"}\n'
            + RS
            + b'{"b": 2}\n'
            + RS
            + b"3 "
        )
        recs = records(payload, chunk_size=1)
        self.assertEqual(3, len(recs))
        self.assertEqual((1, 15), recs[0].span)
        self.assertEqual(16, recs[1].start)
        self.assertEqual(26, recs[2].start)
        self.assertEqual(1, recs[0].position)
        self.assertEqual(1, recs[0].position.offset)
        self.assertEqual((1, 15), recs[0].position.span)

    def test_offsets_with_unicode_and_chunking(self):
        payload = (
            RS
            + b'{"emoji": "\xf0\x9f\x98\x80", "text": "caf\xc3\xa9"}\n'
            + RS
            + b'{"next": 1}\n'
        )
        expected = None
        for chunk_size in (1, 2, 3, 5, 100):
            recs = records(payload, chunk_size=chunk_size)
            spans = [r.span for r in recs]
            if expected is None:
                expected = spans
            else:
                self.assertListEqual(expected, spans)
        self.assertEqual((1, 36), expected[0])
        self.assertEqual(37, expected[1][0])


class Reverse(str):
    """Marker used only to keep the SequenceRecord assertion helpers readable."""


class TestJSONTextSequenceReaderAPI(TestCase):
    def test_feed_result_reports_temporary_values(self):
        reader = JSONTextSequenceReader()
        result = reader.feed(RS + b'{"a": 1}\n' + RS + b"2")
        self.assertEqual(1, len(result))
        self.assertIs(result[0], reader.records[0])
        self.assertEqual(ACCEPTED, result[0].status)
        self.assertEqual(2, result.pending.value)
        self.assertFalse(result.pending.accepted)
        self.assertEqual(NEEDS_REVIEW, result.pending.status)

    def test_per_chunk_temporary_values(self):
        reader = JSONTextSequenceReader()
        seen = []
        for chunk in (b"\x1e{", b'"a": 1', b"}", b"\n", b"\x1e9 ", b"\n"):
            result = reader.feed(chunk)
            pending = result.pending
            seen.append(
                (
                    chunk,
                    None
                    if pending is None
                    else json.loads(json.dumps(to_standard_types(pending.value))),
                    None if pending is None else pending.accepted,
                )
            )
        self.assertListEqual(
            [
                (b"\x1e{", {}, False),
                (b'"a": 1', {}, False),
                (b"}", {"a": 1}, False),
                (b"\n", {"a": 1}, False),
                (b"\x1e9 ", 9, False),
                (b"\n", 9, False),
            ],
            seen,
        )

    def test_finish_is_idempotent(self):
        reader = JSONTextSequenceReader()
        reader.feed(RS + b'{"a": 1}\n')
        self.assertEqual(1, len(reader.finish()))
        self.assertEqual([], list(reader.finish()))

    def test_record_shape(self):
        recs = records(RS + b'{"a": 1}\n', chunk_size=1)
        rec = recs[0]
        self.assertIsInstance(rec, SequenceRecord)
        self.assertEqual(ACCEPTED, rec.status)
        self.assertTrue(rec.accepted)
        self.assertFalse(rec.needs_review)
        self.assertEqual((1, 10), rec.span)
        self.assertEqual((1, 10), rec.byte_range)
        self.assertEqual(0, rec.rs_offset)
        self.assertEqual(rec.value, rec[0])
        self.assertEqual(rec.status, rec[1])
        self.assertIsNone(rec.error)
        self.assertIsNone(rec.reason)
        self.assertEqual({"a": 1}, rec.to_standard_types())

    def test_error_holds_position(self):
        recs = records(RS + b'{"a": }' + RS, chunk_size=1)
        rec = recs[0]
        self.assertFalse(rec.accepted)
        self.assertIsInstance(rec.error, SequenceError)
        self.assertEqual(7, rec.error.offset)
        self.assertEqual(rec.error.position, rec.error_offset)

    def test_stream_from_file_object_and_chunk_iterable(self):
        payload = RS + b'{"a": 1}\n' + RS + b"2 \n"
        from_file = list(load_sequence(io.BytesIO(payload), chunk_size=2))
        from_iter = list(
            load_sequence([payload[i:i + 2] for i in range(0, len(payload), 2)])
        )
        self.assertListEqual(
            [(r.accepted, r.span) for r in from_file],
            [(r.accepted, r.span) for r in from_iter],
        )

    def test_text_chunks_are_measured_as_utf8(self):
        payload = [b'\x1e', '{"emoji": "\U0001f600"}\n', b'\x1e', '{"b": 2}\n']
        recs = list(load_sequence(payload))
        self.assertTrue(recs[0].accepted)
        self.assertEqual((1, 19), recs[0].span)
        self.assertEqual(20, recs[1].start)

    def test_long_record_does_not_need_a_single_read(self):
        expected = {"rows": list(range(20000))}
        payload = RS + json.dumps(expected).encode() + b"\n" + RS + b"9 \n"
        seen_reads = []

        class ChunkedReader(io.BytesIO):
            def read(self, size=-1):
                seen_reads.append(size)
                return super().read(min(size, 97) if size >= 0 else 97)

        recs = list(load_sequence(ChunkedReader(payload), persistent=True))
        self.assertEqual(2, len(recs))
        self.assertTrue(recs[0].accepted)
        self.assertDictEqual(expected, to_standard_types(recs[0].value))
        self.assertGreater(len(seen_reads), 100)


class TestExistingLoadManyUnchanged(TestCase):
    def test_load_many_still_handles_non_sequence_input(self):
        import json_stream

        payload = b'{"a": 1}\n[2]\n3\n'
        values = [
            to_standard_types(v)
            for v in json_stream.load_many(io.BytesIO(payload))
        ]
        self.assertListEqual([{"a": 1}, [2], 3], values)
