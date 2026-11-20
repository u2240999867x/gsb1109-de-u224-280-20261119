import os
import tempfile

import numpy as np

import fitsio
from fitsio import FITS
from fitsio.hdu.append import (
    AppendIssue,
    AppendReport,
    AppendValidationError,
    describe_target_columns,
)


TARGET_DTYPE = np.dtype(
    [
        ('TEMP', 'f4'),
        ('WINDOW', 'f4', (2, 3)),
        ('FLAGS', 'i2', (4,)),
    ]
)


def make_archive(path, nrows=2):
    data = np.zeros(nrows, dtype=TARGET_DTYPE)
    data['TEMP'] = np.arange(nrows, dtype='f4')
    data['WINDOW'] = np.arange(nrows * 6, dtype='f4').reshape(
        nrows, 2, 3
    )
    data['FLAGS'] = np.arange(nrows * 4, dtype='i2').reshape(nrows, 4)
    with FITS(path, 'rw', clobber=True) as fits:
        fits.write_table(data, extname='OBS')
    return data


def source_batch():
    source = np.zeros(
        3,
        dtype=[
            ('temp_in', 'O'),
            ('window_in', 'f4', (3, 2)),
            ('flags_in', 'O'),
        ],
    )
    source['temp_in'] = [300.0, -999.0, None]
    source['window_in'] = np.arange(18, dtype='f4').reshape(3, 3, 2)
    source['flags_in'] = [
        np.array([1, 2], dtype='i2'),
        np.array([3, 4, 5], dtype='i2'),
        np.array([6], dtype='i2'),
    ]
    return source


def test_target_layout_uses_tdim_shape():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'archive.fits')
        make_archive(path)
        with FITS(path, 'rw') as fits:
            targets = {
                target.name: target
                for target in describe_target_columns(fits[1])
            }
            assert targets['TEMP'].shape == ()
            assert targets['WINDOW'].shape == (2, 3)
            assert targets['FLAGS'].shape == (4,)


def test_checked_append_axis_order_and_two_fill_policies():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'archive.fits')
        old = make_archive(path)
        source = source_batch()

        with FITS(path, 'rw') as fits:
            hdu = fits[1]
            report = hdu.append(
                source,
                mapping={
                    'TEMP': 'temp_in',
                    'WINDOW': {'source': 'window_in', 'axes': (1, 0)},
                    'FLAGS': 'flags_in',
                },
                missing={'TEMP': -999.0},
                missing_fill={'TEMP': -1.0},
                fill={'TEMP': -2.0, 'FLAGS': 0},
            )

            assert isinstance(report, AppendReport)
            assert report.ok
            assert report.written
            assert report.rows_written == 3
            assert hdu.get_nrows() == old.size + 3

            assert report.fill_counts['TEMP'] == {
                'missing': 1,
                'absent': 1,
                'absent_column': 0,
                'cells': 2,
            }
            assert report.missing_counts['TEMP'] == 1
            assert report.absent_counts['TEMP'] == 1
            assert report.rows_for('TEMP', kind='missing') == [1]
            assert report.rows_for('TEMP', kind='absent') == [2]
            assert report.values_for('TEMP', kind='missing') == [-1.0]
            assert report.values_for('TEMP', kind='absent') == [-2.0]

            data = hdu.read()
            assert data['TEMP'].tolist() == [
                0.0,
                1.0,
                300.0,
                -1.0,
                -2.0,
            ]

            # The source axis order is (3, 2) and the target is (2, 3).
            expected = np.transpose(source['window_in'][0], (1, 0))
            assert np.array_equal(data['WINDOW'][-3], expected)
            assert np.array_equal(
                data['WINDOW'][-2],
                np.transpose(source['window_in'][1], (1, 0)),
            )

            # Short fixed-length flag arrays are padded; the values land in
            # the same positions the caller supplied.
            assert data['FLAGS'][-3].tolist() == [1, 2, 0, 0]
            assert data['FLAGS'][-2].tolist() == [3, 4, 5, 0]
            assert data['FLAGS'][-1].tolist() == [6, 0, 0, 0]

            # Existing rows are untouched and the column layout is stable.
            assert np.array_equal(data['TEMP'][:2], old['TEMP'])
            assert np.array_equal(data['WINDOW'][:2], old['WINDOW'])
            assert np.array_equal(data['FLAGS'][:2], old['FLAGS'])
            assert hdu.get_colnames() == ['TEMP', 'WINDOW', 'FLAGS']


