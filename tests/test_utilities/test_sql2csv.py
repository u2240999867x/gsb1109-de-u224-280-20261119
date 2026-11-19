import io
import os
import sqlite3
import sys
import types
import unittest
from unittest.mock import patch

try:
    import psycopg2  # noqa: F401
    postgresql_scheme = 'postgresql'
except ImportError:
    # http://docs.sqlalchemy.org/en/latest/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg2cffi
    postgresql_scheme = 'postgresql+psycopg2cffi'

from csvkit.utilities.csvsql import CSVSQL
from csvkit.utilities.sql2csv import DEFAULT_NULL_VALUE, SQL2CSV, encode_null, launch_new_instance
from tests.utils import CSVKitTestCase, EmptyFileTests, stdin_as_string


def decode_null(field, null_value):
    """
    Invert sql2csv's --null-value encoding, mirroring the documented convention.
    """
    if field == null_value:
        return None
    if field.startswith(null_value + null_value):
        return field[len(null_value):]
    return field


class EncodeNullTests(unittest.TestCase):

    def test_null_uses_marker(self):
        self.assertEqual(encode_null(None, '\\N'), '\\N')

    def test_empty_string_is_not_null(self):
        self.assertEqual(encode_null('', '\\N'), '')

    def test_literal_marker_is_escaped(self):
        self.assertEqual(encode_null('\\N', '\\N'), '\\N\\N')

    def test_value_starting_with_marker_is_escaped(self):
        self.assertEqual(encode_null('\\N text', '\\N'), '\\N\\N text')

    def test_value_containing_marker_elsewhere_is_unchanged(self):
        self.assertEqual(encode_null('a \\N b', '\\N'), 'a \\N b')

    def test_non_string_values_are_unchanged(self):
        for value in (0, 42, 3.14, True):
            self.assertEqual(encode_null(value, '\\N'), value)

    def test_custom_marker(self):
        self.assertEqual(encode_null(None, '@NULL@'), '@NULL@')
        self.assertEqual(encode_null('@NULL@', '@NULL@'), '@NULL@@NULL@')
        self.assertEqual(encode_null('@NULL@ suffix', '@NULL@'), '@NULL@@NULL@ suffix')
        self.assertEqual(encode_null('plain', '@NULL@'), 'plain')

    def test_round_trip(self):
        values = [None, '', '\\N', '\\N\\N', '\\N text', 'NULL', 'a \\N b', 42]

        for value in values:
            with self.subTest(value=value):
                encoded = encode_null(value, '\\N')
                self.assertEqual(decode_null(str(encoded), '\\N'), None if value is None else str(value))


