import io
import os
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import agate

from csvkit.utilities.csvstack import CSVStack, launch_new_instance
from tests.utils import CSVKitTestCase, EmptyFileTests, stdin_as_string


@contextmanager
def pipe(text):
    """Yield a path to a pipe containing the text, like Bash's process substitution."""
    r, w = os.pipe()
    os.write(w, text.encode())
    os.close(w)
    try:
        yield f'/dev/fd/{r}'
    finally:
        os.close(r)


class TestCSVStack(CSVKitTestCase, EmptyFileTests):
    Utility = CSVStack
    default_args = ['-']

    def test_launch_new_instance(self):
        with patch.object(sys, 'argv', [self.Utility.__name__.lower(), 'examples/dummy.csv']):
            launch_new_instance()

    def test_options(self):
        self.assertError(
            launch_new_instance,
            ['-g', '1,2'],
            'The number of grouping values must be equal to the number of CSV files being stacked.',
        )

    @unittest.skipIf(os.name != 'nt', 'Windows only')
    def test_glob(self):
        self.assertRows(['examples/dummy*.csv'], [
            ['a', 'b', 'c', 'd'],
            ['1', '2', '3', ''],
            ['1', '2', '3', ''],
            ['1', '2', '3', ''],
            ['1', '4', '5', ''],
            ['1', '2', '3', ''],
            ['1', '2', '3', '4'],
        ])

    def test_skip_lines(self):
        self.assertRows(['--skip-lines', '3', 'examples/test_skip_lines.csv', 'examples/test_skip_lines.csv'], [
            ['a', 'b', 'c'],
            ['1', '2', '3'],
            ['1', '2', '3'],
        ])

    def test_skip_lines_stdin(self):
        with open('examples/test_skip_lines.csv', 'rb') as f, stdin_as_string(f):
            self.assertRows(['--skip-lines', '3', '-', 'examples/test_skip_lines.csv'], [
                ['a', 'b', 'c'],
                ['1', '2', '3'],
                ['1', '2', '3'],
            ])

    @unittest.skipUnless(os.path.isdir('/dev/fd'), 'requires /dev/fd')
    def test_pipe(self):
        with pipe('c,a,d\n7,5,6\n10,8,9\n') as path:
            self.assertRows(['examples/dummy.csv', path], [
                ['a', 'b', 'c', 'd'],
                ['1', '2', '3', ''],
                ['5', '', '7', '6'],
                ['8', '', '10', '9'],
            ])

    def test_single_file_stack(self):
        self.assertRows(['examples/dummy.csv'], [
            ['a', 'b', 'c'],
            ['1', '2', '3'],
        ])

    def test_multiple_file_stack(self):
        self.assertRows(['examples/dummy.csv', 'examples/dummy2.csv'], [
            ['a', 'b', 'c'],
            ['1', '2', '3'],
            ['1', '2', '3'],
        ])

    def test_multiple_file_stack_col(self):
        self.assertRows(['examples/dummy.csv', 'examples/dummy_col_shuffled.csv'], [
            ['a', 'b', 'c'],
            ['1', '2', '3'],
            ['1', '2', '3'],
        ])

        self.assertRows(['examples/dummy_col_shuffled.csv', 'examples/dummy.csv'], [
            ['b', 'c', 'a'],
            ['2', '3', '1'],
            ['2', '3', '1'],
        ])

    def test_multiple_file_stack_col_ragged(self):
        self.assertRows(['examples/dummy.csv', 'examples/dummy_col_shuffled_ragged.csv'], [
            ['a', 'b', 'c', 'd'],
            ['1', '2', '3', ''],
            ['1', '2', '3', '4'],
        ])

    def test_multiple_file_stack_col_ragged_stdin(self):
        with open('examples/dummy.csv', 'rb') as f, stdin_as_string(f):
            self.assertRows(['-', 'examples/dummy_col_shuffled_ragged.csv'], [
                ['a', 'b', 'c', 'd'],
                ['1', '2', '3', ''],
                ['1', '2', '3', '4'],
            ])

        with open('examples/dummy.csv', 'rb') as f, stdin_as_string(f):
            self.assertRows(['examples/dummy_col_shuffled_ragged.csv', '-'], [
                ['b', 'c', 'a', 'd'],
                ['2', '3', '1', '4'],
                ['2', '3', '1', ''],
            ])

    def test_explicit_grouping(self):
        self.assertRows(['--groups', 'asd,sdf', '-n', 'foo', 'examples/dummy.csv', 'examples/dummy2.csv'], [
            ['foo', 'a', 'b', 'c'],
            ['asd', '1', '2', '3'],
            ['sdf', '1', '2', '3'],
        ])

    def test_filenames_grouping(self):
        self.assertRows(['--filenames', '-n', 'path', 'examples/dummy.csv', 'examples/dummy2.csv'], [
            ['path', 'a', 'b', 'c'],
            ['dummy.csv', '1', '2', '3'],
            ['dummy2.csv', '1', '2', '3'],
        ])

    def test_dialects(self):
        self.assertRows([
            '--dialect', 'examples/test_dialects_semicolon.csv=semicolon',
            '--dialect', 'examples/test_dialects_tab.tsv=tab',
            'examples/test_dialects_semicolon.csv',
            'examples/test_dialects_tab.tsv',
            'examples/dummy.csv',
        ], [
            ['a', 'b', 'c'],
            ['1', 'x;y', '3'],
            ['2', 'tab\there', '4'],
            ['5', 'x;y', '7'],
            ['6', 'a,b', '8'],
            ['1', '2', '3'],
        ])

    def test_dialects_alias(self):
        self.assertRows([
            '--input-dialects', 'examples/test_dialects_semicolon.csv=semicolon',
            'examples/test_dialects_semicolon.csv',
        ], [
            ['a', 'b', 'c'],
            ['1', 'x;y', '3'],
            ['2', 'tab\there', '4'],
        ])

    def test_dialects_comma_separated(self):
        self.assertRows([
            '--dialects',
            'examples/test_dialects_semicolon.csv=semicolon,examples/test_dialects_tab.tsv=tab',
            'examples/test_dialects_semicolon.csv',
            'examples/test_dialects_tab.tsv',
        ], [
            ['a', 'b', 'c'],
            ['1', 'x;y', '3'],
            ['2', 'tab\there', '4'],
            ['5', 'x;y', '7'],
            ['6', 'a,b', '8'],
        ])

    def test_dialects_positional(self):
        self.assertRows([
            '--dialects', 'semicolon,tab',
            'examples/test_dialects_semicolon.csv',
            'examples/test_dialects_tab.tsv',
        ], [
            ['a', 'b', 'c'],
            ['1', 'x;y', '3'],
            ['2', 'tab\there', '4'],
            ['5', 'x;y', '7'],
            ['6', 'a,b', '8'],
        ])

    def test_dialects_unlisted_input_uses_common_options(self):
        self.assertRows([
            '--dialect', 'examples/test_dialects_semicolon.csv=semicolon',
            '--delimiter', ';',
            'examples/test_dialects_semicolon.csv',
            'examples/test_dialects_semicolon.csv',
        ], [
            ['a', 'b', 'c'],
            ['1', 'x;y', '3'],
            ['2', 'tab\there', '4'],
            ['1', 'x;y', '3'],
            ['2', 'tab\there', '4'],
        ])

    def test_dialects_delimiter(self):
        self.assertRows([
            '--dialect', 'examples/test_dialects_semicolon.csv=;',
            '--dialect', 'examples/test_dialects_tab.tsv=\\t',
            'examples/test_dialects_semicolon.csv',
            'examples/test_dialects_tab.tsv',
        ], [
            ['a', 'b', 'c'],
            ['1', 'x;y', '3'],
            ['2', 'tab\there', '4'],
            ['5', 'x;y', '7'],
            ['6', 'a,b', '8'],
        ])

    def test_dialects_options(self):
        self.assertRows([
            '--dialect', 'examples/test_dialects_semicolon.csv=delimiter=";" quotechar="\\""',
            'examples/test_dialects_semicolon.csv',
        ], [
            ['a', 'b', 'c'],
            ['1', 'x;y', '3'],
            ['2', 'tab\there', '4'],
        ])

    def test_dialects_basename_and_index(self):
        self.assertRows([
            '--dialect', 'test_dialects_semicolon.csv=semicolon',
            '--dialect', '2=tab',
            'examples/test_dialects_semicolon.csv',
            'examples/test_dialects_tab.tsv',
        ], [
            ['a', 'b', 'c'],
            ['1', 'x;y', '3'],
            ['2', 'tab\there', '4'],
            ['5', 'x;y', '7'],
            ['6', 'a,b', '8'],
        ])

    def test_dialects_file(self):
        self.assertRows([
            '--dialect-file', 'examples/test_dialects.txt',
            'examples/test_dialects_semicolon.csv',
            'examples/test_dialects_tab.tsv',
        ], [
            ['a', 'b', 'c'],
            ['1', 'x;y', '3'],
            ['2', 'tab\there', '4'],
            ['5', 'x;y', '7'],
            ['6', 'a,b', '8'],
        ])

    def test_dialects_file_json(self):
        self.assertRows([
            '--dialect-file', 'examples/test_dialects.json',
            'examples/test_dialects_semicolon.csv',
            'examples/test_dialects_tab.tsv',
        ], [
            ['a', 'b', 'c'],
            ['1', 'x;y', '3'],
            ['2', 'tab\there', '4'],
            ['5', 'x;y', '7'],
            ['6', 'a,b', '8'],
        ])

    def test_dialects_file_csv(self):
        self.assertRows([
            '--dialect-file', 'examples/test_dialects.csv',
            'examples/test_dialects_semicolon.csv',
            'examples/test_dialects_tab.tsv',
        ], [
            ['a', 'b', 'c'],
            ['1', 'x;y', '3'],
            ['2', 'tab\there', '4'],
            ['5', 'x;y', '7'],
            ['6', 'a,b', '8'],
        ])

    def test_dialects_file_invalid(self):
        output_file = io.StringIO()
        error_file = io.StringIO()
        utility = CSVStack([
            '--dialect-file', 'examples/test_dialects_invalid.txt',
            'examples/dummy.csv',
        ], output_file, error_file)

        with self.assertRaises(SystemExit) as e:
            utility.run()

        self.assertEqual(e.exception.code, 1)
        self.assertEqual(
            error_file.getvalue().splitlines()[-1],
            'examples/test_dialects_invalid.txt: line 1: expected "FILE=DIALECT"',
        )

    def test_dialects_stdin(self):
        with open('examples/test_dialects_semicolon.csv', 'rb') as f, stdin_as_string(f):
            self.assertRows([
                '--dialect=-=semicolon',
                '--dialect', 'examples/test_dialects_tab.tsv=tab',
                '-',
                'examples/test_dialects_tab.tsv',
            ], [
                ['a', 'b', 'c'],
                ['1', 'x;y', '3'],
                ['2', 'tab\there', '4'],
                ['5', 'x;y', '7'],
                ['6', 'a,b', '8'],
            ])

    def test_dialects_output_is_canonical(self):
        self.assertLines([
            '--dialect', 'examples/test_dialects_semicolon.csv=semicolon',
            'examples/test_dialects_semicolon.csv',
        ], [
            'a,b,c',
            '1,x;y,3',
            '2,tab\there,4',
        ])

    def test_dialects_invalid(self):
        self.assertError(
            launch_new_instance,
            ['--dialect', 'examples/test_dialects_semicolon.csv=nonsense'],
            "Invalid dialect for 'examples/test_dialects_semicolon.csv': expected a dialect name, a single delimiter "
            "or key=value pairs, got 'nonsense'.",
            args=[],
        )

    def test_dialects_invalid_declaration(self):
        self.assertError(
            launch_new_instance,
            ['--dialect', 'nonsense'],
            "Invalid dialect declaration: 'nonsense'. Expected FILE=DIALECT.",
            args=[],
        )

    def test_dialects_parse_error(self):
        output_file = io.StringIO()
        error_file = io.StringIO()
        utility = CSVStack([
            '--dialect', 'examples/test_dialects_unclosed.csv=semicolon',
            'examples/test_dialects_unclosed.csv',
        ], output_file, error_file)

        with self.assertRaises(SystemExit) as e:
            utility.run()

        self.assertEqual(e.exception.code, 1)
        self.assertEqual(error_file.getvalue().splitlines()[-1],
                         'examples/test_dialects_unclosed.csv: line 3: unexpected end of data')

    def test_dialects_short_row(self):
        output_file = io.StringIO()
        error_file = io.StringIO()
        utility = CSVStack([
            '--dialect', 'examples/test_dialects_missing_column.csv=semicolon',
            'examples/test_dialects_missing_column.csv',
        ], output_file, error_file)

        with self.assertRaises(SystemExit) as e:
            utility.run()

        self.assertEqual(e.exception.code, 1)
        self.assertEqual(error_file.getvalue().splitlines()[-1],
                         'examples/test_dialects_missing_column.csv: line 3: expected 3 columns, found 2')

    def test_dialects_unlisted_input_keeps_ragged_rows(self):
        self.assertRows([
            '--dialect', 'examples/test_dialects_semicolon.csv=semicolon',
            '--delimiter', ';',
            'examples/test_dialects_semicolon.csv',
            'examples/test_dialects_missing_column.csv',
        ], [
            ['a', 'b', 'c'],
            ['1', 'x;y', '3'],
            ['2', 'tab\there', '4'],
            ['1', '2', '3'],
            ['4', '5', ''],
        ])