def test_checked_append_rejects_whole_batch_atomically():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'archive.fits')
        old = make_archive(path)
        source = source_batch()

        with FITS(path, 'rw') as fits:
            hdu = fits[1]

            # Last row only: the flag array is too long.
            too_long = source.copy()
            too_long['flags_in'][-1] = np.array([1, 2, 3, 4, 5])
            with np.testing.assert_raises(AppendValidationError) as ctx:
                hdu.append(
                    too_long,
                    mapping={
                        'TEMP': 'temp_in',
                        'WINDOW': {
                            'source': 'window_in',
                            'axes': (1, 0),
                        },
                        'FLAGS': 'flags_in',
                    },
                    missing={'TEMP': -999.0},
                    fill={'TEMP': np.nan, 'FLAGS': 0},
                )
            issues = ctx.exception.issues
            assert len(issues) == 1
            assert issues[0].row == 2
            assert issues[0].row_number == 3
            assert issues[0].column == 'FLAGS'
            assert issues[0].condition == 'length'
            assert 'length' in issues[0].message

            assert hdu.get_nrows() == old.size
            after = hdu.read()
            assert np.array_equal(after['TEMP'], old['TEMP'])
            assert np.array_equal(after['WINDOW'], old['WINDOW'])
            assert np.array_equal(after['FLAGS'], old['FLAGS'])

            # Last row only: a value is outside the target int16 range.
            out_of_range = source.copy()
            out_of_range['flags_in'][-1] = np.array(
                [1, 2, 3, 40000], dtype='i4'
            )
            with np.testing.assert_raises(AppendValidationError) as ctx:
                hdu.append(
                    out_of_range,
                    mapping={
                        'TEMP': 'temp_in',
                        'WINDOW': {
                            'source': 'window_in',
                            'axes': (1, 0),
                        },
                        'FLAGS': 'flags_in',
                    },
                    missing={'TEMP': -999.0},
                    fill={'TEMP': np.nan, 'FLAGS': 0},
                )
            issues = ctx.exception.issues
            assert len(issues) == 1
            assert issues[0].row == 2
            assert issues[0].row_number == 3
            assert issues[0].column == 'FLAGS'
            assert issues[0].condition == 'range'
            assert issues[0].value == 40000

            assert hdu.get_nrows() == old.size


def test_range_checked_before_narrowing():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'archive.fits')
        data = np.zeros(1, dtype=[('FLAG', 'i2')])
        with FITS(path, 'rw', clobber=True) as fits:
            fits.write_table(data)

        source = np.zeros(1, dtype=[('flag_in', 'f8')])
        source['flag_in'] = [1.5]
        with FITS(path, 'rw') as fits:
            with np.testing.assert_raises(AppendValidationError) as ctx:
                fits[1].append(
                    source,
                    mapping={'FLAG': 'flag_in'},
                )
            assert ctx.exception.issues[0].condition == 'type'
            assert fits[1].get_nrows() == 1

        source = np.zeros(1, dtype=[('flag_in', 'i8')])
        source['flag_in'] = [70000]
        with FITS(path, 'rw') as fits:
            with np.testing.assert_raises(AppendValidationError) as ctx:
                fits[1].append(source, mapping={'FLAG': 'flag_in'})
            assert ctx.exception.issues[0].condition == 'range'
            assert fits[1].get_nrows() == 1


def test_same_total_size_is_not_enough_without_axes():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'archive.fits')
        make_archive(path)
        source = np.zeros(
            1,
            dtype=[
                ('temp_in', 'f4'),
                ('window_in', 'f4', (3, 2)),
                ('flags_in', 'i2', (4,)),
            ],
        )
        with FITS(path, 'rw') as fits:
            with np.testing.assert_raises(AppendValidationError) as ctx:
                fits[1].append(
                    source,
                    mapping={
                        'TEMP': 'temp_in',
                        'WINDOW': 'window_in',
                        'FLAGS': 'flags_in',
                    },
                )
            issue = ctx.exception.issues[0]
            assert issue.column == 'WINDOW'
            assert issue.condition == 'shape'
            assert 'total number of elements is the same' in issue.message
            assert fits[1].get_nrows() == 2


def test_variable_length_flag_column_in_target():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'archive.fits')
        target = np.zeros(2, dtype=[('TEMP', 'f4'), ('FLAGS', 'O')])
        target['TEMP'] = [1.0, 2.0]
        target['FLAGS'] = [
            np.array([1, 2, 3], dtype='i2'),
            np.array([4, 5, 6], dtype='i2'),
        ]
        with FITS(path, 'rw', clobber=True) as fits:
            fits.write_table(target)

        source = np.zeros(2, dtype=[('t', 'f4'), ('flags', 'O')])
        source['t'] = [3.0, 4.0]
        source['flags'] = [
            np.array([7, 8], dtype='i2'),
            np.array([9, 10, 11], dtype='i2'),
        ]
        with FITS(path, 'rw') as fits:
            report = fits[1].append(
                source,
                mapping={'TEMP': 't', 'FLAGS': 'flags'},
            )
            assert report.ok
            data = fits[1].read(vstorage='object')
            assert data['FLAGS'][-2].tolist() == [7, 8]
            assert data['FLAGS'][-1].tolist() == [9, 10, 11]


