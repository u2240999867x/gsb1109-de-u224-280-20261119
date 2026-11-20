import io
import os
import sys
import unittest
from unittest.mock import patch

from csvkit.exceptions import JoinKeyConflictError
from csvkit.utilities.csvjoin import CSVJoin, launch_new_instance
from tests.utils import CSVKitTestCase, EmptyFileTests


STORES = 'examples/stores'
TRANSACTIONS = f'{STORES}/transactions.csv'
CATALOG = f'{STORES}/catalog.csv'
DELIVERIES = f'{STORES}/deliveries.csv'
CATALOG_MAPPING = f'{STORES}/catalog_mapping.csv'
CATALOG_MAPPING_REGIONLESS = f'{STORES}/catalog_mapping_regionless.csv'
CATALOG_CONFLICT = f'{STORES}/catalog_conflict.csv'
CATALOG_CONFLICT_MAPPING = f'{STORES}/catalog_conflict_mapping.csv'

# Join columns per source: transactions, catalog, deliveries (and swapped variants).
STORE_KEYS = 'region,store_no;area,shop_code;zone,outlet_id'
SWAPPED_KEYS = 'region,store_no;zone,outlet_id;area,shop_code'


class TestCSVJoin(CSVKitTestCase, EmptyFileTests):
    Utility = CSVJoin
    default_args = ['examples/dummy.csv', '-']

    def test_launch_new_instance(self):
        with patch.object(sys, 'argv', [self.Utility.__name__.lower(), 'examples/join_a.csv', 'examples/join_b.csv']):
            launch_new_instance()

    def test_options(self):
        for args, message in (
            (
                ['-c' '1,2'],
                'The number of join column names must match the number of files, '
                'or be a single column name that exists in all files.',
            ),
            (['-c', '1', '--left', '--right'], 'It is not valid to specify both a left and a right join.'),
        ):
            with self.subTest(args=args):
                self.assertError(launch_new_instance, args, message)

    def test_join_options(self):
        for option in ('--left', '--right', '--outer'):
            with self.subTest(option=option):
                self.assertError(
                    launch_new_instance,
                    [option],
                    'You must provide join column names when performing an outer join.',
                )

    @unittest.skipIf(os.name != 'nt', 'Windows only')
    def test_glob(self):
        self.assertRows(['--no-inference', '-c', 'a', 'examples/dummy?.csv'], [
            ['a', 'b', 'c', 'b2', 'c2'],
            ['1', '2', '3', '2', '3'],
            ['1', '2', '3', '4', '5'],
        ])

    def test_sequential(self):
        output = self.get_output_as_io(['examples/join_a.csv', 'examples/join_b.csv'])
        self.assertEqual(len(output.readlines()), 4)

    def test_inner(self):
        output = self.get_output_as_io(['-c', 'a', 'examples/join_a.csv', 'examples/join_b.csv'])
        self.assertEqual(len(output.readlines()), 3)

    def test_left(self):
        output = self.get_output_as_io(['-c', 'a', '--left', 'examples/join_a.csv', 'examples/join_b.csv'])
        self.assertEqual(len(output.readlines()), 5)

    def test_right(self):
        output = self.get_output_as_io(['-c', 'a', '--right', 'examples/join_a.csv', 'examples/join_b.csv'])
        self.assertEqual(len(output.readlines()), 4)

    def test_right_indices(self):
        output = self.get_output_as_io(['-c', '1,4', '--right', 'examples/join_a.csv', 'examples/blanks.csv'])
        self.assertEqual(len(output.readlines()), 2)

    def test_outer(self):
        output = self.get_output_as_io(['-c', 'a', '--outer', 'examples/join_a.csv', 'examples/join_b.csv'])
        self.assertEqual(len(output.readlines()), 6)

    def test_left_short_columns(self):
        output = self.get_output_as_io(['-c', 'a', 'examples/join_a_short.csv', 'examples/join_b.csv'])
        with open('examples/join_short.csv') as f:
            self.assertEqual(output.readlines(), f.readlines())

    def test_single(self):
        self.assertRows(['examples/dummy.csv', '--no-inference'], [
            ['a', 'b', 'c'],
            ['1', '2', '3'],
        ])

    def test_no_blanks(self):
        self.assertRows(['examples/blanks.csv', 'examples/blanks.csv'], [
            ['a', 'b', 'c', 'd', 'e', 'f', 'a2', 'b2', 'c2', 'd2', 'e2', 'f2'],
            ['', '', '', '', '', '', '', '', '', '', '', ''],
        ])

    def test_blanks(self):
        self.assertRows(['--blanks', 'examples/blanks.csv', 'examples/blanks.csv'], [
            ['a', 'b', 'c', 'd', 'e', 'f', 'a2', 'b2', 'c2', 'd2', 'e2', 'f2'],
            ['', 'NA', 'N/A', 'NONE', 'NULL', '.', '', 'NA', 'N/A', 'NONE', 'NULL', '.'],
        ])

    def test_no_header_row(self):
        output = self.get_output_as_io(
            ['-c', '1', '--no-header-row', 'examples/join_a.csv', 'examples/join_no_header_row.csv'])
        self.assertEqual(len(output.readlines()), 3)

    def test_no_inference(self):
        self.assertRows(['--no-inference', 'examples/join_a.csv', 'examples/join_short.csv'], [
            ['a', 'b', 'c', 'a2', 'b2', 'c2', 'b2_2', 'c2_2'],
            ['1', 'b', 'c', '1', 'b', '', 'b', 'c'],
            ['2', 'b', 'c', '1', 'b', '', 'b', 'c'],
            ['3', 'b', 'c', '', '', '', '', ''],
        ])

    def test_sniff_limit_no_limit(self):
        self.assertRows(['examples/join_a.csv', 'examples/sniff_limit.csv'], [
            ['a', 'b', 'c', 'a2', 'b2', 'c2'],
            ['1', 'b', 'c', 'True', '2', '3'],
            ['2', 'b', 'c', '', '', ''],
            ['3', 'b', 'c', '', '', ''],
        ])

    def test_sniff_limit_zero_limit(self):
        self.assertRows(['--snifflimit', '0', 'examples/join_a.csv', 'examples/sniff_limit.csv'], [
            ['a', 'b', 'c', 'a;b;c'],
            ['1', 'b', 'c', '1;2;3'],
            ['2', 'b', 'c', ''],
            ['3', 'b', 'c', ''],
        ])

    def test_stores_three_way_outer_join(self):
        self.assertRows([
            '-I', '--outer', '-c', STORE_KEYS,
            '--map-file', f'2:{CATALOG_MAPPING}',
            TRANSACTIONS, CATALOG, DELIVERIES,
        ], [
            ['region', 'store_no', 'txn_id', 'amount', 'area', 'shop_code', 'sku', 'category',
             'zone', 'outlet_id', 'delivery_qty'],
            ['华东', '001', 'T100', '120', '华东', '001', 'SKU-A', 'food', '华东', '001', '12'],
            ['华南', '003', 'T101', '85', '华南', '003', 'SKU-B', 'drink', '华南', '003', '7'],
            ['华北', '005', 'T102', '60', '华北', '010', 'SKU-D', 'dairy', '华北', '005', '4'],
            ['', '', '', '', '华南', '007', 'SKU-C', 'snack', '华南', '007', '9'],
            ['', '', '', '', '华东', '005', 'SKU-F', 'bakery', '', '', ''],
            ['华东', '', 'T103', '40', '', '', '', '', '', '', ''],
            ['', '', '', '', '华东', '', 'SKU-E', 'misc', '', '', ''],
            ['', '', '', '', '', '', '', '', '华南', '', '3'],
        ])

    def test_stores_outer_join_swapped_inputs(self):
        self.assertRows([
            '-I', '--outer', '-c', SWAPPED_KEYS,
            '--map-file', f'3:{CATALOG_MAPPING}',
            TRANSACTIONS, DELIVERIES, CATALOG,
        ], [
            ['region', 'store_no', 'txn_id', 'amount', 'zone', 'outlet_id', 'delivery_qty',
             'area', 'shop_code', 'sku', 'category'],
            ['华东', '001', 'T100', '120', '华东', '001', '12', '华东', '001', 'SKU-A', 'food'],
            ['华南', '003', 'T101', '85', '华南', '003', '7', '华南', '003', 'SKU-B', 'drink'],
            ['华北', '005', 'T102', '60', '华北', '005', '4', '华北', '010', 'SKU-D', 'dairy'],
            ['', '', '', '', '华南', '007', '9', '华南', '007', 'SKU-C', 'snack'],
            ['', '', '', '', '', '', '', '华东', '005', 'SKU-F', 'bakery'],
            ['华东', '', 'T103', '40', '', '', '', '', '', '', ''],
            ['', '', '', '', '华南', '', '3', '', '', '', ''],
            ['', '', '', '', '', '', '', '华东', '', 'SKU-E', 'misc'],
        ])

    def test_stores_original_columns_keep_source_values(self):
        reader = self.get_output_as_reader([
            '-I', '--outer', '-c', STORE_KEYS,
            '--map-file', f'2:{CATALOG_MAPPING}',
            TRANSACTIONS, CATALOG, DELIVERIES,
        ])
        header = next(reader)
        shop_code = header.index('shop_code')
        outlet_id = header.index('outlet_id')
        store_no = header.index('store_no')
        rows = list(reader)

        mapped_row = next(row for row in rows if row[shop_code] == '010')
        self.assertEqual(mapped_row[store_no], '005')
        self.assertEqual(mapped_row[outlet_id], '005')
        self.assertEqual(mapped_row[shop_code], '010')

        store_007 = next(row for row in rows if row[shop_code] == '007')
        self.assertEqual(store_007[store_no], '')
        self.assertEqual(store_007[outlet_id], '007')

    def test_stores_blank_rows_stay_in_their_own_source(self):
        reader = self.get_output_as_reader([
            '-I', '--outer', '-c', STORE_KEYS,
            '--map-file', f'2:{CATALOG_MAPPING}',
            TRANSACTIONS, CATALOG, DELIVERIES,
        ])
        header = next(reader)
        width = len(header)
        key_columns = (header.index('store_no'), header.index('shop_code'), header.index('outlet_id'))
        source_widths = (4, 4, 3)
        blank_rows = []

        for row in reader:
            self.assertEqual(len(row), width)

            if all(row[index] == '' for index in key_columns):
                blank_rows.append(row)

        self.assertEqual(len(blank_rows), 3)

        for row in blank_rows:
            populated_sources = [
                any(row[offset:offset + size])
                for offset, size in zip((0, 4, 8), source_widths)
            ]
            self.assertEqual(sum(populated_sources), 1)

    def test_stores_conflicting_mapping_aborts_before_output(self):
        output_file = io.TextIOWrapper(io.BytesIO(), encoding='utf-8', newline='', write_through=True)
        utility = self.Utility([
            '-I', '--outer', '-c', STORE_KEYS,
            '--map-file', f'2:{CATALOG_CONFLICT_MAPPING}',
            TRANSACTIONS, CATALOG_CONFLICT, DELIVERIES,
        ], output_file)

        with self.assertRaises(JoinKeyConflictError) as context:
            utility.run()

        message = str(context.exception)
        self.assertIn("'华南', '007'", message)
        self.assertIn("shop_code='012'", message)
        self.assertIn("shop_code='013'", message)
        self.assertEqual(output_file.buffer.getvalue(), b'')
        output_file.close()

    def test_map_file_auto_position(self):
        output = self.get_output([
            '-I', '--outer', '-c', 'area,shop_code;zone,outlet_id',
            '--map-file', CATALOG_MAPPING,
            CATALOG, DELIVERIES,
        ])
        self.assertIn('华北,010,SKU-D,dairy,华北,005,4', output)

    def test_regionless_mapping_uses_map_region(self):
        output = self.get_output([
            '-I', '--outer', '-c', STORE_KEYS,
            '--map-region', '华北',
            '--map-file', f'2:{CATALOG_MAPPING_REGIONLESS}',
            TRANSACTIONS, CATALOG, DELIVERIES,
        ])
        self.assertIn('华北,005,T102,60,华北,010,SKU-D,dairy,华北,005,4', output)

    def test_region_scoped_mapping_does_not_cross_regions(self):
        left = self._write_temp_csv('region,store,val\n华南,012,x\n华东,012,y\n')
        right = self._write_temp_csv('region,store,val2\n华南,007,m\n华东,012,n\n')
        mapping = self._write_temp_csv('region,old_number,new_number\n华南,012,007\n')
        self.addCleanup(os.unlink, left)
        self.addCleanup(os.unlink, right)
        self.addCleanup(os.unlink, mapping)

        self.assertRows([
            '-I', '--outer', '-c', 'region,store;region,store',
            '--map-file', f'1:{mapping}', left, right,
        ], [
            ['region', 'store', 'val', 'region2', 'store2', 'val2'],
            ['华南', '012', 'x', '华南', '007', 'm'],
            ['华东', '012', 'y', '华东', '012', 'n'],
        ])

    def test_composite_inner_left_and_right_joins(self):
        left = self._write_temp_csv('region,store,val\n华南,012,x\n华东,012,y\n')
        right = self._write_temp_csv('region,store,val2\n华南,007,m\n华东,012,n\n')
        self.addCleanup(os.unlink, left)
        self.addCleanup(os.unlink, right)

        self.assertRows(['-I', '-c', 'region,store;region,store', left, right], [
            ['region', 'store', 'val', 'region2', 'store2', 'val2'],
            ['华东', '012', 'y', '华东', '012', 'n'],
        ])
        self.assertRows(['-I', '--left', '-c', 'region,store;region,store', left, right], [
            ['region', 'store', 'val', 'region2', 'store2', 'val2'],
            ['华南', '012', 'x', '', '', ''],
            ['华东', '012', 'y', '华东', '012', 'n'],
        ])
        self.assertRows(['-I', '--right', '-c', 'region,store;region,store', left, right], [
            ['region', 'store', 'val', 'region2', 'store2', 'val2'],
            ['华东', '012', 'y', '华东', '012', 'n'],
            ['', '', '', '华南', '007', 'm'],
        ])

    def test_mapping_file_missing_required_columns(self):
        mapping = self._write_temp_csv('foo,bar\n1,2\n')
        self.addCleanup(os.unlink, mapping)

        with self.assertRaises(SystemExit) as context:
            self.get_output([
                '-I', '--outer', '-c', 'shop_code;outlet_id',
                '--map-file', f'1:{mapping}', CATALOG, DELIVERIES,
            ])

        self.assertEqual(context.exception.code, 2)

    def test_mapping_file_rejects_conflicting_targets(self):
        mapping = self._write_temp_csv('old_number,new_number\n010,005\n010,009\n')
        self.addCleanup(os.unlink, mapping)

        with self.assertRaises(SystemExit) as context:
            self.get_output([
                '-I', '--outer', '-c', 'shop_code;outlet_id',
                '--map-file', f'1:{mapping}', CATALOG, DELIVERIES,
            ])

        self.assertEqual(context.exception.code, 2)

    def test_map_file_requires_join_columns(self):
        with self.assertRaises(SystemExit) as context:
            self.get_output(['--map-file', CATALOG_MAPPING, CATALOG, DELIVERIES])

        self.assertEqual(context.exception.code, 2)

    @staticmethod
    def _write_temp_csv(content):
        import tempfile

        with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False, encoding='utf-8', newline='') as f:
            f.write(content)
            return f.name

