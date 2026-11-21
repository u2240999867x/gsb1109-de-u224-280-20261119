import struct
from tempfile import TemporaryDirectory

import pytest
from thrift.protocol import TCompactProtocol
from thrift.transport import TTransport

from parquet_tools.commands.stats import _cli, configure_parser
from parquet_tools.gen_py.parquet.ttypes import (
    ColumnChunk,
    ColumnMetaData,
    ColumnOrder,
    ConvertedType,
    FieldRepetitionType,
    FileMetaData,
    IntType,
    ListType,
    LogicalType,
    RowGroup,
    SchemaElement,
    Statistics,
    Type,
    TypeDefinedOrder,
)
from parquet_tools.parquet.stats import (
    STATE_ALL_NULL,
    STATE_NO_STATS,
    STATE_OK,
    STATE_PARTIAL,
    STATE_ZERO_ROWS,
    STATUS_PROVEN,
    STATUS_UNKNOWN,
    analyze_column,
    build_report,
    schema_columns,
    select_columns,
)

COL_A = 'a.value'
COL_B = 'b.value'
COL_LIST = 'items.list.value'

# uint32 value that exceeds the signed int32 boundary
BIG_UINT32 = 4000000000
BIG_UINT32_AS_SIGNED = BIG_UINT32 - 2 ** 32  # -294967296


def u32(value):
    return struct.pack('<I', value)


def s32(value):
    return struct.pack('<i', value)


def _uint32_leaf(name, repetition=FieldRepetitionType.OPTIONAL):
    return SchemaElement(
        type=Type.INT32,
        repetition_type=repetition,
        name=name,
        converted_type=ConvertedType.UINT_32,
        logicalType=LogicalType(INTEGER=IntType(bitWidth=32, isSigned=False)),
    )


def _schema():
    # root
    #  +- a (group)        -> value: uint32
    #  +- b (group)        -> value: uint32   (same leaf name as a.value)
    #  +- items (LIST)     -> list -> value: uint32 (same leaf name, inside LIST)
    return [
        SchemaElement(repetition_type=FieldRepetitionType.REQUIRED, name='schema', num_children=3),
        SchemaElement(repetition_type=FieldRepetitionType.OPTIONAL, name='a', num_children=1),
        _uint32_leaf('value'),
        SchemaElement(repetition_type=FieldRepetitionType.OPTIONAL, name='b', num_children=1),
        _uint32_leaf('value'),
        SchemaElement(
            repetition_type=FieldRepetitionType.OPTIONAL,
            name='items',
            num_children=1,
            converted_type=ConvertedType.LIST,
            logicalType=LogicalType(LIST=ListType()),
        ),
        SchemaElement(repetition_type=FieldRepetitionType.REPEATED, name='list', num_children=1),
        _uint32_leaf('value'),
    ]


def _chunk(path, num_values, statistics):
    return ColumnChunk(
        meta_data=ColumnMetaData(
            type=Type.INT32,
            encodings=[0],
            path_in_schema=path.split('.'),
            codec=0,
            num_values=num_values,
            total_uncompressed_size=0,
            total_compressed_size=0,
            data_page_offset=4,
            statistics=statistics,
        )
    )


def _stats(null_count=None, min_u=None, max_u=None, min_s=None, max_s=None, variant='new'):
    '''Build Statistics; `variant` selects new-style (min_value/max_value)
    or legacy (signed min/max) fields.'''
    if variant == 'new':
        return Statistics(
            null_count=null_count,
            min_value=None if min_u is None else u32(min_u),
            max_value=None if max_u is None else u32(max_u),
        )
    return Statistics(
        null_count=null_count,
        min=None if min_s is None else s32(min_s),
        max=None if max_s is None else s32(max_s),
    )


