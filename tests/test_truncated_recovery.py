# test_truncated_recovery.py

"""Offline regression tests for interrupted multi-strip TIFF write recovery.

The tests hand-craft uncompressed UINT16 TIFF files from raw bytes. Expected
images are derived only from the physical bytes that are actually present in
the files (parsed with :mod:`struct` and :func:`numpy.frombuffer`); another
tifffile read is never used as an oracle.

Scenarios under test:

- three pages, page three has strips whose logical order is 0..n but whose
  on-disk order is permuted; its directory (IFD plus offset and byte count
  arrays) precedes all pixel data, so truncating pixel bytes leaves a fully
  readable directory;
- two truncation layouts, little and big endian, with the cut inside the
  third strip written to disk at an odd byte offset, leaving the same number
  of complete samples but different missing logical rows;
- a control file whose directory entry counts are shortened in place while
  keeping the raw byte length identical;
- a fully written healthy file;
- a reproducible chain of operations that reuses one pre-marked output array
  across per-page reads, batch segment reads in different orders and buffer
  settings, a different fill value and layout read from another thread, and
  finally per-page reads of the original file again;
- two reversible, test-process-only fault injections (doubles) proving the
  chain tests distinguish a stale-fill leak from a stale strip-position map
  without changing sample counts, array shapes, or log levels.

"""

from __future__ import annotations

import logging
import os
import re
import struct
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy
import pytest

import tifffile
from tifffile.tifffile import TiffPage

if TYPE_CHECKING:
    from collections.abc import Iterator

# geometry of the canonical files
PAGE_COUNT = 3
IMAGE_HEIGHT = 8
IMAGE_WIDTH = 4
ROWS_PER_STRIP = 2
STRIP_COUNT = IMAGE_HEIGHT // ROWS_PER_STRIP
BYTES_PER_SAMPLE = 2
ROW_BYTES = IMAGE_WIDTH * BYTES_PER_SAMPLE

PAGE_BASE = 10000
ROW_BASE = 100

FILL_A = 1111
FILL_B = 2222
OUTPUT_MARKER = 9999

# genuine pixels that intentionally equal fill constants or zero; positional
# masks, never value comparisons, decide whether a cell is present or missing
REAL_ZERO_PAGE = 2
REAL_ZERO_ROW = 0
REAL_ZERO_COL = 0
# present in every truncation layout (first row of strip zero) yet equal to
# FILL_A: a real sample that coincides with a fill value
FILL_COINCIDENT_PAGE = 2
FILL_COINCIDENT_ROW = 0
FILL_COINCIDENT_COL = 1
FILL_LIKE_PAGE = 0
FILL_LIKE_A = (1, 1)
FILL_LIKE_B = (1, 2)

# canonical on-disk strip orders: logical strip index per write position
LAYOUT_A_ORDER = (2, 0, 3, 1)
LAYOUT_B_ORDER = (1, 3, 0, 2)
LAYOUT_A_ENDIAN = '<'
LAYOUT_B_ENDIAN = '>'
ODD_CUT_BYTES = 5  # leaves exactly two complete UINT16 samples

_STRIP_ROW_RE = re.compile(r'\(0,\s*0,\s*(\d+),\s*0,\s*0\)')
_TYPE_SIZES = {3: 2, 4: 4}


# --------------------------------------------------------------------------
# reference pixels
# --------------------------------------------------------------------------


def reference_pixel(page: int, row: int, col: int) -> int:
    """Return deterministic source value identifying page, row, and column."""
    return PAGE_BASE * (page + 1) + ROW_BASE * row + col


def reference_images() -> numpy.ndarray:
    """Return the fully written source images as a native UINT16 array."""
    images = numpy.empty(
        (PAGE_COUNT, IMAGE_HEIGHT, IMAGE_WIDTH), dtype=numpy.uint16
    )
    for page in range(PAGE_COUNT):
        for row in range(IMAGE_HEIGHT):
            for col in range(IMAGE_WIDTH):
                images[page, row, col] = reference_pixel(page, row, col)
    images[REAL_ZERO_PAGE, REAL_ZERO_ROW, REAL_ZERO_COL] = 0
    images[
        FILL_COINCIDENT_PAGE, FILL_COINCIDENT_ROW, FILL_COINCIDENT_COL
    ] = FILL_A
    images[FILL_LIKE_PAGE, FILL_LIKE_A[0], FILL_LIKE_A[1]] = FILL_A
    images[FILL_LIKE_PAGE, FILL_LIKE_B[0], FILL_LIKE_B[1]] = FILL_B
    return images


def _align_even(blob: bytearray) -> None:
    if len(blob) % 2:
        blob.append(0)


@dataclass(frozen=True)
class RawLayout:
    """Physical layout of one generated file, parsed from its own bytes."""

    byteorder: str
    disk_order: tuple[int, ...]
    page_offsets: tuple[tuple[int, ...], ...]
    ifd_offsets: tuple[int, ...]
    file_size: int


