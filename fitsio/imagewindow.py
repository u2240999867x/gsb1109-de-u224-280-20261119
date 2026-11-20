"""
Tracable image sampling windows for FITS image HDUs.

A window describes a requested sampling grid in source image
coordinates.  Grid points that fall outside the source image are kept
in the request so that local indices stay aligned with the pixels that
were actually read; they are simply not delivered.  Sub-windows can be
expressed either in local indices of the parent request or directly in
source image coordinates.

Only the delivered subset is read from disk through the regular cfitsio
subset call; the full image is never loaded into memory and no
projection or regridding is performed.
"""

import numpy


class NoImagePixelsError(Exception):
    """
    Raised in strict mode when a requested range contains no sampled
    source pixels.

    Attributes
    ----------
    target : tuple
        The requested source coordinate bounds ``(lo, hi)`` per axis.
    reason : str
        Why no sampled pixels could be delivered.
    """

    def __init__(self, target, reason):
        self.target = target
        self.reason = reason
        super(NoImagePixelsError, self).__init__(reason)


def _normalize_bound(value, dim):
    if value is None:
        return None
    value = int(value)
    if value < 0:
        value += dim
    return value


def _requested_indices(dim, start, stop, step):
    """
    Build the requested source coordinate sequence for one axis.

    The sequence follows numpy slice semantics for in-bounds slices but
    grid points beyond the source edges are retained instead of being
    clipped away.
    """
    step = 1 if step is None else int(step)
    if step == 0:
        raise ValueError("slice step cannot be zero")

    start = _normalize_bound(start, dim)
    stop = _normalize_bound(stop, dim)

    if step > 0:
        if start is None:
            start = 0
        if stop is None:
            stop = dim
        if start < 0:
            start = 0
        if stop < 0:
            stop = 0
        if stop <= start:
            return start, stop, step, numpy.empty(0, dtype=numpy.int64)
        count = (stop - start - 1) // step + 1
        indices = start + numpy.arange(count, dtype=numpy.int64) * step
    else:
        if start is None:
            start = dim - 1
        if stop is None:
            stop = -1
        if start < 0:
            # a descending scan starting below the first pixel can
            # never reach the source
            return start, stop, step, numpy.empty(0, dtype=numpy.int64)
        if stop < -1:
            stop = -1
        if stop >= dim:
            # descending scans whose exclusive end is above the axis
            # never reach the source either
            return start, stop, step, numpy.empty(0, dtype=numpy.int64)
        if start <= stop:
            return start, stop, step, numpy.empty(0, dtype=numpy.int64)
        back = -step
        count = (start - stop - 1) // back + 1
        indices = start - numpy.arange(count, dtype=numpy.int64) * back

    return start, stop, step, indices