def _row_group_specs(variant):
    '''The same unsigned measurement archived in five row groups.'''
    return [
        # rg0: full stats, max exceeds the signed int32 boundary
        (5, {
            COL_A: _stats(null_count=1, min_u=100, max_u=BIG_UINT32,
                          min_s=BIG_UINT32_AS_SIGNED, max_s=100, variant=variant),
            COL_B: _stats(null_count=0, min_u=10, max_u=90, min_s=10, max_s=90, variant=variant),
            COL_LIST: _stats(null_count=1, min_u=5, max_u=500, min_s=5, max_s=500, variant=variant),
        }),
        # rg1: definite null_count and one-sided range only (no max)
        (4, {
            COL_A: _stats(null_count=2, min_u=50, min_s=50, variant=variant),
            COL_B: _stats(null_count=2, min_u=20, max_u=80, min_s=20, max_s=80, variant=variant),
            COL_LIST: _stats(null_count=0, min_u=7, min_s=7, variant=variant),
        }),
        # rg2: definite all-null group
        (3, {
            COL_A: _stats(null_count=3, variant=variant),
            COL_B: _stats(null_count=3, variant=variant),
            COL_LIST: _stats(null_count=3, variant=variant),
        }),
        # rg3: zero-row group
        (0, {COL_A: None, COL_B: None, COL_LIST: None}),
        # rg4: non-empty group, statistics completely missing for a.value and the list leaf
        (2, {
            COL_A: None,
            COL_B: _stats(null_count=1, min_u=30, max_u=70, min_s=30, max_s=70, variant=variant),
            COL_LIST: None,
        }),
    ]


def make_fmd(variant='new'):
    row_groups = []
    for num_rows, col_stats in _row_group_specs(variant):
        chunks = []
        for path in (COL_A, COL_B, COL_LIST):
            chunks.append(_chunk(path, num_rows, col_stats[path]))
        row_groups.append(RowGroup(
            columns=chunks,
            total_byte_size=0,
            num_rows=num_rows,
            total_compressed_size=0,
            ordinal=len(row_groups),
        ))
    column_orders = None
    if variant == 'new':
        column_orders = [ColumnOrder(TYPE_ORDER=TypeDefinedOrder()) for _ in range(3)]
    return FileMetaData(
        version=2,
        schema=_schema(),
        num_rows=sum(num_rows for num_rows, _ in _row_group_specs(variant)),
        row_groups=row_groups,
        created_by='parquet-tools-test',
        column_orders=column_orders,
    )


def write_footer_only(path, fmd):
    '''Write a parquet file that contains ONLY the footer: no data pages at
    all. Any successful report on this file proves footer-only reading.'''
    transport = TTransport.TMemoryBuffer()
    protocol = TCompactProtocol.TCompactProtocol(transport)
    fmd.write(protocol)
    footer = transport.getvalue()
    with open(path, 'wb') as f:
        f.write(b'PAR1')
        f.write(footer)
        f.write(struct.pack('<i', len(footer)))
        f.write(b'PAR1')
    return 12 + len(footer)  # total file size: magic + footer + footer length + magic


@pytest.fixture
def new_footer_file():
    with TemporaryDirectory() as tmp_path:
        pq_path = f'{tmp_path}/new.pq'
        write_footer_only(pq_path, make_fmd('new'))
        yield pq_path


@pytest.fixture
def old_footer_file():
    with TemporaryDirectory() as tmp_path:
        pq_path = f'{tmp_path}/old.pq'
        write_footer_only(pq_path, make_fmd('old'))
        yield pq_path


def _report(fmd, path):
    column = select_columns(fmd, [path])[0]
    return analyze_column(fmd, column)


class TestSchemaColumns:
    def test_full_paths_disambiguate_same_leaf_name(self):
        columns = schema_columns(make_fmd('new'))
        assert [c.path_string for c in columns] == [COL_A, COL_B, COL_LIST]

    def test_list_leaf_detected(self):
        columns = {c.path_string: c for c in schema_columns(make_fmd('new'))}
        assert not columns[COL_A].is_repeated_leaf
        assert columns[COL_LIST].is_repeated_leaf
        assert columns[COL_LIST].max_repetition_level == 1

    def test_order_declared_only_for_new_footer(self):
        assert all(c.order_declared for c in schema_columns(make_fmd('new')))
        assert not any(c.order_declared for c in schema_columns(make_fmd('old')))

    def test_unknown_path_raises(self):
        with pytest.raises(KeyError):
            select_columns(make_fmd('new'), ['a.unknown'])


