"""
Tests for traceable image sampling windows.

The scenarios use small images whose pixel values encode their source
coordinates, so any shift caused by requested but undeliverable grid
points shows up immediately.
"""

import os
import tempfile

import numpy as np
import pytest

from ..fitslib import FITS, RICE_1
from ..imagewindow import ImageWindow, NoImagePixelsError

DTYPES = ["i2", "i4", "f4", "f8"]
SHAPE = (6, 10)


def _source_data(shape=SHAPE, dtype="i4"):
    data = np.arange(np.prod(shape), dtype=dtype).reshape(shape)
    return data * 7 + 3


def _write_image(tmpdir, data, fname="window.fits", **compress_kwargs):
    path = os.path.join(tmpdir, fname)
    with FITS(path, "rw") as fits:
        fits.write_image(data, **compress_kwargs)
    return path


def _gather(window, data):
    """Reference gather: cell-by-cell indexing of the full source."""
    coords = window.valid_source_coords()
    return data[np.ix_(*coords)]


@pytest.mark.parametrize("dtype", DTYPES)
def test_window_read_matches_full_source(dtype):
    data = _source_data(dtype=dtype)
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = _write_image(tmpdir, data)
        with FITS(fname) as fits:
            hdu = fits[0]
            window = ImageWindow.from_hdu(
                hdu,
                (slice(7, None, -2), slice(0, 12, 1)),
            )

            result = window.read(hdu)

            assert result.shape == window.valid_shape
            np.testing.assert_array_equal(result.data, _gather(window, data))
            assert result.dtype == data.dtype
            np.testing.assert_array_equal(np.asarray(result), result.data)


def test_window_keeps_overshoot_grid_points():
    data = _source_data()
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = _write_image(tmpdir, data)
        with FITS(fname) as fits:
            hdu = fits[0]
            window = ImageWindow.from_hdu(
                hdu,
                (slice(7, None, -2), slice(0, 12, 1)),
            )

            y_axis, x_axis = window.axes

            # requested shape keeps the out-of-bounds grid points
            assert window.request_shape == (4, 12)
            assert window.valid_shape == (3, 10)
            np.testing.assert_array_equal(y_axis.indices, [7, 5, 3, 1])
            np.testing.assert_array_equal(y_axis.missing_indices, [7])
            np.testing.assert_array_equal(x_axis.missing_indices, [10, 11])

            result = window.read(hdu)
            # slot 0 is missing; valid local positions must not move
            np.testing.assert_array_equal(
                result.request_positions[0], [1, 2, 3]
            )
            np.testing.assert_array_equal(result.axis_coords[0], [5, 3, 1])
            np.testing.assert_array_equal(
                result.request_positions[1], np.arange(10)
            )


def test_local_subwindow_spans_overshoot_and_reverses():
    data = _source_data()
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = _write_image(tmpdir, data)
        with FITS(fname) as fits:
            hdu = fits[0]
            parent = ImageWindow.from_hdu(
                hdu,
                (slice(7, None, -2), slice(0, 12, 1)),
            )

            # local indices of the parent request, reversed, starting
            # beyond the source edge and ending at the last overlap
            child = parent.subwindow(slices=(slice(3, None, -1), slice(None)))

            np.testing.assert_array_equal(child.axes[0].indices, [1, 3, 5, 7])
            np.testing.assert_array_equal(
                child.axes[0].valid_indices, [1, 3, 5]
            )
            # the overshoot point stays at the trailing local slot
            np.testing.assert_array_equal(
                child.axes[0].valid_positions, [0, 1, 2]
            )

            result = child.read(hdu)
            np.testing.assert_array_equal(result.axis_coords[0], [1, 3, 5])
            np.testing.assert_array_equal(
                result.data,
                data[np.ix_([1, 3, 5], np.arange(10))],
            )


def test_source_limited_subwindow_between_samples_is_empty():
    data = _source_data()
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = _write_image(tmpdir, data)
        with FITS(fname) as fits:
            hdu = fits[0]
            parent = ImageWindow.from_hdu(
                hdu,
                (slice(7, None, -2), slice(0, 12, 1)),
            )

            # y == 4 lies geometrically inside the source but between
            # the parent samples y = 5 and y = 3
            child = parent.subwindow(source_limits=((4, 4), (0, 9)))
            result = child.read(hdu)

            assert result.is_empty
            assert result.data.size == 0
            assert result.target == ((4, 4), (0, 9))
            assert result.reason is not None
            assert "sampled grid point" in result.reason

            with pytest.raises(NoImagePixelsError) as excinfo:
                child.read(hdu, strict=True)
            assert excinfo.value.target == result.target
            assert "sampled grid point" in excinfo.value.reason

            # a range fully outside the source is distinguished from a
            # merely inter-sample gap
            outside = parent.subwindow(source_limits=((20, 25), (0, 9)))
            outside_result = outside.read(hdu)
            assert outside_result.is_empty
            assert outside_result.target == ((20, 25), (0, 9))
            assert "outside source" in outside_result.reason


