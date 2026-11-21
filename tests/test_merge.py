from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from parquet_tools.parquet.merge import (
    INEXACT_DECIMAL_REDUCTION,
    MISSING_REQUIRED_FIELD,
    TYPE_MISMATCH,
    read_with_schema,
)


TARGET_SCHEMA = pa.schema([
    pa.field('invoice_id', pa.string(), nullable=False),
    pa.field('amount', pa.decimal128(20, 2), nullable=False),
    pa.field('meta', pa.struct([
        pa.field('channel', pa.string(), nullable=False),
        pa.field('note', pa.string(), nullable=True),
    ]), nullable=True),
])


def _meta(channels, notes):
    return pa.StructArray.from_arrays(
        [pa.array(channels), pa.array(notes)], names=['channel', 'note'])


def _write(path, table, row_group_size=None):
    pq.write_table(table, str(path), row_group_size=row_group_size)
    return str(path)


@pytest.fixture
def files(tmp_path):
    paths = {}

    # trailing-zero decimal(38, 10) amounts: exactly reducible to (20, 2)
    paths['trailing_zeros'] = _write(tmp_path / 'a_trailing_zeros.parquet', pa.table({
        'invoice_id': pa.array(['a1', 'a2']),
        'amount': pa.array([Decimal('12.3400000000'), Decimal('-0.5000000000')],
                           type=pa.decimal128(38, 10)),
        'meta': _meta(['web', 'app'], ['n1', None]),
    }))

    # same source type, but the second row group holds a value that cannot be
    # reduced to scale 2 without loss
    paths['bad_second_group'] = _write(
        tmp_path / 'b_bad_second_group.parquet',
        pa.table({
            'invoice_id': pa.array(['b1', 'b2', 'b3', 'b4']),
            'amount': pa.array(
                [Decimal('1.0000000000'), Decimal('2.5000000000'),
                 Decimal('3.2500000000'), Decimal('7.0000000001')],
                type=pa.decimal128(38, 10)),
            'meta': _meta(['web'] * 4, [None] * 4),
        }),
        row_group_size=2,
    )

    # same-named amount written as float
    paths['float_amount'] = _write(tmp_path / 'c_float_amount.parquet', pa.table({
        'invoice_id': pa.array(['c1']),
        'amount': pa.array([1.1], type=pa.float64()),
        'meta': _meta(['web'], [None]),
    }))

    # nullable meta.note missing; amount already at the target type
    paths['missing_nullable'] = _write(tmp_path / 'd_missing_nullable.parquet', pa.table({
        'invoice_id': pa.array(['d1', 'd2']),
        'amount': pa.array([Decimal('9.99'), Decimal('0.01')], type=pa.decimal128(20, 2)),
        'meta': pa.StructArray.from_arrays([pa.array(['app', 'web'])], names=['channel']),
    }))

    # has the same-named nested member (meta.note) but lacks required meta.channel
    paths['missing_required'] = _write(tmp_path / 'e_missing_required.parquet', pa.table({
        'invoice_id': pa.array(['e1']),
        'amount': pa.array([Decimal('1.00')], type=pa.decimal128(20, 2)),
        'meta': pa.StructArray.from_arrays([pa.array(['n1'])], names=['note']),
    }))

    # wrong type only at meta.amount, a same-named sibling of requested amount
    paths['bad_sibling'] = _write(tmp_path / 'f_bad_sibling.parquet', pa.table({
        'invoice_id': pa.array(['f1']),
        'amount': pa.array([Decimal('4.25')], type=pa.decimal128(20, 2)),
        'meta': pa.StructArray.from_arrays(
            [pa.array(['web']), pa.array([8.5])], names=['channel', 'amount']),
    }))

    return paths


def _errors(result, path):
    report = next(f for f in result.files if f.path == path)
    assert not report.accepted
    return report.errors


def test_exact_trailing_zero_reduction_accepted(files):
    result = read_with_schema([files['trailing_zeros']], TARGET_SCHEMA)
    assert result.accepted_files == [files['trailing_zeros']]
    assert result.table.schema.equals(TARGET_SCHEMA)
    assert result.table.column('amount').type == pa.decimal128(20, 2)
    assert result.table.column('amount').to_pylist() == [Decimal('12.34'), Decimal('-0.50')]


