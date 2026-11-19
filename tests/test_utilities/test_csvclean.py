import io
import os
import sys
import tempfile
from unittest.mock import patch

import agate

from csvkit.utilities.csvclean import CSVClean, launch_new_instance
from tests.utils import CSVKitTestCase, EmptyFileTests, stdin_as_string


class TestCSVClean(CSVKitTestCase, EmptyFileTests):
    Utility = CSVClean
    default_args = ['--length-mismatch']

    def assertCleaned(self, args, output_rows, error_rows=[]):
        output_file = io.StringIO()
        error_file = io.StringIO()

        utility = CSVClean(args, output_file, error_file)

        if error_rows:
            with self.assertRaises(SystemExit) as e:
                utility.run()

            self.assertEqual(e.exception.code, 1)
        else:
            utility.run()

        output_file.seek(0)
        error_file.seek(0)

        if output_rows:
            reader = agate.csv.reader(output_file)
            for row in output_rows:
                self.assertEqual(next(reader), row)
            self.assertRaises(StopIteration, next, reader)
        if error_rows:
            reader = agate.csv.reader(error_file)
            for row in error_rows:
                self.assertEqual(next(reader), row)
            self.assertRaises(StopIteration, next, reader)

        output_file.close()
        error_file.close()

    def test_launch_new_instance(self):
        with patch.object(sys, 'argv', [self.Utility.__name__.lower()] + self.default_args + ['examples/dummy.csv']):
            launch_new_instance()

    def test_options(self):
        for args, message in (
            (
                [],
                'No checks or fixes were enabled. See available options with: csvclean --help',
            ),
            (
                ['--join-short-rows', '--fill-short-rows'],
                'The --join-short-rows and --fill-short-rows options are mutually exclusive.',
            ),
        ):
            with self.subTest(args=args):
                self.assertError(launch_new_instance, args, message)

    def test_skip_lines(self):
        self.assertCleaned(
            ['--length-mismatch', '--omit-error-rows', '--skip-lines', '3', 'examples/bad_skip_lines.csv'],
            [
                ['column_a', 'column_b', 'column_c'],
                ['0', 'mixed types.... uh oh', '17'],
            ], [
                ['line_number', 'msg', 'column_a', 'column_b', 'column_c'],
                ['1', 'Expected 3 columns, found 4 columns', '1', '27', '', "I'm too long!"],
                ['2', 'Expected 3 columns, found 2 columns', '', "I'm too short!"],
            ],
        )

    def test_simple(self):
        self.assertCleaned(['--length-mismatch', '--omit-error-rows', 'examples/bad.csv'], [
            ['column_a', 'column_b', 'column_c'],
            ['0', 'mixed types.... uh oh', '17'],
        ], [
            ['line_number', 'msg', 'column_a', 'column_b', 'column_c'],
            ['1', 'Expected 3 columns, found 4 columns', '1', '27', '', "I'm too long!"],
            ['2', 'Expected 3 columns, found 2 columns', '', "I'm too short!"],
        ])

    def test_no_header_row(self):
        self.assertCleaned(['--length-mismatch', 'examples/no_header_row.csv'], [
            ['1', '2', '3'],
        ])

    def test_header_normalize_space(self):
        self.assertCleaned(['--header-normalize-space', 'examples/test_header_newline.csv'], [
            ['start end', 'b', 'c'],
            ['d', 'e', 'f'],
        ])

    def test_join_short_rows(self):
        self.assertCleaned(['--omit-error-rows', '--join-short-rows', 'examples/test_join_short_rows.csv'], [
            ['a', 'b', 'c'],
            ['1', 'cat\ndog', 'c'],
            ['3', 'b', 'c'],
        ])

    def test_join_short_rows_separator(self):
        self.assertCleaned(
            ['--omit-error-rows', '--join-short-rows', '--separator', 'XYZ', 'examples/test_join_short_rows.csv'],
            [
                ['a', 'b', 'c'],
                ['1', 'catXYZdog', 'c'],
                ['3', 'b', 'c'],
            ],
        )

    def test_fill_short_rows(self):
        self.assertCleaned(['--fill-short-rows', 'examples/test_join_short_rows.csv'], [
            ['a', 'b', 'c'],
            ['1', 'cat', ''],
            ['dog', 'c', ''],
            ['3', 'b', 'c'],
        ])

    def test_fill_short_rows_separator(self):
        self.assertCleaned(['--fill-short-rows', '--fillvalue', 'XYZ', 'examples/test_join_short_rows.csv'], [
            ['a', 'b', 'c'],
            ['1', 'cat', 'XYZ'],
            ['dog', 'c', 'XYZ'],
            ['3', 'b', 'c'],
        ])

    def test_empty_columns(self):
        self.assertCleaned(['--empty-columns', 'examples/test_empty_columns.csv'], [
            ['a', 'b', 'c', '', ''],
            ['a', '', '', '', ''],
            ['', '', 'c', ''],
            ['', '', '', '', ''],
        ], [
            ['line_number', 'msg', 'a', 'b', 'c', '', ''],
            ['1', "Empty columns named 'b', '', ''! Try: csvcut -C 2,4,5", '', '', '', '', ''],
        ])

    def test_empty_columns_short_row(self):
        self.assertCleaned(['--empty-columns', 'examples/test_empty_columns_short_row.csv'], [
            ['a', 'b', 'c'],
            ['', ''],
        ], [
            ['line_number', 'msg', 'a', 'b', 'c'],
            ['1', "Empty columns named 'a', 'b', 'c'! Try: csvcut -C 1,2,3", '', '', ''],
        ])

    def test_empty_columns_long_row(self):
        self.assertCleaned(['--empty-columns', 'examples/test_empty_columns_long_row.csv'], [
            ['a', 'b', 'c'],
            ['', '', '', ''],
        ], [
            ['line_number', 'msg', 'a', 'b', 'c'],
            ['1', "Empty columns named 'a', 'b', 'c'! Try: csvcut -C 1,2,3", '', '', ''],
        ])

    def test_empty_columns_zero(self):
        self.assertCleaned(['--empty-columns', '--zero', 'examples/test_empty_columns.csv'], [
            ['a', 'b', 'c', '', ''],
            ['a', '', '', '', ''],
            ['', '', 'c', ''],
            ['', '', '', '', ''],
        ], [
            ['line_number', 'msg', 'a', 'b', 'c', '', ''],
            ['1', "Empty columns named 'b', '', ''! Try: csvcut -C 1,3,4", '', '', '', '', ''],
        ])

    def test_remove_empty_columns(self):
        self.assertCleaned(['--remove-empty-columns', 'examples/test_empty_columns.csv'], [
            ['a', 'c'],
            ['a', ''],
            ['', 'c'],
            ['', ''],
        ])

    def test_remove_empty_columns_no_empty(self):
        self.assertCleaned(['--remove-empty-columns', 'examples/dummy.csv'], [
            ['a', 'b', 'c'],
            ['1', '2', '3'],
        ])

    def test_remove_empty_columns_short_row(self):
        # A short row stays short: removing empty columns does not pad it out.
        input_file = io.BytesIO(b'a,b,c\n1,,3\nx\n')

        with stdin_as_string(input_file):
            self.assertCleaned(['--remove-empty-columns'], [
                ['a', 'c'],
                ['1', '3'],
                ['x'],
            ])

    def test_remove_empty_columns_counts_omitted_rows(self):
        # 'c' has a value only in the omitted (long) row, so it is not empty in every data row and must be kept,
        # even though it is empty in every row written to output.
        input_file = io.BytesIO(b'a,b,c\n1,2,\n3,4,\n5,6,7,extra\n')

        with stdin_as_string(input_file):
            self.assertCleaned(['--omit-error-rows', '--remove-empty-columns'], [
                ['a', 'b', 'c'],
                ['1', '2', ''],
                ['3', '4', ''],
            ])

    def test_enable_all_checks(self):
        self.assertCleaned(['-a', 'examples/test_empty_columns.csv'], [
            ['a', 'b', 'c', '', ''],
            ['a', '', '', '', ''],
            ['', '', 'c', ''],
            ['', '', '', '', ''],
        ], [
            ['line_number', 'msg', 'a', 'b', 'c', '', ''],
            ['2', 'Expected 5 columns, found 4 columns', '', '', 'c', ''],
            ['1', "Empty columns named 'b', '', ''! Try: csvcut -C 2,4,5", '', '', '', '', ''],
        ])

    def test_label(self):
        self.assertCleaned(['-a', '--label', 'xyz', 'examples/test_empty_columns.csv'], [
            ['a', 'b', 'c', '', ''],
            ['a', '', '', '', ''],
            ['', '', 'c', ''],
            ['', '', '', '', ''],
        ], [
            ['label', 'line_number', 'msg', 'a', 'b', 'c', '', ''],
            ['xyz', '2', 'Expected 5 columns, found 4 columns', '', '', 'c', ''],
            ['xyz', '1', "Empty columns named 'b', '', ''! Try: csvcut -C 2,4,5", '', '', '', '', ''],
        ])

    def test_label_default(self):
        self.assertCleaned(['-a', '--label', '-', 'examples/test_empty_columns.csv'], [
            ['a', 'b', 'c', '', ''],
            ['a', '', '', '', ''],
            ['', '', 'c', ''],
            ['', '', '', '', ''],
        ], [
            ['label', 'line_number', 'msg', 'a', 'b', 'c', '', ''],
            ['examples/test_empty_columns.csv', '2', 'Expected 5 columns, found 4 columns', '', '', 'c', ''],
            ['examples/test_empty_columns.csv', '1', "Empty columns named 'b', '', ''! Try: csvcut -C 2,4,5", '', '', '', '', ''],  # noqa: E501
        ])

    def test_label_default_stdin(self):
        input_file = io.BytesIO(b'a,b,c\n,\n')

        with stdin_as_string(input_file):
            self.assertCleaned(['-a', '--label', '-'], [
                ['a', 'b', 'c'],
                ['', ''],
            ], [
                ['label', 'line_number', 'msg', 'a', 'b', 'c'],
                ['stdin', '1', 'Expected 3 columns, found 2 columns', '', ''],
                ['stdin', '1', "Empty columns named 'a', 'b', 'c'! Try: csvcut -C 1,2,3", '', '', ''],
            ])

    def test_removes_optional_quote_characters(self):
        self.assertCleaned(['--length-mismatch', 'examples/optional_quote_characters.csv'], [
            ['a', 'b', 'c'],
            ['1', '2', '3'],
        ])

    def test_changes_line_endings(self):
        self.assertCleaned(['--length-mismatch', 'examples/mac_newlines.csv'], [
            ['a', 'b', 'c'],
            ['1', '2', '3'],
            ['Once upon\na time', '5', '6'],
        ])

    def test_changes_character_encoding(self):
        self.assertCleaned(['--length-mismatch', '-e', 'latin1', 'examples/test_latin1.csv'], [
            ['a', 'b', 'c'],
            ['1', '2', '3'],
            ['4', '5', '©'],
        ])

    def test_removes_bom(self):
        self.assertCleaned(['--length-mismatch', 'examples/test_utf8_bom.csv'], [
            ['foo', 'bar', 'baz'],
            ['1', '2', '3'],
            ['4', '5', 'ʤ'],
        ])

    def assertLineMap(self, args, line_map_rows):
        output_file = io.StringIO()
        error_file = io.StringIO()

        with tempfile.TemporaryDirectory() as tmpdir:
            line_map_path = os.path.join(tmpdir, 'line_map.csv')
            utility = CSVClean(args + ['--line-map', line_map_path], output_file, error_file)

            try:
                utility.run()
            except SystemExit:
                pass

            with open(line_map_path, newline='', encoding='utf-8') as f:
                reader = agate.csv.reader(f)
                for row in line_map_rows:
                    self.assertEqual(next(reader), row)
                self.assertRaises(StopIteration, next, reader)

        output_file.close()
        error_file.close()

    def test_line_map(self):
        self.assertLineMap(
            [
                '--length-mismatch', '--omit-error-rows', '--join-short-rows',
                'examples/test_line_map.csv',
            ],
            [
                ['kind', 'output_line', 'input_line_start', 'input_line_end'],
                ['header', '1', '1', '1'],
                # A quoted line break spans multiple physical lines.
                ['data', '2', '2', '3'],
                # Short rows merged by --join-short-rows.
                ['data', '3', '4', '5'],
                ['data', '4', '6', '6'],
                # An error row omitted from standard output.
                ['error', '', '7', '7'],
            ],
        )

    def test_line_map_error_rows_in_output(self):
        self.assertLineMap(
            ['--length-mismatch', 'examples/test_line_map.csv'],
            [
                ['kind', 'output_line', 'input_line_start', 'input_line_end'],
                ['header', '1', '1', '1'],
                ['data', '2', '2', '3'],
                ['data', '3', '4', '4'],
                ['error', '', '4', '4'],
                ['data', '4', '5', '5'],
                ['error', '', '5', '5'],
                ['data', '5', '6', '6'],
                ['data', '6', '7', '7'],
                ['error', '', '7', '7'],
            ],
        )

    def test_line_map_skip_lines(self):
        self.assertLineMap(
            ['--length-mismatch', '--omit-error-rows', '--skip-lines', '3', 'examples/bad_skip_lines.csv'],
            [
                ['kind', 'output_line', 'input_line_start', 'input_line_end'],
                ['header', '1', '4', '4'],
                ['error', '', '5', '5'],
                ['error', '', '6', '6'],
                ['data', '2', '7', '7'],
            ],
        )

    def test_line_map_remove_empty_columns(self):
        self.assertLineMap(
            ['--remove-empty-columns', 'examples/test_empty_columns.csv'],
            [
                ['kind', 'output_line', 'input_line_start', 'input_line_end'],
                ['header', '1', '1', '1'],
                ['data', '2', '2', '2'],
                ['data', '3', '3', '3'],
                ['data', '4', '4', '4'],
            ],
        )