class _Axis(object):
    """
    One sampling axis of a window.

    Attributes
    ----------
    dim : int
        Size of the source image along this axis.
    start, stop : int
        Raw normalized bounds of the request in source coordinates.
    step : int
        Signed source coordinate stride between consecutive samples.
    indices : numpy.ndarray
        Requested source coordinates, including out-of-bounds grid
        points.  This array is the authoritative description of the
        sampling grid.
    valid_mask : numpy.ndarray
        Boolean mask marking requested positions that have a source
        pixel.
    """

    def __init__(self, dim, slc=None):
        self.dim = int(dim)
        if slc is None:
            slc = slice(None)
        if isinstance(slc, int):
            slc = slice(slc, slc + 1, 1)
        if not isinstance(slc, slice):
            raise ValueError("axis arguments must be slices or integers")

        start, stop, step, indices = _requested_indices(
            self.dim, slc.start, slc.stop, slc.step
        )
        self.start = start
        self.stop = stop
        self.step = step
        self.indices = indices
        self.valid_mask = (indices >= 0) & (indices < self.dim)
        if indices.size:
            self.target = (int(indices.min()), int(indices.max()))
        else:
            self.target = (start, stop)
        self.note = None

    @classmethod
    def _from_indices(cls, dim, indices, step, target=None, note=None):
        axis = cls.__new__(cls)
        axis.dim = int(dim)
        axis.step = int(step)
        axis.indices = numpy.asarray(indices, dtype=numpy.int64)
        if axis.indices.size:
            axis.start = int(axis.indices[0])
            axis.stop = int(axis.indices[-1])
            if target is None:
                target = (
                    int(axis.indices.min()),
                    int(axis.indices.max()),
                )
        else:
            axis.start = 0
            axis.stop = 0
            if target is None:
                target = (0, 0)
        axis.target = tuple(int(value) for value in target)
        axis.note = note
        axis.valid_mask = (axis.indices >= 0) & (axis.indices < axis.dim)
        return axis

    @property
    def request_count(self):
        return int(self.indices.size)

    @property
    def valid_count(self):
        return int(self.valid_mask.sum())

    @property
    def valid_indices(self):
        """Source coordinates of sampled pixels, in request order."""
        return self.indices[self.valid_mask]

    @property
    def missing_indices(self):
        """Requested source coordinates that have no source pixel."""
        return self.indices[~self.valid_mask]

    @property
    def valid_positions(self):
        """Request-local positions of sampled pixels."""
        return numpy.nonzero(self.valid_mask)[0]

    @property
    def first_valid_position(self):
        positions = self.valid_positions
        if positions.size == 0:
            return None
        return int(positions[0])

    @property
    def last_valid_position(self):
        positions = self.valid_positions
        if positions.size == 0:
            return None
        return int(positions[-1])

    def subaxis(self, start, stop, step):
        """
        Select a sub-range using request-local indices of this axis.
        """
        length = self.request_count
        _, _, local_step, local = _requested_indices(length, start, stop, step)
        selected = self.indices[local]
        note = None
        if selected.size == 0:
            note = (
                "local request %s:%s:%s selects no grid points of an "
                "axis with %d requested position(s)"
                % (start, stop, step, length)
            )
            target = self.target
        else:
            target = (
                int(selected.min()),
                int(selected.max()),
            )
        return _Axis._from_indices(
            self.dim,
            selected,
            self.step * local_step,
            target=target,
            note=note,
        )

    def subaxis_source(self, lo, hi):
        """
        Select requested samples whose source coordinate lies within
        the inclusive ``[lo, hi]`` range.
        """
        if lo is None:
            lo = 0
        if hi is None:
            hi = self.dim - 1
        lo = int(lo)
        hi = int(hi)
        if lo > hi:
            lo, hi = hi, lo
        mask = (self.indices >= lo) & (self.indices <= hi)
        selected = self.indices[mask]
        note = None
        if selected.size == 0:
            overlaps_source = hi >= 0 and lo <= self.dim - 1
            if overlaps_source:
                note = (
                    "source range [%d, %d] overlaps the axis geometry but "
                    "contains none of this window's sampled grid points"
                    % (lo, hi)
                )
            else:
                note = (
                    "source range [%d, %d] lies entirely outside source "
                    "size %d" % (lo, hi, self.dim)
                )
        return _Axis._from_indices(
            self.dim, selected, self.step, target=(lo, hi), note=note
        )


