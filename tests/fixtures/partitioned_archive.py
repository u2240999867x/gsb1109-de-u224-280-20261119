import os

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tests.fixtures.parquet_footer import strip_column_statistics

PLUS_REGION = 'us+east'
ENCODED_WEST_REGION = 'eu+west'
# Single percent-decode of 'region=ap%25south' yields this literal value; a
# second decode would wrongly turn '%2B' into '+'.
ENCODED_SOUTH_REGION = 'ap%south'


def _write_row_groups(path, frames):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    table = pa.Table.from_pandas(pd.concat(frames, ignore_index=True))
    with pq.ParquetWriter(path, table.schema) as writer:
        offset = 0
        for frame in frames:
            writer.write_table(table.slice(offset, len(frame)))
            offset += len(frame)


def _frame(rows):
    return pd.DataFrame(
        rows, columns=['region', 'date', 'group_id', 'measurement'])


def build_partitioned_archive(base_dir):
    """Create the local archive described by the reconciliation brief.

    Returns the business root and a map of notable file paths.  The same tree
    is uploaded verbatim to mocked S3 by the S3 fixture.
    """
    root = os.path.join(base_dir, 'archive')

    plus_dir = os.path.join(root, 'region=us+east', 'date=2026-01-01')
    west_dir = os.path.join(root, 'region=eu%2Bwest', 'date=2026-01-02')
    south_dir = os.path.join(root, 'region=ap%25south', 'date=2026-01-02')

    # Clean file: every physical region matches the plus-encoded path.
    clean_path = os.path.join(plus_dir, 'clean.parquet')
    _write_row_groups(clean_path, [
        _frame([
            (PLUS_REGION, '2026-01-01', 'g1', 1.0),
            (PLUS_REGION, '2026-01-01', 'g1', 2.0),
        ]),
        _frame([
            (PLUS_REGION, '2026-01-01', 'g2', 3.0),
            (PLUS_REGION, '2026-01-01', 'g2', 4.0),
        ]),
    ])

    # The physical region column is entirely absent: it must be filled from
    # the path.  One row group deliberately has no measurement statistics.
    fill_path = os.path.join(plus_dir, 'fill_region.parquet')
    os.makedirs(plus_dir, exist_ok=True)
    fill_frames = [
        pd.DataFrame([
            ('2026-01-01', 'g1', 5.0),
            ('2026-01-01', 'g1', 6.0),
        ], columns=['date', 'group_id', 'measurement']),
        pd.DataFrame([
            ('2026-01-01', 'g2', 7.0),
            ('2026-01-01', 'g2', 8.0),
        ], columns=['date', 'group_id', 'measurement']),
        pd.DataFrame([
            ('2026-01-01', 'g3', 9.0),
            ('2026-01-01', 'g3', 10.0),
        ], columns=['date', 'group_id', 'measurement']),
    ]
    table = pa.Table.from_pandas(pd.concat(fill_frames, ignore_index=True))
    with pq.ParquetWriter(fill_path, table.schema) as writer:
        writer.write_table(table.slice(0, 2))
        writer.write_table(table.slice(2, 2))
        writer.write_table(table.slice(4, 2))
    strip_column_statistics(fill_path, 2, 'measurement')

    # Physical region column present and every value matches the path, but the
    # late row group has its region statistics stripped.  The reader must
    # decode that group instead of assuming equality from statistics.
    nostats_path = os.path.join(plus_dir, 'nostats.parquet')
    _write_row_groups(nostats_path, [
        _frame([
            (PLUS_REGION, '2026-01-01', 'g1', 40.0),
            (PLUS_REGION, '2026-01-01', 'g1', 41.0),
        ]),
        _frame([
            (PLUS_REGION, '2026-01-01', 'g2', 42.0),
            (PLUS_REGION, '2026-01-01', 'g2', 43.0),
        ]),
        _frame([
            (PLUS_REGION, '2026-01-01', 'g3', 44.0),
            (PLUS_REGION, '2026-01-01', 'g3', 45.0),
        ]),
    ])
    strip_column_statistics(nostats_path, 2, 'region')

    # Unfinished batch: rows reconcile fine but the file must stay withheld.
    withheld_path = os.path.join(plus_dir, '_incomplete', 'withheld.parquet')
    _write_row_groups(withheld_path, [
        _frame([
            (PLUS_REGION, '2026-01-01', 'g1', 11.0),
            (PLUS_REGION, '2026-01-01', 'g1', 12.0),
        ]),
        _frame([
            (PLUS_REGION, '2026-01-01', 'g2', 13.0),
            (PLUS_REGION, '2026-01-01', 'g2', 14.0),
        ]),
    ])

    # First two groups match exactly.  The late group's sole conflict row is
    # dated 2026-01-03, so the report date filter removes it; its region
    # statistics are also stripped, so equality cannot be assumed from stats.
    conflict_path = os.path.join(west_dir, 'conflict.parquet')
    _write_row_groups(conflict_path, [
        _frame([
            (ENCODED_WEST_REGION, '2026-01-02', 'g1', 20.0),
            (ENCODED_WEST_REGION, '2026-01-02', 'g1', 21.0),
        ]),
        _frame([
            (ENCODED_WEST_REGION, '2026-01-02', 'g2', 22.0),
            (ENCODED_WEST_REGION, '2026-01-02', 'g2', 23.0),
        ]),
        _frame([
            ('ap-south-1', '2026-01-03', 'g3', 24.0),
            (ENCODED_WEST_REGION, '2026-01-03', 'g3', 25.0),
        ]),
    ])
    strip_column_statistics(conflict_path, 2, 'region')

    # Late group carries a physical null region, which must be identified
    # separately from a missing physical column.
    null_path = os.path.join(south_dir, 'nulls.parquet')
    _write_row_groups(null_path, [
        _frame([
            (ENCODED_SOUTH_REGION, '2026-01-02', 'g1', 30.0),
            (ENCODED_SOUTH_REGION, '2026-01-02', 'g1', 31.0),
        ]),
        _frame([
            (None, '2026-01-03', 'g2', 32.0),
            (ENCODED_SOUTH_REGION, '2026-01-03', 'g2', 33.0),
        ]),
    ])

    # A region-looking directory outside the business root must never be read.
    outside_dir = os.path.join(base_dir, 'outside', 'region=zz-ghost',
                               'date=2026-01-02')
    decoy_path = os.path.join(outside_dir, 'decoy.parquet')
    _write_row_groups(decoy_path, [
        _frame([('zz-ghost', '2026-01-02', 'g9', 99.0)]),
    ])

    return {
        'root': root,
        'outside_root': os.path.join(base_dir, 'outside'),
        'clean.parquet': clean_path,
        'fill_region.parquet': fill_path,
        'nostats.parquet': nostats_path,
        'withheld.parquet': withheld_path,
        'conflict.parquet': conflict_path,
        'nulls.parquet': null_path,
        'decoy.parquet': decoy_path,
    }


@pytest.fixture
def partitioned_archive(tmp_path):
    return build_partitioned_archive(str(tmp_path))


@pytest.fixture
def partitioned_archive_s3(partitioned_archive, aws_session, aws_s3_bucket):
    client = aws_session.resource('s3').meta.client
    root = partitioned_archive['root']
    for current_dir, _, names in os.walk(root):
        for name in names:
            local = os.path.join(current_dir, name)
            key = os.path.relpath(local, os.path.dirname(root)).replace(
                os.sep, '/')
            client.upload_file(local, aws_s3_bucket, key)
    s3_root = f's3://{aws_s3_bucket}/archive'
    return {
        **partitioned_archive,
        's3_root': s3_root,
        'bucket': aws_s3_bucket,
    }
