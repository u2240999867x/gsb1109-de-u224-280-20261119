import os
import tempfile

import numpy as np
import pytest

from ..fitslib import (
    FITS,
    read_header,
    write,
    RICE_1,
    GZIP_1,
    PLIO_1,
    HCOMPRESS_1,
)


BSCALE = 0.25
BZERO = -32.5
BLANK = -9999


def _make_image(ny=23, nx=37):
    data = np.arange(ny * nx, dtype='i2').reshape(ny, nx) * 7 - 500
    data[0, 0] = BLANK
    data[5, 7] = BLANK
    data[-1, -1] = BLANK
    data[-2, 0] = BLANK
    return data


def _header():
    return {
        'BSCALE': BSCALE,
        'BZERO': BZERO,
        'BUNIT': 'K',
        'BLANK': BLANK,
        'OBJECT': 'deep field',
        'EXPTIME': 120.0,
        'CUSTOM': 'keepme',
    }


def _write_source(path, data, tile_dims=(6, 10)):
    write(
        path,
        data,
        header=_header(),
        compress='RICE',
        tile_dims=tile_dims,
        clobber=True,
    )


def _read_raw(path, ext):
    with FITS(path) as fits:
        fits[ext].ignore_scaling = True
        return fits[ext].read()


def _read_physical(path, ext):
    with FITS(path) as fits:
        return fits[ext].read()


@pytest.mark.parametrize(
    'old_tile_dims,new_tile_dims',
    [
        ((6, 10), (9, 13)),
        ((7, 11), (10, 6)),
        ((6, 10), (31, 7)),
        ((6, 10), (24, 40)),
    ],
)
def test_resample_tiles_preserves_codes_and_scaling(
    old_tile_dims, new_tile_dims
):
    data = _make_image()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.fits')
        _write_source(path, data, tile_dims=old_tile_dims)

        with FITS(path, 'rw') as fits:
            dst = fits.resample_tiles(
                1, 2, new_tile_dims, compress='RICE', buffer_factor=1
            )
            hdr = dst.read_header()
            assert hdr['ZTILE1'] == new_tile_dims[1]
            assert hdr['ZTILE2'] == new_tile_dims[0]
            assert hdr['ZNAXIS1'] == data.shape[1]
            assert hdr['ZNAXIS2'] == data.shape[0]
            assert hdr['ZBITPIX'] == 16
            assert hdr['ZCMPTYPE'] in ('RICE_1', 'RICE_ONE')

            for key, value in _header().items():
                assert hdr[key] == value

        raw_old = _read_raw(path, 1)
        raw_new = _read_raw(path, 2)
        phys_old = _read_physical(path, 1)
        phys_new = _read_physical(path, 2)

        np.testing.assert_array_equal(raw_old, data)
        np.testing.assert_array_equal(raw_new, data)
        np.testing.assert_array_equal(raw_new, raw_old)
        assert raw_new.dtype == np.int16

        expected_phys = data.astype('f8') * BSCALE + BZERO
        np.testing.assert_array_equal(phys_old, expected_phys)
        np.testing.assert_array_equal(phys_new, expected_phys)
        np.testing.assert_array_equal(phys_new, phys_old)
        assert phys_new.dtype == np.float32


def test_resample_tiles_half_tiles_on_both_edges():
    # 23 = 2*9 + 5 and 37 = 2*13 + 11: both final tiles are partial, and the
    # old tile sizes (6, 10) also divide neither axis.
    data = _make_image(ny=23, nx=37)
    old_tile = (6, 10)
    new_tile = (9, 13)
    assert data.shape[0] % old_tile[0] and data.shape[0] % old_tile[1]
    assert data.shape[1] % old_tile[0] and data.shape[1] % old_tile[1]
    assert data.shape[0] % new_tile[0] and data.shape[0] % new_tile[1]
    assert data.shape[1] % new_tile[0] and data.shape[1] % new_tile[1]

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.fits')
        _write_source(path, data, tile_dims=old_tile)
        with FITS(path, 'rw') as fits:
            dst = fits.resample_tiles(1, 2, new_tile, compress='RICE')
            hdr = dst.read_header()
            assert (hdr['ZTILE1'], hdr['ZTILE2']) == (new_tile[1], new_tile[0])
        np.testing.assert_array_equal(_read_raw(path, 2), data)
        np.testing.assert_array_equal(
            _read_physical(path, 2), data.astype('f8') * BSCALE + BZERO
        )


