"""
Tests for combined (batched) row reads across multiple table HDUs.

The scenario mirrors a telescope archive: two same-named table extensions hold
spectra with different on-disk layouts, one with a variable length column and
one with a fixed length column plus a bit flag field, with different legal row
ranges and data types.
"""

import os
import tempfile
import importlib.resources

import numpy as np
import pytest

from ..fitslib import FITS
from ..hdu.table import TableHDU
from ..hdu import CompressedTableHDU
from .. import RowBatchResult


VAR_NROWS = 3
FIXED_NROWS = 5
SPEC_LEN = 4


def _make_archive(fname):
    """
    Build a file with two same-named table extensions.

    ext 1: variable length ``spec`` column (object storage)
    ext 2: fixed length ``spec`` vector plus a bit flag field
    """
    var = np.zeros(VAR_NROWS, dtype=[('id', 'i4'), ('spec', 'O')])
    var['id'] = [10, 11, 12]
    var['spec'][0] = np.array([1.0, 2.0, 3.0])
    var['spec'][1] = np.array([4.0, 5.0])
    var['spec'][2] = np.array([6.0])

    fixed = np.zeros(
        FIXED_NROWS,
        dtype=[('id', 'i4'), ('spec', 'f8', SPEC_LEN), ('flags', '?', 2)],
    )
    fixed['id'] = np.arange(100, 100 + FIXED_NROWS)
    fixed['spec'] = np.arange(FIXED_NROWS * SPEC_LEN).reshape(
        FIXED_NROWS, SPEC_LEN
    )
    fixed['flags'] = np.array(
        [[1, 0], [0, 1], [1, 1], [0, 0], [1, 0]], dtype='?'
    )

    with FITS(fname, 'rw') as fits:
        fits.write(var, extname='spectra')
        fits.write(fixed, extname='spectra', write_bitcols=True)


@pytest.fixture()
def archive():
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = os.path.join(tmpdir, 'archive.fits')
        _make_archive(fname)
        with FITS(fname) as fits:
            yield fits


def _requests():
    # interleaved across HDUs: duplicate source rows, an equal row number in
    # both HDUs, a row only legal in the longer extension, plus illegal rows
    return [
        (1, 0),   # variable length row 0
        (2, 0),   # fixed length row 0, same row number, different HDU
        (1, 0),   # same source row requested again
        (2, 4),   # only legal in the longer (fixed length) extension
        (1, 4),   # illegal: variable extension only has 3 rows
        (2, 9),   # illegal: fixed extension only has 5 rows
        (1, 2),
    ]


def test_batch_result_traceability_and_order(archive):
    results, errors = archive.read_rows_batch(_requests(), vstorage='object')

    assert len(results) == len(_requests())
    assert sorted(errors) == [4, 5]
    for position in errors:
        assert results[position] is None

    valid = [result for result in results if result is not None]
    assert [result.hdu for result in valid] == [1, 2, 1, 2, 1]
    assert [result.row for result in valid] == [0, 0, 0, 4, 2]
    assert all(isinstance(result, RowBatchResult) for result in valid)
    assert all(result.extname == 'spectra' for result in valid)
    # ext is kept as an alias of hdu
    assert all(result.ext == result.hdu for result in valid)


def test_batch_result_dtypes_match_their_tables(archive):
    results, errors = archive.read_rows_batch(_requests(), vstorage='object')

    var_result = results[0]
    fixed_result = results[1]

    # variable length column is an object field, fixed length is a vector
    assert var_result.dtype['spec'].kind == 'O'
    assert fixed_result.dtype['spec'].shape == (SPEC_LEN,)
    # bit flag field is exposed as boolean
    assert fixed_result.dtype['flags'].base.kind == 'b'
    assert fixed_result.data['flags'].dtype == bool
    np.testing.assert_array_equal(
        fixed_result.data['flags'], np.array([1, 0], dtype='?')
    )

    assert var_result.dtype != fixed_result.dtype

    # data match the existing single-row entry points
    np.testing.assert_array_equal(
        results[3].data['spec'], archive[2].read_rows([4])[0]['spec']
    )
    np.testing.assert_array_equal(
        results[6].data['spec'],
        archive[1].read_rows([2], vstorage='object')[0]['spec'],
    )


