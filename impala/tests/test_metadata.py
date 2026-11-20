# Copyright 2026 Cloudera Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Local tests for exact batch metadata manifests."""

import itertools

import pytest

from impala import metadata
from impala.hiveserver2 import HiveServer2Cursor


GET_COLUMNS_WIDTH = 18


def column_row(table_cat, table_schem, table_name, column_name,
               type_name, ordinal_position=None, column_size=None,
               decimal_digits=None, data_type=None, nullable=None):
    row = [None] * GET_COLUMNS_WIDTH
    row[0] = table_cat
    row[1] = table_schem
    row[2] = table_name
    row[3] = column_name
    row[4] = data_type
    row[5] = type_name
    row[6] = column_size
    row[8] = decimal_digits
    row[10] = nullable
    row[16] = ordinal_position
    return tuple(row)


def table_row(table_cat, table_schem, table_name, table_type):
    return (table_cat, table_schem, table_name, table_type, '')


TARGETS = [
    ('cat_a', 'sales', 'orders'),
    ('cat_b', 'sales', 'orders'),
    ('cat_a', 'sales', 'orders_archive'),
    ('cat_a', 'sales', 'orders_nested'),
]

TABLE_ROWS = [
    table_row('cat_a', 'sales', 'orders', 'TABLE'),
    table_row('cat_b', 'sales', 'orders', 'VIEW'),
    table_row('cat_a', 'sales', 'orders_archive', 'TABLE'),
    table_row('cat_a', 'sales', 'orders_nested', 'TABLE'),
    # This row is what a server wildcard would also return; it must never be
    # emitted because it was not explicitly selected.
    table_row('cat_a', 'sales', 'orders_extra', 'TABLE'),
]

COLUMN_ROWS = [
    column_row('cat_a', 'sales', 'orders', 'id', 'INT', 1, data_type='4'),
    column_row('cat_a', 'sales', 'orders', 'customer', 'STRUCT', 2,
               data_type='2002'),
    column_row('cat_a', 'sales', 'orders', 'customer.name', 'STRING', None,
               column_size=64, data_type='12'),
    # Missing catalog and the same name/schema in two requested objects.
    column_row(None, 'sales', 'orders', 'amount', 'DECIMAL', 3,
               column_size=10, decimal_digits=2, data_type='3'),
    # Two valid, contradictory type declarations at one ordinal.
    column_row('cat_b', 'sales', 'orders', 'order_id', 'INT', 1,
               data_type='4'),
    column_row('cat_b', 'sales', 'orders', 'order_id', 'VARCHAR', 1,
               column_size=64, data_type='12'),
    # A nested field whose parent is not declared by this object.
    column_row('cat_a', 'sales', 'orders_nested', 'customer.name', 'STRING',
               None, column_size=64, data_type='12'),
]


def build(column_rows=COLUMN_ROWS, table_rows=TABLE_ROWS):
    return metadata.build_manifest(TARGETS, table_rows, column_rows)


def test_targets_and_types_are_distinct():
    manifest = build()
    orders_a = manifest.get(('cat_a', 'sales', 'orders'))
    orders_b = manifest.get(('cat_b', 'sales', 'orders'))
    assert orders_a.table_type == 'TABLE'
    assert orders_b.table_type == 'VIEW'
    assert orders_a.is_table and not orders_a.is_view
    assert orders_b.is_view and not orders_b.is_table

    extra = [t for t in manifest if t.name == 'orders_extra']
    assert extra == []


def test_known_columns_keep_ordinal_order():
    orders_a = build().get(('cat_a', 'sales', 'orders'))
    known = [(c.ordinal, c.name) for c in orders_a.columns]
    assert known == [(1, 'id'), (2, 'customer')]
    assert orders_a.column('customer').type_range == 'STRUCT'


def test_missing_catalog_is_kept_unassigned():
    manifest = build()
    assert len(manifest.unassigned) == 1
    item = manifest.unassigned[0]
    assert item['reason'] == metadata.ISSUE_MISSING_CATALOG
    assert sorted(item['candidates']) == ['cat_a.sales.orders',
                                          'cat_b.sales.orders']

    orders_a = manifest.get(('cat_a', 'sales', 'orders'))
    orders_b = manifest.get(('cat_b', 'sales', 'orders'))
    for structure in (orders_a, orders_b):
        assert structure.status != metadata.KNOWN
        assert metadata.ISSUE_MISSING_CATALOG in structure.reasons
        assert structure.column('amount') is None


def test_conflicting_valid_types_are_not_resolved_by_pick_one():
    orders_b = build().get(('cat_b', 'sales', 'orders'))
    order_id = orders_b.column('order_id')
    assert order_id.status == metadata.COLUMN_CONFLICT
    assert order_id.type_name is None
    assert order_id.type_known is False
    assert len(order_id.declarations) == 2
    assert orders_b.status == metadata.CONFLICT


def test_nested_field_attaches_to_explicit_parent():
    orders_a = build().get(('cat_a', 'sales', 'orders'))
    customer = orders_a.column('customer')
    assert [field.path for field in customer.nested] == ['customer.name']
    nested = customer.nested[0]
    assert nested.type_name == 'STRING'
    assert nested.type_range == 'STRING(64)'
    assert orders_a.status == metadata.PARTIAL  # missing catalog row above


