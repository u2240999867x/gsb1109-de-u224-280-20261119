"""An xarray backend over the CDF-5 diagnostics, with byte-accurate auditing.

The backend deliberately mirrors the contract of a real NetCDF store:

* ``get_variables`` exposes lazy backend arrays; it does not read data.
* Every data read goes through ``Reader.read`` and is recorded with logical
  positions, physical byte range, block IDs, and phase (dtype inference,
  default-index creation, caller window, load).
* Opening the file parses only the header.

This lets us compare xarray's *online* behaviour (what a caller can observe
from header + block extrema + one window) against *offline* full decode.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from xarray.backends import BackendEntrypoint
from xarray.backends.common import AbstractDataStore, BackendArray
from xarray.backends.store import StoreBackendEntrypoint
from xarray.core import indexing
from xarray.core.utils import FrozenDict
from xarray.core.variable import Variable

from . import cdf5

OPENED: list[cdf5.Reader] = []

# The caller window must be known *before* xarray performs any read.  Set this
# immediately before calling xr.open_dataset; the backend copies it onto the
# new reader so dtype-inference and index-creation reads are audited too.
PENDING_WINDOW: tuple[int, int] | None = None


class CDF5Array(BackendArray):
    def __init__(self, reader: cdf5.Reader, name: str) -> None:
        self.reader = reader
        self.name = name
        v = reader.variables[name]
        self.shape = v.shape
        self.dtype = v.dtype

    def __getitem__(self, key: indexing.ExplicitIndexer) -> np.ndarray:
        return indexing.explicit_indexing_adapter(
            key, self.shape, indexing.IndexingSupport.OUTER, self._getitem
        )

    def _getitem(self, key: tuple[Any, ...]) -> np.ndarray:
        if not key:
            return self.reader.read(self.name, slice(None))
        if len(key) != 1:
            raise IndexError(f"diagnostic variables are 1-D, got key {key!r}")
        return self.reader.read(self.name, key[0])


class CDF5Store(AbstractDataStore):
    def __init__(self, reader: cdf5.Reader) -> None:
        self.reader = reader

    def get_variables(self) -> FrozenDict:
        return FrozenDict(
            (
                name,
                Variable(
                    tuple(v.dims),
                    indexing.LazilyIndexedArray(CDF5Array(self.reader, name)),
                    attrs=dict(self.reader.visible_attrs(name)),
                ),
            )
            for name, v in self.reader.variables.items()
        )

    def get_attrs(self) -> FrozenDict:
        return FrozenDict(dict(self.reader.global_attrs))

    def get_dimensions(self) -> dict[str, int]:
        return dict(self.reader.dimensions)

    def get_encoding(self) -> dict[str, set[str]]:
        return {"unlimited_dims": set()}


class CDF5Backend(BackendEntrypoint):
    description = "Read-tracked CDF-5 diagnostics (netCDF classic model, version 5)"
    url = ""

    open_dataset_parameters = (
        "filename_or_obj",
        "drop_variables",
        "mask_and_scale",
        "decode_times",
        "concat_characters",
        "decode_coords",
        "use_cftime",
        "decode_timedelta",
    )

    def open_dataset(
        self,
        filename_or_obj,
        *,
        drop_variables=None,
        mask_and_scale: bool = True,
        decode_times=True,
        concat_characters: bool = True,
        decode_coords: bool = True,
        use_cftime: bool | None = None,
        decode_timedelta: bool | None = None,
    ):
        reader = cdf5.Reader.open(filename_or_obj)
        reader.window = PENDING_WINDOW
        OPENED.append(reader)
        store = CDF5Store(reader)
        ds = StoreBackendEntrypoint().open_dataset(
            store,
            drop_variables=drop_variables,
            mask_and_scale=mask_and_scale,
            decode_times=decode_times,
            concat_characters=concat_characters,
            decode_coords=decode_coords,
            use_cftime=use_cftime,
            decode_timedelta=decode_timedelta,
        )
        return ds