def test_duplicate_source_rows_are_independent_object_storage(archive):
    results, errors = archive.read_rows_batch(
        [(1, 0), (1, 0)], vstorage='object'
    )
    assert not errors
    first, second = results

    assert first.data is not second.data
    assert first.data['spec'] is not second.data['spec']

    original = second.data['spec'].copy()
    first.data['spec'][0] = 999.0
    first.data['id'] = 777

    # post-processing the first result cannot change the second occurrence
    np.testing.assert_array_equal(second.data['spec'], original)
    assert second.data['id'] == 10


def test_duplicate_source_rows_are_independent_fixed_storage(archive):
    results, errors = archive.read_rows_batch([(2, 1), (2, 1)])
    assert not errors
    first, second = results

    assert first.data is not second.data
    original = second.data['spec'].copy()
    first.data['spec'][0] = -1.0
    first.data['flags'][0] = not second.data['flags'][0]

    np.testing.assert_array_equal(second.data['spec'], original)
    np.testing.assert_array_equal(
        second.data['flags'], np.array([0, 1], dtype='?')
    )


def test_equal_rows_from_different_hdus_are_not_merged(archive):
    results, errors = archive.read_rows_batch([(1, 0), (2, 0)])
    assert not errors

    var_result, fixed_result = results
    assert var_result is not fixed_result
    assert var_result.hdu != fixed_result.hdu
    assert var_result.row == fixed_result.row == 0
    assert var_result.dtype != fixed_result.dtype


def test_invalid_requests_only_error_at_their_positions(archive):
    results, errors = archive.read_rows_batch(_requests(), vstorage='object')

    assert set(errors) == {4, 5}
    for position in (4, 5):
        assert isinstance(errors[position], ValueError)
        assert results[position] is None

    # valid positions are still delivered in request order
    valid_positions = [i for i in range(len(results)) if i not in errors]
    assert valid_positions == [0, 1, 2, 3, 6]
    assert all(results[i] is not None for i in valid_positions)

    message = str(errors[4])
    assert 'HDU 1' in message and 'row 4' in message


def test_malformed_and_non_table_requests(archive):
    results, errors = archive.read_rows_batch(
        [
            (0, 0),     # primary array is not a table
            ('x', 1),   # non-integer HDU
            (1, 'a'),   # non-integer row
            (9, 0),     # HDU does not exist
            (1, -1),    # negative row
            (1, 0),     # valid
        ]
    )

    assert set(errors) == {0, 1, 2, 3, 4}
    assert results[5] is not None
    assert results[5].hdu == 1 and results[5].row == 0
    for position in range(5):
        assert results[position] is None


def test_underlying_reads_are_reused_not_repeated(archive, monkeypatch):
    calls = []
    original = TableHDU.read_rows

    def spy(self, rows, **kwargs):
        calls.append((self._ext, None if rows is None else list(rows)))
        return original(self, rows, **kwargs)

    monkeypatch.setattr(TableHDU, 'read_rows', spy)

    archive.read_rows_batch(
        [(1, 0), (2, 0), (1, 0), (2, 4), (1, 2), (2, 0)],
        vstorage='object',
    )

    # each HDU is read once; never a full-table (rows=None) read
    assert [ext for ext, rows in calls] == [1, 2]
    assert all(rows is not None for ext, rows in calls)

    seen = {}
    for ext, rows in calls:
        for row in rows:
            key = (ext, int(row))
            assert key not in seen
            seen[key] = True

    # a second batch only reads newly requested rows, cached rows are reused
    calls.clear()
    archive.read_rows_batch([(1, 1), (2, 2), (1, 0)], vstorage='object')
    newly_read = {
        (ext, int(row)) for ext, rows in calls for row in rows
    }
    assert newly_read == {(1, 1), (2, 2)}