class TestNoHeaderRow(TestCSVStack):

    def test_no_header_row_basic(self):
        self.assertRows(['--no-header-row', 'examples/no_header_row.csv', 'examples/no_header_row2.csv'], [
            ['a', 'b', 'c'],
            ['1', '2', '3'],
            ['4', '5', '6'],
        ])

    def test_no_header_row_basic_stdin(self):
        with open('examples/no_header_row.csv', 'rb') as f, stdin_as_string(f):
            self.assertRows(['--no-header-row', '-', 'examples/no_header_row2.csv'], [
                ['a', 'b', 'c'],
                ['1', '2', '3'],
                ['4', '5', '6'],
            ])

        with open('examples/no_header_row.csv', 'rb') as f, stdin_as_string(f):
            self.assertRows(['--no-header-row', 'examples/no_header_row2.csv', '-'], [
                ['a', 'b', 'c'],
                ['4', '5', '6'],
                ['1', '2', '3'],
            ])

    def test_no_header_row_skip_lines_stdin(self):
        with open('examples/test_skip_lines.csv', 'rb') as f, stdin_as_string(f):
            self.assertRows(['--no-header-row', '--skip-lines', '3', 'examples/test_skip_lines.csv', '-'], [
                ['a', 'b', 'c'],
                ['a', 'b', 'c'],
                ['1', '2', '3'],
                ['a', 'b', 'c'],
                ['1', '2', '3'],
            ])

    @unittest.skipUnless(os.path.isdir('/dev/fd'), 'requires /dev/fd')
    def test_no_header_row_pipe(self):
        with pipe('skip\n5,6,7\n8,9,10\n') as path:
            self.assertRows(['--no-header-row', '--skip-lines', '1', '-g', 'pipe,file', path, 'examples/dummy.csv'], [
                ['group', 'a', 'b', 'c'],
                ['pipe', '5', '6', '7'],
                ['pipe', '8', '9', '10'],
                ['file', '1', '2', '3'],
            ])

    def test_grouped_manual_and_named_column_stdin(self):
        with open('examples/no_header_row.csv', 'rb') as f, stdin_as_string(f):
            self.assertRows(
                ['--no-header-row', '--groups', 'foo,bar', '-n', 'origin', '-', 'examples/no_header_row2.csv'],
                [
                    ['origin', 'a', 'b', 'c'],
                    ['foo', '1', '2', '3'],
                    ['bar', '4', '5', '6'],
                ],
            )

    def test_grouped_filenames_stdin(self):
        with open('examples/no_header_row.csv', 'rb') as f, stdin_as_string(f):
            self.assertRows(['--no-header-row', '--filenames', '-', 'examples/no_header_row2.csv'], [
                ['group', 'a', 'b', 'c'],
                ['no_header_row.csv', '1', '2', '3'],
                ['no_header_row2.csv', '4', '5', '6'],
            ])

    def test_grouped_manual_and_named_column(self):
        self.assertRows(
            [
                "--no-header-row",
                "--groups",
                "foo,bar",
                "-n",
                "hey",
                "examples/dummy.csv",
                "examples/dummy3.csv",
            ],
            [
                ["hey", "a", "b", "c"],
                ["foo", "a", "b", "c"],
                ["foo", "1", "2", "3"],
                ["bar", "a", "b", "c"],
                ["bar", "1", "2", "3"],
                ["bar", "1", "4", "5"],
            ],
        )

    def test_grouped_filenames(self):
        self.assertRows(
            [
                "-H",
                "--filenames",
                "examples/no_header_row.csv",
                "examples/no_header_row2.csv",
            ],
            [
                ["group",                "a", "b", "c"],
                ["no_header_row.csv",    "1", "2", "3"],
                ["no_header_row2.csv",   "4", "5", "6"],
            ],
        )

    def test_grouped_filenames_and_named_column(self):
        self.assertRows(
            [
                "-H",
                "--filenames",
                "-n",
                "hello",
                "examples/no_header_row.csv",
                "examples/no_header_row2.csv",
            ],
            [
                ["hello", "a", "b", "c"],
                ["no_header_row.csv", "1", "2", "3"],
                ["no_header_row2.csv", "4", "5", "6"],
            ],
        )
