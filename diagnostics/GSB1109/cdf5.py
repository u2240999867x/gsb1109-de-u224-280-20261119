"""Minimal CDF-5 (NetCDF classic model, version 5) reader/writer with
byte-accurate read accounting.

Why CDF-5: the archive contract stores signed 64-bit nanosecond offsets.
NetCDF-3 classic (CDF-1/CDF-2) has no int64 type, and this host has no
netCDF4/h5py stack.  CDF-5 is the classic-model format that adds NC_INT64
(nc_type 10), so the on-disk integers stay exact.

Only the fixed-size (non-record) subset needed by the diagnostic is
implemented.  All integers are big-endian; names and attribute values are
padded to a 4-byte boundary, as in the classic format.

The reader never touches data bytes while parsing the header.  Every data
access is recorded with the requested logical positions and the physical byte
range actually fetched, so an online audit can prove whether a library read
outside a caller's window.
"""

from __future__ import annotations

import hashlib
import json
import struct
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

MAGIC = b"CDF"
VERSION = 5

NC_BYTE = 1
NC_CHAR = 2
NC_SHORT = 3
NC_INT = 4
NC_FLOAT = 5
NC_DOUBLE = 6
NC_UBYTE = 7
NC_USHORT = 8
NC_UINT = 9
NC_INT64 = 10
NC_UINT64 = 11

NC_DIMENSION = b"\x00\x00\x00\x0a"
NC_VARIABLE = b"\x00\x00\x00\x0b"
NC_ATTRIBUTE = b"\x00\x00\x00\x0c"
ZERO4 = b"\x00\x00\x00\x00"

_TYPE_TO_NC: dict[np.dtype, int] = {
    np.dtype("i1"): NC_BYTE,
    np.dtype("i2"): NC_SHORT,
    np.dtype("i4"): NC_INT,
    np.dtype("i8"): NC_INT64,
    np.dtype("u1"): NC_UBYTE,
    np.dtype("u2"): NC_USHORT,
    np.dtype("u4"): NC_UINT,
    np.dtype("u8"): NC_UINT64,
    np.dtype("f4"): NC_FLOAT,
    np.dtype("f8"): NC_DOUBLE,
}
_NC_TO_DTYPE: dict[int, np.dtype] = {v: k for k, v in _TYPE_TO_NC.items()}

# Attributes that describe archive block structure.  They are part of the
# archive's block table, not of the semantic file header exposed online.
BLOCK_STRUCT_ATTRS = (
    "archive_block_start",
    "archive_block_stop",
    "archive_block_min",
    "archive_block_max",
    "archive_block_offset",
    "archive_block_order",
)


def _pad4(n: int) -> int:
    return (-n) % 4


def _be(dtype: np.dtype) -> np.dtype:
    return dtype.newbyteorder(">")


def _encode_name(name: str) -> bytes:
    raw = name.encode("utf-8")
    return struct.pack(">q", len(raw)) + raw + b"\x00" * _pad4(len(raw))


def _decode_name(f) -> str:
    (n,) = struct.unpack(">q", f.read(8))
    raw = f.read(n)
    f.read(_pad4(n))
    return raw.decode("utf-8")


def _encode_values(values: np.ndarray) -> bytes:
    raw = values.astype(_be(values.dtype), copy=False).tobytes()
    return raw + b"\x00" * _pad4(len(raw))