def test_resample_tiles_new_layout_does_not_change_pixels():
    data = _make_image()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.fits')
        _write_source(path, data)

        layouts = [(9, 13), (6, 10), (10, 6), (31, 7)]
        with FITS(path, 'rw') as fits:
            for ext, tile_dims in enumerate(layouts, start=2):
                dst = fits.resample_tiles(
                    1, ext, tile_dims, compress='RICE', buffer_factor=2
                )
                hdr = dst.read_header()
                assert hdr['ZTILE1'] == tile_dims[1]
                assert hdr['ZTILE2'] == tile_dims[0]

        assert _read_raw(path, 1).dtype == np.int16
        baseline_raw = _read_raw(path, 1)
        baseline_phys = _read_physical(path, 1)
        for ext in range(2, len(layouts) + 2):
            np.testing.assert_array_equal(_read_raw(path, ext), baseline_raw)
            np.testing.assert_array_equal(
                _read_physical(path, ext), baseline_phys
            )


def test_resample_tiles_checks_tile_boundaries_and_missing_codes():
    data = _make_image()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.fits')
        _write_source(path, data, tile_dims=(6, 10))
        with FITS(path, 'rw') as fits:
            fits.resample_tiles(
                1, 2, (9, 13), compress='RICE', buffer_factor=1
            )

        raw = _read_raw(path, 2)
        phys = _read_physical(path, 2)

        centres = [(0, 0), (0, 12), (0, 13), (5, 7), (6, 10), (22, 36)]
        for y, x in centres:
            assert raw[y, x] == data[y, x]
            assert phys[y, x] == pytest.approx(
                data[y, x] * BSCALE + BZERO, rel=0, abs=0
            )

        missing = np.argwhere(data == BLANK)
        assert len(missing) > 0
        for y, x in missing:
            assert raw[y, x] == BLANK
            assert phys[y, x] == pytest.approx(
                BLANK * BSCALE + BZERO, rel=0, abs=0
            )

        assert np.array_equal(raw, data)
        assert raw.shape == data.shape


@pytest.mark.parametrize('compress', ['RICE', 'GZIP', 'GZIP_2'])
def test_resample_tiles_supported_lossless_targets(compress):
    data = _make_image()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.fits')
        _write_source(path, data)
        with FITS(path, 'rw') as fits:
            dst = fits.resample_tiles(1, 2, (9, 13), compress=compress)
            assert dst.is_compressed()
        np.testing.assert_array_equal(_read_raw(path, 2), data)
        np.testing.assert_array_equal(
            _read_physical(path, 2), data.astype('f8') * BSCALE + BZERO
        )


@pytest.mark.parametrize(
    'compress,dtype',
    [
        (PLIO_1, 'u2'),
        (HCOMPRESS_1, 'u2'),
        (RICE_1, 'i8'),
        (GZIP_1, 'i8'),
        (RICE_1, 'f4'),
    ],
)
def test_resample_tiles_rejects_unsupported_targets(compress, dtype):
    from ..fitslib import check_lossless_target

    with pytest.raises(ValueError):
        check_lossless_target(compress, dtype)


def test_resample_tiles_float_source_uses_lossless_gzip():
    data = np.arange(23 * 37, dtype='f4').reshape(23, 37) * 0.5 - 3.0
    data[3, 4] = np.nan
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.fits')
        write(
            path,
            data,
            header={'BUNIT': 'ADU'},
            compress='GZIP',
            qlevel=0,
            tile_dims=(6, 10),
            clobber=True,
        )
        with FITS(path, 'rw') as fits:
            dst = fits.resample_tiles(1, 2, (9, 13), compress='GZIP')
            hdr = dst.read_header()
            assert hdr['ZTILE1'] == 13
            assert hdr['ZTILE2'] == 9
            assert hdr['ZBITPIX'] == -32
            assert hdr['ZQUANTIZ'] == 'NONE'
        np.testing.assert_array_equal(_read_physical(path, 2), data)


def test_resample_tiles_requires_compressed_destination():
    data = _make_image()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.fits')
        _write_source(path, data)
        with FITS(path, 'rw') as fits:
            with pytest.raises(ValueError):
                fits.resample_tiles(1, 2, (9, 13), compress=0)
            with pytest.raises(ValueError):
                fits.resample_tiles(1, 2, (9, 13), compress=None)
            with pytest.raises(ValueError):
                fits.resample_tiles(1, 2, (9, 13), buffer_factor=0)


def test_resample_tiles_destination_guard():
    data = _make_image()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.fits')
        _write_source(path, data)
        with FITS(path, 'rw') as fits:
            with pytest.raises(ValueError):
                fits.resample_tiles(1, 1, (9, 13), compress='RICE')
            with pytest.raises(ValueError):
                fits.resample_tiles(1, 5, (9, 13), compress='RICE')
            with pytest.raises(ValueError):
                fits.resample_tiles(1, 2, (0, 13), compress='RICE')
            with pytest.raises(ValueError):
                fits.resample_tiles(1, 2, (9, 13), buffer_factor=0)


