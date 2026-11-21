import io
from unittest import TestCase

import json_stream
from json_stream import EnvelopeStream, to_standard_types

ENVELOPE_OBJECT = b'{"route": "line\\nend"}'  # object ending in an escaped string
ENVELOPE_NUMBER = b'1.5e10'  # number with an exponent
BUSINESS = '"日本語"'.encode('utf-8')  # legal root whose content starts multi-byte
WHITESPACE = b' \r\n '


def logged_chunks(chunks, log):
    for chunk in chunks:
        log.append(chunk)
        yield chunk


class TestProbe(TestCase):
    def test_budget_insufficient_then_extended(self):
        log = []
        chunks = [b'', b'  ', b'\t\n', b'{"route": 1}', b' ', b'{"x": 2}']
        stream = EnvelopeStream(logged_chunks(chunks, log))

        self.assertEqual(stream.peek(0), b'')
        self.assertIsNone(stream.peek_root_type(0))
        self.assertEqual(log, [])

        # budget only covers whitespace: cannot distinguish candidates
        self.assertIsNone(stream.peek_root_type(2))
        self.assertEqual(log, [b'', b'  '])  # empty chunk skipped, no over-consumption

        self.assertIsNone(stream.peek_root_type(4))
        self.assertEqual(log, [b'', b'  ', b'\t\n'])

        # extend the budget: the first non-whitespace byte decides
        self.assertEqual(stream.peek_root_type(6), 'object')
        self.assertEqual(stream.peek(6), b'  \t\n{"')
        self.assertEqual(log, [b'', b'  ', b'\t\n', b'{"route": 1}'])
        # probing must not consume subsequent chunks
        self.assertNotIn(b' ', log[4:])
        self.assertNotIn(b'{"x": 2}', log)

    def test_root_types(self):
        cases = [
            (b'{', 'object'), (b'[', 'array'), (b'"', 'string'),
            (b'7', 'number'), (b'-', 'number'), (b'0', 'number'),
            (b't', 'boolean'), (b'f', 'boolean'), (b'n', 'null'),
            (b'N', 'number'), (b'I', 'number'),
        ]
        for first, expected in cases:
            with self.subTest(first=first):
                stream = EnvelopeStream([b' \t', first + b'rest'])
                self.assertEqual(stream.peek_root_type(10), expected)

    def test_invalid_first_character(self):
        stream = EnvelopeStream([b'x'])
        with self.assertRaises(ValueError):
            stream.peek_root_type(1)

    def test_peek_after_choice_raises(self):
        stream = EnvelopeStream([b'1'])
        stream.load_many()
        with self.assertRaises(RuntimeError):
            stream.peek(1)
        with self.assertRaises(RuntimeError):
            stream.peek_root_type(1)


class TestLoadManyPath(TestCase):
    def test_lossless_full_replay(self):
        full = b'  {"a": 1}\n[1, 2] 3 true null "x" {} [] 1.5e10 ' + '"日本"  '.encode('utf-8')
        chunks = [b'', full[:5], b'', full[5:17], full[17:40], b'', full[40:]]
        stream = EnvelopeStream(chunks)
        self.assertEqual(stream.peek_root_type(8), 'object')

        for persistent in (False, True):
            stream = EnvelopeStream(list(chunks))
            stream.peek_root_type(8)
            items = [to_standard_types(v) for v in stream.load_many(persistent=persistent)]
            expected = [
                to_standard_types(v)
                for v in json_stream.load_many(io.BytesIO(full), persistent=persistent)
            ]
            self.assertEqual(items, expected)

    def test_replay_is_byte_for_byte(self):
        full = b' \n{"a": "' + '日'.encode('utf-8') + b'"} [1] '
        stream = EnvelopeStream([full[:3], full[3:10], full[10:], b''])
        stream.peek(5)
        self.assertEqual(b''.join(stream._replay_chunks()), full)


