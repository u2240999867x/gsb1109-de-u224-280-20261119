from __future__ import annotations

import bisect
import functools
from collections.abc import Callable
from operator import getitem
from typing import Any

import numpy as np
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype, is_numeric_dtype
from tlz import unique

from dask.dataframe import methods
from dask.dataframe.core import _concat, _map_freq_to_period_start, split_evenly
from dask.dataframe.dask_expr._expr import (
    Expr,
    Filter,
    Projection,
    plain_column_projection,
)
from dask.dataframe.dask_expr._reductions import TotalMemoryUsageFrame
from dask.dataframe.dask_expr._util import LRU
from dask.dataframe.utils import is_series_like
from dask.tokenize import tokenize
from dask.utils import iter_chunks, parse_bytes


class Repartition(Expr):
    """Abstract repartitioning expression"""

    _parameters = [
        "frame",
        "new_partitions",
        "new_divisions",
        "force",
        "partition_size",
    ]
    _defaults = {
        "new_partitions": None,
        "new_divisions": None,
        "force": False,
        "partition_size": None,
    }
    _is_length_preserving = True
    _filter_passthrough = True

    @functools.cached_property
    def _meta(self):
        return self.frame._meta

    def _divisions(self):
        if (
            self.operand("new_partitions") is not None
            or self.partition_size is not None
        ):
            x = self.optimize(fuse=False)
            return x._divisions()
        return self.new_divisions

    @property
    def npartitions(self):
        if (
            "new_partitions" in self._parameters
            and self.operand("new_partitions") is not None
        ):
            new_partitions = self.operand("new_partitions")
            if isinstance(new_partitions, Callable):
                return new_partitions(self.frame.npartitions)
            return new_partitions
        return super().npartitions

    @functools.cached_property
    def unique_partition_mapping_columns_from_shuffle(self):
        if (
            "new_partitions" in self._parameters
            and self.operand("new_partitions") is not None
            and self.npartitions <= self.frame.npartitions
        ):
            return self.frame.unique_partition_mapping_columns_from_shuffle
        else:
            return set()

    def _lower(self):
        if type(self) != Repartition:
            # This lower logic should not be inherited
            return None
        if self.operand("new_partitions") is not None:
            if self.new_partitions < self.frame.npartitions:
                return RepartitionToFewer(self.frame, self.operand("new_partitions"))
            elif self.new_partitions == self.frame.npartitions:
                # Remove if partitions are equal
                return self.frame
            else:
                original_divisions = divisions = pd.Series(
                    self.frame.divisions
                ).drop_duplicates()
                if self.frame.known_divisions and (
                    is_datetime64_any_dtype(divisions.dtype)
                    or is_numeric_dtype(divisions.dtype)
                ):
                    npartitions = self.new_partitions
                    df = self.frame
                    if is_datetime64_any_dtype(divisions.dtype):
                        divisions = divisions.values.astype("float64")

                    if is_series_like(divisions):
                        divisions = divisions.values

                    n = len(divisions)
                    divisions = np.interp(
                        x=np.linspace(0, n, npartitions + 1),
                        xp=np.linspace(0, n, n),
                        fp=divisions,
                    )
                    if is_datetime64_any_dtype(original_divisions.dtype):
                        divisions = methods.tolist(
                            pd.Series(divisions).astype(original_divisions.dtype)
                        )
                    elif np.issubdtype(original_divisions.dtype, np.integer):
                        divisions = divisions.astype(original_divisions.dtype)

                    if isinstance(divisions, np.ndarray):
                        divisions = divisions.tolist()

                    divisions = list(divisions)
                    divisions[0] = df.divisions[0]
                    divisions[-1] = df.divisions[-1]

                    # Ensure the computed divisions are unique
                    divisions = list(unique(divisions[:-1])) + [divisions[-1]]
                    return RepartitionDivisions(df, divisions, self.force)
                else:
                    return RepartitionToMore(self.frame, self.operand("new_partitions"))
        elif self.new_divisions:
            if tuple(self.new_divisions) == self.frame.divisions:
                return self.frame
            elif self.frame.divisions[0] is None:
                raise ValueError(
                    "Cannot repartition on divisions with unknown divisions"
                )
            return RepartitionDivisions(self.frame, self.new_divisions, self.force)
        elif self.partition_size is not None:
            return RepartitionSize(self.frame, partition_size=self.partition_size)
        else:
            raise NotImplementedError()

    def _simplify_up(self, parent, dependents):
        if isinstance(parent, Filter) and self._filter_passthrough_available(
            parent, dependents
        ):
            if self._name == parent.predicate._name:
                # We shouldn't push through the predicate, these pushdowns should
                # always come from frame.
                return
            return self._filter_simplification(parent)
        if isinstance(parent, Projection):
            return plain_column_projection(self, parent, dependents)

    @functools.cached_property
    def new_partitions(self):
        return (
            self.operand("new_partitions")(self.frame.npartitions)
            if isinstance(self.operand("new_partitions"), Callable)
            else self.operand("new_partitions")
        )