def write_tiff(
    path: str,
    *,
    images: numpy.ndarray,
    byteorder: str,
    disk_order: tuple[int, ...],
    rows_per_strip: int = ROWS_PER_STRIP,
    nbytes: int | None = None,
) -> RawLayout:
    """Write an uncompressed UINT16 TIFF with pages and permuted strips.

    Every directory (IFD and the external StripOffsets/StripByteCounts arrays)
    is written before any pixel data. The last page stores its strips in
    ``disk_order``; the other pages store strips in logical order.
    """
    page_count, height, width = images.shape
    strip_count = (height + rows_per_strip - 1) // rows_per_strip
    assert sorted(disk_order) == list(range(strip_count))
    row_bytes = width * BYTES_PER_SAMPLE
    endian = b'II' if byteorder == '<' else b'MM'
    fmt = byteorder

    blob = bytearray()
    blob += endian
    blob += struct.pack(fmt + 'H', 42)
    blob += struct.pack(fmt + 'I', 8)

    ifd_offsets: list[int] = []
    offsets_arrays: list[int] = []
    counts_arrays: list[int] = []
    directory_size = 2 + 10 * 12 + 4 + 2 * 4 * strip_count
    for _ in range(page_count):
        _align_even(blob)
        ifd_offsets.append(len(blob))
        offsets_arrays.append(len(blob) + 2 + 10 * 12 + 4)
        counts_arrays.append(offsets_arrays[-1] + 4 * strip_count)
        blob += b'\x00' * directory_size

    page_offsets: list[list[int]] = [[] for _ in range(page_count)]
    for page in range(page_count):
        order = (
            tuple(range(strip_count))
            if page != page_count - 1
            else disk_order
        )
        offsets = [0] * strip_count
        for strip in order:
            _align_even(blob)
            offsets[strip] = len(blob)
            row0 = strip * rows_per_strip
            row1 = min(height, row0 + rows_per_strip)
            block = images[page, row0:row1]
            blob += block.astype(fmt + 'u2').tobytes()
        page_offsets[page] = offsets

    for page in range(page_count):
        for strip in range(strip_count):
            struct.pack_into(
                fmt + 'I',
                blob,
                offsets_arrays[page] + 4 * strip,
                page_offsets[page][strip],
            )
            strip_height = min(
                rows_per_strip, height - strip * rows_per_strip
            )
            struct.pack_into(
                fmt + 'I',
                blob,
                counts_arrays[page] + 4 * strip,
                strip_height * row_bytes,
            )
        tags = (
            (256, 4, 1, width),  # ImageWidth
            (257, 4, 1, height),  # ImageLength
            (258, 3, 1, 16),  # BitsPerSample
            (259, 3, 1, 1),  # Compression = none
            (262, 3, 1, 1),  # Photometric = MinIsBlack
            (273, 4, strip_count, offsets_arrays[page]),  # StripOffsets
            (277, 3, 1, 1),  # SamplesPerPixel
            (278, 4, 1, rows_per_strip),  # RowsPerStrip
            (279, 4, strip_count, counts_arrays[page]),  # StripByteCounts
            (284, 3, 1, 1),  # PlanarConfiguration
        )
        pos = ifd_offsets[page]
        struct.pack_into(fmt + 'H', blob, pos, len(tags))
        entry = pos + 2
        for code, typ, count, value in sorted(tags):
            if typ == 3:
                inline = struct.pack(fmt + 'H', value) + b'\x00\x00'
            else:
                inline = struct.pack(fmt + 'I', value)
            blob[entry : entry + 12] = (
                struct.pack(fmt + 'HHI', code, typ, count) + inline
            )
            entry += 12
        next_ifd = ifd_offsets[page + 1] if page + 1 < page_count else 0
        struct.pack_into(fmt + 'I', blob, entry, next_ifd)

    if nbytes is not None:
        del blob[nbytes:]
    with open(path, 'wb') as fh:
        fh.write(blob)

    return RawLayout(
        byteorder=byteorder,
        disk_order=disk_order,
        page_offsets=tuple(tuple(o) for o in page_offsets),
        ifd_offsets=tuple(ifd_offsets),
        file_size=len(blob),
    )


def _read_raw_ifd(
    path: str, page_index: int
) -> dict[int, tuple[int, int, Any]]:
    """Parse one IFD directly from file bytes without using tifffile."""
    with open(path, 'rb') as fh:
        blob = fh.read()
    byteorder = '<' if blob[:2] == b'II' else '>'
    offset = struct.unpack_from(byteorder + 'I', blob, 4)[0]
    for _ in range(page_index):
        count = struct.unpack_from(byteorder + 'H', blob, offset)[0]
        offset = struct.unpack_from(
            byteorder + 'I', blob, offset + 2 + count * 12
        )[0]
    tags: dict[int, tuple[int, int, Any]] = {}
    count = struct.unpack_from(byteorder + 'H', blob, offset)[0]
    for entry in range(count):
        pos = offset + 2 + entry * 12
        code, typ, tag_count = struct.unpack_from(
            byteorder + 'HHI', blob, pos
        )
        value_position = pos + 8
        if tag_count == 1:
            if typ == 3:
                value: Any = struct.unpack_from(
                    byteorder + 'H', blob, value_position
                )[0]
            else:
                value = struct.unpack_from(
                    byteorder + 'I', blob, value_position
                )[0]
        else:
            array_offset = struct.unpack_from(
                byteorder + 'I', blob, value_position
            )[0]
            raw = blob[
                array_offset : array_offset
                + _TYPE_SIZES[typ] * tag_count
            ]
            value = struct.unpack(
                byteorder + str(tag_count) + ('H' if typ == 3 else 'I'),
                raw,
            )
        tags[code] = (typ, tag_count, value)
    return tags