class ImageWindow(object):
    """
    A sampling window over a source image, one axis per image dimension.

    Parameters
    ----------
    shape : sequence of int
        Source image shape in numpy (C) order.
    slices : sequence, optional
        One slice or integer per axis, interpreted in source image
        coordinates.  ``None`` selects the full axis.
    """

    def __init__(self, shape, slices=None):
        self._shape = tuple(int(dim) for dim in shape)
        if slices is None:
            slices = (slice(None),) * len(self._shape)
        if isinstance(slices, (slice, int)):
            slices = (slices,)
        slices = tuple(slices)
        if len(slices) != len(self._shape):
            raise ValueError(
                "got %d slice dimensions, expected %d"
                % (len(slices), len(self._shape))
            )
        self._axes = tuple(
            _Axis(dim, slc) for dim, slc in zip(self._shape, slices)
        )

    @classmethod
    def from_hdu(cls, hdu, slices=None):
        """
        Build a window matching the dimensions of an image HDU.
        """
        if not hdu.has_data():
            raise ValueError("attempt to window an empty extension")
        return cls(hdu.get_dims(), slices)

    @property
    def shape(self):
        return self._shape

    @property
    def axes(self):
        return self._axes

    @property
    def ndim(self):
        return len(self._axes)

    def __len__(self):
        return len(self._axes)

    def __iter__(self):
        return iter(self._axes)

    def __getitem__(self, index):
        return self._axes[index]

    @property
    def request_shape(self):
        """Shape including requested but undeliverable grid points."""
        return tuple(axis.request_count for axis in self._axes)

    @property
    def valid_shape(self):
        """Shape of the deliverable sampled pixels."""
        return tuple(axis.valid_count for axis in self._axes)

    @property
    def is_empty(self):
        return any(axis.valid_count == 0 for axis in self._axes)

    def source_coords(self):
        """Requested source coordinates per axis, including gaps."""
        return tuple(axis.indices for axis in self._axes)

    def valid_source_coords(self):
        """Source coordinates of deliverable pixels per axis."""
        return tuple(axis.valid_indices for axis in self._axes)

    def missing_source_coords(self):
        """Requested coordinates without a source pixel, per axis."""
        return tuple(axis.missing_indices for axis in self._axes)

    def valid_request_positions(self):
        """Request-local positions of deliverable pixels per axis."""
        return tuple(axis.valid_positions for axis in self._axes)

    def _target(self):
        return tuple(axis.target for axis in self._axes)

    def subwindow(self, slices=None, source_limits=None):
        """
        Build a sub-window.

        Parameters
        ----------
        slices : sequence, optional
            Slices expressed in request-local indices of this window.
        source_limits : sequence, optional
            Inclusive ``(lo, hi)`` source coordinate ranges per axis;
            an entry may be ``None`` to leave that axis unrestricted.
        """
        if slices is not None and source_limits is not None:
            raise ValueError("pass either slices or source_limits, not both")
        if slices is None and source_limits is None:
            raise ValueError(
                "pass slices or source_limits to define the sub-window"
            )

        if slices is not None:
            if isinstance(slices, (slice, int)):
                slices = (slices,)
            slices = tuple(slices)
            if len(slices) != self.ndim:
                raise ValueError(
                    "got %d slice dimensions, expected %d"
                    % (len(slices), self.ndim)
                )
            new_axes = []
            for axis, slc in zip(self._axes, slices):
                if slc is None:
                    slc = slice(None)
                if isinstance(slc, int):
                    slc = slice(slc, slc + 1, 1)
                if not isinstance(slc, slice):
                    raise ValueError(
                        "axis arguments must be slices or integers"
                    )
                new_axes.append(axis.subaxis(slc.start, slc.stop, slc.step))
        else:
            limits = tuple(source_limits)
            if len(limits) != self.ndim:
                raise ValueError(
                    "got %d source limits, expected %d"
                    % (len(limits), self.ndim)
                )
            new_axes = []
            for axis, limit in zip(self._axes, limits):
                if limit is None:
                    lo, hi = None, None
                else:
                    lo, hi = limit
                new_axes.append(axis.subaxis_source(lo, hi))

        window = ImageWindow.__new__(ImageWindow)
        window._shape = self._shape
        window._axes = tuple(new_axes)
        return window

    def read(self, hdu, strict=False):
        """
        Read the deliverable sampled pixels from an image HDU.

        Only the valid subset is requested from cfitsio, so out-of-bounds
        grid points never shift the local indexing of the delivered
        pixels.  An empty result carrying the original target and the
        reason is returned when no sampled pixel exists; pass
        ``strict=True`` to raise :class:`NoImagePixelsError` instead.
        """
        dtype = numpy.dtype(hdu._get_image_numpy_dtype())
        target = self._target()

        if self.is_empty:
            reasons = []
            for dim_index, axis in enumerate(self._axes):
                if axis.valid_count == 0:
                    if axis.note is not None:
                        reasons.append("axis %d: %s" % (dim_index, axis.note))
                    elif axis.request_count == 0:
                        reasons.append(
                            "axis %d: no grid points were requested "
                            "(source size %d, target %s)"
                            % (dim_index, axis.dim, axis.target)
                        )
                    else:
                        reasons.append(
                            "axis %d: all %d requested grid point(s) "
                            "lie outside source size %d (target %s)"
                            % (
                                dim_index,
                                axis.request_count,
                                axis.dim,
                                axis.target,
                            )
                        )
            reason = "; ".join(reasons)
            empty = numpy.empty(self.valid_shape, dtype=dtype)
            result = ImageSliceResult(
                empty,
                self,
                self.valid_source_coords(),
                self.valid_request_positions(),
                target,
                reason,
            )
            if strict:
                raise NoImagePixelsError(target, reason)
            return result

        first = []
        last = []
        steps = []
        flip_axes = []
        for dim_index, axis in enumerate(self._axes):
            coords = axis.valid_indices
            low = int(coords.min())
            high = int(coords.max())
            # cfitsio uses 1-based offsets
            first.append(low + 1)
            last.append(high + 1)
            steps.append(abs(axis.step))
            if axis.step < 0:
                flip_axes.append(dim_index)

        data = numpy.zeros(self.valid_shape, dtype=dtype)
        # cfitsio expects dimensions in Fortran order
        hdu._FITS.read_image_slice(
            hdu._ext + 1,
            numpy.array(first[::-1], dtype="i8"),
            numpy.array(last[::-1], dtype="i8"),
            numpy.array(steps[::-1], dtype="i8"),
            hdu._ignore_scaling,
            data,
        )
        for dim_index in flip_axes:
            data = numpy.flip(data, axis=dim_index)
        data = numpy.ascontiguousarray(data)

        return ImageSliceResult(
            data,
            self,
            self.valid_source_coords(),
            self.valid_request_positions(),
            target,
            None,
        )

    def __repr__(self):
        return "ImageWindow(shape=%s, request=%s, valid=%s)" % (
            self._shape,
            self.request_shape,
            self.valid_shape,
        )