class RepartitionToFewer(Repartition):
    """Reduce the partition count"""

    _parameters = ["frame", "new_partitions"]

    def _divisions(self):
        return tuple(self.frame.divisions[i] for i in self._partitions_boundaries)

    @staticmethod
    def _compute_partition_boundaries(n_new_partitions, n_old_partitions):
        npartitions_ratio = n_old_partitions / n_new_partitions
        new_partitions_boundaries = [
            int(new_partition_index * npartitions_ratio)
            for new_partition_index in range(n_new_partitions + 1)
        ]
        return _clean_new_division_boundaries(
            new_partitions_boundaries, n_old_partitions
        )

    @functools.cached_property
    def _partitions_boundaries(self):
        npartitions = self.new_partitions
        npartitions_input = self.frame.npartitions
        assert npartitions_input > npartitions
        return self._compute_partition_boundaries(npartitions, npartitions_input)

    def _layer(self):
        new_partitions_boundaries = self._partitions_boundaries
        return {
            (self._name, i): (
                _concat,
                [(self.frame._name, j) for j in range(start, end)],
            )
            for i, (start, end) in enumerate(
                zip(new_partitions_boundaries, new_partitions_boundaries[1:])
            )
        }


class RepartitionToMore(Repartition):
    """Increase the partition count"""

    _parameters = ["frame", "new_partitions"]

    def _divisions(self):
        return (None,) * (1 + sum(self._nsplits))

    @functools.cached_property
    def _nsplits(self):
        df = self.frame
        div, mod = divmod(self.new_partitions, df.npartitions)
        nsplits = [div] * df.npartitions
        nsplits[-1] += mod
        if len(nsplits) != df.npartitions:
            raise ValueError(f"nsplits should have len={df.npartitions}")
        return nsplits

    def _layer(self):
        dsk = {}
        nsplits = self._nsplits
        df = self.frame
        new_name = self._name
        split_name = f"split-{new_name}"
        j = 0
        for i, k in enumerate(nsplits):
            if k == 1:
                dsk[new_name, j] = (df._name, i)
                j += 1
            else:
                dsk[split_name, i] = (split_evenly, (df._name, i), k)
                for jj in range(k):
                    dsk[new_name, j] = (getitem, (split_name, i), jj)
                    j += 1
        return dsk


class RepartitionDivisions(Repartition):
    """Repartition to specific divisions"""

    _parameters = ["frame", "new_divisions", "force"]
    _defaults = {"force": False}

    def _divisions(self):
        return self.new_divisions

    def _layer(self):
        token = self._name.split("-")[-1]
        a = self.frame.divisions
        b = self.new_divisions
        name = self.frame._name
        out1 = f"repartition-split-{token}"
        out2 = self._name
        force = self.force

        if len(b) < 2:
            # minimum division is 2 elements, like [0, 0]
            raise ValueError("New division must be longer than 2 elements")

        if force:
            if a[0] < b[0]:
                msg = (
                    "left side of the new division must be equal or smaller "
                    "than old division"
                )
                raise ValueError(msg)
            if a[-1] > b[-1]:
                msg = (
                    "right side of the new division must be equal or larger "
                    "than old division"
                )
                raise ValueError(msg)
        else:
            if a[0] != b[0]:
                msg = "left side of old and new divisions are different"
                raise ValueError(msg)
            if a[-1] != b[-1]:
                msg = "right side of old and new divisions are different"
                raise ValueError(msg)

        d = dict()
        k = 0  # index for temp splits
        nout = len(b) - 1
        last_out = nout - 1
        splits = [[] for _ in range(nout)]

        # Slice every input partition independently against the boundaries of
        # the output partitions it overlaps. A partition covers ``[a[i],
        # a[i + 1]]``, so rows equal to an output boundary are taken from the
        # output partition that starts at that boundary. This keeps duplicate
        # labels that sit on a boundary (possibly spread over both neighbouring
        # input partitions) exactly once, and concatenating splits in input
        # partition order preserves the original order of equal labels.
        for i in range(self.frame.npartitions):
            low, high = a[i], a[i + 1]
            j = bisect.bisect_right(b, low) - 1
            j = min(max(j, 0), last_out)
            while j <= last_out and b[j] <= high:
                # ``high`` is inclusive here: rows equal to the upper bound of
                # an input partition may live in that partition. Include them
                # when they are not the (exclusive) start of the next output
                # partition.
                right_boundary = j == last_out or high < b[j + 1]
                d[(out1, k)] = (
                    methods.boundary_slice,
                    (name, i),
                    b[j],
                    b[j + 1],
                    right_boundary,
                )
                splits[j].append((out1, k))
                k += 1
                if j == last_out or high < b[j + 1]:
                    break
                j += 1

        for j, tmp in enumerate(splits):
            if len(tmp) == 0:
                # dummy slice to return empty DataFrame or Series,
                # which retain original data attributes (columns / name)
                d[(out2, j)] = (
                    methods.boundary_slice,
                    (name, 0),
                    a[0],
                    a[0],
                    False,
                )
            elif len(tmp) == 1:
                d[(out2, j)] = tmp[0]
            else:
                d[(out2, j)] = (methods.concat, tmp)
        return d