def test_subwindow_accepts_none_axis():
    data = _source_data()
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = _write_image(tmpdir, data)
        with FITS(fname) as fits:
            hdu = fits[0]
            parent = ImageWindow.from_hdu(
                hdu,
                (slice(7, None, -2), slice(0, 12, 1)),
            )
            child = parent.subwindow(slices=(slice(1, 4), None))
            result = child.read(hdu)
            np.testing.assert_array_equal(result.axis_coords[1], np.arange(10))
            np.testing.assert_array_equal(result.data, _gather(child, data))


def test_source_limited_subwindow_keeps_sample_order():
    data = _source_data()
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = _write_image(tmpdir, data)
        with FITS(fname) as fits:
            hdu = fits[0]
            parent = ImageWindow.from_hdu(
                hdu,
                (slice(7, None, -2), slice(0, 12, 1)),
            )

            child = parent.subwindow(source_limits=((1, 5), (2, 8)))
            result = child.read(hdu)

            np.testing.assert_array_equal(result.axis_coords[0], [5, 3, 1])
            np.testing.assert_array_equal(
                result.axis_coords[1], np.arange(2, 9)
            )
            np.testing.assert_array_equal(
                result.data,
                data[np.ix_([5, 3, 1], np.arange(2, 9))],
            )


def test_chained_local_subwindows_track_source_coordinates():
    data = _source_data(shape=(8, 12))
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = _write_image(tmpdir, data)
        with FITS(fname) as fits:
            hdu = fits[0]
            parent = ImageWindow.from_hdu(
                hdu,
                (slice(9, None, -2), slice(0, 14, 2)),
            )
            child = parent.subwindow(slices=(slice(1, 4), slice(2, 6)))
            grandchild = child.subwindow(
                slices=(slice(None, None, -1), slice(None, None, 2))
            )

            result = grandchild.read(hdu)
            np.testing.assert_array_equal(
                result.data, _gather(grandchild, data)
            )
            # every delivered pixel remains traceable to its source
            y_coords, x_coords = grandchild.valid_source_coords()
            assert np.all(np.diff(y_coords) > 0)
            assert np.all(np.diff(x_coords) > 0)


def test_window_matches_numpy_for_in_bounds_slices():
    dim = 10
    grid = np.arange(dim)
    trials = [
        slice(None),
        slice(2, 8),
        slice(8, 2, -1),
        slice(None, None, -1),
        slice(8, 2, -2),
        slice(1, 9, 2),
        slice(-1, -9, -2),
        slice(-9, -1, 2),
        slice(None, 3, -1),
        slice(5, None, -2),
        slice(-3, -1),
        slice(0, -1),
    ]
    for slc in trials:
        axis = ImageWindow((dim,), (slc,)).axes[0]
        np.testing.assert_array_equal(axis.valid_indices, grid[slc])


def test_window_numpy_parity_with_overshoot():
    dim = 10
    grid = np.arange(dim)
    overshoot = [
        (slice(11, None, -2), [11]),
        (slice(0, 13, 1), [10, 11, 12]),
        (slice(11, None, -1), [11, 10]),
        (slice(8, 14, 2), [10, 12]),
    ]
    for slc, missing in overshoot:
        axis = ImageWindow((dim,), (slc,)).axes[0]
        np.testing.assert_array_equal(axis.valid_indices, grid[slc])
        np.testing.assert_array_equal(axis.missing_indices, missing)


def test_degenerate_slices_are_empty_but_keep_target():
    data = _source_data()
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = _write_image(tmpdir, data)
        with FITS(fname) as fits:
            hdu = fits[0]
            window = ImageWindow.from_hdu(hdu, (slice(3, 3), slice(None)))
            result = window.read(hdu)
            assert result.is_empty
            # the non-empty axis keeps its width in the empty shape
            assert result.shape == (0, SHAPE[1])
            assert result.target[0] == (3, 3)
            assert result.reason is not None

            with pytest.raises(NoImagePixelsError):
                window.read(hdu, strict=True)