def test_nested_field_without_declared_parent_stays_unresolved():
    nested_table = build().get(('cat_a', 'sales', 'orders_nested'))
    assert nested_table.columns == []
    # Only a nested row was returned; it does not establish a top-level
    # column list, so the object must not be reported with zero columns.
    assert nested_table.columns_defined is False
    assert nested_table.column_count is None
    assert [field.path for field in nested_table.unresolved_nested] == [
        'customer.name']
    assert nested_table.unresolved_nested[0].status == \
        metadata.COLUMN_UNRESOLVED
    assert nested_table.status == metadata.PARTIAL


def test_exists_only_is_not_zero_columns():
    archive = build().get(('cat_a', 'sales', 'orders_archive'))
    assert archive.status == metadata.EXISTS_ONLY
    assert archive.column_count is None
    assert archive.columns == []
    assert metadata.ISSUE_EXISTS_ONLY in archive.reasons


def test_missing_object_is_not_zero_columns():
    manifest = metadata.build_manifest([('cat_a', 'sales', 'absent')], [], [])
    absent = manifest.get(('cat_a', 'sales', 'absent'))
    assert absent.status == metadata.MISSING
    assert absent.column_count is None


def test_nested_type_pending_is_kept_pending():
    rows = [
        column_row('cat_a', 'sales', 'orders', 'customer', 'STRUCT', 1),
        column_row('cat_a', 'sales', 'orders', 'customer.name', None, None),
    ]
    manifest = metadata.build_manifest([('cat_a', 'sales', 'orders')],
                                       [TABLE_ROWS[0]], rows)
    structure = manifest.get(('cat_a', 'sales', 'orders'))
    nested = structure.column('customer').nested[0]
    assert nested.status == metadata.COLUMN_PENDING
    assert nested.type_name is None
    assert structure.status == metadata.PARTIAL
    assert metadata.ISSUE_PENDING_NESTED_TYPE in structure.reasons


def test_ordering_is_independent_of_response_order():
    baseline = build().to_dict()
    for table_perm in itertools.islice(itertools.permutations(TABLE_ROWS), 4):
        for column_perm in itertools.islice(
                itertools.permutations(COLUMN_ROWS), 4):
            variant = metadata.build_manifest(
                TARGETS, list(table_perm), list(column_perm)).to_dict()
            assert variant == baseline


def test_manifests_only_contain_requested_objects():
    manifest = build()
    assert [t.identity for t in manifest] == [tuple(t) for t in TARGETS]


class _FakeOperation(object):
    def __init__(self, rows):
        self._rows = list(rows)
        self.handle = None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _FakeSession(object):
    def __init__(self, table_rows, column_rows):
        self.table_rows = list(table_rows)
        self.column_rows = list(column_rows)
        self.calls = []

    def get_tables(self, database='.*', table_like='.*', catalog=None,
                   table_types=None):
        self.calls.append(('tables', catalog, database, table_like))
        return _FakeOperation(self.table_rows)

    def get_table_schema(self, table, database='.*', catalog=None):
        self.calls.append(('columns', catalog, database, table))
        return _FakeOperation(self.column_rows)


def _cursor_with(monkeypatch, session):
    cursor = HiveServer2Cursor(session)
    monkeypatch.setattr(cursor, '_execute_async',
                        lambda operation_fn: operation_fn())
    monkeypatch.setattr(cursor, 'fetchall',
                        lambda: cursor._last_operation.fetchall())
    return cursor


def test_cursor_batches_tables_and_columns(monkeypatch):
    session = _FakeSession(TABLE_ROWS, COLUMN_ROWS)
    cursor = _cursor_with(monkeypatch, session)
    manifest = cursor.get_table_structures(TARGETS)

    assert [t.identity for t in manifest] == [tuple(t) for t in TARGETS]
    assert 'orders_extra' not in [t.name for t in manifest]
    assert cursor._last_operation is None

    table_calls = [call for call in session.calls if call[0] == 'tables']
    column_calls = [call for call in session.calls if call[0] == 'columns']
    assert len(table_calls) == len(TARGETS)
    assert len(column_calls) == len(TARGETS)


def test_cursor_result_is_independent_of_rpc_order(monkeypatch):
    forward = _cursor_with(
        monkeypatch, _FakeSession(TABLE_ROWS, COLUMN_ROWS)
    ).get_table_structures(TARGETS).to_dict()

    reversed_session = _FakeSession(list(reversed(TABLE_ROWS)),
                                    list(reversed(COLUMN_ROWS)))
    backward = _cursor_with(
        monkeypatch, reversed_session).get_table_structures(TARGETS).to_dict()

    assert forward == backward


def test_literal_pattern_escapes_server_metacharacters():
    assert metadata.literal_pattern('sales_2024') == 'sales\\_2024'
    assert metadata.literal_pattern('100%') == '100\\%'
    assert metadata.literal_pattern('a*b') == 'a\\*b'


def test_target_parser_keeps_three_part_identity():
    parsed = metadata.normalize_targets('cat_a.sales.orders')
    assert parsed[0].identity == ('cat_a', 'sales', 'orders')

    batch = metadata.normalize_targets(['cat_a.sales.orders',
                                        'cat_b.sales.orders'])
    assert [target.identity for target in batch] == [
        ('cat_a', 'sales', 'orders'), ('cat_b', 'sales', 'orders')]
