import io

from csvkit.convert import fixed
from csvkit.exceptions import FieldBoundaryError
from csvkit.utilities.in2csv import In2CSV
from tests.utils import CSVKitTestCase, stdin_as_string


class TestFixed(CSVKitTestCase):
    Utility = In2CSV

    def test_fixed(self):
        with open('examples/testfixed') as f, open('examples/testfixed_schema.csv') as schema:
            output = fixed.fixed2csv(f, schema)

        with open('examples/testfixed_converted.csv') as f:
            self.assertEqual(f.read(), output)

    def test_fixed_skip_lines(self):
        with open('examples/testfixed_skip_lines') as f, open('examples/testfixed_schema.csv') as schema:
            output = fixed.fixed2csv(f, schema, skip_lines=3)

        with open('examples/testfixed_converted.csv') as f:
            self.assertEqual(f.read(), output)

    def test_fixed_no_inference(self):
        input_file = io.BytesIO(b'     1   2 3')

        with stdin_as_string(input_file):
            self.assertLines(['--no-inference', '-f', 'fixed', '--schema',
                              'examples/testfixed_schema_no_inference.csv'], [
                'a,b,c',
                '1,2,3',
            ])

        input_file.close()

    def test_fixed_streaming(self):
        with open('examples/testfixed') as f, open('examples/testfixed_schema.csv') as schema:
            output_file = io.StringIO()
            fixed.fixed2csv(f, schema, output=output_file)
            output = output_file.getvalue()
            output_file.close()

        with open('examples/testfixed_converted.csv') as f:
            self.assertEqual(f.read(), output)

    def test_fixed_character_positions(self):
        # A schema written with character positions reads as before, even for
        # multibyte (Japanese) place names.
        with open('examples/testfixed_bytes', 'rb') as binary:
            text = io.TextIOWrapper(binary, encoding='shift_jis')
            with open('examples/testfixed_bytes_character_schema.csv') as schema:
                output = fixed.fixed2csv(text, schema)

        with open('examples/testfixed_bytes_converted.csv', encoding='utf-8') as f:
            self.assertEqual(f.read(), output)

    def test_fixed_byte_positions(self):
        with open('examples/testfixed_bytes', 'rb') as f, \
                open('examples/testfixed_bytes_schema.csv') as schema:
            output = fixed.fixed2csv(f, schema, encoding='shift_jis', positions='bytes')

        with open('examples/testfixed_bytes_converted.csv', encoding='utf-8') as f:
            self.assertEqual(f.read(), output)

    def test_fixed_byte_positions_streaming(self):
        with open('examples/testfixed_bytes', 'rb') as f, \
                open('examples/testfixed_bytes_schema.csv') as schema:
            output_file = io.StringIO()
            fixed.fixed2csv(f, schema, output=output_file, encoding='shift_jis', positions='bytes')
            output = output_file.getvalue()
            output_file.close()

        with open('examples/testfixed_bytes_converted.csv', encoding='utf-8') as f:
            self.assertEqual(f.read(), output)

    def test_fixed_byte_positions_skip_lines(self):
        with open('examples/testfixed_bytes_skip_lines', 'rb') as f, \
                open('examples/testfixed_bytes_schema.csv') as schema:
            output = fixed.fixed2csv(f, schema, skip_lines=2, encoding='shift_jis', positions='bytes')

        with open('examples/testfixed_bytes_converted.csv', encoding='utf-8') as f:
            self.assertEqual(f.read(), output)

    def test_fixed_byte_positions_split_raises(self):
        with open('examples/testfixed_bytes', 'rb') as f, \
                open('examples/testfixed_bytes_bad_schema.csv') as schema:
            with self.assertRaises(FieldBoundaryError) as cm:
                fixed.fixed2csv(f, schema, encoding='shift_jis', positions='bytes')

        error = cm.exception
        self.assertEqual(1, error.line_number)
        self.assertEqual('place', error.field.name)
        self.assertEqual(0, error.field.start)
        self.assertEqual(1, error.field.length)
        self.assertEqual('shift_jis', error.encoding)
        self.assertIn('Column "place"', str(error))
        self.assertIn('line 1', str(error))
        self.assertIn(repr(error.line), str(error))

    def test_fixed_byte_positions_split_at_field_start(self):
        # 東京 in Shift-JIS is four bytes; starting a field at byte 3 splits 京.
        data = io.BytesIO('東京'.encode('shift_jis') + b'\n')
        schema = io.StringIO('column,start,length\na,0,2\nb,3,1\n')

        with self.assertRaises(FieldBoundaryError) as cm:
            fixed.fixed2csv(data, schema, encoding='shift_jis', positions='bytes')

        self.assertEqual('b', cm.exception.field.name)
        self.assertEqual(3, cm.exception.field.start)

    def test_fixed_byte_positions_split_at_field_start_silent_case(self):
        # A field starting at byte 1 (the second byte of 東, 0x93 0x8c) would be
        # mis-decoded as a different, valid character by per-field decoding.
        # The whole-line boundary map must reject it.
        data = io.BytesIO('東京AB'.encode('shift_jis') + b'\n')
        schema = io.StringIO('column,start,length\na,0,2\nb,1,4\n')

        with self.assertRaises(FieldBoundaryError) as cm:
            fixed.fixed2csv(data, schema, encoding='shift_jis', positions='bytes')

        self.assertEqual('b', cm.exception.field.name)
        self.assertEqual(1, cm.exception.field.start)

    def test_fixed_byte_positions_one_based(self):
        data = io.BytesIO('東京AB'.encode('shift_jis') + b'\n')
        schema = io.StringIO('column,start,length\na,1,2\nb,3,2\n')

        output = fixed.fixed2csv(data, schema, encoding='shift_jis', positions='bytes')

        self.assertEqual('a,b\n東,京\n', output)

    def test_fixed_byte_positions_requires_encoding(self):
        with open('examples/testfixed_bytes', 'rb') as f, \
                open('examples/testfixed_bytes_schema.csv') as schema:
            with self.assertRaises(ValueError):
                fixed.fixed2csv(f, schema, positions='bytes')

    def test_fixed_byte_positions_requires_binary(self):
        with open('examples/testfixed_bytes_schema.csv') as schema:
            with self.assertRaises(ValueError):
                fixed.FixedWidthReader(io.StringIO('text only'), schema,
                                       encoding='shift_jis', positions='bytes')

    def test_fixed_invalid_positions(self):
        with open('examples/testfixed_bytes_schema.csv') as schema:
            with self.assertRaises(ValueError):
                fixed.FixedWidthReader(io.BytesIO(b'x'), schema,
                                       encoding='shift_jis', positions='words')

    def test_fixed_byte_positions_cli(self):
        self.assertLines(
            ['-e', 'shift_jis', '-f', 'fixed', '--positions', 'bytes',
             '-s', 'examples/testfixed_bytes_schema.csv', 'examples/testfixed_bytes'],
            [
                'place,date,amount',
                '東京,2026-11-20,1234.5',
                '大阪,2026-11-19,980.0',
                '横浜,2026-11-18,42000.0',
            ])

    def test_fixed_byte_positions_cli_stdin(self):
        input_file = open('examples/testfixed_bytes', 'rb')

        with stdin_as_string(input_file):
            self.assertLines(
                ['-e', 'shift_jis', '--no-inference', '-f', 'fixed', '--positions', 'bytes',
                 '-s', 'examples/testfixed_bytes_schema.csv'],
                [
                    'place,date,amount',
                    '東京,2026-11-20,1234.5',
                    '大阪,2026-11-19,980.0',
                    '横浜,2026-11-18,42000.0',
                ])

        input_file.close()

    def test_fixed_byte_positions_cli_error(self):
        output_file = io.StringIO()
        utility = self.Utility(
            ['-e', 'shift_jis', '-f', 'fixed', '--positions', 'bytes',
             '-s', 'examples/testfixed_bytes_bad_schema.csv', 'examples/testfixed_bytes'],
            output_file)

        with self.assertRaises(FieldBoundaryError):
            utility.run()

        output_file.close()

    def test_schema_decoder_init(self):
        rd = fixed.SchemaDecoder(['column', 'start', 'length'])
        self.assertEqual(1, rd.start)
        self.assertEqual(2, rd.length)
        self.assertEqual(0, rd.column)

    def test_schema_decoder_in_action(self):
        rd = fixed.SchemaDecoder(['comment', 'start', 'length', 'column'])

        (column, start, length) = rd(['This is a comment', '0', '1', 'column_name'])
        self.assertEqual(False, rd.one_based)
        self.assertEqual('column_name', column)
        self.assertEqual(0, start)
        self.assertEqual(1, length)

        (column, start, length) = rd(['This is another comment', '1', '5', 'column_name2'])
        self.assertEqual(False, rd.one_based)
        self.assertEqual('column_name2', column)
        self.assertEqual(1, start)
        self.assertEqual(5, length)

        (column, start, length) = rd(['yet another comment', '9', '14', 'column_name3'])
        self.assertEqual(False, rd.one_based)
        self.assertEqual('column_name3', column)
        self.assertEqual(9, start)
        self.assertEqual(14, length)

    def test_one_based_row_decoder(self):
        rd = fixed.SchemaDecoder(['column', 'start', 'length'])

        (column, start, length) = rd(['LABEL', '1', '5'])
        self.assertEqual(True, rd.one_based)
        self.assertEqual('LABEL', column)
        self.assertEqual(0, start)
        self.assertEqual(5, length)

        (column, start, length) = rd(['LABEL2', '6', '15'])
        self.assertEqual('LABEL2', column)
        self.assertEqual(5, start)
        self.assertEqual(15, length)

    def test_schematic_line_parser(self):
        schema = """column,start,length
foo,1,5
bar,6,2
baz,8,5"""

        f = io.StringIO(schema)
        parser = fixed.FixedWidthRowParser(f)
        f.close()

        self.assertEqual('foo', parser.headers[0])
        self.assertEqual('bar', parser.headers[1])
        self.assertEqual('baz', parser.headers[2])

        parsed = parser.parse("111112233333")
        self.assertEqual('11111', parsed[0])
        self.assertEqual('22', parsed[1])
        self.assertEqual('33333', parsed[2])

        parsed = parser.parse("    1 2    3")
        self.assertEqual('1', parsed[0])
        self.assertEqual('2', parsed[1])
        self.assertEqual('3', parsed[2])

        parsed = parser.parse("1  1  233  3")
        self.assertEqual('1  1', parsed[0])
        self.assertEqual('2', parsed[1])
        self.assertEqual('33  3', parsed[2])