def test_window_reverses_both_axes_like_numpy():
    rng = np.random.RandomState(0)
    data = rng.randn(12, 15)
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = _write_image(tmpdir, data, fname="rev.fits")
        with FITS(fname) as fits:
            hdu = fits[0]
            window = ImageWindow.from_hdu(
                hdu,
                (slice(None, None, -2), slice(14, 0, -3)),
            )
            result = window.read(hdu)
            expected = data[::-2, 14:0:-3]
            assert result.shape == expected.shape
            np.testing.assert_array_equal(result.data, expected)
            # both axes are delivered in their reversed source order
            np.testing.assert_array_equal(
                result.axis_coords[0], np.arange(11, -1, -2)
            )
            np.testing.assert_array_equal(
                result.axis_coords[1], np.arange(14, 0, -3)
            )


def test_window_read_does_not_load_full_image(monkeypatch):
    data = _source_data()
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = _write_image(tmpdir, data)
        with FITS(fname) as fits:
            hdu = fits[0]

            def explode(self):
                raise AssertionError("full image read was triggered")

            monkeypatch.setattr("fitsio.hdu.image.ImageHDU.read", explode)

            window = ImageWindow.from_hdu(
                hdu,
                (slice(7, None, -2), slice(0, 12, 1)),
            )
            result = window.read(hdu)
            assert result.size == 30


def test_window_on_compressed_image():
    data = _source_data()
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = _write_image(
            tmpdir,
            data,
            fname="compressed.fits",
            compress=RICE_1,
            tile_dims=(2, 5),
            qlevel=None,
        )
        with FITS(fname) as fits:
            hdu = fits[1]
            assert hdu.is_compressed()
            window = ImageWindow.from_hdu(
                hdu,
                (slice(7, None, -2), slice(0, 12, 2)),
            )
            result = window.read(hdu)
            np.testing.assert_array_equal(result.data, _gather(window, data))


def test_window_on_memory_image():
    data = _source_data()
    with FITS("mem://", "rw") as fits:
        fits.write_image(data)
        hdu = fits[0]
        window = ImageWindow.from_hdu(
            hdu,
            (slice(7, None, -2), slice(0, 12, 1)),
        )
        result = window.read(hdu)
        np.testing.assert_array_equal(result.data, _gather(window, data))


def test_three_dimensional_window():
    shape = (5, 6, 8)
    data = _source_data(shape=shape)
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = _write_image(tmpdir, data)
        with FITS(fname) as fits:
            hdu = fits[0]
            window = ImageWindow.from_hdu(
                hdu,
                (
                    slice(6, None, -2),
                    slice(0, 8, 1),
                    slice(1, 7, 2),
                ),
            )

            assert window.request_shape == (4, 8, 3)
            assert window.valid_shape == (3, 6, 3)

            result = window.read(hdu)
            np.testing.assert_array_equal(result.data, _gather(window, data))

            child = window.subwindow(
                slices=(
                    slice(None, None, -1),
                    slice(1, 5),
                    slice(None),
                )
            )
            child_result = child.read(hdu)
            np.testing.assert_array_equal(
                child_result.data, _gather(child, data)
            )


def test_integer_axis_argument():
    data = _source_data()
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = _write_image(tmpdir, data)
        with FITS(fname) as fits:
            hdu = fits[0]
            window = ImageWindow.from_hdu(hdu, (2, slice(1, 6)))
            result = window.read(hdu)
            np.testing.assert_array_equal(result.data, data[2:3, 1:6])


def test_window_dimension_mismatch_raises():
    data = _source_data()
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = _write_image(tmpdir, data)
        with FITS(fname) as fits:
            hdu = fits[0]
            with pytest.raises(ValueError):
                ImageWindow.from_hdu(hdu, (slice(None),))
            parent = ImageWindow.from_hdu(hdu)
            with pytest.raises(ValueError):
                parent.subwindow(slices=(slice(None),))
            with pytest.raises(ValueError):
                parent.subwindow(source_limits=((0, 1),))
            with pytest.raises(ValueError):
                parent.subwindow()
            with pytest.raises(ValueError):
                parent.subwindow(
                    slices=(slice(None), slice(None)),
                    source_limits=((0, 1), (0, 1)),
                )


def test_zero_step_raises():
    with tempfile.TemporaryDirectory() as tmpdir:
        fname = _write_image(tmpdir, _source_data())
        with FITS(fname) as fits:
            hdu = fits[0]
            with pytest.raises(ValueError):
                ImageWindow.from_hdu(hdu, (slice(None, None, 0), slice(None)))