def raw_page_oracle(
    path: str, page_index: int, fillvalue: int
) -> tuple[numpy.ndarray, numpy.ndarray, tuple[int, ...]]:
    """Return expected image, present mask, and flagged strip start rows.

    Geometry is taken from the directory itself, and only the physical bytes
    reachable from the directory are inspected. Strips missing from a short
    directory count are flagged as well.
    """
    with open(path, 'rb') as fh:
        header = fh.read(2)
    byteorder = '<' if header == b'II' else '>'
    tags = _read_raw_ifd(path, page_index)
    width = tags[256][2]
    height = tags[257][2]
    rows_per_strip = tags[278][2]
    expected_strip_count = (height + rows_per_strip - 1) // rows_per_strip
    offsets = tags[273][2]
    bytecounts = tags[279][2]
    file_size = os.path.getsize(path)

    expected = numpy.full((height, width), fillvalue, dtype=numpy.uint16)
    present = numpy.zeros((height, width), dtype=bool)
    flagged_rows: list[int] = []

    for strip in range(expected_strip_count):
        row0 = strip * rows_per_strip
        row1 = min(height, row0 + rows_per_strip)
        if strip >= len(offsets) or strip >= len(bytecounts):
            flagged_rows.append(row0)
            continue
        data_offset = offsets[strip]
        byte_count = bytecounts[strip]
        readable = max(0, min(byte_count, file_size - data_offset))
        complete_samples = readable // BYTES_PER_SAMPLE
        strip_size = (row1 - row0) * width
        if complete_samples < strip_size:
            flagged_rows.append(row0)
        if complete_samples == 0:
            continue
        with open(path, 'rb') as fh:
            fh.seek(data_offset)
            raw = fh.read(complete_samples * BYTES_PER_SAMPLE)
        values = numpy.frombuffer(
            raw, dtype=byteorder + 'u2'
        ).astype(numpy.uint16)
        flat_cells = present[row0:row1].reshape(-1)
        flat_cells[:complete_samples] = True
        block = expected[row0:row1].reshape(-1)
        block[:complete_samples] = values
    return expected, present, tuple(sorted(flagged_rows))


def shorten_directory_counts(
    path: str, page_index: int, new_count: int
) -> None:
    """Overwrite IFD entry counts in place, preserving the raw file length."""
    with open(path, 'rb') as fh:
        blob = bytearray(fh.read())
    byteorder = '<' if blob[:2] == b'II' else '>'
    ifd_offset = struct.unpack_from(byteorder + 'I', blob, 4)[0]
    for _ in range(page_index):
        count = struct.unpack_from(byteorder + 'H', blob, ifd_offset)[0]
        ifd_offset = struct.unpack_from(
            byteorder + 'I', blob, ifd_offset + 2 + count * 12
        )[0]
    count = struct.unpack_from(byteorder + 'H', blob, ifd_offset)[0]
    for entry in range(count):
        pos = ifd_offset + 2 + entry * 12
        code = struct.unpack_from(byteorder + 'H', blob, pos)[0]
        if code in (273, 279):
            struct.pack_into(byteorder + 'I', blob, pos + 4, new_count)
    with open(path, 'wb') as fh:
        fh.write(blob)


def truncation_length(
    path: str,
    page_index: int,
    disk_order: tuple[int, ...],
    extra_bytes: int,
    *,
    disk_position: int = 2,
) -> int:
    """Return a cut length inside the strip at ``disk_position`` on disk.

    The default cuts the third written strip at an odd byte offset, leaving
    the strip written after it entirely unwritten.
    """
    tags = _read_raw_ifd(path, page_index)
    offsets = tags[273][2]
    target_strip = disk_order[disk_position]
    return offsets[target_strip] + extra_bytes


# --------------------------------------------------------------------------
# file fixtures
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Corpus:
    images: numpy.ndarray
    healthy_a: str
    healthy_b: str
    truncated_a: str
    truncated_b: str
    directory_control: str


@pytest.fixture(scope='module')
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Corpus]:
    base = tmp_path_factory.mktemp('truncated_recovery')
    images = reference_images()

    healthy_a = str(base / 'healthy_a.tif')
    healthy_b = str(base / 'healthy_b.tif')
    truncated_a = str(base / 'truncated_a.tif')
    truncated_b = str(base / 'truncated_b.tif')
    directory_control = str(base / 'directory_control.tif')

    layout_a = write_tiff(
        healthy_a,
        images=images,
        byteorder=LAYOUT_A_ENDIAN,
        disk_order=LAYOUT_A_ORDER,
    )
    write_tiff(
        healthy_b,
        images=images,
        byteorder=LAYOUT_B_ENDIAN,
        disk_order=LAYOUT_B_ORDER,
    )
    cut_a = truncation_length(
        healthy_a, PAGE_COUNT - 1, LAYOUT_A_ORDER, ODD_CUT_BYTES
    )
    write_tiff(
        truncated_a,
        images=images,
        byteorder=LAYOUT_A_ENDIAN,
        disk_order=LAYOUT_A_ORDER,
        nbytes=cut_a,
    )
    cut_b = truncation_length(
        healthy_b, PAGE_COUNT - 1, LAYOUT_B_ORDER, ODD_CUT_BYTES
    )
    write_tiff(
        truncated_b,
        images=images,
        byteorder=LAYOUT_B_ENDIAN,
        disk_order=LAYOUT_B_ORDER,
        nbytes=cut_b,
    )

    control_size = write_tiff(
        directory_control,
        images=images,
        byteorder=LAYOUT_A_ENDIAN,
        disk_order=LAYOUT_A_ORDER,
    ).file_size
    shorten_directory_counts(directory_control, PAGE_COUNT - 1, 2)
    assert os.path.getsize(directory_control) == control_size

    assert layout_a.file_size > cut_a
    yield Corpus(
        images=images,
        healthy_a=healthy_a,
        healthy_b=healthy_b,
        truncated_a=truncated_a,
        truncated_b=truncated_b,
        directory_control=directory_control,
    )