class TestEnvelopePath(TestCase):
    def test_object_envelope_same_chunk(self):
        # last char of envelope, following whitespace and start of next root in one chunk
        joint = ENVELOPE_OBJECT[-1:] + WHITESPACE + BUSINESS[:3]
        chunks = [b'', ENVELOPE_OBJECT[:-1], joint, BUSINESS[3:], b'']
        stream = EnvelopeStream(chunks)
        self.assertEqual(stream.peek_root_type(1), 'object')
        envelope = stream.load_envelope()
        self.assertEqual(dict(envelope.items()), {'route': 'line\nend'})
        tail = list(stream.remaining_bytes())
        self.assertEqual(b''.join(tail), WHITESPACE + BUSINESS)
        self.assertEqual(json_stream.load(iter(tail)), '日本語')

    def test_object_envelope_split_chunks(self):
        # last char of envelope, whitespace and next root start in different chunks,
        # with the multi-byte start of the next root split across chunks
        chunks = [
            ENVELOPE_OBJECT[:-1],
            ENVELOPE_OBJECT[-1:],
            b' ',
            b'\r\n ',
            BUSINESS[:1],
            BUSINESS[1:3],  # splits the 3-byte 日
            BUSINESS[3:],
        ]
        stream = EnvelopeStream(chunks)
        envelope = stream.load_envelope()
        envelope.read_all()
        tail = list(stream.remaining_bytes())
        self.assertEqual(b''.join(tail), WHITESPACE + BUSINESS)
        self.assertEqual(json_stream.load(iter(tail)), '日本語')

    def test_object_envelope_persistent(self):
        chunks = [ENVELOPE_OBJECT + WHITESPACE + BUSINESS]
        stream = EnvelopeStream(chunks)
        envelope = stream.load_envelope(persistent=True)
        tail = list(stream.remaining_bytes())
        self.assertEqual(envelope['route'], 'line\nend')
        self.assertEqual(b''.join(tail), WHITESPACE + BUSINESS)

    def test_number_envelope_with_exponent(self):
        for chunks in (
            # delimiter whitespace in the same chunk as the final exponent digit
            [ENVELOPE_NUMBER + b' ', BUSINESS],
            # delimiter in its own chunk
            [ENVELOPE_NUMBER, b' ', BUSINESS],
            # exponent split across chunks, whitespace and next root split too
            [b'1.5e1', b'0', b' ', BUSINESS[:2], BUSINESS[2:]],
        ):
            with self.subTest(chunks=chunks):
                stream = EnvelopeStream(chunks)
                self.assertEqual(stream.peek_root_type(1), 'number')
                envelope = stream.load_envelope()
                self.assertEqual(envelope, 1.5e10)
                tail = list(stream.remaining_bytes())
                # handoff starts at the first byte after the number: the
                # original whitespace is preserved, not consumed by the parser
                self.assertEqual(b''.join(tail), b' ' + BUSINESS)
                self.assertEqual(json_stream.load(iter(tail)), '日本語')

    def test_number_envelope_at_eof(self):
        stream = EnvelopeStream([b'1.5', b'e10'])
        self.assertEqual(stream.load_envelope(), 1.5e10)
        self.assertEqual(list(stream.remaining_bytes()), [])

    def test_number_envelope_whitespace_only_tail(self):
        stream = EnvelopeStream([ENVELOPE_NUMBER + b' ', b' \t'])
        self.assertEqual(stream.load_envelope(), 1.5e10)
        tail = list(stream.remaining_bytes())
        self.assertEqual(b''.join(tail), b'  \t')
        self.assertEqual(list(json_stream.load_many(iter(tail))), [])

    def test_object_envelope_no_followable_root(self):
        stream = EnvelopeStream([ENVELOPE_OBJECT, b'', b' \n'])
        envelope = stream.load_envelope()
        self.assertEqual(dict(envelope.items()), {'route': 'line\nend'})
        tail = list(stream.remaining_bytes())
        self.assertEqual(b''.join(tail), b' \n')
        self.assertEqual(list(json_stream.load_many(iter(tail))), [])

    def test_tail_is_not_reencoded(self):
        # the business root contains an escape sequence; the handoff must
        # deliver the original bytes, not a decoded/re-encoded form
        business = b'"\\u65e5\\u672c"'
        chunks = [ENVELOPE_OBJECT, b' ', business]
        stream = EnvelopeStream(chunks)
        envelope = stream.load_envelope()
        envelope.read_all()
        tail = list(stream.remaining_bytes())
        self.assertEqual(b''.join(tail), b' ' + business)
        self.assertEqual(json_stream.load(iter(tail)), '日本')

    def test_chunking_invariance(self):
        full = ENVELOPE_OBJECT + WHITESPACE + BUSINESS
        chunkings = [
            [full],
            [bytes([byte]) for byte in full],
            [b'', full[:7], full[7:20], b'', full[20:]],
        ]
        tails = []
        for chunks in chunkings:
            stream = EnvelopeStream(list(chunks))
            envelope = stream.load_envelope()
            envelope.read_all()
            tails.append(b''.join(stream.remaining_bytes()))
        self.assertEqual(tails[0], tails[1])
        self.assertEqual(tails[0], tails[2])
        self.assertEqual(tails[0], WHITESPACE + BUSINESS)

    def test_leading_whitespace_before_envelope(self):
        stream = EnvelopeStream([b'  ', ENVELOPE_OBJECT, b' ', BUSINESS])
        envelope = stream.load_envelope()
        self.assertEqual(dict(envelope.items()), {'route': 'line\nend'})
        self.assertEqual(b''.join(stream.remaining_bytes()), b' ' + BUSINESS)