def _attr_type_and_array(value: Any) -> tuple[int, np.ndarray]:
    if isinstance(value, str):
        return NC_CHAR, np.frombuffer(value.encode("utf-8"), dtype="u1")
    if isinstance(value, (bytes, bytearray)):
        return NC_CHAR, np.frombuffer(bytes(value), dtype="u1")
    if isinstance(value, (list, tuple)):
        value = np.asarray(value)
    if not isinstance(value, np.ndarray):
        value = np.asarray(value)

    if value.dtype.kind in "US":
        raw = value.astype(str).tobytes() if value.dtype.kind == "U" else value.tobytes()
        return NC_CHAR, np.frombuffer(raw, dtype="u1")
    if value.dtype.kind == "b":
        value = value.astype("i1")
    if value.dtype.kind == "i":
        if value.dtype.itemsize > 8:
            raise ValueError(f"integer attribute too wide: {value.dtype}")
        if value.size == 0:
            return NC_INT64, value.astype("i8")
        if value.dtype.itemsize == 8:
            return NC_INT64, value.astype("i8")
        if value.min() < -(2**31) or value.max() > 2**31 - 1:
            return NC_INT64, value.astype("i8")
        return NC_INT, value.astype("i4")
    if value.dtype.kind == "u":
        if value.dtype.itemsize == 8:
            return NC_UINT64, value.astype("u8")
        if value.max(initial=0) > 2**31 - 1:
            return NC_UINT, value.astype("u4")
        return NC_INT, value.astype("i4")
    if value.dtype.kind == "f":
        if value.dtype.itemsize <= 4:
            return NC_FLOAT, value.astype("f4")
        return NC_DOUBLE, value.astype("f8")
    raise TypeError(f"unsupported attribute type: {value.dtype}")


def _encode_att(name: str, value: Any) -> bytes:
    nc_type, arr = _attr_type_and_array(value)
    return (
        _encode_name(name)
        + struct.pack(">i", nc_type)
        + struct.pack(">q", arr.size)
        + _encode_values(arr)
    )


def _encode_att_list(attrs: dict[str, Any] | None) -> bytes:
    if not attrs:
        return ZERO4 + ZERO4
    parts = [NC_ATTRIBUTE, struct.pack(">q", len(attrs))]
    for name, value in attrs.items():
        parts.append(_encode_att(name, value))
    return b"".join(parts)


def _encode_var_header(
    name: str,
    dimids: list[int],
    attrs: dict[str, Any],
    nc_type: int,
    vsize: int,
    begin: int,
    ndims: int,
) -> bytes:
    return (
        _encode_name(name)
        + struct.pack(">q", ndims)
        + b"".join(struct.pack(">q", d) for d in dimids)
        + _encode_att_list(attrs)
        + struct.pack(">i", nc_type)
        + struct.pack(">q", vsize)
        + struct.pack(">q", begin)
    )


@dataclass
class VarHeader:
    name: str
    dims: tuple[str, ...]
    attrs: dict[str, Any]
    dtype: np.dtype
    shape: tuple[int, ...]
    begin: int
    vsize: int


@dataclass
class ReadEvent:
    seq: int
    phase: str
    var: str
    mode: str
    logical_first: int
    logical_last: int
    logical_count: int
    byte_first: int
    byte_last: int
    block_ids: tuple[int, ...]
    inside_window: bool
    first_outside_logical: int | None
    first_outside_byte: int | None
    admissible: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "phase": self.phase,
            "var": self.var,
            "mode": self.mode,
            "logical_first": self.logical_first,
            "logical_last": self.logical_last,
            "logical_count": self.logical_count,
            "byte_first": self.byte_first,
            "byte_last": self.byte_last,
            "block_ids": list(self.block_ids),
            "inside_window": self.inside_window,
            "first_outside_logical": self.first_outside_logical,
            "first_outside_byte": self.first_outside_byte,
            "admissible": self.admissible,
        }