class RepartitionFreq(Repartition):
    _parameters = ["frame", "freq"]

    def _divisions(self):
        freq = _map_freq_to_period_start(self.freq)

        try:
            start = self.frame.divisions[0].ceil(freq)
        except ValueError:
            start = self.frame.divisions[0]
        divisions = methods.tolist(
            pd.date_range(start=start, end=self.frame.divisions[-1], freq=freq)
        )
        if not len(divisions):
            divisions = [self.frame.divisions[0], self.frame.divisions[-1]]
        else:
            divisions.append(self.frame.divisions[-1])
            if divisions[0] != self.frame.divisions[0]:
                divisions = [self.frame.divisions[0]] + divisions
        return divisions

    def _lower(self):
        if not isinstance(self.frame.divisions[0], pd.Timestamp):
            raise TypeError("Can only repartition on frequency for timeseries")
        return RepartitionDivisions(self.frame, self._divisions())


class RepartitionSize(Repartition):

    @functools.cached_property
    def _size(self):
        size = self.operand("partition_size")
        if isinstance(size, str):
            size = parse_bytes(size)
        return int(size)

    @functools.cached_property
    def _mem_usage(self):
        return _get_mem_usages(self.frame)

    @functools.cached_property
    def _nsplits(self):
        return 1 + self._mem_usage // self._size

    @functools.cached_property
    def _partition_boundaries(self):
        nsplits = self._nsplits
        mem_usages = self._mem_usage

        if np.any(nsplits > 1):
            split_mem_usages = []
            for n, usage in zip(nsplits, mem_usages):
                split_mem_usages.extend([usage / n] * n)
            mem_usages = pd.Series(split_mem_usages)

        assert np.all(mem_usages <= self._size)
        new_npartitions = list(map(len, iter_chunks(mem_usages, self._size)))
        new_partitions_boundaries = np.cumsum(new_npartitions)
        return _clean_new_division_boundaries(
            new_partitions_boundaries, self.frame.npartitions
        )

    def _divisions(self):
        if np.any(self._nsplits > 1):
            return (None,) * len(self._partition_boundaries)
        return (self.frame.divisions[i] for i in self._partition_boundaries)

    def _lower(self):
        # populate cache
        self._mem_usage
        return super()._lower()

    def _layer(self) -> dict:
        df = self.frame
        dsk: dict[tuple, Any] = {}

        if np.any(self._nsplits > 1):
            split_name = f"split-{tokenize(df, self._nsplits)}"
            new_name = f"repartition-split-{self._size}-{tokenize(df)}"
            j = 0
            for i, k in enumerate(self._nsplits):
                if k == 1:
                    dsk[new_name, j] = (df._name, i)
                    j += 1
                else:
                    dsk[split_name, i] = (split_evenly, (df._name, i), k)
                    for jj in range(k):
                        dsk[new_name, j] = (getitem, (split_name, i), jj)
                        j += 1
        else:
            new_name = self.frame._name

        dsk.update(
            {
                (self._name, i): (
                    methods.concat,
                    [(new_name, j) for j in range(start, end)],
                )
                for i, (start, end) in enumerate(
                    zip(self._partition_boundaries, self._partition_boundaries[1:])
                )
            }
        )
        return dsk


def _clean_new_division_boundaries(new_partitions_boundaries, frame_npartitions):
    if not isinstance(new_partitions_boundaries, list):
        new_partitions_boundaries = list(new_partitions_boundaries)
    if new_partitions_boundaries[0] > 0:
        new_partitions_boundaries.insert(0, 0)
    if new_partitions_boundaries[-1] < frame_npartitions:
        new_partitions_boundaries[-1] = frame_npartitions
    return new_partitions_boundaries


mem_usages_lru = LRU(10)  # type: ignore[var-annotated]


def _get_mem_usages(frame):
    if frame._name in mem_usages_lru:
        return mem_usages_lru[frame._name]
    result = _compute_mem_usages(frame)
    mem_usages_lru[frame._name] = result
    return result


def _compute_mem_usages(frame):
    from dask.dataframe.dask_expr._collection import new_collection

    return new_collection(TotalMemoryUsageFrame(frame, deep=True)).compute()