class TestCorruptEnvelope(TestCase):
    def test_corrupt_object_envelope_no_handoff(self):
        # prefix identifies the type, but the root corrupts before it ends
        chunks = [b'{"route": "abc', b'} garbage']
        stream = EnvelopeStream(chunks)
        self.assertEqual(stream.peek_root_type(1), 'object')
        envelope = stream.load_envelope()
        with self.assertRaises(ValueError):
            list(stream.remaining_bytes())

    def test_truncated_object_envelope_no_handoff(self):
        stream = EnvelopeStream([b'{"route": "abc"'])  # never closed
        self.assertEqual(stream.peek_root_type(10), 'object')
        stream.load_envelope()
        with self.assertRaises(ValueError):
            list(stream.remaining_bytes())

    def test_corrupt_number_envelope_no_handoff(self):
        # '1.5e' looks like a legal value prefix but breaks before the root ends
        stream = EnvelopeStream([b'1.5e', b'x'])
        self.assertEqual(stream.peek_root_type(1), 'number')
        with self.assertRaises(ValueError):
            stream.load_envelope()

    def test_invalid_utf8_in_envelope_no_handoff(self):
        stream = EnvelopeStream([b'{"route": "', b'\xff\xfe"}'])
        stream.load_envelope()
        with self.assertRaises(ValueError):
            list(stream.remaining_bytes())


class TestExclusivity(TestCase):
    def test_only_one_path(self):
        stream = EnvelopeStream([b'{}'])
        stream.load_many()
        with self.assertRaises(RuntimeError):
            stream.load_envelope()

        stream = EnvelopeStream([b'{}'])
        stream.load_envelope()
        with self.assertRaises(RuntimeError):
            stream.load_many()
        with self.assertRaises(RuntimeError):
            stream.load_envelope()

    def test_remaining_bytes_requires_envelope(self):
        stream = EnvelopeStream([b'{}'])
        with self.assertRaises(RuntimeError):
            stream.remaining_bytes()

    def test_remaining_bytes_only_once(self):
        stream = EnvelopeStream([b'{}', b' '])
        envelope = stream.load_envelope()
        envelope.read_all()
        list(stream.remaining_bytes())
        with self.assertRaises(RuntimeError):
            stream.remaining_bytes()


class TestStreaming(TestCase):
    def test_envelope_not_fully_cached(self):
        # an envelope too large to cache in full streams through chunk by chunk
        log = []
        envelope = b'{"data": [' + b','.join(str(i).encode() for i in range(100)) + b']}'
        chunks = [envelope[i:i + 7] for i in range(0, len(envelope), 7)] + [b' ', BUSINESS]
        stream = EnvelopeStream(logged_chunks(chunks, log))

        envelope_root = stream.load_envelope()
        self.assertLessEqual(len(log), 1)  # only enough for the first token

        self.assertEqual(len(list(envelope_root['data'])), 100)
        pulled_after_envelope = len(log)
        self.assertLess(pulled_after_envelope, len(chunks))

        tail = list(stream.remaining_bytes())
        self.assertEqual(b''.join(tail), b' ' + BUSINESS)
        self.assertEqual(len(log), len(chunks))  # everything pulled only by the end

    def test_probing_does_not_consume_later_chunks(self):
        log = []
        chunks = [b' ', b'{', b'"route": 1}', b' ', BUSINESS]
        stream = EnvelopeStream(logged_chunks(chunks, log))
        self.assertEqual(stream.peek_root_type(2), 'object')
        self.assertEqual(log, [b' ', b'{'])