def test_inexact_value_in_later_row_group_rejected(files):
    result = read_with_schema([files['bad_second_group']], TARGET_SCHEMA)
    errors = _errors(result, files['bad_second_group'])
    assert len(errors) == 1
    assert errors[0].path == 'amount'
    assert errors[0].reason == INEXACT_DECIMAL_REDUCTION
    assert errors[0].row_groups == [1]


def test_rejected_file_contributes_no_rows(files):
    result = read_with_schema(
        [files['bad_second_group'], files['trailing_zeros']], TARGET_SCHEMA)
    assert result.accepted_files == [files['trailing_zeros']]
    # the well-formed first row group of the rejected file must not leak in
    assert result.table.column('invoice_id').to_pylist() == ['a1', 'a2']


def test_float_amount_rejected(files):
    result = read_with_schema([files['float_amount']], TARGET_SCHEMA)
    errors = _errors(result, files['float_amount'])
    assert errors[0].path == 'amount'
    assert errors[0].reason == TYPE_MISMATCH
    assert 'double' in errors[0].detail


def test_missing_nullable_field_filled_with_null(files):
    result = read_with_schema([files['missing_nullable']], TARGET_SCHEMA)
    assert result.accepted_files == [files['missing_nullable']]
    meta = result.table.column('meta').combine_chunks()
    assert meta.field('note').to_pylist() == [None, None]
    assert meta.field('channel').to_pylist() == ['app', 'web']


def test_missing_required_path_rejected(files):
    result = read_with_schema([files['missing_required']], TARGET_SCHEMA)
    errors = _errors(result, files['missing_required'])
    assert errors[0].path == 'meta.channel'
    assert errors[0].reason == MISSING_REQUIRED_FIELD


def test_input_order_only_reorders_output(files):
    forward = read_with_schema(
        [files['trailing_zeros'], files['missing_nullable']], TARGET_SCHEMA)
    backward = read_with_schema(
        [files['missing_nullable'], files['trailing_zeros']], TARGET_SCHEMA)

    assert forward.accepted_files == [files['trailing_zeros'], files['missing_nullable']]
    assert backward.accepted_files == [files['missing_nullable'], files['trailing_zeros']]

    # column types come from the target schema, never from the first file
    assert forward.table.schema.equals(TARGET_SCHEMA)
    assert backward.table.schema.equals(TARGET_SCHEMA)

    assert forward.table.column('invoice_id').to_pylist() == ['a1', 'a2', 'd1', 'd2']
    assert backward.table.column('invoice_id').to_pylist() == ['d1', 'd2', 'a1', 'a2']
    assert backward.table.column('amount').to_pylist()[:2] == [Decimal('9.99'), Decimal('0.01')]


def test_wrong_type_at_unrequested_sibling_path(files):
    schema_with_sibling = pa.schema([
        pa.field('invoice_id', pa.string(), nullable=False),
        pa.field('amount', pa.decimal128(20, 2), nullable=False),
        pa.field('meta', pa.struct([
            pa.field('channel', pa.string(), nullable=False),
            pa.field('amount', pa.decimal128(20, 2), nullable=True),
        ]), nullable=True),
    ])
    rejected = read_with_schema([files['bad_sibling']], schema_with_sibling)
    errors = _errors(rejected, files['bad_sibling'])
    assert errors[0].path == 'meta.amount'
    assert errors[0].reason == TYPE_MISMATCH

    accepted = read_with_schema([files['bad_sibling']], TARGET_SCHEMA)
    assert accepted.accepted_files == [files['bad_sibling']]
    assert accepted.table.column('amount').to_pylist() == [Decimal('4.25')]
    assert accepted.table.schema.equals(TARGET_SCHEMA)


def test_report_locates_file_path_and_row_group(files):
    result = read_with_schema([files['bad_second_group']], TARGET_SCHEMA)
    report = str(result.files[0])
    assert files['bad_second_group'] in report
    assert 'amount' in report
    assert '[1]' in report


def test_all_rejected_yields_empty_table_with_target_schema(files):
    result = read_with_schema(
        [files['float_amount'], files['missing_required']], TARGET_SCHEMA)
    assert result.accepted_files == []
    assert result.table.num_rows == 0
    assert result.table.schema.equals(TARGET_SCHEMA)