# --------------------------------------------------------------------------
# assertions
# --------------------------------------------------------------------------


def error_rows(caplog: pytest.LogCaptureFixture) -> set[int]:
    """Extract logical strip start rows from ERROR records, text agnostic."""
    rows: set[int] = set()
    for record in caplog.records:
        if record.name != 'tifffile' or record.levelno != logging.ERROR:
            continue
        rows.update(
            int(match)
            for match in _STRIP_ROW_RE.findall(record.getMessage())
        )
    return rows


def _strips_for_cells(mask: numpy.ndarray) -> tuple[int, ...]:
    """Return logical strip start rows containing any masked cell."""
    rows = numpy.flatnonzero(mask.any(axis=1))
    return tuple(
        sorted({int(r // ROWS_PER_STRIP) * ROWS_PER_STRIP for r in rows})
    )


def assert_matches_oracle(
    actual: numpy.ndarray,
    path: str,
    fillvalue: int,
    *,
    step: str,
    caplog: pytest.LogCaptureFixture | None = None,
) -> None:
    """Assert the canonical page three matches the physical-byte oracle."""
    expected, present, flagged = raw_page_oracle(
        path, PAGE_COUNT - 1, fillvalue
    )
    location = f'{step} page {PAGE_COUNT - 1} ({os.path.basename(path)})'
    assert actual.shape == (IMAGE_HEIGHT, IMAGE_WIDTH), location
    assert actual.dtype == numpy.uint16 and actual.dtype.isnative, location
    present_mismatch = present & (actual != expected)
    assert not present_mismatch.any(), (
        f'{location}: present samples moved or changed in strips '
        f'{_strips_for_cells(present_mismatch)}'
    )
    missing = ~present
    fill_mismatch = missing & (actual != fillvalue)
    assert not fill_mismatch.any(), (
        f'{location}: missing cells must use this read fill value '
        f'{fillvalue}, found {numpy.unique(actual[missing]).tolist()} in '
        f'strips {_strips_for_cells(fill_mismatch)}'
    )
    assert OUTPUT_MARKER not in actual, f'{location}: output marker leaked'
    # the genuine zero is present in both canonical truncation layouts
    assert actual[REAL_ZERO_ROW, REAL_ZERO_COL] == 0, location
    # a present sample equal to the fill constant stays present and is never
    # replaced by a different fill value
    assert present[FILL_COINCIDENT_ROW, FILL_COINCIDENT_COL], location
    assert actual[
        FILL_COINCIDENT_ROW, FILL_COINCIDENT_COL
    ] == FILL_A, location
    if caplog is not None and flagged:
        assert error_rows(caplog) >= set(flagged), (
            f'{location}: diagnostics must point at the truly missing strips '
            f'{flagged}, got {sorted(error_rows(caplog))}'
        )


def marked_output() -> numpy.ndarray:
    return numpy.full(
        (IMAGE_HEIGHT, IMAGE_WIDTH), OUTPUT_MARKER, dtype=numpy.uint16
    )


def assemble_segments(
    page: TiffPage,
    output: numpy.ndarray,
    *,
    sort: bool,
    fillvalue: int,
    maxworkers: int,
    buffersize: int | None,
) -> numpy.ndarray:
    """Batch read segments into a reused array, mirroring TiffPage.asarray."""
    output.fill(OUTPUT_MARKER)
    for segment, indices, shape in page.segments(
        sort=sort,
        fillvalue=fillvalue,
        maxworkers=maxworkers,
        buffersize=buffersize,
    ):
        height0, width0 = indices[2], indices[3]
        segment_height, segment_width = shape[1], shape[2]
        target = output[
            height0 : height0 + segment_height,
            width0 : width0 + segment_width,
        ]
        if segment is None:
            target.fill(fillvalue)
        else:
            target[:] = segment.reshape(segment_height, segment_width)[
                : IMAGE_HEIGHT - height0, : IMAGE_WIDTH - width0
            ]
    return output


# --------------------------------------------------------------------------
# healthy and per-operation behavior
# --------------------------------------------------------------------------


def test_healthy_files_match_physical_bytes(corpus: Corpus) -> None:
    """Per-page, series, and sequence reads match the byte-only oracle."""
    for path in (corpus.healthy_a, corpus.healthy_b):
        with tifffile.TiffFile(path) as tif:
            stacked = tif.asarray()
            for page in range(PAGE_COUNT):
                expected, present, flagged = raw_page_oracle(
                    path, page, FILL_A
                )
                assert flagged == ()
                assert present.all()
                numpy.testing.assert_array_equal(
                    tif.pages[page].asarray(), expected
                )
                numpy.testing.assert_array_equal(stacked[page], expected)
        sequence = tifffile.TiffSequence([path])
        sequenced = sequence.asarray()
        numpy.testing.assert_array_equal(sequenced[0], stacked)

    # planted real zero and fill-like pixels survive at exact positions
    with tifffile.TiffFile(corpus.healthy_a) as tif:
        healthy = tif.asarray()
    assert healthy[REAL_ZERO_PAGE, REAL_ZERO_ROW, REAL_ZERO_COL] == 0
    assert healthy[FILL_LIKE_PAGE, FILL_LIKE_A[0], FILL_LIKE_A[1]] == FILL_A
    assert healthy[FILL_LIKE_PAGE, FILL_LIKE_B[0], FILL_LIKE_B[1]] == FILL_B


def test_truncated_files_have_equal_complete_samples(corpus: Corpus) -> None:
    """The two layouts keep the same sample count and raw length."""
    assert os.path.getsize(corpus.truncated_a) == os.path.getsize(
        corpus.truncated_b
    )
    totals: list[int] = []
    for path in (corpus.truncated_a, corpus.truncated_b):
        tags = _read_raw_ifd(path, PAGE_COUNT - 1)
        file_size = os.path.getsize(path)
        total = 0
        for offset, byte_count in zip(
            tags[273][2], tags[279][2], strict=True
        ):
            readable = max(0, min(byte_count, file_size - offset))
            total += readable // BYTES_PER_SAMPLE
        totals.append(total)
    assert totals == [18, 18]
    # missing logical rows differ between layouts
    _, _, rows_a = raw_page_oracle(corpus.truncated_a, PAGE_COUNT - 1, FILL_A)
    _, _, rows_b = raw_page_oracle(corpus.truncated_b, PAGE_COUNT - 1, FILL_B)
    assert rows_a != rows_b
    assert rows_a == (2, 6)
    assert rows_b == (0, 4)


@pytest.mark.parametrize(
    ('path', 'fillvalue', 'maxworkers', 'buffersize'),
    [
        ('truncated_a', FILL_A, 1, None),
        ('truncated_a', FILL_B, 1, 16),
        ('truncated_b', FILL_A, 2, None),
        ('truncated_b', FILL_B, 2, 16),
    ],
)
def test_per_page_read_uses_current_fill_and_positions(
    corpus: Corpus,
    caplog: pytest.LogCaptureFixture,
    path: str,
    fillvalue: int,
    maxworkers: int,
    buffersize: int | None,
) -> None:
    caplog.set_level(logging.ERROR, logger='tifffile')
    filename = getattr(corpus, path)
    output = marked_output()
    with tifffile.TiffFile(filename) as tif:
        actual = tif.pages[PAGE_COUNT - 1].asarray(
            out=output,
            fillvalue=fillvalue,
            maxworkers=maxworkers,
            buffersize=buffersize,
        )
    assert numpy.shares_memory(actual, output)
    assert_matches_oracle(
        actual,
        filename,
        fillvalue,
        step=f'per-page {path} fill={fillvalue}',
        caplog=caplog,
    )


def test_batch_segments_both_orders_reuse_output(corpus: Corpus) -> None:
    output = marked_output()
    with tifffile.TiffFile(corpus.truncated_a) as tif:
        page = tif.pages[PAGE_COUNT - 1]
        first = assemble_segments(
            page,
            output,
            sort=True,
            fillvalue=FILL_A,
            maxworkers=1,
            buffersize=16,
        )
        snapshot = first.copy()
        second = assemble_segments(
            page,
            output,
            sort=False,
            fillvalue=FILL_A,
            maxworkers=2,
            buffersize=None,
        )
        numpy.testing.assert_array_equal(first, second)
        numpy.testing.assert_array_equal(second, snapshot)
    assert_matches_oracle(
        second,
        corpus.truncated_a,
        FILL_A,
        step='batch segments reused output',
    )


def test_series_reuse_keeps_healthy_pages(corpus: Corpus) -> None:
    output = numpy.full(
        (PAGE_COUNT, IMAGE_HEIGHT, IMAGE_WIDTH),
        OUTPUT_MARKER,
        dtype=numpy.uint16,
    )
    with tifffile.TiffFile(corpus.truncated_a) as tif:
        stacked = tif.series[0].asarray(
            out=output, fillvalue=FILL_A, maxworkers=1
        )
    assert numpy.shares_memory(stacked, output)
    for page in range(PAGE_COUNT - 1):
        expected, present, _ = raw_page_oracle(
            corpus.truncated_a, page, FILL_A
        )
        numpy.testing.assert_array_equal(
            stacked[page][present], expected[present]
        )
        assert OUTPUT_MARKER not in stacked[page]
    assert_matches_oracle(
        stacked[PAGE_COUNT - 1],
        corpus.truncated_a,
        FILL_A,
        step='series reused output page three',
    )


def test_directory_count_control_is_not_a_healthy_page(
    corpus: Corpus, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger='tifffile')
    healthy = tifffile.imread(corpus.healthy_a)[PAGE_COUNT - 1]

    with tifffile.TiffFile(corpus.directory_control) as tif:
        direct = tif.pages[PAGE_COUNT - 1].asarray(fillvalue=FILL_A)
        enumerated = list(tif.pages)[PAGE_COUNT - 1].asarray(fillvalue=FILL_A)

    expected, present, flagged = raw_page_oracle(
        corpus.directory_control, PAGE_COUNT - 1, FILL_A
    )
    assert flagged  # directory itself advertises missing strips
    for label, image in (('direct', direct), ('enumerated', enumerated)):
        assert not numpy.array_equal(image, healthy), (
            f'{label}: truncated directory must not inherit a healthy page'
        )
        assert image.shape == healthy.shape
        numpy.testing.assert_array_equal(
            image[present], expected[present], err_msg=label
        )
        assert (image[~present] == FILL_A).all(), label
    # direct and enumerate-then-read are allowed to differ in diagnostics,
    # but neither may expose an unwritten cell as healthy data
    assert not present[IMAGE_HEIGHT - 1].any()


def test_serial_pages_then_seek_page_three(corpus: Corpus) -> None:
    """Read healthy pages serially, then seek page three in the same file."""
    with tifffile.TiffFile(corpus.truncated_a) as tif:
        first = tif.pages[0].asarray()
        second = tif.pages[1].asarray()
        numpy.testing.assert_array_equal(first, corpus.images[0])
        numpy.testing.assert_array_equal(second, corpus.images[1])
        # seek back to page three after the healthy pages were materialized
        third = tif.pages[PAGE_COUNT - 1].asarray(fillvalue=FILL_A)
    assert_matches_oracle(
        third,
        corpus.truncated_a,
        FILL_A,
        step='serial pages then seek page three',
    )


def test_byte_order_handling(corpus: Corpus) -> None:
    """Native and non-native byte orders both decode to the same values."""
    native = '<' if sys.byteorder == 'little' else '>'
    orders = {
        corpus.truncated_a: LAYOUT_A_ENDIAN,
        corpus.truncated_b: LAYOUT_B_ENDIAN,
    }
    assert LAYOUT_A_ENDIAN != LAYOUT_B_ENDIAN
    assert native in orders.values()
    non_native = [
        path for path, order in orders.items() if order != native
    ]
    assert len(non_native) == 1

    for path, order in orders.items():
        tags = _read_raw_ifd(path, PAGE_COUNT - 1)
        first_offset = tags[273][2][0]  # logical strip zero
        with open(path, 'rb') as fh:
            fh.seek(first_offset)
            raw = fh.read(ROW_BYTES)
        sample_count = len(raw) // BYTES_PER_SAMPLE
        assert sample_count >= 2
        raw = raw[: sample_count * BYTES_PER_SAMPLE]
        # only the file's declared byte order reproduces the first samples;
        # the swapped order does not
        correct = struct.unpack(order + str(sample_count) + 'H', raw)
        swapped = struct.unpack(
            ('>' if order == '<' else '<') + str(sample_count) + 'H', raw
        )
        assert correct[0] == 0  # planted genuine zero
        assert correct[1] == FILL_A  # planted fill-coincident real sample
        assert swapped != correct
        with tifffile.TiffFile(path) as tif:
            decoded = tif.pages[PAGE_COUNT - 1].asarray(fillvalue=FILL_A)
        assert decoded.dtype.isnative
        assert decoded[REAL_ZERO_ROW, REAL_ZERO_COL] == 0
        assert decoded[FILL_COINCIDENT_ROW, FILL_COINCIDENT_COL] == FILL_A


def test_no_state_inherited_across_opens(corpus: Corpus) -> None:
    with tifffile.TiffFile(corpus.truncated_a) as tif:
        tif.pages[PAGE_COUNT - 1].asarray(fillvalue=FILL_A)
    with tifffile.TiffFile(corpus.healthy_a) as tif:
        healthy = tif.asarray()
    numpy.testing.assert_array_equal(healthy, corpus.images)
    with tifffile.TiffFile(corpus.truncated_b) as tif:
        other = tif.pages[PAGE_COUNT - 1].asarray(fillvalue=FILL_B)
    expected, present, _ = raw_page_oracle(
        corpus.truncated_b, PAGE_COUNT - 1, FILL_B
    )
    numpy.testing.assert_array_equal(other[present], expected[present])
    assert (other[~present] == FILL_B).all()


# --------------------------------------------------------------------------
# reproducible chain: the counterexample only fails across operations
# --------------------------------------------------------------------------


def run_recovery_chain(corpus: Corpus) -> None:
    """Execute the interleaved operation sequence with positional checks."""
    shared_output = marked_output()

    # step 1: per-page read of layout A with fill value F1
    with tifffile.TiffFile(corpus.truncated_a) as tif:
        page_three = tif.pages[PAGE_COUNT - 1]
        step1 = page_three.asarray(
            out=shared_output, fillvalue=FILL_A, maxworkers=1
        )
    assert_matches_oracle(
        step1, corpus.truncated_a, FILL_A, step='step1 per-page A F1'
    )

    # step 2: batch reads in different orders into the same marked array
    with tifffile.TiffFile(corpus.truncated_a) as tif:
        page_three = tif.pages[PAGE_COUNT - 1]
        step2_disk = assemble_segments(
            page_three,
            shared_output,
            sort=True,
            fillvalue=FILL_A,
            maxworkers=1,
            buffersize=16,
        )
        assert_matches_oracle(
            step2_disk,
            corpus.truncated_a,
            FILL_A,
            step='step2 disk-order batch A',
        )
        step2_logical = assemble_segments(
            page_three,
            shared_output,
            sort=False,
            fillvalue=FILL_A,
            maxworkers=1,
            buffersize=None,
        )
        assert_matches_oracle(
            step2_logical,
            corpus.truncated_a,
            FILL_A,
            step='step2 logical-order batch A',
        )

    # step 3: a new thread first reads page three of A, then batch reads page
    # three of layout B with fill value F2 and another buffer setting. B is
    # big endian (non-native where layout A is little endian).
    thread_result: dict[str, numpy.ndarray] = {}

    def worker() -> None:
        with tifffile.TiffFile(corpus.truncated_a) as tif:
            prior = tif.pages[PAGE_COUNT - 1].asarray(fillvalue=FILL_A)
            assert prior.shape == (IMAGE_HEIGHT, IMAGE_WIDTH)
        output_b = marked_output()
        with tifffile.TiffFile(corpus.truncated_b) as tif:
            page_b = tif.pages[PAGE_COUNT - 1]
            step3 = assemble_segments(
                page_b,
                output_b,
                sort=True,
                fillvalue=FILL_B,
                maxworkers=2,
                buffersize=ROW_BYTES,
            )
        thread_result['step3'] = step3

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert_matches_oracle(
        thread_result['step3'],
        corpus.truncated_b,
        FILL_B,
        step='step3 threaded batch B F2',
    )
    _, _, rows_a = raw_page_oracle(corpus.truncated_a, PAGE_COUNT - 1, FILL_A)
    _, _, rows_b = raw_page_oracle(corpus.truncated_b, PAGE_COUNT - 1, FILL_B)
    assert rows_a != rows_b

    # step 4: return to the original file, reusing the original output with
    # the other fill value, then verify the healthy pages again
    with tifffile.TiffFile(corpus.truncated_a) as tif:
        step4 = tif.pages[PAGE_COUNT - 1].asarray(
            out=shared_output, fillvalue=FILL_B, maxworkers=2
        )
        assert_matches_oracle(
            step4,
            corpus.truncated_a,
            FILL_B,
            step='step4 per-page A F2 reused output',
        )
        healthy_pages = tif.asarray()[: PAGE_COUNT - 1]
    for page in range(PAGE_COUNT - 1):
        numpy.testing.assert_array_equal(
            healthy_pages[page],
            corpus.images[page],
            err_msg=f'step4 healthy page {page} changed by output reuse',
        )

    # the sequence entry must also work on reused storage
    sequence = tifffile.TiffSequence([corpus.truncated_a, corpus.truncated_b])
    sequence_output = numpy.full(
        (2, PAGE_COUNT, IMAGE_HEIGHT, IMAGE_WIDTH),
        OUTPUT_MARKER,
        dtype=numpy.uint16,
    )
    sequenced = sequence.asarray(
        out=sequence_output,
        imreadargs={'fillvalue': FILL_A, 'maxworkers': 1},
    )
    assert numpy.shares_memory(sequenced, sequence_output)
    assert OUTPUT_MARKER not in sequenced


def test_recovery_chain_passes_on_library(corpus: Corpus) -> None:
    run_recovery_chain(corpus)


# --------------------------------------------------------------------------
# reversible, test-process-only fault injections
# --------------------------------------------------------------------------


def _page_is_truncated(page: TiffPage) -> bool:
    """Return True if any declared segment extends past the physical file."""
    file_size = page.parent.filehandle._size
    return any(
        offset + byte_count > file_size
        for offset, byte_count in zip(
            page.dataoffsets, page.databytecounts, strict=True
        )
    )


class StaleFillDouble:
    """Leak the previous fill value only when a reused output switches fill.

    Fresh outputs, single operations, and reads keeping the same fill value
    are unaffected. Array shape, sample count, and log records are unchanged.
    """

    def __init__(self) -> None:
        self._original = TiffPage.asarray
        self._last_fill: dict[int, numpy.generic] = {}

    def __enter__(self) -> 'StaleFillDouble':
        def asarray_double(
            page: TiffPage, *args: Any, **kwargs: Any
        ) -> numpy.ndarray:
            result = self._original(page, *args, **kwargs)
            out = kwargs.get('out')
            fill = page._fillvalue(kwargs.get('fillvalue'))
            if (
                isinstance(out, numpy.ndarray)
                and page.index == PAGE_COUNT - 1
                and _page_is_truncated(page)
            ):
                previous = self._last_fill.get(id(out))
                if previous is not None and previous != fill:
                    result[result == fill] = previous
                self._last_fill[id(out)] = fill
            return result

        TiffPage.asarray = asarray_double  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: object) -> None:
        TiffPage.asarray = self._original  # type: ignore[method-assign]


class StaleStripMapDouble:
    """Reuse the previous page-three strip map when batch reading another file.

    Only armed after page three of one file has been read and a subsequent
    segment batch reads page three of a different file, mirroring the chain's
    thread boundary. Sample counts, shapes, and log levels are untouched; only
    logical positions move.
    """

    def __init__(self) -> None:
        self._original = TiffPage.segments
        self._state = threading.local()

    def __enter__(self) -> 'StaleStripMapDouble':
        def segments_double(
            page: TiffPage, *args: Any, **kwargs: Any
        ) -> Any:
            state = self._state
            filename = os.path.basename(page.parent.filehandle._name)
            armed = (
                page.index == PAGE_COUNT - 1 and _page_is_truncated(page)
            )
            previous_rows = getattr(state, 'rows', None)
            previous_name = getattr(state, 'name', None)
            captured_rows: list[int] = []
            # consume decode results directly even when the caller passes a
            # func callback (TiffPage.asarray does), then replay it ourselves
            func = kwargs.pop('func', None)
            for position, result in enumerate(
                self._original(page, *args, **kwargs)
            ):
                segment, indices, shape = result
                if armed:
                    captured_rows.append(indices[2])
                    if (
                        previous_rows is not None
                        and previous_name != filename
                        and position < len(previous_rows)
                    ):
                        indices = (
                            indices[0],
                            indices[1],
                            previous_rows[position],
                            indices[3],
                            indices[4],
                        )
                decoded = (segment, indices, shape)
                if func is not None:
                    func(decoded)
                    yield None
                else:
                    yield decoded
            if armed:
                state.rows = captured_rows
                state.name = filename

        TiffPage.segments = segments_double  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: object) -> None:
        TiffPage.segments = self._original  # type: ignore[method-assign]


def test_stale_fill_double_caught_only_in_chain(corpus: Corpus) -> None:
    # healthy controls and each isolated operation still pass under the double
    with StaleFillDouble():
        test_healthy_files_match_physical_bytes(corpus)
        output = marked_output()
        with tifffile.TiffFile(corpus.truncated_a) as tif:
            once = tif.pages[PAGE_COUNT - 1].asarray(
                out=output, fillvalue=FILL_A
            )
        assert_matches_oracle(
            once, corpus.truncated_a, FILL_A, step='double isolated F1'
        )
        with pytest.raises(AssertionError, match='step4'):
            run_recovery_chain(corpus)


def test_stale_strip_map_double_caught_only_in_chain(corpus: Corpus) -> None:
    with StaleStripMapDouble():
        test_healthy_files_match_physical_bytes(corpus)
        with tifffile.TiffFile(corpus.truncated_a) as tif:
            page = tif.pages[PAGE_COUNT - 1]
            single = marked_output()
            assemble_segments(
                page,
                single,
                sort=True,
                fillvalue=FILL_A,
                maxworkers=1,
                buffersize=16,
            )
        assert_matches_oracle(
            single,
            corpus.truncated_a,
            FILL_A,
            step='map double isolated batch',
        )
        with pytest.raises(AssertionError, match='step3'):
            run_recovery_chain(corpus)


def test_doubles_preserve_shape_count_and_log_level(
    corpus: Corpus, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger='tifffile')

    def observe() -> tuple[tuple[int, ...], int, int]:
        caplog.clear()
        with tifffile.TiffFile(corpus.truncated_a) as tif:
            image = tif.pages[PAGE_COUNT - 1].asarray(fillvalue=FILL_A)
        error_count = sum(
            1
            for record in caplog.records
            if record.name == 'tifffile' and record.levelno == logging.ERROR
        )
        return image.shape, int(image.size), error_count

    baseline = observe()
    with StaleFillDouble():
        assert observe() == baseline
    with StaleStripMapDouble():
        assert observe() == baseline


# --------------------------------------------------------------------------
# geometry variations: strip order, last strip height, cut position
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('height', 'rows_per_strip', 'disk_order', 'cut_bytes'),
    [
        (8, 2, (2, 0, 3, 1), 5),
        (12, 3, (3, 1, 2, 0), 3),
        (12, 3, (0, 2, 1, 3), 7),
        # last strip is shorter: heights 3, 3, 3, 1
        (10, 3, (3, 1, 2, 0), 5),
    ],
)
def test_oracle_holds_for_varied_geometry(
    tmp_path: Any,
    height: int,
    rows_per_strip: int,
    disk_order: tuple[int, ...],
    cut_bytes: int,
) -> None:
    strip_count = (height + rows_per_strip - 1) // rows_per_strip
    assert len(disk_order) == strip_count
    images = numpy.zeros(
        (PAGE_COUNT, height, IMAGE_WIDTH), dtype=numpy.uint16
    )
    canonical = reference_images()
    images[:, :IMAGE_HEIGHT] = canonical
    for row in range(IMAGE_HEIGHT, height):
        for col in range(IMAGE_WIDTH):
            images[:, row, col] = reference_pixel(0, row, col)

    healthy_path = str(tmp_path / f'geo_{height}_{rows_per_strip}.tif')
    write_tiff(
        healthy_path,
        images=images,
        byteorder=LAYOUT_A_ENDIAN,
        disk_order=disk_order,
        rows_per_strip=rows_per_strip,
    )
    cut = truncation_length(
        healthy_path,
        PAGE_COUNT - 1,
        disk_order,
        cut_bytes,
    )
    truncated_path = str(tmp_path / f'geo_{height}_{rows_per_strip}.trunc.tif')
    write_tiff(
        truncated_path,
        images=images,
        byteorder=LAYOUT_A_ENDIAN,
        disk_order=disk_order,
        rows_per_strip=rows_per_strip,
        nbytes=cut,
    )

    expected, present, flagged = raw_page_oracle(
        truncated_path, PAGE_COUNT - 1, FILL_B
    )
    assert flagged
    with tifffile.TiffFile(truncated_path) as tif:
        actual = tif.pages[PAGE_COUNT - 1].asarray(
            fillvalue=FILL_B, maxworkers=2, buffersize=ROW_BYTES
        )
    assert actual.shape == (height, IMAGE_WIDTH)
    numpy.testing.assert_array_equal(actual[present], expected[present])
    assert (actual[~present] == FILL_B).all()
    # the truncated last-written strip is among the flagged strips
    assert disk_order[-1] * rows_per_strip in flagged