class TestSQL2CSV(CSVKitTestCase, EmptyFileTests):
    Utility = SQL2CSV

    def test_launch_new_instance(self):
        with patch.object(sys, 'argv', [self.Utility.__name__.lower(), '--query', 'select 1']):
            launch_new_instance()

    def setUp(self):
        self.db_file = 'foo.db'

    def tearDown(self):
        try:
            os.remove(self.db_file)
        except OSError:
            pass

    def csvsql(self, csv_file, db=None):
        """
        Load test data into the DB and return it as a string for comparison.
        """
        if not db:
            db = 'sqlite:///' + self.db_file

        args = ['--db', db, '--tables', 'foo', '--insert', csv_file, '--no-inference']

        utility = CSVSQL(args)
        utility.run()

        with open(csv_file) as f:
            text = f.read()

        return text.strip()

    def test_encoding(self):
        # Test that csvstat doesn't error on Latin-1 input.
        self.get_output(['-e', 'latin1', 'examples/test.sql'])

    def test_query(self):
        csv = self.get_output(['--query', 'select 6*9 as question'])

        self.assertTrue('question' in csv)
        self.assertTrue('54' in csv)

    def test_file(self):
        csv = self.get_output(['examples/test.sql'])

        self.assertTrue('question' in csv)
        self.assertTrue('36' in csv)

    def test_file_with_query(self):
        csv = self.get_output(['examples/test.sql', '--query', 'select 6*9 as question'])

        self.assertTrue('question' in csv)
        self.assertTrue('54' in csv)

    def test_stdin(self):
        input_file = io.BytesIO(b'select cast(3.1415 * 13.37 as integer) as answer')

        with stdin_as_string(input_file):
            csv = self.get_output([])

            self.assertTrue('answer' in csv)
            self.assertTrue('42' in csv)

        input_file.close()

    def test_stdin_with_query(self):
        input_file = io.BytesIO(b'select cast(3.1415 * 13.37 as integer) as answer')

        with stdin_as_string(input_file):
            csv = self.get_output(['--query', 'select 6*9 as question'])

            self.assertTrue('question' in csv)
            self.assertTrue('54' in csv)

        input_file.close()

    def test_stdin_with_file(self):
        input_file = io.BytesIO(b'select cast(3.1415 * 13.37 as integer) as answer')

        with stdin_as_string(input_file):
            csv = self.get_output(['examples/test.sql'])

            self.assertTrue('question' in csv)
            self.assertTrue('36' in csv)

        input_file.close()

    def test_stdin_with_file_and_query(self):
        input_file = io.BytesIO(b'select cast(3.1415 * 13.37 as integer) as answer')

        with stdin_as_string(input_file):
            csv = self.get_output(['examples/test.sql', '--query', 'select 6*9 as question'])

            self.assertTrue('question' in csv)
            self.assertTrue('54' in csv)

        input_file.close()

    def test_unicode(self):
        expected = self.csvsql('examples/test_utf8.csv')
        csv = self.get_output(['--db', 'sqlite:///' + self.db_file, '--query', 'select * from foo'])
        self.assertEqual(csv.strip(), expected)

    def test_no_header_row(self):
        self.csvsql('examples/dummy.csv')
        csv = self.get_output(['--db', 'sqlite:///' + self.db_file, '--no-header-row', '--query', 'select * from foo'])

        self.assertTrue('a,b,c' not in csv)
        self.assertTrue('1,2,3' in csv)

    def test_linenumbers(self):
        self.csvsql('examples/dummy.csv')
        csv = self.get_output(['--db', 'sqlite:///' + self.db_file, '--linenumbers', '--query', 'select * from foo'])

        self.assertTrue('line_number,a,b,c' in csv)
        self.assertTrue('1,1,2,3' in csv)

    def test_wildcard_on_sqlite(self):
        self.csvsql('examples/iris.csv')
        csv = self.get_output(['--db', 'sqlite:///' + self.db_file, '--query',
                              "select * from foo where species LIKE '%'"])

        self.assertTrue('sepal_length,sepal_width,petal_length,petal_width,species' in csv)
        self.assertTrue('5.1,3.5,1.4,0.2,Iris-setosa' in csv)

    def _create_note_table(self, values):
        connection = sqlite3.connect(self.db_file)
        connection.execute('CREATE TABLE t (id INTEGER, note TEXT)')
        connection.executemany('INSERT INTO t VALUES (?, ?)', values)
        connection.commit()
        connection.close()

    def test_null_value_distinguishes_null_empty_and_literal(self):
        self._create_note_table([
            (1, None),
            (2, ''),
            (3, 'NULL'),
            (4, '\\N'),
            (5, '\\N suffix'),
            (6, 'plain'),
        ])

        reader = self.get_output_as_reader([
            '--db', 'sqlite:///' + self.db_file, '--null-value',
            '--query', 'select id, note from t order by id',
        ])

        self.assertEqual(next(reader), ['id', 'note'])
        rows = list(reader)

        # SQL NULL, the empty string, and the literal text "NULL" stay distinct.
        self.assertEqual(rows[0], ['1', DEFAULT_NULL_VALUE])
        self.assertEqual(rows[1], ['2', ''])
        self.assertEqual(rows[2], ['3', 'NULL'])
        # Values containing the marker are escaped by doubling the marker prefix.
        self.assertEqual(rows[3], ['4', '\\N\\N'])
        self.assertEqual(rows[4], ['5', '\\N\\N suffix'])
        self.assertEqual(rows[5], ['6', 'plain'])

        # Every exported field reads back to the original database value.
        decoded = [decode_null(row[1], DEFAULT_NULL_VALUE) for row in rows]
        self.assertEqual(decoded, [None, '', 'NULL', '\\N', '\\N suffix', 'plain'])

    def test_null_value_custom_marker(self):
        self._create_note_table([(1, None), (2, '@NULL@'), (3, '')])

        reader = self.get_output_as_reader([
            '--db', 'sqlite:///' + self.db_file, '--null-value', '@NULL@',
            '--query', 'select id, note from t order by id',
        ])

        self.assertEqual(next(reader), ['id', 'note'])
        rows = list(reader)
        self.assertEqual(rows, [['1', '@NULL@'], ['2', '@NULL@@NULL@'], ['3', '']])

        decoded = [decode_null(row[1], '@NULL@') for row in rows]
        self.assertEqual(decoded, [None, '@NULL@', ''])

    def test_without_null_value_writes_null_as_empty_field(self):
        self._create_note_table([(1, None), (2, '')])

        reader = self.get_output_as_reader([
            '--db', 'sqlite:///' + self.db_file,
            '--query', 'select id, note from t order by id',
        ])

        self.assertEqual(list(reader), [['id', 'note'], ['1', ''], ['2', '']])

    def test_null_value_consistent_across_batches(self):
        # A streaming result drained in many small batches, mirroring a
        # server-side cursor (e.g. PostgreSQL with stream_results + yield_per).
        # The same encoding must be applied from the first batch to the last.
        rows = [
            (index, None if index % 4 == 0 else ('\\N' if index % 4 == 1 else ('NULL' if index % 4 == 2 else '')))
            for index in range(300)
        ]

        class FakeResult:
            def __init__(self):
                self._index = 0
                self.batch_size = 7
                self.fetchmany_calls = 0
                self.returns_rows = True
                self._metadata = types.SimpleNamespace(keys=['id', 'note'])

            def fetchmany(self, size=None):
                self.fetchmany_calls += 1
                size = size or self.batch_size
                chunk = rows[self._index:self._index + size]
                self._index += len(chunk)
                return chunk

            def __iter__(self):
                while True:
                    chunk = self.fetchmany(self.batch_size)
                    if not chunk:
                        break
                    yield from chunk

        result = FakeResult()

        class FakeConnection:
            def execution_options(self, **kwargs):
                return self

            def exec_driver_sql(self, query):
                return result

            def close(self):
                pass

        class FakeEngine:
            def connect(self):
                return FakeConnection()

            def dispose(self):
                pass

        with patch('csvkit.utilities.sql2csv.create_engine', return_value=FakeEngine()):
            reader = self.get_output_as_reader([
                '--db', 'postgresql:///example', '--null-value',
                '--execution-option', 'stream_results', 'True',
                '--execution-option', 'yield_per', '7',
                '--query', 'select id, note from t order by id',
            ])

        # The extraction really was served in multiple batches.
        self.assertGreater(result.fetchmany_calls, 1)

        self.assertEqual(next(reader), ['id', 'note'])
        for index, row in enumerate(reader):
            original_note = rows[index][1]
            expected_note = '' if original_note == '' else encode_null(original_note, DEFAULT_NULL_VALUE)
            self.assertEqual(row, [str(index), expected_note])

    def test_null_value_rejects_line_breaks(self):
        self.assertError(
            launch_new_instance,
            ['--null-value', 'bad\nvalue', '--query', 'select 1'],
            'The --null-value marker must not contain line breaks.',
            args=[],
        )

    def test_null_value_rejects_empty_marker(self):
        self.assertError(
            launch_new_instance,
            ['--null-value', '', '--query', 'select 1'],
            'The --null-value marker must not be an empty string, because an empty marker cannot distinguish '
            'NULL from an empty string.',
            args=[],
        )

    # def test_wildcard_on_postgresql(self):
    #     db = postgresql_scheme + ':///dummy_test'

    #     self.csvsql('examples/iris.csv', db)
    #     csv = self.get_output(['--db', db, '--query', "select * from foo where species LIKE '%'"])

    #     self.assertTrue('sepal_length,sepal_width,petal_length,petal_width,species' in csv)
    #     self.assertTrue('5.1,3.5,1.4,0.2,Iris-setosa' in csv)
