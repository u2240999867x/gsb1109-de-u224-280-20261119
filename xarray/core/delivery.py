"""Delivery manifest groupby aggregation.

This module implements summarizing instrument sampling records against a
*delivery manifest* (交付清单): an ordered list of report rows naming the
region/instrument combinations that may be reported, plus the regional
subtotal rows to deliver.

A sampling :py:class:`~xarray.Dataset` stores one record per element along a
1-D record axis: the reporting region, the instrument, the integer round
number, the integer reading (or a missing value), and the original file name
and line number.  Files carry consecutive archive sequence numbers; when the
files are concatenated in that order the round numbers never decrease, and a
single round may span any number of files.

``Dataset.groupby(manifest=...)`` returns a :py:class:`DeliveryGroupBy` whose
:py:meth:`DeliveryGroupBy.deliver` method reduces one file (or one contiguous
span of files) to a compact, mergeable segment :py:class:`~xarray.Dataset`.
Segments covering adjacent, non-overlapping archive ranges and built from the
same manifest can be combined with :py:func:`merge_delivery_segments` in any
order.  A human readable report and the list of out-of-manifest reports can
be viewed on any segment through the ``delivery`` accessor without consuming
it.

Except for the separately readable list of out-of-manifest reports, the size
of a segment does not grow with the number of fully processed rounds it
contains: completed rounds are folded into a constant-size summary per
subtotal row, and only the rounds still open at the two boundaries of the
segment are kept in detail.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

import numpy as np
import pandas as pd

from xarray.core.dataset import Dataset
from xarray.core.extensions import register_dataset_accessor
from xarray.namedarray.utils import is_duck_dask_array

_FORMAT_ATTR = "delivery_format"
_FORMAT_VERSION = "xarray-delivery/1"
_MANIFEST_ATTR = "delivery_manifest"
_ARCHIVE_START_ATTR = "delivery_archive_start"
_ARCHIVE_END_ATTR = "delivery_archive_end"

#: report status: the row has valid readings (and, for subtotals, qualifying rounds)
STATUS_OK = "ok"
#: report status: the row has no samples at all
STATUS_NO_SAMPLES = "no_samples"
#: report status: the row has samples, but every reading is missing
STATUS_ALL_INVALID = "all_invalid"
#: report status: the subtotal has valid readings but no qualifying round
STATUS_NO_QUALIFYING_ROUNDS = "no_qualifying_rounds"

_SUMMARY_KEYS = ("n", "first", "last", "prefix", "suffix", "longest", "total", "qual")

_SUMMARY_VARIABLES = {
    "n": "subtotal_round_count",
    "first": "subtotal_first_round",
    "last": "subtotal_last_round",
    "prefix": "subtotal_prefix_streak",
    "suffix": "subtotal_suffix_streak",
    "longest": "subtotal_longest_streak",
    "total": "subtotal_total",
    "qual": "subtotal_qualifying_rounds",
}


class DeliveryManifest:
    """An ordered delivery manifest (交付清单) for ``Dataset.groupby``.

    Parameters
    ----------
    rows : iterable of (str, str or None)
        Report rows in report order.  Each row is a ``(region, instrument)``
        tuple naming an allowed region/instrument combination, or
        ``(region, None)`` to request the subtotal row for a region.
    dim : str, default: "record"
        Name of the record dimension of the sampling datasets.
    region, instrument, round, reading, filename, line : str, optional
        Names of the variables holding the region, the instrument, the
        integer round number, the reading (missing values mark 缺测), the
        original file name and the original line number.

    Examples
    --------
    >>> manifest = DeliveryManifest(
    ...     [("南区", "乙"), ("北区", None), ("北区", "甲"), ("北区", "丙"), ("北区", "乙")]
    ... )
    >>> manifest.rows
    (('南区', '乙'), ('北区', None), ('北区', '甲'), ('北区', '丙'), ('北区', '乙'))
    """

    def __init__(
        self,
        rows: Iterable[tuple[str, str | None]],
        *,
        dim: str = "record",
        region: str = "region",
        instrument: str = "instrument",
        round: str = "round",
        reading: str = "reading",
        filename: str = "filename",
        line: str = "line",
    ) -> None:
        parsed: list[tuple[str, str | None]] = []
        seen: set[tuple[str, str | None]] = set()
        for row in rows:
            reg, ins = row
            if not isinstance(reg, str) or (
                ins is not None and not isinstance(ins, str)
            ):
                raise TypeError(
                    "manifest rows must be (region, instrument) pairs of strings, "
                    f"with None marking a subtotal row; got {row!r}"
                )
            key = (reg, ins)
            if key in seen:
                raise ValueError(f"duplicate manifest row: {row!r}")
            seen.add(key)
            parsed.append(key)
        if not parsed:
            raise ValueError("the delivery manifest must contain at least one row")
        self._rows = tuple(parsed)
        self.dim = dim
        self.columns = {
            "region": region,
            "instrument": instrument,
            "round": round,
            "reading": reading,
            "filename": filename,
            "line": line,
        }
        self._instrument_rows = tuple((r, i) for r, i in parsed if i is not None)
        self._subtotal_regions = tuple(r for r, i in parsed if i is None)
        self._key_to_index = {key: pos for pos, key in enumerate(self._instrument_rows)}
        self._subtotal_index = {
            reg: pos for pos, reg in enumerate(self._subtotal_regions)
        }
        # instrument row -> subtotal row of its region (or -1)
        self._instrument_subtotal = np.array(
            [self._subtotal_index.get(reg, -1) for reg, _ in self._instrument_rows],
            dtype=np.int64,
        )
        # subtotal row -> instrument rows of its region
        self._subtotal_instruments: tuple[tuple[int, ...], ...] = tuple(
            tuple(
                pos for pos, (reg, _) in enumerate(self._instrument_rows) if reg == sub
            )
            for sub in self._subtotal_regions
        )

    @property
    def rows(self) -> tuple[tuple[str, str | None], ...]:
        """Report rows in report order; ``None`` marks a subtotal row."""
        return self._rows

    @property
    def instrument_rows(self) -> tuple[tuple[str, str], ...]:
        """The allowed (region, instrument) combinations, in report order."""
        return self._instrument_rows

    @property
    def subtotal_regions(self) -> tuple[str, ...]:
        """The regions with a subtotal row, in report order."""
        return self._subtotal_regions

    def allows(self, region: str, instrument: str) -> bool:
        """Whether the (region, instrument) combination may be reported."""
        return (region, instrument) in self._key_to_index

    def to_json(self) -> str:
        """Serialize the report rows (used to compare manifests on merge)."""
        return json.dumps(list(self._rows), ensure_ascii=False)

    @classmethod
    def from_json(cls, payload: str, **kwargs: Any) -> DeliveryManifest:
        """Rebuild a manifest from :py:meth:`to_json` output."""
        rows = [(reg, ins) for reg, ins in json.loads(payload)]
        return cls(rows, **kwargs)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DeliveryManifest):
            return NotImplemented
        return self._rows == other._rows

    def __hash__(self) -> int:
        return hash(self._rows)

    def __repr__(self) -> str:
        rows = ", ".join(
            f"{reg}小计" if ins is None else f"{reg}{ins}" for reg, ins in self._rows
        )
        return f"DeliveryManifest([{rows}])"


def _as_manifest(
    manifest: DeliveryManifest | Iterable[tuple[str, str | None]],
) -> DeliveryManifest:
    if isinstance(manifest, DeliveryManifest):
        return manifest
    return DeliveryManifest(manifest)


class _RoundState:
    """Partial per-subtotal state of one round at a segment boundary."""

    __slots__ = ("mask", "number", "sums")

    def __init__(self, number: int, nsub: int, ninstr: int) -> None:
        self.number = number
        # mask[s, k]: instrument row k reported a valid reading for subtotal s
        self.mask = np.zeros((nsub, ninstr), dtype=bool)
        # sums[s]: valid readings of subtotal s in this round
        self.sums = np.zeros(nsub, dtype=np.int64)


def _empty_summary(nsub: int) -> dict[str, np.ndarray]:
    return {key: np.zeros(nsub, dtype=np.int64) for key in _SUMMARY_KEYS}


def _round_summary(state: _RoundState) -> dict[str, np.ndarray]:
    """Summary of a single, fully observed round."""
    nsub = state.sums.size
    qualified = state.mask.sum(axis=1) >= 2
    summary = _empty_summary(nsub)
    summary["n"][:] = 1
    summary["first"][:] = state.number
    summary["last"][:] = state.number
    summary["prefix"] = qualified.astype(np.int64)
    summary["suffix"] = qualified.astype(np.int64)
    summary["longest"] = qualified.astype(np.int64)
    summary["qual"] = qualified.astype(np.int64)
    summary["total"] = np.where(qualified, state.sums, np.int64(0))
    return summary


def _combine_summaries(
    left: dict[str, np.ndarray], right: dict[str, np.ndarray]
) -> dict[str, np.ndarray]:
    """Associatively combine the summaries of two consecutive round sequences."""
    left_empty = left["n"] == 0
    right_empty = right["n"] == 0
    connect = (
        (right["first"] == left["last"] + 1)
        & (left["suffix"] > 0)
        & (right["prefix"] > 0)
    )
    out = {
        "total": left["total"] + right["total"],
        "qual": left["qual"] + right["qual"],
        "n": left["n"] + right["n"],
        "first": np.where(left_empty, right["first"], left["first"]),
        "last": np.where(right_empty, left["last"], right["last"]),
    }
    joined = np.where(connect, left["suffix"] + right["prefix"], np.int64(0))
    out["longest"] = np.maximum(np.maximum(left["longest"], right["longest"]), joined)
    prefix = np.where(
        connect & (left["prefix"] == left["n"]),
        left["n"] + right["prefix"],
        left["prefix"],
    )
    suffix = np.where(
        connect & (right["suffix"] == right["n"]),
        right["n"] + left["suffix"],
        right["suffix"],
    )
    out["prefix"] = np.where(left_empty, right["prefix"], prefix)
    out["suffix"] = np.where(right_empty, left["suffix"], suffix)
    return out


def _combine_rounds(first: _RoundState, second: _RoundState) -> _RoundState:
    """Merge two partial observations of the same round."""
    if first.number != second.number:
        raise ValueError("can only combine partial states of the same round")
    combined = _RoundState(first.number, first.sums.size, first.mask.shape[1])
    combined.mask = first.mask | second.mask
    combined.sums = first.sums + second.sums
    return combined


class _SegmentBuilder:
    """Streaming reducer turning record blocks into a segment dataset."""

    def __init__(self, manifest: DeliveryManifest) -> None:
        self._manifest = manifest
        ninstr = len(manifest.instrument_rows)
        nsub = len(manifest.subtotal_regions)
        self._ninstr = ninstr
        self._nsub = nsub
        self._instr_total = np.zeros(ninstr, dtype=np.int64)
        self._instr_raw = np.zeros(ninstr, dtype=np.int64)
        self._instr_valid = np.zeros(ninstr, dtype=np.int64)
        self._middle = _empty_summary(nsub)
        self._head: _RoundState | None = None
        self._tail: _RoundState | None = None
        self._open: _RoundState | None = None
        self._errors: dict[str, list[Any]] = {
            "filename": [],
            "line": [],
            "region": [],
            "instrument": [],
            "reading": [],
        }

    def update(
        self,
        region: np.ndarray,
        instrument: np.ndarray,
        rounds: np.ndarray,
        reading: np.ndarray,
        filename: np.ndarray,
        line: np.ndarray,
    ) -> None:
        manifest = self._manifest
        n = rounds.shape[0]
        if n == 0:
            return
        if rounds.dtype.kind not in "iu":
            raise ValueError(
                f"round numbers in variable {manifest.columns['round']!r} must be integers"
            )
        rounds = rounds.astype(np.int64)
        if n > 1 and bool((rounds[1:] < rounds[:-1]).any()):
            raise ValueError(
                "round numbers must be non-decreasing along the record axis"
            )
        if self._open is not None and int(rounds[0]) < self._open.number:
            raise ValueError(
                "round numbers must be non-decreasing across record blocks"
            )
        keys = np.fromiter(
            (
                manifest._key_to_index.get((reg, ins), -1)
                for reg, ins in zip(region, instrument, strict=True)
            ),
            dtype=np.int64,
            count=n,
        )
        out_of_manifest = keys < 0
        if out_of_manifest.any():
            (bad,) = np.nonzero(out_of_manifest)
            self._errors["filename"].extend(str(v) for v in filename[bad])
            self._errors["line"].extend(int(v) for v in line[bad])
            self._errors["region"].extend(str(v) for v in region[bad])
            self._errors["instrument"].extend(str(v) for v in instrument[bad])
            self._errors["reading"].extend(
                float(v) if not pd.isna(v) else np.nan for v in reading[bad]
            )
        valid_reading = ~pd.isna(reading)
        # split the block into runs of equal round numbers
        change = np.flatnonzero(rounds[1:] != rounds[:-1]) + 1
        starts = np.concatenate(([0], change))
        stops = np.concatenate((change, [n]))
        for start, stop in zip(starts, stops, strict=True):
            number = int(rounds[start])
            if self._open is None or self._open.number != number:
                if self._open is not None:
                    self._place(self._open)
                self._open = _RoundState(number, self._nsub, self._ninstr)
            block_keys = keys[start:stop]
            keep = block_keys >= 0
            if not keep.any():
                continue
            kept = block_keys[keep]
            np.add.at(self._instr_raw, kept, 1)
            valid = valid_reading[start:stop] & keep
            if not valid.any():
                continue
            valid_keys = block_keys[valid]
            values = np.asarray(reading[start:stop][valid], dtype=np.int64)
            np.add.at(self._instr_valid, valid_keys, 1)
            np.add.at(self._instr_total, valid_keys, values)
            sub = manifest._instrument_subtotal[valid_keys]
            has_subtotal = sub >= 0
            if has_subtotal.any():
                sub = sub[has_subtotal]
                np.add.at(self._open.sums, sub, values[has_subtotal])
                np.logical_or.at(self._open.mask, (sub, valid_keys[has_subtotal]), True)

    def _place(self, state: _RoundState) -> None:
        if self._head is None:
            self._head = state
        elif self._tail is None:
            self._tail = state
        else:
            self._middle = _combine_summaries(self._middle, _round_summary(self._tail))
            self._tail = state

    def finish(self) -> None:
        if self._open is not None:
            self._place(self._open)
            self._open = None


def _segment_dataset(
    manifest: DeliveryManifest,
    instr_total: np.ndarray,
    instr_raw: np.ndarray,
    instr_valid: np.ndarray,
    middle: dict[str, np.ndarray],
    head: _RoundState | None,
    tail: _RoundState | None,
    errors: dict[str, list[Any]],
    archive_start: int,
    archive_end: int,
) -> Dataset:
    nsub = len(manifest.subtotal_regions)
    ninstr = len(manifest.instrument_rows)

    def round_vars(prefix: str, state: _RoundState | None) -> dict[str, Any]:
        if state is None:
            return {
                f"has_{prefix}": False,
                f"{prefix}_round": np.int64(0),
                f"{prefix}_instrument_mask": (
                    ("manifest_subtotal", "manifest_instrument"),
                    np.zeros((nsub, ninstr), dtype=bool),
                ),
                f"{prefix}_round_sum": (
                    ("manifest_subtotal",),
                    np.zeros(nsub, dtype=np.int64),
                ),
            }
        return {
            f"has_{prefix}": True,
            f"{prefix}_round": np.int64(state.number),
            f"{prefix}_instrument_mask": (
                ("manifest_subtotal", "manifest_instrument"),
                state.mask,
            ),
            f"{prefix}_round_sum": (("manifest_subtotal",), state.sums),
        }

    data_vars: dict[str, Any] = {
        "instrument_total": (("manifest_instrument",), instr_total),
        "instrument_raw_count": (("manifest_instrument",), instr_raw),
        "instrument_valid_count": (("manifest_instrument",), instr_valid),
        "error_filename": (("error",), np.array(errors["filename"], dtype=object)),
        "error_line": (("error",), np.array(errors["line"], dtype=np.int64)),
        "error_region": (("error",), np.array(errors["region"], dtype=object)),
        "error_instrument": (
            ("error",),
            np.array(errors["instrument"], dtype=object),
        ),
        "error_reading": (
            ("error",),
            np.array(errors["reading"], dtype=np.float64),
        ),
    }
    for key, var_name in _SUMMARY_VARIABLES.items():
        data_vars[var_name] = (("manifest_subtotal",), middle[key])
    data_vars.update(round_vars("head", head))
    data_vars.update(round_vars("tail", tail))

    coords = {
        "instrument_region": (
            ("manifest_instrument",),
            np.array([reg for reg, _ in manifest.instrument_rows], dtype=object),
        ),
        "instrument_name": (
            ("manifest_instrument",),
            np.array([ins for _, ins in manifest.instrument_rows], dtype=object),
        ),
        "subtotal_region": (
            ("manifest_subtotal",),
            np.array(manifest.subtotal_regions, dtype=object),
        ),
    }
    dataset = Dataset(data_vars, coords)
    dataset.attrs[_FORMAT_ATTR] = _FORMAT_VERSION
    dataset.attrs[_MANIFEST_ATTR] = manifest.to_json()
    dataset.attrs[_ARCHIVE_START_ATTR] = int(archive_start)
    dataset.attrs[_ARCHIVE_END_ATTR] = int(archive_end)
    return dataset


def _extract_summary(dataset: Dataset) -> dict[str, np.ndarray]:
    return {
        key: np.asarray(dataset[var_name].values, dtype=np.int64).copy()
        for key, var_name in _SUMMARY_VARIABLES.items()
    }


def _extract_round(dataset: Dataset, prefix: str) -> _RoundState | None:
    if not bool(dataset[f"has_{prefix}"]):
        return None
    mask = np.asarray(dataset[f"{prefix}_instrument_mask"].values, dtype=bool)
    state = _RoundState(int(dataset[f"{prefix}_round"]), mask.shape[0], mask.shape[1])
    state.mask = mask.copy()
    state.sums = np.asarray(
        dataset[f"{prefix}_round_sum"].values, dtype=np.int64
    ).copy()
    return state


def _extract_errors(dataset: Dataset) -> dict[str, list[Any]]:
    return {
        "filename": [str(v) for v in dataset["error_filename"].values],
        "line": [int(v) for v in dataset["error_line"].values],
        "region": [str(v) for v in dataset["error_region"].values],
        "instrument": [str(v) for v in dataset["error_instrument"].values],
        "reading": [float(v) for v in dataset["error_reading"].values],
    }


def _archive_span(dataset: Dataset) -> tuple[int, int]:
    return (
        int(dataset.attrs[_ARCHIVE_START_ATTR]),
        int(dataset.attrs[_ARCHIVE_END_ATTR]),
    )


def _require_segment(dataset: Dataset) -> None:
    if dataset.attrs.get(_FORMAT_ATTR) != _FORMAT_VERSION:
        raise ValueError(
            "expected a delivery segment produced by "
            "Dataset.groupby(manifest=...).deliver(...)"
        )


def _span_label(dataset: Dataset) -> str:
    start, end = _archive_span(dataset)
    return f"archive [{start}, {end}]"


def _merge_pair(first: Dataset, second: Dataset) -> Dataset:
    first_start, first_end = _archive_span(first)
    second_start, second_end = _archive_span(second)
    if second_start <= first_end:
        raise ValueError(
            "cannot merge delivery segments with overlapping archive ranges: "
            f"{_span_label(first)} and {_span_label(second)}"
        )
    if second_start > first_end + 1:
        raise ValueError(
            "cannot merge delivery segments with a gap in the archive sequence: "
            f"{_span_label(first)} and {_span_label(second)}"
        )
    if first.attrs.get(_MANIFEST_ATTR) != second.attrs.get(_MANIFEST_ATTR):
        raise ValueError(
            "cannot merge delivery segments built from different manifests: "
            f"{_span_label(first)} and {_span_label(second)}"
        )
    manifest = DeliveryManifest.from_json(first.attrs[_MANIFEST_ATTR])

    instr_total = first["instrument_total"].values + second["instrument_total"].values
    instr_raw = (
        first["instrument_raw_count"].values + second["instrument_raw_count"].values
    )
    instr_valid = (
        first["instrument_valid_count"].values + second["instrument_valid_count"].values
    )

    first_errors = _extract_errors(first)
    second_errors = _extract_errors(second)
    errors = {key: first_errors[key] + second_errors[key] for key in first_errors}

    first_head = _extract_round(first, "head")
    first_tail = _extract_round(first, "tail")
    second_head = _extract_round(second, "head")
    second_tail = _extract_round(second, "tail")
    first_middle = _extract_summary(first)
    second_middle = _extract_summary(second)

    if first_head is None:
        head, middle, tail = second_head, second_middle, second_tail
    elif second_head is None:
        head, middle, tail = first_head, first_middle, first_tail
    else:
        first_last = first_tail if first_tail is not None else first_head
        first_single = first_tail is None
        second_single = second_tail is None
        middle = first_middle
        if first_last.number == second_head.number:
            joined = _combine_rounds(first_last, second_head)
            head = first_head if not first_single else joined
            if second_single:
                tail = None if first_single else joined
            else:
                tail = second_tail
            if not first_single and not second_single:
                middle = _combine_summaries(middle, _round_summary(joined))
        else:
            if first_last.number > second_head.number:
                raise ValueError(
                    "round numbers decrease between adjacent delivery segments: "
                    f"{_span_label(first)} and {_span_label(second)}"
                )
            head = first_head
            tail = second_head if second_single else second_tail
            if not first_single:
                middle = _combine_summaries(middle, _round_summary(first_last))
            if not second_single:
                middle = _combine_summaries(middle, _round_summary(second_head))
        if not second_single:
            middle = _combine_summaries(middle, second_middle)

    return _segment_dataset(
        manifest,
        np.asarray(instr_total, dtype=np.int64),
        np.asarray(instr_raw, dtype=np.int64),
        np.asarray(instr_valid, dtype=np.int64),
        middle,
        head,
        tail,
        errors,
        first_start,
        second_end,
    )


def merge_delivery_segments(segments: Iterable[Dataset]) -> Dataset:
    """Merge delivery segments covering adjacent, non-overlapping archive ranges.

    The segments may be passed in any order; they are sorted by their archive
    range first.  Merging is associative, so any grouping of the same adjacent
    intervals yields the same result.

    Parameters
    ----------
    segments : iterable of Dataset
        Segments produced by ``Dataset.groupby(manifest=...).deliver(...)``.

    Returns
    -------
    Dataset
        A single segment covering the combined archive range.

    Raises
    ------
    ValueError
        If two segments overlap, are not adjacent in the archive sequence, or
        were built from different manifests.  The message names the two
        segments that cannot be merged.
    """
    prepared = list(segments)
    if not prepared:
        raise ValueError("need at least one delivery segment to merge")
    for segment in prepared:
        _require_segment(segment)
    prepared.sort(key=_archive_span)
    merged = prepared[0]
    for segment in prepared[1:]:
        merged = _merge_pair(merged, segment)
    return merged


def _iter_blocks(
    dataset: Dataset, dim: str, block_size: int | None
) -> Iterator[Dataset]:
    n = dataset.sizes[dim]
    if block_size is not None:
        if block_size <= 0:
            raise ValueError("block_size must be a positive integer")
        for start in range(0, n, block_size):
            yield dataset.isel({dim: slice(start, min(start + block_size, n))})
        return
    chunks = None
    for var in dataset.variables.values():
        if dim in var.dims and is_duck_dask_array(var.data):
            axis = var.get_axis_num(dim)
            chunks = var.chunks[axis]
            break
    if chunks is None:
        yield dataset
        return
    start = 0
    for size in chunks:
        block = dataset.isel({dim: slice(start, start + size)})
        yield block.compute()
        start += size


class DeliveryGroupBy:
    """Object returned by ``Dataset.groupby(manifest=...)``.

    Use :py:meth:`deliver` to reduce the records of one file (or one
    contiguous span of files) to a mergeable segment dataset.
    """

    def __init__(
        self,
        dataset: Dataset,
        manifest: DeliveryManifest | Iterable[tuple[str, str | None]],
    ) -> None:
        self._dataset = dataset
        self._manifest = _as_manifest(manifest)

    @property
    def manifest(self) -> DeliveryManifest:
        """The delivery manifest this grouping summarizes against."""
        return self._manifest

    def deliver(
        self,
        archive: int | tuple[int, int] = 0,
        *,
        block_size: int | None = None,
    ) -> Dataset:
        """Reduce the records to a mergeable segment dataset.

        Records are processed block by block, so neither the whole sampling
        dataset nor a round spanning several blocks is ever held in memory at
        once.  When the dataset was opened with chunks, its chunks define the
        blocks.

        Parameters
        ----------
        archive : int or (int, int), default: 0
            Archive sequence number of the delivered file, or the inclusive
            ``(first, last)`` sequence numbers of a contiguous span of files.
            Adjacent segments must have adjacent, non-overlapping ranges to
            be mergeable.
        block_size : int, optional
            Process the records in blocks of this many rows instead of using
            the dataset's own chunks.

        Returns
        -------
        Dataset
            A segment whose size does not grow with the number of fully
            processed rounds (the out-of-manifest list excepted).  View the
            report and the out-of-manifest list through the ``delivery``
            accessor, and merge adjacent segments with
            :py:func:`merge_delivery_segments`.
        """
        if isinstance(archive, tuple):
            archive_start, archive_end = archive
        else:
            archive_start = archive_end = archive
        archive_start = int(archive_start)
        archive_end = int(archive_end)
        if archive_end < archive_start:
            raise ValueError(
                f"archive range end {archive_end} is before its start {archive_start}"
            )
        manifest = self._manifest
        dataset = self._dataset
        dim = manifest.dim
        if dim not in dataset.sizes:
            raise ValueError(f"the sampling dataset has no record dimension {dim!r}")
        missing = [
            name for name in manifest.columns.values() if name not in dataset.variables
        ]
        if missing:
            raise ValueError(
                f"the sampling dataset is missing record variables: {missing}"
            )
        for name in manifest.columns.values():
            if dataset[name].dims != (dim,):
                raise ValueError(
                    f"record variable {name!r} must be one-dimensional along {dim!r}"
                )
        builder = _SegmentBuilder(manifest)
        for block in _iter_blocks(dataset, dim, block_size):
            builder.update(
                np.asarray(block[manifest.columns["region"]].values, dtype=object),
                np.asarray(block[manifest.columns["instrument"]].values, dtype=object),
                np.asarray(block[manifest.columns["round"]].values),
                np.asarray(block[manifest.columns["reading"]].values),
                np.asarray(block[manifest.columns["filename"]].values, dtype=object),
                np.asarray(block[manifest.columns["line"]].values),
            )
        builder.finish()
        return _segment_dataset(
            manifest,
            builder._instr_total,
            builder._instr_raw,
            builder._instr_valid,
            builder._middle,
            builder._head,
            builder._tail,
            builder._errors,
            archive_start,
            archive_end,
        )

    def __repr__(self) -> str:
        return f"<DeliveryGroupBy, manifest={self._manifest!r}>"


def _report(dataset: Dataset) -> Dataset:
    _require_segment(dataset)
    manifest = DeliveryManifest.from_json(dataset.attrs[_MANIFEST_ATTR])
    nsub = len(manifest.subtotal_regions)
    middle = _extract_summary(dataset)
    head = _extract_round(dataset, "head")
    tail = _extract_round(dataset, "tail")
    summary = _empty_summary(nsub)
    if head is not None:
        summary = _combine_summaries(summary, _round_summary(head))
    summary = _combine_summaries(summary, middle)
    if tail is not None:
        summary = _combine_summaries(summary, _round_summary(tail))

    instr_total = dataset["instrument_total"].values
    instr_raw = dataset["instrument_raw_count"].values
    instr_valid = dataset["instrument_valid_count"].values

    nrows = len(manifest.rows)
    region_labels: list[str] = []
    instrument_labels: list[str | None] = []
    is_subtotal = np.zeros(nrows, dtype=bool)
    total = np.full(nrows, np.nan)
    raw_count = np.zeros(nrows, dtype=np.int64)
    valid_count = np.zeros(nrows, dtype=np.int64)
    qualifying = np.full(nrows, np.nan)
    longest = np.full(nrows, np.nan)
    status: list[str] = []

    instr_pos = {key: pos for pos, key in enumerate(manifest.instrument_rows)}
    sub_pos = {reg: pos for pos, reg in enumerate(manifest.subtotal_regions)}
    for row, (reg, ins) in enumerate(manifest.rows):
        region_labels.append(reg)
        instrument_labels.append(ins)
        if ins is None:
            is_subtotal[row] = True
            s = sub_pos[reg]
            members = manifest._subtotal_instruments[s]
            raw = int(instr_raw[list(members)].sum()) if members else 0
            valid = int(instr_valid[list(members)].sum()) if members else 0
            raw_count[row] = raw
            valid_count[row] = valid
            qualifying[row] = summary["qual"][s]
            longest[row] = summary["longest"][s]
            if summary["qual"][s] > 0:
                total[row] = float(summary["total"][s])
                status.append(STATUS_OK)
            elif valid > 0:
                status.append(STATUS_NO_QUALIFYING_ROUNDS)
            elif raw > 0:
                status.append(STATUS_ALL_INVALID)
            else:
                status.append(STATUS_NO_SAMPLES)
        else:
            k = instr_pos[(reg, ins)]
            raw_count[row] = instr_raw[k]
            valid_count[row] = instr_valid[k]
            if instr_valid[k] > 0:
                total[row] = float(instr_total[k])
                status.append(STATUS_OK)
            elif instr_raw[k] > 0:
                status.append(STATUS_ALL_INVALID)
            else:
                status.append(STATUS_NO_SAMPLES)

    report = Dataset(
        {
            "total": (("row",), total),
            "raw_count": (("row",), raw_count),
            "valid_count": (("row",), valid_count),
            "qualifying_rounds": (("row",), qualifying),
            "longest_consecutive_rounds": (("row",), longest),
            "is_subtotal": (("row",), is_subtotal),
            "status": (("row",), np.array(status, dtype=object)),
        },
        coords={
            "region": (("row",), np.array(region_labels, dtype=object)),
            "instrument": (
                ("row",),
                np.array(instrument_labels, dtype=object),
            ),
        },
    )
    return report


def _errors(dataset: Dataset) -> Dataset:
    _require_segment(dataset)
    return Dataset(
        {
            "filename": dataset["error_filename"],
            "line": dataset["error_line"],
            "region": dataset["error_region"],
            "instrument": dataset["error_instrument"],
            "reading": dataset["error_reading"],
        }
    )


@register_dataset_accessor("delivery")
class DeliveryAccessor:
    """View and merge delivery segments produced by ``DeliveryGroupBy.deliver``."""

    def __init__(self, dataset: Dataset) -> None:
        self._dataset = dataset

    @property
    def is_segment(self) -> bool:
        """Whether this dataset is a delivery segment."""
        return self._dataset.attrs.get(_FORMAT_ATTR) == _FORMAT_VERSION

    def _require(self) -> None:
        _require_segment(self._dataset)

    @property
    def manifest(self) -> DeliveryManifest:
        """The delivery manifest the segment was built from."""
        self._require()
        return DeliveryManifest.from_json(self._dataset.attrs[_MANIFEST_ATTR])

    @property
    def archive_span(self) -> tuple[int, int]:
        """The inclusive archive sequence range covered by the segment."""
        self._require()
        return _archive_span(self._dataset)

    @property
    def report(self) -> Dataset:
        """The delivery report based only on this segment.

        Viewing the report does not consume the segment; it can still be
        merged afterwards.
        """
        self._require()
        return _report(self._dataset)

    @property
    def errors(self) -> Dataset:
        """The out-of-manifest reports, with original file name and line."""
        self._require()
        return _errors(self._dataset)

    def merge(self, other: Dataset) -> Dataset:
        """Merge with another segment; see :py:func:`merge_delivery_segments`."""
        return merge_delivery_segments([self._dataset, other])
