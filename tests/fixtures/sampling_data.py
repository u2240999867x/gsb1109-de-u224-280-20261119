import struct

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from thrift.protocol import TCompactProtocol
from thrift.transport import TTransport

from parquet_tools.gen_py.parquet.ttypes import FileMetaData


def strip_row_group_stats(path: str, group_index: int) -> None:
    '''Rewrite the parquet footer so one row group reports no statistics.

    This mirrors a writer that did not collect statistics for the group.
    '''
    with open(path, 'rb') as fo:
        fo.seek(-8, 2)
        footer_size = struct.unpack('<i', fo.read(4))[0]
        fo.seek(-(8 + footer_size), 2)
        footer_bytes = fo.read(footer_size)
        data_prefix = fo.tell() - footer_size

    transport = TTransport.TMemoryBuffer(footer_bytes)
    metadata = FileMetaData()
    metadata.read(TCompactProtocol.TCompactProtocol(transport))
    for chunk in metadata.row_groups[group_index].columns:
        chunk.meta_data.statistics = None

    out = TTransport.TMemoryBuffer()
    metadata.write(TCompactProtocol.TCompactProtocol(out))
    new_footer = out.getvalue()
    with open(path, 'rb') as fo:
        data = fo.read()[:data_prefix]
    with open(path, 'wb') as fo:
        fo.write(data)
        fo.write(new_footer)
        fo.write(struct.pack('<i', len(new_footer)))
        fo.write(b'PAR1')


def _write_grouped(path: str, groups, schema):
    writer = pq.ParquetWriter(path, schema)
    for group in groups:
        writer.write_table(pa.Table.from_pylist(group, schema=schema))
    writer.close()


def full_read_order(file1: str, file2: str):
    '''Business order of every row from an unrestricted read.'''
    rows = []
    for order, path in enumerate([file1, file2]):
        reader = pq.ParquetFile(path)
        for group_index in range(reader.num_row_groups):
            table = reader.read_row_groups([group_index]).to_pylist()
            for row_index, row in enumerate(table):
                rows.append({
                    'file': order,
                    'file_path': path,
                    'row_group': group_index,
                    'row_in_group': row_index,
                    'status': row['status'],
                    'amount': row['amount'],
                })
    return rows


@pytest.fixture
def workspace(tmp_path):
    '''Two-file local list with three distinctive row groups in file one.

    file1 groups:
      0: every row fails the predicate, reliable statistics allow pruning
      1: a mix whose statistics are stripped (uncertain -> real row filter)
      2: more than twenty qualifying rows, so matches survive past row 20
    file2 lacks the nullable `source` display column.
    '''
    schema1 = pa.schema([
        ('amount', pa.float64()),
        ('status', pa.string()),
        ('source', pa.string()),
    ])
    schema2 = pa.schema([
        ('amount', pa.float64()),
        ('status', pa.string()),
    ])

    next_amount = 0

    def amount():
        nonlocal next_amount
        next_amount += 1
        return float(next_amount)

    group0 = [{'amount': amount(), 'status': 'BAD', 'source': 'g0'}
              for _ in range(5)]
    group1_status = ['BAD', 'OK', 'OK', 'BAD', 'OK', 'OK',
                     'BAD', 'OK', 'OK', 'OK']
    group1 = [{'amount': amount(), 'status': status, 'source': 'g1'}
              for status in group1_status]
    group2 = []
    for idx in range(30):
        group2.append({
            'amount': amount(),
            'status': 'BAD' if idx == 29 else 'OK',
            'source': 'g2',
        })

    group3_status = ['OK'] * 18 + ['BAD', 'BAD']
    group3 = [{'amount': amount(), 'status': status}
              for status in group3_status]

    file1 = str(tmp_path / 'first.parquet')
    file2 = str(tmp_path / 'second.parquet')
    _write_grouped(file1, [group0, group1, group2], schema1)
    strip_row_group_stats(file1, 1)
    _write_grouped(file2, [group3], schema2)

    return {
        'dir': tmp_path,
        'file1': file1,
        'file2': file2,
        'group1_ok_in_group': [i for i, s in enumerate(group1_status) if s == 'OK'],
        'group2_ok_in_group': list(range(29)),
        'group3_ok_in_group': list(range(18)),
    }
