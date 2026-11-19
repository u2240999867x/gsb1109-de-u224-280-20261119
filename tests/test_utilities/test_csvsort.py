import io
import sys
from unittest.mock import patch

from csvkit.utilities.csvsort import CSVSort, launch_new_instance
from tests.utils import ColumnsTests, CSVKitTestCase, EmptyFileTests, NamesTests, stdin_as_string


class TestCSVSort(CSVKitTestCase, ColumnsTests, EmptyFileTests, NamesTests):
    Utility = CSVSort

    def test_launch_new_instance(self):
        with patch.object(sys, 'argv', [self.Utility.__name__.lower(), 'examples/dummy.csv']):
            launch_new_instance()

    def test_runs(self):
        self.assertRows(['examples/test_utf8.csv'], [
            ['foo', 'bar', 'baz'],
            ['1', '2', '3'],
            ['4', '5', 'ʤ'],
        ])

    def test_encoding(self):
        self.assertRows(['-e', 'latin1', 'examples/test_latin1.csv'], [
            ['a', 'b', 'c'],
            ['1', '2', '3'],
            ['4', '5', '©'],
        ])

    def test_sort_string_reverse(self):
        reader = self.get_output_as_reader(['-c', '1', '-r', 'examples/testxls_converted.csv'])
        test_order = ['text', 'Unicode! Σ', 'This row has blanks',
                      'Chicago Tribune', 'Chicago Sun-Times', 'Chicago Reader']
        new_order = [str(r[0]) for r in reader]
        self.assertEqual(test_order, new_order)

    def test_sort_date(self):
        reader = self.get_output_as_reader(['-c', '2', 'examples/testxls_converted.csv'])
        test_order = ['text', 'Chicago Tribune', 'Chicago Sun-Times',
                      'Chicago Reader', 'This row has blanks', 'Unicode! Σ']
        new_order = [str(r[0]) for r in reader]
        self.assertEqual(test_order, new_order)

    def test_ignore_case(self):
        self.assertRows(['-i', 'examples/test_ignore_case.csv'], [
            ['a', 'b', 'c'],
            ['3', '2009-01-01', 'd'],
            ['20', '2001-01-01', 'c'],
            ['20', '2002-01-01', 'b'],
            ['100', '2003-01-01', 'a'],
            ['100', '2003-01-01', 'A'],
        ])

    def test_no_blanks(self):
        reader = self.get_output_as_reader(['examples/blanks.csv'])
        test_order = [
            ['a', 'b', 'c', 'd', 'e', 'f'],
            ['', '', '', '', '', ''],
        ]
        new_order = list(reader)
        self.assertEqual(test_order, new_order)

    def test_blanks(self):
        reader = self.get_output_as_reader(['--blanks', 'examples/blanks.csv'])
        test_order = [
            ['a', 'b', 'c', 'd', 'e', 'f'],
            ['', 'NA', 'N/A', 'NONE', 'NULL', '.'],
        ]
        new_order = list(reader)
        self.assertEqual(test_order, new_order)

    def test_no_header_row(self):
        self.assertRows(['--no-header-row', '--no-inference', 'examples/no_header_row.csv'], [
            ['a', 'b', 'c'],
            ['1', '2', '3'],
        ])

    def test_no_inference(self):
        reader = self.get_output_as_reader(['--no-inference', '-c', '1', 'examples/test_literal_order.csv'])
        test_order = ['a', '192', '27', '3']
        new_order = [str(r[0]) for r in reader]
        self.assertEqual(test_order, new_order)

    def test_sort_t_and_nulls(self):
        reader = self.get_output_as_reader(['-c', '2', 'examples/sort_ints_nulls.csv'])
        test_order = ['b', '1', '2', '']
        new_order = [str(r[1]) for r in reader]
        self.assertEqual(test_order, new_order)

    def test_stdin(self):
        input_file = io.BytesIO(b'a,b,c\n4,5,6\n1,2,3\n')

        with stdin_as_string(input_file):
            self.assertLines([], [
                'a,b,c',
                '1,2,3',
                '4,5,6',
            ])

        input_file.close()

    def test_max_rows(self):
        self.assertRows(['--max-rows', '2', 'examples/test_utf8.csv'], [
            ['foo', 'bar', 'baz'],
            ['1', '2', '3'],
            ['4', '5', 'ʤ'],
        ])

    def test_max_rows_matches_single_pass(self):
        for args in ([], ['-r'], ['-i'], ['-c', '2'], ['--no-inference']):
            single_pass = self.get_output(args + ['examples/testxls_converted.csv'])

            for max_rows in (1, 2, 3, 100):
                self.assertEqual(
                    single_pass,
                    self.get_output(args + ['--max-rows', str(max_rows), 'examples/testxls_converted.csv']),
                )

    def test_max_rows_stable(self):
        input_file = io.BytesIO(b'a,b\n1,x\n2,y\n1,z\n2,w\n1,v\n')

        with stdin_as_string(input_file):
            self.assertLines(['--max-rows', '1', '-c', '1'], [
                'a,b',
                '1,x',
                '1,z',
                '1,v',
                '2,y',
                '2,w',
            ])

        input_file.close()

    def test_max_rows_reverse_stable(self):
        input_file = io.BytesIO(b'a,b\n1,x\n2,y\n1,z\n2,w\n1,v\n')

        with stdin_as_string(input_file):
            self.assertLines(['--max-rows', '1', '-c', '1', '-r'], [
                'a,b',
                '2,y',
                '2,w',
                '1,x',
                '1,z',
                '1,v',
            ])

        input_file.close()

    def test_max_rows_type_inference_is_global(self):
        # The "x" in a later chunk means column "a" is text, so the numbers in earlier
        # chunks must be ordered as text too, not as numbers.
        input_file = io.BytesIO(b'a\n1\n2\n3\nx\n10\n')

        with stdin_as_string(input_file):
            self.assertLines(['--max-rows', '2'], [
                'a',
                '1',
                '10',
                '2',
                '3',
                'x',
            ])

        input_file.close()

    def test_max_rows_unseekable_stdin(self):
        class UnseekableStdin(io.TextIOWrapper):
            def seekable(self):
                return False

        input_file = UnseekableStdin(io.BufferedReader(io.BytesIO(b'a,b,c\n4,5,6\n1,2,3\n')))
        stdin = sys.stdin
        sys.stdin = input_file

        try:
            self.assertLines(['--max-rows', '1'], [
                'a,b,c',
                '1,2,3',
                '4,5,6',
            ])
        finally:
            sys.stdin = stdin
            input_file.close()

    def test_max_rows_invalid(self):
        self.assertError(launch_new_instance, ['--max-rows', '0'], '--max-rows must be at least 1.')