def test_resample_tiles_clobber_replaces_empty_destination():
    data = _make_image()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.fits')
        _write_source(path, data)
        with FITS(path, 'rw') as fits:
            fits.create_image_hdu(dims=data.shape, dtype=data.dtype)
            fits.resample_tiles(1, 2, (9, 13), compress='RICE', clobber=True)
            assert len(fits) == 3
        np.testing.assert_array_equal(_read_raw(path, 2), data)


def test_resample_tiles_uses_source_decoded_baseline_for_quantized_source():
    rng = np.random.RandomState(seed=10)
    data = (rng.normal(size=(31, 47)) * 1000).astype('f4')

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.fits')
        write(
            path,
            data,
            header={'BUNIT': 'ADU'},
            compress='RICE',
            qlevel=16,
            qmethod='SUBTRACTIVE_DITHER_1',
            dither_seed=42,
            tile_dims=(8, 9),
            clobber=True,
        )
        baseline = _read_physical(path, 1)

        with FITS(path, 'rw') as fits:
            fits.resample_tiles(1, 2, (11, 13), compress='GZIP')

        retiled = _read_physical(path, 2)
        np.testing.assert_array_equal(retiled, baseline)

        raw = _read_raw(path, 2)
        assert raw.dtype == np.float32
        np.testing.assert_array_equal(raw, baseline)


def test_resample_tiles_aliases():
    data = _make_image()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.fits')
        _write_source(path, data)
        with FITS(path, 'rw') as fits:
            fits.copy_image_tiles(1, 2, (9, 13), compress='RICE')
            fits.retile_image(1, 3, (6, 10), compress='RICE')
        np.testing.assert_array_equal(_read_raw(path, 2), data)
        np.testing.assert_array_equal(_read_raw(path, 3), data)


def test_resample_tiles_does_not_keep_old_layout_keywords():
    data = _make_image()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.fits')
        _write_source(path, data, tile_dims=(6, 10))
        old = read_header(path, ext=1)
        assert old['ZTILE1'] == 10
        assert old['ZTILE2'] == 6

        with FITS(path, 'rw') as fits:
            fits.resample_tiles(1, 2, (9, 13), compress='RICE')

        new = read_header(path, ext=2)
        assert new['ZTILE1'] == 13
        assert new['ZTILE2'] == 9
        assert new['ZCMPTYPE'] in ('RICE_1', 'RICE_ONE')


def test_resample_tiles_reads_small_blocks(monkeypatch):
    data = _make_image(ny=97, nx=103)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.fits')
        _write_source(path, data, tile_dims=(11, 13))

        blocks = []
        orig = type(FITS(path, 'r')[1])._read_image_slice

        def recording_read(self, arg):
            if isinstance(arg, tuple):
                shape = []
                for slc, dim in zip(arg, self.get_dims()):
                    start = 0 if slc.start is None else slc.start
                    stop = dim if slc.stop is None else slc.stop
                    shape.append(stop - start)
                blocks.append(tuple(shape))
            return orig(self, arg)

        monkeypatch.setattr(
            type(FITS(path, 'r')[1]), '_read_image_slice', recording_read
        )

        with FITS(path, 'rw') as fits:
            fits.resample_tiles(1, 2, (17, 19), compress='RICE')

        assert blocks
        assert max(b[0] for b in blocks) <= 17
        assert max(b[1] for b in blocks) <= 19
        np.testing.assert_array_equal(_read_raw(path, 2), data)


def test_resample_tiles_3d_keeps_axis_order():
    data = np.arange(5 * 7 * 9, dtype='i2').reshape(5, 7, 9) - 100
    data[0, 0, 0] = BLANK
    data[-1, -1, -1] = BLANK
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'test.fits')
        write(
            path,
            data,
            header={'BSCALE': 2.0, 'BZERO': 1.0, 'BLANK': BLANK},
            compress='RICE',
            tile_dims=(2, 3, 4),
            clobber=True,
        )
        with FITS(path, 'rw') as fits:
            fits.resample_tiles(1, 2, (3, 2, 5), compress='RICE')
        raw = _read_raw(path, 2)
        phys = _read_physical(path, 2)
        np.testing.assert_array_equal(raw, data)
        np.testing.assert_array_equal(phys, data.astype('f8') * 2.0 + 1.0)
        assert raw.shape == data.shape
