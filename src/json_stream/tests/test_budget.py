from io import BytesIO, StringIO
from unittest import TestCase

from json_stream import (
    FieldReport,
    OrderReport,
    StringBudgetError,
    validate_string_budgets,
)


class TrackedChunks:
    """Iterable of chunks that records how many chunks were pulled."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.pulls = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.pulls >= len(self.chunks):
            raise StopIteration
        chunk = self.chunks[self.pulls]
        self.pulls += 1
        return chunk


def two_chunk_splits(data):
    return [[data[:i], data[i:]] for i in range(1, len(data))]


def one_byte_chunks(data):
    return [data[i:i + 1] for i in range(len(data))]


class TestStringBudgets(TestCase):
    # an order exactly at the limit: field "abA\n<emoji>" is 5 decoded code
    # points (direct UTF-8, escaped control char, escaped key in order 1),
    # the repeated title brings the order total to exactly the quota
    EXACT_JSON = (
        b'{"orders": ['
        b'{"title": "abA\\n\xf0\x9f\x98\x80", "note": "a long note that is not budgeted", "title": "xy"},'
        b'{"\\u0074itle": "zz"}'
        b']}'
    )
    EXACT_REPORT = [
        OrderReport(0, (
            FieldReport(0, 1, ("orders", 0, "title"), 5),
            FieldReport(0, 2, ("orders", 0, "title"), 2),
        ), 7),
        OrderReport(1, (
            FieldReport(1, 1, ("orders", 1, "title"), 2),
        ), 2),
    ]

    def test_order_exactly_at_budget_is_readable(self):
        for source in (
            BytesIO(self.EXACT_JSON),
            StringIO(self.EXACT_JSON.decode()),
            TrackedChunks([self.EXACT_JSON]),
            TrackedChunks([self.EXACT_JSON.decode()]),
        ):
            with self.subTest(source=source):
                report = validate_string_budgets(source, field_limit=5, order_quota=7)
                self.assertEqual(self.EXACT_REPORT, report)

    def test_exact_budget_report_does_not_depend_on_chunking(self):
        for chunks in two_chunk_splits(self.EXACT_JSON) + [one_byte_chunks(self.EXACT_JSON)]:
            with self.subTest(chunks=chunks):
                report = validate_string_budgets(TrackedChunks(chunks), field_limit=5, order_quota=7)
                self.assertEqual(self.EXACT_REPORT, report)

    def test_surrogate_pair_counts_like_direct_utf8(self):
        # the emoji in EXACT_JSON written as a \uXXXX\uXXXX pair instead
        escaped = self.EXACT_JSON.replace(b'\xf0\x9f\x98\x80', b'\\ud83d\\ude00')
        for chunks in two_chunk_splits(escaped) + [one_byte_chunks(escaped)]:
            with self.subTest(chunks=chunks):
                report = validate_string_budgets(TrackedChunks(chunks), field_limit=5, order_quota=7)
                self.assertEqual(self.EXACT_REPORT, report)

    def test_each_field_ok_but_order_quota_exceeded(self):
        # both titles pass the per-field limit (7), but the order total
        # exceeds the quota of 6 at the 3rd character of the second title
        payload = b'{"orders": [{"title": "aaaa", "title": "aaaxxxx"}]}'
        for chunks in two_chunk_splits(payload) + [one_byte_chunks(payload), [payload]]:
            with self.subTest(chunks=chunks):
                with self.assertRaises(StringBudgetError) as ctx:
                    validate_string_budgets(TrackedChunks(chunks), field_limit=7, order_quota=6)
                error = ctx.exception
                self.assertEqual(StringBudgetError.ORDER_QUOTA, error.kind)
                self.assertEqual(0, error.order_index)
                self.assertEqual(2, error.occurrence)
                self.assertEqual(("orders", 0, "title"), error.path)
                self.assertEqual(6, error.limit)

    def test_field_limit_exceeded(self):
        payload = b'{"orders": [{"title": "abce"}]}'
        with self.assertRaises(StringBudgetError) as ctx:
            validate_string_budgets(BytesIO(payload), field_limit=3)
        error = ctx.exception
        self.assertEqual(StringBudgetError.FIELD_LIMIT, error.kind)
        self.assertEqual(0, error.order_index)
        self.assertEqual(1, error.occurrence)
        self.assertEqual(("orders", 0, "title"), error.path)
        self.assertEqual(3, error.limit)

    def test_invalid_escape_before_first_over_budget_character(self):
        # with a limit of 3 only 2 characters have been decoded when the
        # invalid escape \q appears: this is an escape error, not a budget
        # violation, even though the value would have exceeded the budget
        payload = b'{"orders": [{"title": "ab\\qcccc"}]}'
        for chunks in two_chunk_splits(payload) + [[payload]]:
            with self.subTest(chunks=chunks):
                with self.assertRaises(StringBudgetError) as ctx:
                    validate_string_budgets(TrackedChunks(chunks), field_limit=3)
                error = ctx.exception
                self.assertEqual(StringBudgetError.ESCAPE, error.kind)
                self.assertEqual(0, error.order_index)
                self.assertEqual(1, error.occurrence)
                self.assertIn("Invalid string escape", error.detail)

    def test_unpaired_surrogate_at_limit_is_escape_error_not_budget(self):
        # the count is exactly at the limit when the unpaired surrogate
        # appears: the next character never legally decodes, so this must
        # not be reported as exceeding the budget
        payload = b'{"orders": [{"title": "a\\ud83dx"}]}'
        with self.assertRaises(StringBudgetError) as ctx:
            validate_string_budgets(BytesIO(payload), field_limit=1)
        error = ctx.exception
        self.assertEqual(StringBudgetError.ESCAPE, error.kind)
        self.assertIn("surrogate", error.detail.lower())

    def test_surrogate_pair_exceeding_budget(self):
        # the pair decodes to a single code point that takes the value over
        # the budget; identical outcome however the payload is chunked
        for payload in (
            b'{"orders": [{"title": "a\\ud83d\\ude00"}]}',
            '{"orders": [{"title": "a😀"}]}'.encode(),
        ):
            for chunks in two_chunk_splits(payload) + [one_byte_chunks(payload), [payload]]:
                with self.subTest(payload=payload, chunks=chunks):
                    with self.assertRaises(StringBudgetError) as ctx:
                        validate_string_budgets(TrackedChunks(chunks), field_limit=1)
                    error = ctx.exception
                    self.assertEqual(StringBudgetError.FIELD_LIMIT, error.kind)
                    self.assertEqual(("orders", 0, "title"), error.path)
                    self.assertEqual(1, error.occurrence)

    def test_order_quota_resets_for_adjacent_orders(self):
        # each order is within the quota of 5, but their combined total of
        # 6 would exceed it: the quota is per order, so this must pass
        payload = b'{"orders": [{"title": "aaa"}, {"title": "aaa"}]}'
        report = validate_string_budgets(BytesIO(payload), order_quota=5)
        self.assertEqual([
            OrderReport(0, (FieldReport(0, 1, ("orders", 0, "title"), 3),), 3),
            OrderReport(1, (FieldReport(1, 1, ("orders", 1, "title"), 3),), 3),
        ], report)

    def test_escaped_key_matches_field_name(self):
        payload = b'{"orders": [{"\\u0074itle": "abcdef"}]}'
        with self.assertRaises(StringBudgetError) as ctx:
            validate_string_budgets(BytesIO(payload), field_limit=3)
        error = ctx.exception
        self.assertEqual(StringBudgetError.FIELD_LIMIT, error.kind)
        self.assertEqual(("orders", 0, "title"), error.path)

    def test_only_declared_paths_are_counted(self):
        payload = (
            b'{"title": "root title is not budgeted",'
            b' "orders": [{"note": "notes are not budgeted", "title": "ok"}]}'
        )
        report = validate_string_budgets(BytesIO(payload), field_limit=2, order_quota=2)
        self.assertEqual([
            OrderReport(0, (FieldReport(0, 1, ("orders", 0, "title"), 2),), 2),
        ], report)

    def test_nested_title_inside_order_is_counted(self):
        payload = b'{"orders": [{"items": [{"title": "abcdef"}]}]}'
        with self.assertRaises(StringBudgetError) as ctx:
            validate_string_budgets(BytesIO(payload), field_limit=3)
        error = ctx.exception
        self.assertEqual(("orders", 0, "items", 0, "title"), error.path)
        self.assertEqual(0, error.order_index)
        self.assertEqual(1, error.occurrence)

    def test_no_extra_pulls_after_rejection(self):
        # the over-budget character is the last byte of chunk 1; chunk 2
        # (holding the rest of the string and document) must never be pulled
        chunks = [
            b'{"orders": [{"title": "aa',
            b'aab',
            b'bb"}]}',
        ]
        tracker = TrackedChunks(chunks)
        with self.assertRaises(StringBudgetError) as ctx:
            validate_string_budgets(tracker, field_limit=3)
        self.assertEqual(StringBudgetError.FIELD_LIMIT, ctx.exception.kind)
        self.assertEqual(2, tracker.pulls)

    def test_no_extra_pulls_after_escape_error(self):
        chunks = [
            b'{"orders": [{"title": "ab\\q',
            b'cccc"}]}',
        ]
        tracker = TrackedChunks(chunks)
        with self.assertRaises(StringBudgetError) as ctx:
            validate_string_budgets(tracker, field_limit=10)
        self.assertEqual(StringBudgetError.ESCAPE, ctx.exception.kind)
        self.assertEqual(1, tracker.pulls)

    def test_no_budgets_still_reports(self):
        report = validate_string_budgets(BytesIO(self.EXACT_JSON))
        self.assertEqual(self.EXACT_REPORT, report)

    def test_non_string_field_value_is_ignored(self):
        payload = b'{"orders": [{"title": 123, "title": "ok"}]}'
        report = validate_string_budgets(BytesIO(payload), field_limit=2)
        self.assertEqual([
            OrderReport(0, (FieldReport(0, 2, ("orders", 0, "title"), 2),), 2),
        ], report)

    def test_custom_orders_path_and_field(self):
        payload = b'{"catalog": {"orders": [{"name": "abcdef"}]}}'
        with self.assertRaises(StringBudgetError) as ctx:
            validate_string_budgets(
                BytesIO(payload),
                orders_path=("catalog", "orders"),
                field="name",
                field_limit=3,
            )
        self.assertEqual(("catalog", "orders", 0, "name"), ctx.exception.path)

    def test_malformed_json_raises_value_error(self):
        with self.assertRaises(ValueError):
            validate_string_budgets(BytesIO(b'{"orders": [{"title": "abc",}]}'), field_limit=5)

    def test_empty_stream(self):
        with self.assertRaisesRegex(ValueError, 'No JSON value found'):
            validate_string_budgets(BytesIO(b''), field_limit=5)