@dataclass
class Reader:
    path: str
    dimensions: dict[str, int] = field(default_factory=dict)
    global_attrs: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, VarHeader] = field(default_factory=dict)
    header_bytes: int = 0
    events: list[ReadEvent] = field(default_factory=list)
    window: tuple[int, int] | None = None
    phase_override: str | None = None
    recording: bool = True

    # ---------- header ----------
    @classmethod
    def open(cls, path: str | Path) -> "Reader":
        self = cls(path=str(path))
        with open(self.path, "rb") as f:
            magic = f.read(3)
            if magic != MAGIC:
                raise ValueError(f"{path}: not a CDF file (magic={magic!r})")
            version = f.read(1)
            if version != bytes([VERSION]):
                raise ValueError(f"{path}: expected CDF-5, got version {version!r}")
            (numrecs,) = struct.unpack(">q", f.read(8))
            if numrecs != 0:
                raise ValueError("only fixed-size variables are supported")

            self.dimensions = self._read_dim_list(f)
            self.global_attrs = self._read_att_list(f)

            tag = f.read(4)
            if tag == ZERO4:
                if f.read(4) != ZERO4:
                    raise ValueError("malformed empty variable list")
                self.header_bytes = f.tell()
                return self
            if tag != NC_VARIABLE:
                raise ValueError(f"unexpected variable list tag {tag!r}")
            (count,) = struct.unpack(">q", f.read(8))
            dim_names = list(self.dimensions)
            for _ in range(count):
                name = _decode_name(f)
                (ndims,) = struct.unpack(">q", f.read(8))
                dimids = [struct.unpack(">q", f.read(8))[0] for _ in range(ndims)]
                dims = tuple(dim_names[i] for i in dimids)
                attrs = self._read_att_list(f)
                (nc_type,) = struct.unpack(">i", f.read(4))
                (vsize,) = struct.unpack(">q", f.read(8))
                (begin,) = struct.unpack(">q", f.read(8))
                if nc_type not in _NC_TO_DTYPE:
                    raise ValueError(f"unsupported nc_type {nc_type} for {name}")
                dtype = _NC_TO_DTYPE[nc_type]
                shape = tuple(self.dimensions[d] for d in dims)
                self.variables[name] = VarHeader(
                    name=name,
                    dims=dims,
                    attrs=attrs,
                    dtype=dtype,
                    shape=shape,
                    begin=begin,
                    vsize=vsize,
                )
            self.header_bytes = f.tell()
        return self

    @staticmethod
    def _read_dim_list(f) -> dict[str, int]:
        tag = f.read(4)
        if tag == ZERO4:
            # An absent list is the 8-byte sentinel ZERO ZERO, not a count.
            if f.read(4) != ZERO4:
                raise ValueError("malformed empty dimension list")
            return {}
        if tag != NC_DIMENSION:
            raise ValueError(f"unexpected dimension list tag {tag!r}")
        (count,) = struct.unpack(">q", f.read(8))
        dims: dict[str, int] = {}
        for _ in range(count):
            name = _decode_name(f)
            (length,) = struct.unpack(">q", f.read(8))
            dims[name] = length
        return dims

    @staticmethod
    def _read_att_list(f) -> dict[str, Any]:
        tag = f.read(4)
        if tag == ZERO4:
            if f.read(4) != ZERO4:
                raise ValueError("malformed empty attribute list")
            return {}
        if tag != NC_ATTRIBUTE:
            raise ValueError(f"unexpected attribute list tag {tag!r}")
        (count,) = struct.unpack(">q", f.read(8))
        attrs: dict[str, Any] = {}
        for _ in range(count):
            name = _decode_name(f)
            (nc_type,) = struct.unpack(">i", f.read(4))
            (nelems,) = struct.unpack(">q", f.read(8))
            if nc_type == NC_CHAR:
                raw = f.read(nelems)
                f.read(_pad4(nelems))
                attrs[name] = raw.decode("utf-8")
            else:
                dtype = _NC_TO_DTYPE[nc_type]
                nbytes = nelems * dtype.itemsize
                raw = f.read(nbytes)
                f.read(_pad4(nbytes))
                arr = np.frombuffer(raw, dtype=_be(dtype)).copy().astype(dtype, copy=False)
                attrs[name] = arr[0] if arr.size == 1 else arr
        return attrs

    # ---------- block table ----------
    def block_starts_stops(self, var: str) -> tuple[list[int], list[int]]:
        v = self.variables[var]
        starts = np.atleast_1d(v.attrs["archive_block_start"]).astype(int).tolist()
        stops = np.atleast_1d(v.attrs["archive_block_stop"]).astype(int).tolist()
        return starts, stops

    def block_offsets(self, var: str) -> list[int]:
        v = self.variables[var]
        starts, stops = self.block_starts_stops(var)
        offsets = v.attrs.get("archive_block_offset")
        if offsets is None:
            out = []
            pos = 0
            for a, b in zip(starts, stops, strict=True):
                out.append(pos)
                pos += (b - a) * v.dtype.itemsize
            return out
        return np.atleast_1d(offsets).astype(int).tolist()

    def block_extrema(self, var: str) -> list[dict[str, Any]]:
        v = self.variables[var]
        starts, stops = self.block_starts_stops(var)
        mins = np.atleast_1d(v.attrs.get("archive_block_min")).tolist()
        maxs = np.atleast_1d(v.attrs.get("archive_block_max")).tolist()
        return [
            {"block": i, "start": a, "stop": b, "min": int(mins[i]), "max": int(maxs[i])}
            for i, (a, b) in enumerate(zip(starts, stops, strict=True))
        ]

    # ---------- data ----------
    def _block_ids(self, var: str, first: int, last: int) -> tuple[int, ...]:
        starts, stops = self.block_starts_stops(var)
        return tuple(
            i
            for i, (a, b) in enumerate(zip(starts, stops, strict=True))
            if b > first and a <= last
        )

    def _widen_to_window(self, var: str, lo: int, hi: int) -> tuple[bool, int | None, int | None]:
        """Decide whether [lo, hi] stays in the caller window.

        Returns (inside, first_outside_logical, first_outside_byte).  The
        reported byte is where an actual read first touched data outside the
        window, not the file offset of the offending logical position (which
        would be wrong when blocks are physically permuted).
        """
        if self.window is None:
            return True, None, None
        w0, w1 = self.window
        if hi < w0:
            return False, hi, None
        if lo >= w1:
            return False, lo, None
        # The read straddles a window edge; the first out-of-window logical
        # position is w1 when it starts inside, or w0-1 when it starts before.
        if lo < w0:
            return False, lo, None
        return False, w1, None

    def _current_phase(self) -> str:
        if self.phase_override is not None:
            return self.phase_override
        names = {fr.name for fr in traceback.extract_stack()}
        if names & {"_decode_cf_datetime_dtype", "first_n_items", "last_item"}:
            return "dtype_inference"
        if names & {
            "_maybe_create_default_indexes",
            "create_default_index_implicit",
            "from_variables",
        }:
            return "index_creation"
        if names & {"load", "compute", "_load"}:
            return "load"
        return "other"

    def _record(
        self,
        var: str,
        mode: str,
        block: int,
        first: int,
        last: int,
        byte_first: int,
        byte_last: int,
    ) -> None:
        if not self.recording:
            return
        phase = self._current_phase()
        isz = self.variables[var].dtype.itemsize
        inside = True
        first_out_logical = None
        first_out_byte = None
        if self.window is not None:
            w0, w1 = self.window
            if first < w0:
                # Positions below the window are the first unaffordable data.
                inside = False
                first_out_logical = first
                first_out_byte = byte_first
            elif last >= w1:
                inside = False
                first_out_logical = max(first, w1)
                first_out_byte = byte_first + (first_out_logical - first) * isz
        self.events.append(
            ReadEvent(
                seq=len(self.events),
                phase=phase,
                var=var,
                mode=mode,
                logical_first=first,
                logical_last=last,
                logical_count=last - first + 1,
                byte_first=byte_first,
                byte_last=byte_last,
                block_ids=(block,),
                inside_window=inside,
                first_outside_logical=first_out_logical,
                first_outside_byte=first_out_byte,
                admissible=inside,
            )
        )

    def _locate(self, var: str, i: int) -> tuple[int, int]:
        """Return (block, physical byte offset) for logical index i."""
        v = self.variables[var]
        starts, stops = self.block_starts_stops(var)
        offsets = self.block_offsets(var)
        blk = int(np.searchsorted(np.asarray(stops), i, side="right"))
        blk = min(blk, len(starts) - 1)
        return blk, v.begin + offsets[blk] + (i - starts[blk]) * v.dtype.itemsize

    def _read_block(
        self, var: str, block: int, lo: int, hi: int, mode: str
    ) -> np.ndarray:
        """Read the inclusive logical range [lo, hi] of one logical block."""
        v = self.variables[var]
        isz = v.dtype.itemsize
        byte_first = v.begin + self.block_offsets(var)[block] + (
            lo - self.block_starts_stops(var)[0][block]
        ) * isz
        byte_last = byte_first + (hi - lo + 1) * isz
        with open(self.path, "rb") as f:
            f.seek(byte_first)
            raw = f.read(byte_last - byte_first)
        self._record(var, mode, block, lo, hi, byte_first, byte_last)
        return np.frombuffer(raw, dtype=_be(v.dtype)).astype(v.dtype, copy=False)

    def read(self, var: str, key: Any) -> np.ndarray:
        v = self.variables[var]
        if len(v.shape) != 1:
            raise ValueError("diagnostic variables are one-dimensional")
        n = v.shape[0]
        isz = v.dtype.itemsize
        if isinstance(key, tuple):
            if len(key) != 1:
                raise IndexError(key)
            key = key[0]
        if isinstance(key, slice):
            start, stop, step = key.indices(n)
            if step == 1:
                if stop <= start:
                    return np.empty(0, dtype=v.dtype)
                return self._read_range(var, start, stop)
            return self.read(var, np.arange(start, stop, step, dtype=np.int64))
        if isinstance(key, (int, np.integer)):
            i = int(key)
            if i < 0:
                i += n
            if i < 0 or i >= n:
                raise IndexError(i)
            blk, _ = self._locate(var, i)
            return self._read_block(var, blk, i, i, "scalar")[:1]
        idx = np.asarray(key)
        if idx.dtype.kind == "b":
            idx = np.flatnonzero(idx)
        idx = idx.astype(np.int64)
        if idx.size == 0:
            return np.empty(0, dtype=v.dtype)
        idx = np.where(idx < 0, idx + n, idx)
        if idx.min() < 0 or idx.max() >= n:
            raise IndexError(int(idx.min() if idx.min() < 0 else idx.max()))
        starts, stops = self.block_starts_stops(var)
        block_of = np.clip(np.searchsorted(np.asarray(stops), idx, side="right"), 0, len(starts) - 1)
        out = np.empty(idx.size, dtype=v.dtype)
        for blk in np.unique(block_of):
            sel = block_of == blk
            sub = idx[sel]
            lo, hi = int(sub.min()), int(sub.max())
            span = self._read_block(var, blk, lo, hi, "fancy")
            out[sel] = span[sub - lo]
        return out

    def _read_range(self, var: str, start: int, stop: int) -> np.ndarray:
        v = self.variables[var]
        starts, stops = self.block_starts_stops(var)
        pieces = []
        for blk, (a, b) in enumerate(zip(starts, stops, strict=True)):
            lo, hi = max(start, a), min(stop, b)
            if hi <= lo:
                continue
            pieces.append(self._read_block(var, blk, lo, hi - 1, "slice"))
        if not pieces:
            return np.empty(0, dtype=v.dtype)
        return np.concatenate(pieces)

    def read_all(self, var: str, phase: str = "offline_full") -> np.ndarray:
        old = self.phase_override
        self.phase_override = phase
        try:
            return self.read(var, slice(None))
        finally:
            self.phase_override = old

    # ---------- summaries ----------
    def visible_attrs(self, var: str) -> dict[str, Any]:
        return {
            k: v
            for k, v in self.variables[var].attrs.items()
            if k not in BLOCK_STRUCT_ATTRS
        }

    def catalog(self, window: tuple[int, int] | None = None, include_extrema: bool = True) -> dict[str, Any]:
        """The externally observable online summary.

        Header (dims, dtype, semantic attributes), per-block raw signed int64
        min/max, and one caller window.  Physical block offsets/order are
        deliberately excluded: they are storage details, not archive evidence.
        """
        out: dict[str, Any] = {
            "dimensions": dict(self.dimensions),
            "global_attrs": _jsonable(self.global_attrs),
            "variables": {},
        }
        for name, v in self.variables.items():
            entry: dict[str, Any] = {
                "dims": list(v.dims),
                "dtype": v.dtype.str,
                "attrs": _jsonable(self.visible_attrs(name)),
                "shape": list(v.shape),
                "header_bytes": self.header_bytes,
            }
            if include_extrema:
                entry["blocks"] = _jsonable(self.block_extrema(name))
            out["variables"][name] = entry
        if window is not None:
            old = self.recording
            self.recording = False
            try:
                raw = {
                    name: self.read(name, slice(*window)).tolist()
                    for name in self.variables
                    if self.variables[name].shape and self.variables[name].shape[0] > 0
                }
            finally:
                self.recording = old
            out["window"] = {"start": window[0], "stop": window[1], "raw": raw}
        return out

    def catalog_digest(self, window: tuple[int, int] | None = None) -> str:
        payload = json.dumps(self.catalog(window), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_jsonable(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and np.isnan(value):
        return "NaN"
    return value


# ---------------------------------------------------------------------------
# writer
# ---------------------------------------------------------------------------

@dataclass
class VariableSpec:
    dims: tuple[str, ...]
    data: np.ndarray
    attrs: dict[str, Any] = field(default_factory=dict)
    dtype: str | np.dtype | None = None


def write_cdf5(
    path: str | Path,
    *,
    dimensions: dict[str, int],
    variables: dict[str, VariableSpec],
    global_attrs: dict[str, Any] | None = None,
    block_order: dict[str, list[int]] | None = None,
) -> dict[str, Any]:
    """Write a fixed-size CDF-5 file.

    ``block_order`` permutes the *physical* placement of logical blocks without
    changing logical values, block boundaries, or block extrema.  The logical
    block -> physical offset map is stored in ``archive_block_offset`` so the
    reader still returns logical order.
    """
    dim_names = list(dimensions)
    prepared: dict[str, tuple[np.ndarray, np.dtype, dict[str, Any]]] = {}
    for name, spec in variables.items():
        arr = np.asarray(spec.data)
        dtype = np.dtype(spec.dtype) if spec.dtype is not None else arr.dtype
        arr = arr.astype(dtype, copy=False)
        if arr.ndim != len(spec.dims):
            raise ValueError(f"{name}: data ndim {arr.ndim} != dims {spec.dims}")
        attrs = dict(spec.attrs)
        if "archive_block_start" in attrs and "archive_block_stop" in attrs:
            starts = np.atleast_1d(attrs["archive_block_start"]).astype(int).tolist()
            stops = np.atleast_1d(attrs["archive_block_stop"]).astype(int).tolist()
        else:
            # Variables without an explicit block table are one block.  The
            # time axis always carries the real table in these cases.
            starts, stops = [0], [int(arr.shape[0])]
            attrs["archive_block_start"] = np.asarray(starts, dtype="i8")
            attrs["archive_block_stop"] = np.asarray(stops, dtype="i8")
        if not starts or not stops:
            raise ValueError(f"{name}: empty block table")
        if stops[-1] != arr.shape[0]:
            raise ValueError(f"{name}: block table does not cover the data")
        mins, maxs = [], []
        for a, b in zip(starts, stops, strict=True):
            chunk = arr[a:b]
            mins.append(int(chunk.min()))
            maxs.append(int(chunk.max()))
        attrs["archive_block_min"] = np.asarray(mins, dtype="i8")
        attrs["archive_block_max"] = np.asarray(maxs, dtype="i8")
        prepared[name] = (arr, dtype, attrs)

    def build_header(begins: dict[str, int], offsets: dict[str, list[int]]) -> bytes:
        parts: list[bytes] = [MAGIC, bytes([VERSION]), struct.pack(">q", 0)]
        if dimensions:
            parts.append(NC_DIMENSION)
            parts.append(struct.pack(">q", len(dimensions)))
            for dname, length in dimensions.items():
                parts.append(_encode_name(dname))
                parts.append(struct.pack(">q", length))
        else:
            parts.extend([ZERO4, ZERO4])
        parts.append(_encode_att_list(global_attrs))
        if variables:
            parts.append(NC_VARIABLE)
            parts.append(struct.pack(">q", len(variables)))
            for name, (arr, dtype, attrs) in prepared.items():
                nc_type = _TYPE_TO_NC[dtype]
                dimids = [dim_names.index(d) for d in variables[name].dims]
                vsize = int(np.prod(arr.shape)) * dtype.itemsize
                vsize += _pad4(vsize)
                attrs_with_offsets = dict(attrs)
                attrs_with_offsets["archive_block_offset"] = np.asarray(
                    offsets[name], dtype="i8"
                )
                parts.append(
                    _encode_var_header(
                        name,
                        dimids,
                        attrs_with_offsets,
                        nc_type,
                        vsize,
                        begins[name],
                        len(variables[name].dims),
                    )
                )
        else:
            parts.extend([ZERO4, ZERO4])
        return b"".join(parts)

    # Compute offsets for the requested physical order.
    offsets: dict[str, list[int]] = {}
    sizes: dict[str, list[int]] = {}
    for name, (arr, dtype, attrs) in prepared.items():
        starts = np.atleast_1d(attrs["archive_block_start"]).astype(int).tolist()
        stops = np.atleast_1d(attrs["archive_block_stop"]).astype(int).tolist()
        order = (block_order or {}).get(name, list(range(len(starts))))
        if sorted(order) != list(range(len(starts))):
            raise ValueError(f"{name}: block_order must be a permutation")
        per_block = [(stops[i] - starts[i]) * dtype.itemsize for i in range(len(starts))]
        offs = [0] * len(starts)
        pos = 0
        for slot, blk in enumerate(order):
            offs[blk] = pos
            pos += per_block[blk]
        offsets[name] = offs
        sizes[name] = per_block

    # Header length is stable because begin/offset fields are fixed width.
    header_len = len(build_header({name: 0 for name in prepared}, offsets))
    begins: dict[str, int] = {}
    cursor = header_len
    for name, (arr, dtype, _) in prepared.items():
        begins[name] = cursor
        cursor += int(np.prod(arr.shape)) * dtype.itemsize
        cursor += _pad4(cursor)
    header = build_header(begins, offsets)
    if len(header) != header_len:
        raise AssertionError("CDF-5 header length changed between passes")

    with open(path, "wb") as f:
        f.write(header)
        f.write(b"\x00" * (header_len - f.tell()))
        for name, (arr, dtype, attrs) in prepared.items():
            starts = np.atleast_1d(attrs["archive_block_start"]).astype(int).tolist()
            stops = np.atleast_1d(attrs["archive_block_stop"]).astype(int).tolist()
            for blk, (a, b) in enumerate(zip(starts, stops, strict=True)):
                f.seek(begins[name] + offsets[name][blk])
                f.write(arr[a:b].astype(_be(dtype), copy=False).tobytes())
            pad = _pad4(int(np.prod(arr.shape)) * dtype.itemsize)
            if pad:
                f.seek(begins[name] + int(np.prod(arr.shape)) * dtype.itemsize)
                f.write(b"\x00" * pad)
    return {"header_bytes": header_len, "begins": begins, "block_offsets": offsets}


def sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
