import io
import json
import math
from unittest import TestCase

from json_stream import (
    ByteIndexer,
    ByteIndexUnavailableError,
    IndexedValue,
    extract,
    load,
    visit,
    visit_byte_ranges,
)


class _CountingStream:
    """File-like object that records physical reads and counts them."""

    def __init__(self, data, chunk_size):
        self._data = data
        self._chunk_size = chunk_size
        self._cursor = 0
        self.read_count = 0
        self.bytes_delivered = 0

    def read(self, size=-1):
        if size == 0:
            return b''
        self.read_count += 1
        part = self._data[self._cursor:self._cursor + self._chunk_size]
        self._cursor += len(part)
        self.bytes_delivered += len(part)
        return part


class TestByteIndex(TestCase):
    # the two "name" values decode to the same text but are spelled
    # differently; neighbouring content includes bracket-like characters,
    # quotes and spaces inside strings
    DOCUMENT = (
        '{'
        '"name": "中文 \\"x\\" [y] {z}", '
        '"name": "\\u4e2d\\u6587 \\"x\\" [y] {z}", '
        '"nested": {"a": [1, {"b": "() \\\" \\t []{}"}], "c": true, "d": null, "e": {}}'
        '}'
    )

    def setUp(self):
        self.text = self.DOCUMENT
        self.data = self.DOCUMENT.encode('utf-8')

    def _index(self, data, chunk_size=8192):
        return list(visit_byte_ranges(io.BytesIO(data), chunk_size=chunk_size))

    def test_half_open_ranges_slice_exact_original_literal(self):
        events = self._index(self.data)
        for event in events:
            self.assertIsInstance(event, IndexedValue)
            self.assertTrue(event.complete)
            literal = extract(self.data, event)
            self.assertEqual(literal, self.data[event.start:event.end])
            self.assertEqual(event.span, (event.start, event.end))
            # no neighbouring whitespace or separator is carried along
            self.assertNotIn(literal[:1], (b' ', b'\t', b'\n', b',', b':'))
            self.assertNotIn(literal[-1:], (b' ', b'\t', b'\n', b',', b':'))

    def test_ranges_include_value_boundaries(self):
        events = {event.path: event for event in self._index(self.data)}
        # strings keep their quotes
        string_literal = extract(self.data, events[('name',)])
        self.assertEqual(string_literal[0:1], b'"')
        self.assertEqual(string_literal[-1:], b'"')
        # containers keep their brackets
        root_literal = extract(self.data, events[()])
        self.assertEqual(root_literal[0:1], b'{')
        self.assertEqual(root_literal[-1:], b'}')
        nested_list = extract(self.data, events[('nested', 'a')])
        self.assertEqual(nested_list[0:1], b'[')
        self.assertEqual(nested_list[-1:], b']')

    def test_duplicate_keys_share_path_but_keep_distinct_literals(self):
        events = self._index(self.data)
        names = [event for event in events if event.path == ('name',)]
        self.assertEqual(2, len(names))
        first, second = names
        # identical decoded values and visitor-style paths
        self.assertEqual(first.value, second.value)
        self.assertEqual('中文 "x" [y] {z}', first.value)
        # distinct original spellings survive indexing
        first_literal = extract(self.data, first)
        second_literal = extract(self.data, second)
        self.assertNotEqual(first_literal, second_literal)
        self.assertIn('中文'.encode('utf-8'), first_literal)
        self.assertNotIn(b'\\u', first_literal)
        self.assertIn(b'\\u4e2d', second_literal)
        # each literal parses on its own back to the same value
        self.assertEqual(json.loads(first_literal), json.loads(second_literal))
        # occurrence order is preserved
        self.assertLess(first.start, second.start)

    def test_paths_and_decoded_values_match_visitor_semantics(self):
        events = self._index(self.data)
        leaves = [(event.path, event.value) for event in events
                  if not event.container]
        visited = []

        def collect(value, path):
            if not isinstance(value, (dict, list)):  # skip container events
                visited.append((path, value))

        visit(io.BytesIO(self.data), collect)
        self.assertEqual(visited, leaves)

    def test_offsets_are_independent_of_chunk_boundaries(self):
        reference = [
            (event.path, event.start, event.end)
            for event in self._index(self.data, chunk_size=10 ** 6)
        ]
        # chunk size 1 splits every multi-byte character and every escape;
        # other odd sizes split them at different points
        for chunk_size in range(1, 17):
            with self.subTest(chunk_size=chunk_size):
                got = [
                    (event.path, event.start, event.end)
                    for event in self._index(self.data, chunk_size=chunk_size)
                ]
                self.assertEqual(reference, got)

    def test_chunk_split_inside_escape_and_multibyte_character(self):
        # \\u4e2d escape sequence spans several chunk boundaries at size 2,
        # the raw 中 (3 UTF-8 bytes) is split too
        events = self._index(self.data, chunk_size=2)
        for event in events:
            self.assertEqual(
                self.data[event.start:event.end], extract(self.data, event)
            )

    def test_optional_utf8_bom_is_skipped_and_offsets_count_from_byte_zero(self):
        bom_data = b'\xef\xbb\xbf' + self.data
        for chunk_size in (1, 2, 3, 4, 8, 64):
            with self.subTest(chunk_size=chunk_size):
                indexer = ByteIndexer(io.BytesIO(bom_data), chunk_size=chunk_size)
                events = list(indexer.events())
                self.assertEqual(3, indexer.bom_size)
                root = next(event for event in events if event.path == ())
                self.assertEqual(3, root.start)  # first real byte after BOM
                for event in events:
                    self.assertEqual(
                        bom_data[event.start:event.end],
                        extract(bom_data, event),
                    )
        # without a BOM the document starts at byte 0
        indexer = ByteIndexer(io.BytesIO(self.data))
        root = next(event for event in indexer.events() if event.path == ())
        self.assertEqual(0, root.start)
        self.assertEqual(0, indexer.bom_size)

    def test_non_rewindable_chunk_iterable(self):
        chunks = [self.data[i:i + 5] for i in range(0, len(self.data), 5)]
        events = list(visit_byte_ranges(iter(chunks), chunk_size=3))
        reference = self._index(self.data)
        self.assertEqual(
            [(event.path, event.start, event.end) for event in reference],
            [(event.path, event.start, event.end) for event in events],
        )
        for event in events:
            # leaf literals are cached; containers are not (bounded memory)
            if not event.container:
                self.assertEqual(self.data[event.start:event.end], event.raw)

    def test_events_are_emitted_incrementally_without_reading_whole_stream(self):
        stream = _CountingStream(self.data, chunk_size=4)
        generator = visit_byte_ranges(stream, chunk_size=4)
        first = next(generator)
        self.assertEqual(('name',), first.path)
        # the first value is reported while most of the stream is unread
        self.assertLess(stream.bytes_delivered, len(self.data))
        self.assertEqual(self.data[first.start:first.end], first.raw)
        # draining the rest gives the full index
        rest = list(generator)
        self.assertTrue(any(event.path == () for event in rest))

    def test_token_end_is_not_the_next_physical_read_position(self):
        # a number ending one byte before a chunk boundary must not absorb
        # the following comma; its end is computed from consumed bytes
        data = b'{"a": 1234, "b": 5}'
        for cut in range(1, len(data)):
            chunks = [data[:cut], data[cut:]]
            events = list(visit_byte_ranges(iter(chunks), chunk_size=cut))
            number = next(event for event in events if event.path == ('a',))
            self.assertEqual((6, 10), number.span)
            self.assertEqual(b'1234', extract(data, number))

    def test_truncated_container_reports_finished_children_only(self):
        truncated = b'{"a": 1, "b": {"x": [1, 2], "y": "done"'
        indexer = ByteIndexer(io.BytesIO(truncated), chunk_size=4)
        events = list(indexer.events())
        paths = [event.path for event in events]
        # finished sub-values keep fully re-sliceable ranges
        self.assertEqual(
            [('a',), ('b', 'x', 0), ('b', 'x', 1), ('b', 'x'), ('b', 'y')],
            paths,
        )
        for event in events:
            self.assertEqual(
                truncated[event.start:event.end], extract(truncated, event)
            )
        # neither the root nor "b" ever closed: no complete range for them
        self.assertNotIn((), paths)
        self.assertNotIn(('b',), paths)
        self.assertEqual([(), ('b',)], indexer.incomplete_paths)

    def test_truncation_at_every_grammar_point(self):
        # A cut either lands mid-token (unterminated string/number/literal,
        # which is malformed JSON and must raise) or at a structural point:
        # then every reported range must still re-slice exactly and the open
        # containers are listed instead of being given fake complete ranges.
        full = b'{"a": [1, {"b": "x"}], "c": true}'
        structural_outcomes = 0
        for cut in range(1, len(full) + 1):
            prefix = full[:cut]
            indexer = ByteIndexer(io.BytesIO(prefix), chunk_size=3)
            try:
                events = list(indexer.events())
            except ValueError:
                continue  # mid-token truncation: malformed, correctly rejected
            structural_outcomes += 1
            for event in events:
                self.assertEqual(
                    prefix[event.start:event.end], extract(prefix, event)
                )
                self.assertTrue(event.complete)
            if cut < len(full):
                self.assertTrue(indexer.incomplete_paths)
        self.assertGreater(structural_outcomes, 0)
        # the full document closes every frame and reports the root
        indexer = ByteIndexer(io.BytesIO(full))
        events = list(indexer.events())
        self.assertEqual([], indexer.incomplete_paths)
        self.assertIn((), [event.path for event in events])

    def test_truncated_unterminated_string_raises(self):
        with self.assertRaisesRegex(ValueError, "Unterminated"):
            list(visit_byte_ranges(io.BytesIO(b'["abc')))

    def test_text_stream_signals_byte_limitations(self):
        text_events = list(visit_byte_ranges(io.StringIO(self.text), chunk_size=5))
        binary_events = self._index(self.data)
        # offsets and values are still trustworthy (UTF-8 assumed)
        self.assertEqual(
            [(e.path, e.start, e.end, e.value) for e in binary_events],
            [(e.path, e.start, e.end, e.value) for e in text_events],
        )
        leaf = next(event for event in text_events if not event.container)
        self.assertIsNone(leaf.raw)
        with self.assertRaises(ByteIndexUnavailableError):
            extract(None, leaf)
        # retaining the original bytes alongside still allows re-slicing
        self.assertEqual(
            self.data[leaf.start:leaf.end], extract(self.data, leaf)
        )
        # a text file passed directly to extract() cannot supply real bytes
        with self.assertRaises(ByteIndexUnavailableError):
            extract(io.StringIO(self.text), leaf)

    def test_extract_accepts_bytes_likes_and_seekable_files(self):
        events = self._index(self.data)
        event = events[0]
        expected = self.data[event.start:event.end]
        self.assertEqual(expected, extract(bytearray(self.data), event))
        self.assertEqual(expected, extract(memoryview(self.data), event))
        binary_file = io.BytesIO(self.data)
        self.assertEqual(expected, extract(binary_file, event))
        # leaf events from binary sources cache the bytes directly
        self.assertEqual(expected, event.raw)
        self.assertEqual(expected, extract(None, event))

    def test_empty_containers_and_whitespace_input(self):
        small = b'{"a": {}, "b": []}'
        events = self._index(small)
        containers = {event.path: event for event in events if event.container}
        self.assertEqual(b'{}', extract(small, containers[('a',)]))
        self.assertEqual(b'[]', extract(small, containers[('b',)]))
        self.assertEqual([], list(visit_byte_ranges(io.BytesIO(b'  \n\t\r'))))

    def test_primitives_and_literals_with_exact_ranges(self):
        stream = io.BytesIO(b'123 "ab" true false null NaN Infinity -Infinity 0.5e2')
        events = list(visit_byte_ranges(stream))
        self.assertEqual(
            [b'123', b'"ab"', b'true', b'false', b'null', b'NaN',
             b'Infinity', b'-Infinity', b'0.5e2'],
            [event.raw for event in events],
        )
        values = [event.value for event in events]
        self.assertEqual([123, 'ab', True, False, None], values[:5])
        self.assertTrue(math.isnan(values[5]))
        self.assertEqual([float('inf'), float('-inf'), 50.0], values[6:])
        for event in events:
            self.assertEqual((), event.path)

    def test_concatenated_documents_are_each_indexed(self):
        data = b'{"a": 1}{"a": 2} "solo" 42'
        indexer = ByteIndexer(io.BytesIO(data), chunk_size=3)
        events = list(indexer.events())
        roots = [event for event in events if event.path == ()]
        self.assertEqual(4, indexer.document_count)
        self.assertEqual(4, len(roots))
        for event in events:
            self.assertEqual(data[event.start:event.end], extract(data, event))

    def test_invalid_json_raises_value_error_with_position(self):
        for bad in (b'tru,', b'01', b'falsx', b'"\\h"', b'{"a":1]', b'?'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    list(visit_byte_ranges(io.BytesIO(bad)))

    def test_original_decoded_value_callback_remains_available(self):
        # the pre-existing decoded-only visitor and loader are unchanged
        visited = []
        visit(io.BytesIO(self.data), lambda value, path: visited.append((path, value)))
        self.assertIn((('name',), '中文 "x" [y] {z}'), visited)
        loaded = load(io.BytesIO(self.data))
        self.assertEqual('中文 "x" [y] {z}', loaded['name'])

    def test_invalid_chunk_size(self):
        with self.assertRaises(ValueError):
            ByteIndexer(io.BytesIO(b'{}'), chunk_size=0)