class TestUnsignedMeasurement:
    def test_new_footer_proves_unsigned_range_beyond_signed_boundary(self):
        report = _report(make_fmd('new'), COL_A)
        rg0 = report.row_groups[0]
        assert rg0.state == STATE_OK
        assert rg0.min.status == STATUS_PROVEN and rg0.min.value == 100
        assert rg0.max.status == STATUS_PROVEN and rg0.max.value == BIG_UINT32

    def test_old_footer_cannot_prove_unsigned_range(self):
        report = _report(make_fmd('old'), COL_A)
        rg0 = report.row_groups[0]
        assert rg0.min.status == STATUS_UNKNOWN
        assert 'legacy signed min' in rg0.min.reason
        assert rg0.max.status == STATUS_UNKNOWN
        assert 'legacy signed max' in rg0.max.reason
        # null_count stays provable even when the range is not
        assert rg0.null_count.status == STATUS_PROVEN
        assert rg0.null_count.value == 1


class TestRowGroupStates:
    def test_states_are_distinct(self):
        report = _report(make_fmd('new'), COL_A)
        states = [rg.state for rg in report.row_groups]
        assert states == [STATE_OK, STATE_PARTIAL, STATE_ALL_NULL, STATE_ZERO_ROWS, STATE_NO_STATS]

    def test_partial_group_keeps_proven_min_and_null_count(self):
        report = _report(make_fmd('new'), COL_A)
        rg1 = report.row_groups[1]
        assert rg1.min.status == STATUS_PROVEN and rg1.min.value == 50
        assert rg1.max.status == STATUS_UNKNOWN
        assert rg1.null_count.status == STATUS_PROVEN and rg1.null_count.value == 2

    def test_zero_row_group_proves_zero_nulls(self):
        report = _report(make_fmd('new'), COL_A)
        rg3 = report.row_groups[3]
        assert rg3.num_rows == 0
        assert rg3.null_count.status == STATUS_PROVEN and rg3.null_count.value == 0
        assert rg3.min.status == 'n/a'

    def test_all_null_group_has_no_range(self):
        report = _report(make_fmd('new'), COL_A)
        rg2 = report.row_groups[2]
        assert rg2.min.status == 'n/a' and rg2.max.status == 'n/a'
        assert rg2.null_count.status == STATUS_PROVEN and rg2.null_count.value == 3


class TestFileTotals:
    def test_file_totals_unknown_with_known_row_group_evidence(self):
        report = _report(make_fmd('new'), COL_A)
        assert report.file_min.status == STATUS_UNKNOWN
        assert report.file_min.evidence == 'known row-group min: 50'
        assert report.file_max.status == STATUS_UNKNOWN
        assert report.file_max.evidence == f'known row-group max: {BIG_UINT32}'
        assert report.file_null_count.status == STATUS_UNKNOWN
        assert report.file_null_count.evidence == 'known row-group null_count sum: 6'

    def test_file_totals_proven_when_all_contributors_proven(self):
        report = _report(make_fmd('new'), COL_B)
        assert report.file_min.status == STATUS_PROVEN and report.file_min.value == 10
        assert report.file_max.status == STATUS_PROVEN and report.file_max.value == 90
        assert report.file_null_count.status == STATUS_PROVEN and report.file_null_count.value == 6

    def test_remove_and_restore_missing_item_changes_totals_independently(self):
        fmd = make_fmd('new')
        report = _report(fmd, COL_B)
        assert report.file_null_count.status == STATUS_PROVEN
        assert report.file_null_count.value == 6

        # take away rg4's null_count: only the null total degrades
        fmd.row_groups[4].columns[1].meta_data.statistics.null_count = None
        report = _report(fmd, COL_B)
        assert report.file_null_count.status == STATUS_UNKNOWN
        assert report.file_null_count.evidence == 'known row-group null_count sum: 5'
        assert report.file_min.status == STATUS_PROVEN and report.file_min.value == 10
        assert report.file_max.status == STATUS_PROVEN and report.file_max.value == 90

        # restore it: the total is provable again
        fmd.row_groups[4].columns[1].meta_data.statistics.null_count = 1
        report = _report(fmd, COL_B)
        assert report.file_null_count.status == STATUS_PROVEN
        assert report.file_null_count.value == 6

        # take away rg1's max_value: only the max total degrades
        fmd.row_groups[1].columns[1].meta_data.statistics.max_value = None
        report = _report(fmd, COL_B)
        assert report.file_max.status == STATUS_UNKNOWN
        assert report.file_max.evidence == 'known row-group max: 90'
        assert report.file_min.status == STATUS_PROVEN and report.file_min.value == 10
        assert report.file_null_count.status == STATUS_PROVEN and report.file_null_count.value == 6

        # restore it
        fmd.row_groups[1].columns[1].meta_data.statistics.max_value = u32(80)
        report = _report(fmd, COL_B)
        assert report.file_max.status == STATUS_PROVEN and report.file_max.value == 90