def test_validate_append_writes_nothing():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'archive.fits')
        old = make_archive(path)
        source = source_batch()
        with FITS(path, 'rw') as fits:
            report = fits[1].validate_append(
                source,
                mapping={
                    'TEMP': 'temp_in',
                    'WINDOW': {
                        'source': 'window_in',
                        'axes': (1, 0),
                    },
                    'FLAGS': 'flags_in',
                },
                missing={'TEMP': -999.0},
                fill={'TEMP': np.nan, 'FLAGS': 0},
            )
            assert report.ok
            assert not report.written
            assert fits[1].get_nrows() == old.size


def test_plain_append_still_uses_old_behaviour():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'archive.fits')
        data = np.zeros(2, dtype=[('X', 'f4'), ('Y', 'i4')])
        with FITS(path, 'rw', clobber=True) as fits:
            fits.write_table(data)

        more = np.zeros(1, dtype=[('X', 'f4')])
        more['X'] = [3.5]
        with FITS(path, 'rw') as fits:
            assert fits[1].append(more) is None
            result = fits[1].read()
            assert result['X'].tolist() == [0.0, 0.0, 3.5]
            assert result['Y'].tolist() == [0, 0, 0]


def test_append_issue_and_report_helpers():
    issue = AppendIssue(
        "bad value",
        row=4,
        column='X',
        condition='range',
        value=10,
    )
    assert issue['row_number'] == 5
    report = AppendReport(nrows=5)
    report.add_issue(issue)
    assert not report
    assert len(report) == 1
    assert report.as_dict()['issues'][0]['condition'] == 'range'
    with np.testing.assert_raises(AppendValidationError):
        report.raise_if_errors()


def test_two_sources_with_four_dimensional_axis_mapping():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'archive.fits')
        target = np.zeros(1, dtype=[('CUBE', 'f4', (2, 3, 4))])
        with FITS(path, 'rw', clobber=True) as fits:
            fits.write_table(target)

        source = np.zeros(
            2,
            dtype=[('cube_in', 'f4', (4, 2, 3))],
        )
        values = np.arange(48, dtype='f4').reshape(2, 4, 2, 3)
        source['cube_in'] = values
        with FITS(path, 'rw') as fits:
            report = fits[1].append(
                source,
                mapping={
                    'CUBE': {'source': 'cube_in', 'axes': (1, 2, 0)}
                },
            )
            assert report.ok
            data = fits[1].read()
            expected = np.transpose(values[0], (1, 2, 0))
            assert np.array_equal(data['CUBE'][-2], expected)
            assert np.array_equal(
                data['CUBE'][-1],
                np.transpose(values[1], (1, 2, 0)),
            )


def test_same_source_mapped_to_two_targets():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'archive.fits')
        target = np.zeros(1, dtype=[('T1', 'f4'), ('T2', 'f4')])
        with FITS(path, 'rw', clobber=True) as fits:
            fits.write_table(target)

        source = np.zeros(1, dtype=[('raw', 'f4')])
        source['raw'] = [12.5]
        with FITS(path, 'rw') as fits:
            report = fits[1].append(
                source,
                mapping={'T1': 'raw', 'T2': 'raw'},
                strict_mapping=True,
            )
            assert report.ok
            data = fits[1].read()
            assert data['T1'][-1] == 12.5
            assert data['T2'][-1] == 12.5


def test_strict_mapping_rejects_unknown_input():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'archive.fits')
        make_archive(path)
        source = np.zeros(
            1,
            dtype=[
                ('temp_in', 'f4'),
                ('window_in', 'f4', (2, 3)),
                ('flags_in', 'i2', (4,)),
                ('extra', 'f4'),
            ],
        )
        with FITS(path, 'rw') as fits:
            report = fits[1].validate_append(
                source,
                mapping={
                    'TEMP': 'temp_in',
                    'WINDOW': 'window_in',
                    'FLAGS': 'flags_in',
                },
                strict_mapping=True,
            )
            assert not report
            assert report.ignored_sources == ['extra']
            assert report.issues[0].condition == 'mapping'


def test_axis_declaration_and_invalid_permutation():
    # A declared permutation is applied before shape checks, and an invalid
    # permutation is reported against the column.
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'archive.fits')
        target = np.zeros(1, dtype=[('WINDOW', 'f4', (2, 3))])
        with FITS(path, 'rw', clobber=True) as fits:
            fits.write_table(target)

        values = np.arange(6, dtype='f4').reshape(1, 3, 2)
        source = np.zeros(1, dtype=[('w', 'f4', (3, 2))])
        source['w'] = values
        with FITS(path, 'rw') as fits:
            report = fits[1].append(
                source,
                mapping={'WINDOW': {'source': 'w', 'axes': (1, 0)}},
            )
            assert report.ok
            assert np.array_equal(
                fits[1].read()['WINDOW'][-1],
                values[0].T,
            )

        with FITS(path, 'rw') as fits:
            with np.testing.assert_raises(AppendValidationError) as ctx:
                fits[1].append(
                    source,
                    mapping={'WINDOW': {'source': 'w', 'axes': (1, 2)}},
                )
            assert ctx.exception.issues[0].condition == 'axis_order'