def test_compressed_table_requests_are_rejected_per_position():
    ref = importlib.resources.files('fitsio') / 'test_images'
    with importlib.resources.as_file(
        ref / 'test_compressed_table.fits.fz'
    ) as fname:
        with FITS(str(fname)) as fits:
            assert isinstance(fits[2], CompressedTableHDU)

            results, errors = fits.read_rows_batch([(2, 0), (1, 0), (2, 5)])

            assert set(errors) == {0, 2}
            assert results[0] is None and results[2] is None
            # a readable table at another position is still delivered
            assert results[1] is not None
            assert results[1].hdu == 1


def test_ascii_table_rows_are_supported():
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = os.path.join(tmpdir, 'ascii.fits')
        data = np.zeros(4, dtype=[('i', 'i4'), ('x', 'f8')])
        data['i'] = np.arange(4)
        data['x'] = np.arange(4.0)

        with FITS(fname, 'rw') as fits:
            fits.write_table(data, table_type='ascii', extname='atab')
            fits.write_table(data, extname='btab')

        with FITS(fname) as fits:
            results, errors = fits.read_rows_batch(
                [(1, 3), (2, 1), (1, 3), (1, 9)]
            )

            assert set(errors) == {3}
            valid = [result for result in results if result is not None]
            assert [result.hdu for result in valid] == [1, 2, 1]
            assert valid[0].data['i'] == 3
            assert valid[1].data['x'] == 1.0

            # duplicate rows from the ascii table stay independent
            valid[0].data['i'] = -1
            assert valid[2].data['i'] == 3


def test_per_hdu_read_rows_batch(archive):
    records, dtype = archive[1].read_rows_batch(np.array([2, 0, 2]))

    assert records.size == 3
    assert list(records['id']) == [12, 10, 12]
    assert dtype == records.dtype
    # duplicate row yields independent records
    assert records[0] is not records[2]
    records[0]['id'] = -9
    assert records[2]['id'] == 12


def test_cache_invalidated_on_table_mutation():
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = os.path.join(tmpdir, 'archive.fits')
        _make_archive(fname)

        with FITS(fname, 'rw') as archive:
            archive.read_rows_batch([(1, 0)])
            hdu = archive[1]
            assert set(hdu._row_batch_cache[1]) == {0}

            extra = np.zeros(1, dtype=[('id', 'i4'), ('spec', 'O')])
            extra['id'][0] = 13
            extra['spec'][0] = np.array([7.0])
            hdu.append(extra)

            assert hdu._row_batch_cache is None
            results, errors = archive.read_rows_batch([(1, VAR_NROWS)])
            assert not errors
            assert results[0].row == VAR_NROWS
            assert results[0].data['id'] == 13

            hdu.delete_rows([VAR_NROWS])
            results, errors = archive.read_rows_batch([(1, VAR_NROWS)])
            assert set(errors) == {0}
            assert results[0] is None


def test_vstorage_switch_changes_dtype(archive):
    object_results, _ = archive.read_rows_batch(
        [(1, 0)], vstorage='object'
    )
    fixed_results, _ = archive.read_rows_batch([(1, 0)], vstorage='fixed')

    assert object_results[0].dtype['spec'].kind == 'O'
    assert fixed_results[0].dtype['spec'].kind == 'V'
    np.testing.assert_array_equal(
        object_results[0].data['spec'], [1.0, 2.0, 3.0]
    )
    np.testing.assert_array_equal(
        fixed_results[0].data['spec'][:3], [1.0, 2.0, 3.0]
    )


def test_empty_request_list(archive):
    results, errors = archive.read_rows_batch([])
    assert results == []
    assert errors == {}


def test_standard_read_rows_behavior_unchanged(archive):
    var_rows = archive[1].read_rows([2, 0], vstorage='object')
    assert list(var_rows['id']) == [12, 10]

    fixed_rows = archive[2].read_rows([4, 1])
    assert list(fixed_rows['id']) == [104, 101]
    np.testing.assert_array_equal(
        fixed_rows[0]['flags'], np.array([1, 0], dtype='?')
    )