class TestListLeaf:
    def test_list_leaf_marks_missing_row_level_evidence_but_keeps_values(self):
        report = _report(make_fmd('new'), COL_LIST)
        assert any('not rows' in caveat for caveat in report.caveats)
        rg0 = report.row_groups[0]
        assert rg0.min.status == STATUS_PROVEN and rg0.min.value == 5
        assert rg0.null_count.status == STATUS_PROVEN
        assert rg0.null_count.note == 'counts leaf values, not rows'


class TestBuildReport:
    def test_report_on_footer_only_file_reads_no_data_pages(self):
        import os
        with TemporaryDirectory() as tmp_path:
            pq_path = f'{tmp_path}/footer_only.pq'
            expected_size = write_footer_only(pq_path, make_fmd('new'))
            # the sample contains only magic bytes + footer: no data page
            # exists, so a successful report proves footer-only reading
            assert os.path.getsize(pq_path) == expected_size
            out = build_report(pq_path, [COL_A])
        assert 'footer statistics report' in out
        assert f'max: {BIG_UINT32} (proven)' in out

    def test_report_with_two_explicit_paths_new_and_old(self, new_footer_file, old_footer_file):
        paths = [COL_A, COL_LIST]
        new_out = build_report(new_footer_file, paths)
        old_out = build_report(old_footer_file, paths)
        for out in (new_out, old_out):
            assert f'Column({COL_A})' in out
            assert f'Column({COL_LIST})' in out
            assert f'Column({COL_B})' not in out
        assert f'max: {BIG_UINT32} (proven)' in new_out
        assert 'legacy signed max cannot prove the unsigned range' in old_out

    def test_report_defaults_to_all_columns(self, new_footer_file):
        out = build_report(new_footer_file)
        for path in (COL_A, COL_B, COL_LIST):
            assert f'Column({path})' in out

    def test_unknown_path_lists_available_paths(self, new_footer_file):
        with pytest.raises(ValueError) as exc_info:
            build_report(new_footer_file, ['value'])
        assert COL_A in str(exc_info.value)


class TestCli:
    def test_cli_with_explicit_columns(self, capfd, new_footer_file):
        from argparse import Namespace
        args = Namespace(file=new_footer_file, columns=[COL_A, COL_LIST],
                         awsprofile=None, endpoint_url=None)
        _cli(args)
        out, err = capfd.readouterr()
        assert err == ''
        assert 'footer statistics report' in out
        assert f'Column({COL_A})' in out
        assert f'Column({COL_LIST})' in out

    def test_cli_unknown_column_reports_to_stderr(self, capfd, new_footer_file):
        from argparse import Namespace
        args = Namespace(file=new_footer_file, columns=['nope'],
                         awsprofile=None, endpoint_url=None)
        _cli(args)
        out, err = capfd.readouterr()
        assert out == ''
        assert 'not found' in err

    def test_configure_parser(self):
        from argparse import ArgumentParser
        parser = configure_parser(ArgumentParser())
        args = parser.parse_args(['file.pq', '--column', COL_A, '--column', COL_LIST])
        assert args.columns == [COL_A, COL_LIST]