class ImageSliceResult(object):
    """
    Delivered pixels from :meth:`ImageWindow.read`.

    Attributes
    ----------
    data : numpy.ndarray
        Sampled pixel values, ordered exactly as a cell-by-cell read of
        the full source image would produce.
    window : ImageWindow
        The window that produced this result.
    axis_coords : tuple of numpy.ndarray
        Source coordinate of the delivered samples along each axis.
    request_positions : tuple of numpy.ndarray
        Request-local positions of the delivered samples per axis;
        missing grid points leave gaps instead of compacting these.
    target : tuple
        Requested source coordinate bounds per axis.
    reason : str or None
        Why the result is empty, or ``None`` when pixels were read.
    """

    def __init__(
        self,
        data,
        window,
        axis_coords,
        request_positions,
        target,
        reason,
    ):
        self.data = data
        self.window = window
        self.axis_coords = tuple(
            numpy.asarray(coords, dtype=numpy.int64) for coords in axis_coords
        )
        self.request_positions = tuple(
            numpy.asarray(positions, dtype=numpy.int64)
            for positions in request_positions
        )
        self.target = target
        self.reason = reason

    @property
    def shape(self):
        return self.data.shape

    @property
    def size(self):
        return int(self.data.size)

    @property
    def dtype(self):
        return self.data.dtype

    @property
    def is_empty(self):
        return self.data.size == 0

    def __array__(self, dtype=None):
        if dtype is None:
            return self.data
        return self.data.astype(dtype)

    def __repr__(self):
        if self.reason is None:
            return "ImageSliceResult(shape=%s, dtype=%s)" % (
                self.data.shape,
                self.data.dtype,
            )
        return "ImageSliceResult(empty, target=%s, reason=%s)" % (
            self.target,
            self.reason,
        )
