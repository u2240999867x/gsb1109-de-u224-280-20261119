"""Run the online/offline diagnostic and emit machine-readable evidence.

Outputs
-------
reports/diagnostic.json   full evidence (reads, dtypes, values, conclusions)
reports/summary.txt       human-readable compact summary

The analysis never uses a value obtained by a read that left the caller's
window as evidence for an archive decision.  Such reads are still reported,
because their existence is itself an observation about the library.
"""

from __future__ import annotations

import json
import math
import sys
import traceback
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

import xarray as xr
from xarray.coders import CFDatetimeCoder

from . import cases, cdf5
from . import backend
from .backend import CDF5Backend, OPENED

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
REPORTS = HERE / "reports"

UNITS = ("s", "ms", "us", "ns")
UNIT_NS = {"s": 10**9, "ms": 10**6, "us": 10**3, "ns": 1}
REF = cases.REF


def _dtype_unit(units: str) -> str:
    return {
        "nanoseconds": "ns",
        "microseconds": "us",
        "milliseconds": "ms",
        "seconds": "s",
        "minutes": "m",
        "hours": "h",
        "days": "D",
    }[units]


def _encode(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return [_encode(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return _encode(value.item())
    if isinstance(value, (np.datetime64,)):
        return str(value)
    if isinstance(value, (np.timedelta64,)):
        return f"{value.astype('int64')} ns"
    if isinstance(value, float) and np.isnan(value):
        return "NaN"
    if isinstance(value, float) and np.isnat(np.datetime64("NaT")):
        pass
    try:
        import cftime

        if isinstance(value, cftime.datetime):
            return f"{value} [{value.calendar}]"
    except Exception:
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _open(
    path: Path, window: tuple[int, int] | None, **kwargs: Any
) -> tuple[xr.Dataset | None, cdf5.Reader | None, str | None]:
    OPENED.clear()
    backend.PENDING_WINDOW = window
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ds = xr.open_dataset(path, engine=CDF5Backend, cache=False, **kwargs)
        ds.attrs["_warnings"] = [str(w.message) for w in caught]
        return ds, OPENED[-1], None
    except Exception as exc:  # noqa: BLE001 - the failure is part of the evidence
        reader = OPENED[-1] if OPENED else None
        return None, reader, f"{type(exc).__name__}: {exc}"
    finally:
        backend.PENDING_WINDOW = None


def _reads(reader: cdf5.Reader | None) -> list[dict[str, Any]]:
    return [] if reader is None else [e.as_dict() for e in reader.events]


def _earliest_outside(reader: cdf5.Reader | None) -> dict[str, Any] | None:
    if reader is None:
        return None
    bad = [e for e in reader.events if not e.inside_window]
    if not bad:
        return None
    first = bad[0]
    # The earliest physical byte that actually left the window, independent of
    # logical ordering and of physical block permutation.
    by_byte = min(bad, key=lambda e: e.first_outside_byte or e.byte_first)
    by_phase: dict[str, dict[str, Any]] = {}
    for e in bad:
        by_phase.setdefault(e.phase, e.as_dict())
    index_bad = [e for e in bad if e.phase == "index_creation"]
    return {
        "by_sequence": first.as_dict(),
        "by_physical_byte": by_byte.as_dict(),
        "by_phase": by_phase,
        "earliest_index_creation_outside": index_bad[0].as_dict() if index_bad else None,
        "budget_exceeded": True,
        "budget_rule": "the caller window is [start, stop); any read whose logical span is not contained in it exceeds the acquisition budget",
        "evidence_use": "forbidden: this read left the caller window and its values must not be used for archive conclusions",
    }


def _window_call(ds: xr.Dataset, case: cases.Case) -> dict[str, Any]:
    dim = "record" if case.layout == "aux" else "time"
    start, stop = case.window
    sub = ds.isel({dim: slice(start, stop)})
    vals = sub.time.values
    raw_window = _raw_exact(DATA / f"{case.name}_{case.layout}.cdf5")[start:stop]
    raw_expected = raw_window * _storage_multiplier(case.units)
    measured = _raw_measure(vals)
    if measured is not None and vals.dtype.kind in "iu":
        # Raw integer mode returns storage units; bring it to ns for comparison.
        measured = measured * _storage_multiplier(case.units)
    deviation = None
    if measured is not None:
        deviation = [int(a) - int(b) for a, b in zip(measured.tolist(), raw_expected.tolist(), strict=True)]
    fill_count = 0 if case.fill_code is None else int((raw_window == case.fill_code).sum())
    return {
        "budget_admissible": True,
        "isel": {dim: [start, stop]},
        "dtype": str(vals.dtype),
        "values": _encode(vals),
        "raw_expected_int64": [int(v) for v in raw_window.tolist()],
        "expected_ns": [int(v) for v in raw_expected.tolist()],
        "deviation_ns": deviation,
        "max_abs_deviation_ns": max((abs(d) for d in deviation), default=0) if deviation else None,
        "has_nat": bool(np.isnat(vals).any()) if vals.dtype.kind == "M" else False,
        "nat_count": int(np.isnat(vals).sum()) if vals.dtype.kind == "M" else 0,
        "fill_code": case.fill_code,
        "fill_count_in_window": fill_count,
        "n": int(vals.size),
    }


def _raw_measure(vals: np.ndarray) -> np.ndarray | None:
    """Re-express decoded values as integer offsets in the storage unit."""
    if vals.dtype.kind == "M":
        ref = np.datetime64(REF, vals.dtype.name.split("[")[1].rstrip("]"))
        return (vals - ref).astype("timedelta64[ns]").astype("int64")
    if vals.dtype.kind == "i":
        return vals.astype("int64")
    if vals.dtype.kind == "O":
        try:
            import cftime
        except Exception:
            return None
        out = []
        for value in vals:
            if isinstance(value, cftime.datetime):
                # Count the calendar's own days from 2100-01-01.  A 360_day
                # "year" is 360 days here, so subtracting datetime objects and
                # converting to seconds would undercount.  The stored unit for
                # the control is microseconds.
                ref = cftime.datetime(2100, 1, 1, calendar=value.calendar)
                out.append(int(value.toordinal() - ref.toordinal()) * 86_400 * 10**6)
            else:
                out.append(None)
        return np.array(out, dtype=object)
    if vals.dtype.kind == "f":
        return np.rint(vals).astype("int64")
    return None


def _reselect_evidence(path: Path, case: cases.Case, window: tuple[int, int]) -> dict[str, Any]:
    """Re-select the same window twice on one lazy handle."""
    ds, reader, err = _open(
        path, window, create_default_indexes=False, decode_times=CFDatetimeCoder()
    )
    out: dict[str, Any] = {
        "mode": "reselect_window",
        "open_error": err,
        "reads_on_open": _reads(reader),
        "earliest_outside_open": _earliest_outside(reader),
    }
    if ds is None:
        return out
    dim = "record" if case.layout == "aux" else "time"
    try:
        first = _window_call(ds, case)
        reads1 = _reads(reader)
        second = _window_call(ds, case)
        reads2 = _reads(reader)
        out.update(
            {
                "open_dtype": str(ds.time.dtype),
                "indexes": sorted(map(str, ds.xindexes)),
                "first_selection": first,
                "second_selection": second,
                "selections_identical": first["values"] == second["values"]
                and first["dtype"] == second["dtype"],
                "reads_after_first": reads1,
                "reads_after_second": reads2,
                "new_reads_on_second": len(reads2) - len(reads1),
                "earliest_outside_after_selection": _earliest_outside(reader),
                "budget_admissible_selection": bool(first["budget_admissible"]),
            }
        )
    except Exception as exc:  # noqa: BLE001
        out["selection_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        ds.close()
    return out


def _mode_evidence(
    path: Path,
    case: cases.Case,
    label: str,
    kwargs: dict[str, Any],
    *,
    window: tuple[int, int] | None = None,
    load_all: bool = False,
) -> dict[str, Any]:
    ds, reader, err = _open(path, window, **kwargs)
    out: dict[str, Any] = {
        "mode": label,
        "kwargs": {k: (v if isinstance(v, (str, int, float, bool, type(None))) else repr(v)) for k, v in kwargs.items()},
        "open_error": err,
        "reads_on_open": _reads(reader),
        "earliest_outside_open": _earliest_outside(reader),
    }
    if ds is None:
        out["open_dtype"] = None
        out["indexes"] = None
        out["window"] = None
        return out

    try:
        out["open_dtype"] = str(ds.time.dtype)
        out["indexes"] = sorted(map(str, ds.xindexes))
        out["coords"] = sorted(map(str, ds.coords))
        out["time_attrs"] = cdf5._jsonable(dict(ds.time.attrs))
        out["time_encoding"] = cdf5._jsonable(
            {k: str(v) if isinstance(v, np.dtype) else v for k, v in ds.time.encoding.items()}
        )
        out["warnings"] = ds.attrs.get("_warnings", [])

        if window is not None:
            try:
                out["window"] = _window_call(ds, case)
                out["window_error"] = None
            except Exception as exc:  # noqa: BLE001
                out["window"] = None
                out["window_error"] = f"{type(exc).__name__}: {exc}"
            out["reads_after_window"] = _reads(reader)
            out["earliest_outside_after_window"] = _earliest_outside(reader)

        if load_all:
            reader.phase_override = "load_all"
            try:
                full = ds.time.values
                out["loaded_dtype"] = str(full.dtype)
                out["loaded_values"] = _encode(full)
                out["loaded_has_nat"] = bool(np.isnat(full).any()) if full.dtype.kind == "M" else False
                out["load_error"] = None
            except Exception as exc:  # noqa: BLE001
                out["loaded_dtype"] = None
                out["loaded_values"] = None
                out["load_error"] = f"{type(exc).__name__}: {exc}"
            out["reads_after_load"] = _reads(reader)
            out["earliest_outside_after_load"] = _earliest_outside(reader)
            load_events = [e for e in reader.events if e.phase == "load_all"]
            out["load_budget_exceeded"] = any(not e.inside_window for e in load_events)
            out["load_values_used_as_evidence"] = False
            out["load_evidence_note"] = (
                "load_at_open reads every block, so its values exceed the "
                "acquisition budget and are recorded only to document what "
                "the library did; they are not archive evidence"
                if out["load_budget_exceeded"]
                else "load stayed within the caller window"
            )
            reader.phase_override = None
    finally:
        ds.close()
    return out


def _raw_exact(path: Path, var: str = "time") -> np.ndarray:
    reader = cdf5.Reader.open(path)
    reader.recording = False
    return reader.read_all(var)


def _blocks(path: Path, var: str = "time") -> list[dict[str, Any]]:
    return cdf5.Reader.open(path).block_extrema(var)


def _gcd(values: np.ndarray) -> int:
    g = 0
    for v in values.tolist():
        g = math.gcd(g, abs(int(v)))
    return g


def _storage_multiplier(units: str) -> int:
    """Exact factor from the file's time unit to nanoseconds."""
    return {
        "nanoseconds": 1,
        "microseconds": 10**3,
        "milliseconds": 10**6,
        "seconds": 10**9,
        "minutes": 60 * 10**9,
        "hours": 3600 * 10**9,
        "days": 86_400 * 10**9,
    }[units]


def _unit_fit_ns(value_ns: int, unit: str) -> tuple[bool, bool, int | None]:
    """Return (divisible, in_absolute_range, absolute_value_in_unit)."""
    step = UNIT_NS[unit]
    if value_ns % step != 0:
        return False, False, None
    ref_in_unit = int(np.datetime64(REF, unit).astype("int64"))
    abs_in_unit = ref_in_unit + value_ns // step
    lo = int(np.iinfo("int64").min)
    hi = int(np.iinfo("int64").max)
    return True, lo <= abs_in_unit <= hi, abs_in_unit


def _unit_sets_ns(value_ns: int) -> set[str]:
    return {u for u in UNITS if all(_unit_fit_ns(value_ns, u)[:2])}


def _segments_ns(values_ns: np.ndarray) -> list[dict[str, Any]]:
    if values_ns.size == 0:
        return []
    sets = [_unit_sets_ns(int(v)) for v in values_ns]
    segs: list[dict[str, Any]] = []
    start = 0
    common = set(sets[0])
    for i in range(1, values_ns.size):
        nxt = common & sets[i]
        if not nxt:
            segs.append({"start": start, "stop": i, "units": sorted(common)})
            start = i
            common = set(sets[i])
        else:
            common = nxt
    segs.append({"start": start, "stop": values_ns.size, "units": sorted(common)})
    order = {"s": 0, "ms": 1, "us": 2, "ns": 3}
    for seg in segs:
        seg["chosen_unit"] = sorted(seg["units"], key=lambda u: order[u])[0] if seg["units"] else None
    return segs


def _whole_file_units_ns(values_ns: np.ndarray) -> list[str]:
    common: set[str] | None = None
    for v in values_ns.tolist():
        s = _unit_sets_ns(int(v))
        common = s if common is None else (common & s)
        if not common:
            return []
    return sorted(common or set(), key=lambda u: {"s": 0, "ms": 1, "us": 2, "ns": 3}[u])


def _cf_legal_units_ns(values_ns: np.ndarray) -> list[str]:
    """CF/cftime can only decode these units; nanoseconds is not one."""
    legal = ("s", "ms", "us")
    return [u for u in legal if u in _whole_file_units_ns(values_ns)]


def _fill_analysis(values: np.ndarray, fill: int | None, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    if fill is None:
        return {
            "fill_code": None,
            "fill_count": 0,
            "max_nonfill": int(values.max()),
            "min_nonfill": int(values.min()),
            "blocks": [],
        }
    mask = values == fill
    nonfill = values[~mask]
    return {
        "fill_code": int(fill),
        "fill_count": int(mask.sum()),
        "fill_positions": [int(i) for i in np.flatnonzero(mask)],
        "max_nonfill": int(nonfill.max()) if nonfill.size else None,
        "min_nonfill": int(nonfill.min()) if nonfill.size else None,
        "blocks": [
            {
                "block": b["block"],
                "start": b["start"],
                "stop": b["stop"],
                "fill_count": int((values[b["start"]:b["stop"]] == fill).sum()),
                "max_nonfill": int(values[b["start"]:b["stop"]][values[b["start"]:b["stop"]] != fill].max())
                if (values[b["start"]:b["stop"]] != fill).any()
                else None,
                "min_nonfill": int(values[b["start"]:b["stop"]][values[b["start"]:b["stop"]] != fill].min())
                if (values[b["start"]:b["stop"]] != fill).any()
                else None,
            }
            for b in blocks
        ],
    }


def _supplement(values: np.ndarray, values_ns: np.ndarray, fill: int | None, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """The minimal per-block addition to the online summary.

    Existing summary: exact block min/max (including fill).  This adds, per
    logical block and excluding the fill code:
      * fill_count;
      * min_nonfill and max_nonfill (exact signed int64);
      * gcd_nonfill (exact signed int64).
    Together these decide unit coverage and fill extent without reading the
    user's window or depending on physical block placement.
    """
    out_blocks = []
    for b in blocks:
        a, z = b["start"], b["stop"]
        chunk = values[a:z]
        chunk_ns = values_ns[a:z]
        if fill is not None:
            keep = chunk != fill
            data_ns = chunk_ns[keep]
        else:
            data_ns = chunk_ns
        if data_ns.size == 0:
            out_blocks.append(
                {
                    "block": b["block"],
                    "start": a,
                    "stop": z,
                    "fill_count": int(chunk.size) if fill is not None else 0,
                    "nonfill_count": 0,
                    "subsecond_count": 0,
                    "min_nonfill_ns": None,
                    "max_nonfill_ns": None,
                    "gcd_nonfill_ns": None,
                    "units": [],
                }
            )
            continue
        gcd = _gcd(data_ns)
        units = []
        for u in UNITS:
            step = UNIT_NS[u]
            if gcd % step != 0:
                continue
            if not all(_unit_fit_ns(int(v), u)[1] for v in (data_ns.min(), data_ns.max())):
                continue
            units.append(u)
        out_blocks.append(
            {
                "block": b["block"],
                "start": a,
                "stop": z,
                "fill_count": int((chunk == fill).sum()) if fill is not None else 0,
                "nonfill_count": int(data_ns.size),
                "subsecond_count": int((data_ns % 10**9 != 0).sum()),
                "min_nonfill_ns": int(data_ns.min()),
                "max_nonfill_ns": int(data_ns.max()),
                "gcd_nonfill_ns": int(gcd),
                "units": sorted(units, key=lambda u: {"s": 0, "ms": 1, "us": 2, "ns": 3}[u]),
            }
        )
    common: set[str] | None = None
    for b in out_blocks:
        s = set(b["units"])
        common = s if common is None else (common & s)
    return {
        "per_block": out_blocks,
        "intersection_units": sorted(common or set(), key=lambda u: {"s": 0, "ms": 1, "us": 2, "ns": 3}[u]),
        "max_nonfill_ns": max((b["max_nonfill_ns"] for b in out_blocks if b["max_nonfill_ns"] is not None), default=None),
        "min_nonfill_ns": min((b["min_nonfill_ns"] for b in out_blocks if b["min_nonfill_ns"] is not None), default=None),
        "notes": "units are judged by exact divisibility of the block gcd and by the exact non-fill extrema; no interior value needs to be read",
    }


def _offline(path: Path, case: cases.Case) -> dict[str, Any]:
    raw = _raw_exact(path)
    mult = _storage_multiplier(case.units)
    ns = raw * mult
    if ns.max(initial=0) > np.iinfo("int64").max or ns.min(initial=0) < np.iinfo("int64").min:
        raise OverflowError("exact ns conversion overflowed; diagnostic assumes ns-scale values")
    blocks = _blocks(path)
    fill = case.fill_code
    keep = raw != fill if fill is not None else np.ones(raw.shape, dtype=bool)
    data_ns = ns[keep]
    whole = _whole_file_units_ns(data_ns)
    segs = _segments_ns(ns)
    supplement = _supplement(raw, ns, fill, blocks)
    per_block = []
    for b in blocks:
        chunk_ns = ns[b["start"]:b["stop"]]
        per_block.append(
            {
                "block": b["block"],
                "start": b["start"],
                "stop": b["stop"],
                "min_raw": b["min"],
                "max_raw": b["max"],
                "gcd_raw": _gcd(raw[b["start"]:b["stop"]]),
                "gcd_ns": _gcd(chunk_ns),
            }
        )
    return {
        "storage_unit": case.units,
        "storage_to_ns": mult,
        "raw_int64": [int(v) for v in raw.tolist()],
        "ns_int64": [int(v) for v in ns.tolist()],
        "raw_sha256_of_values": _values_sha(raw),
        "fill": _fill_analysis(raw, fill, blocks),
        "whole_file_units": whole,
        "cf_legal_units": _cf_legal_units_ns(data_ns),
        "segments": segs,
        "per_block": per_block,
        "supplement": supplement,
        "relative_ns_fits_int64": True,
        "absolute_ns_fits": all(bool(_unit_fit_ns(int(v), "ns")[1]) for v in ns.tolist()),
        "absolute_s_fits": all(bool(_unit_fit_ns(int(v), "s")[1]) for v in ns.tolist()),
        "verdict": _verdict(case, whole, segs, data_ns),
        "supplement_verdict": _supplement_verdict(case, supplement),
        "decision": _archive_decision(case, whole, segs, data_ns, supplement, fill),
        "supplement_decision": _supplement_decision(case, supplement),
    }


def _values_sha(values: np.ndarray) -> str:
    import hashlib

    return hashlib.sha256(np.ascontiguousarray(values, dtype="<i8").tobytes()).hexdigest()


def _verdict(case: cases.Case, whole: list[str], segs: list[dict[str, Any]], data_ns: np.ndarray) -> str:
    if case.calendar == "360_day":
        return "preserve_original_calendar"
    if whole:
        return f"whole_file_lossless_in:{','.join(whole)}"
    if len(segs) > 1 and all(s["chosen_unit"] for s in segs):
        return "lossless_segmentation_only"
    return "no_lossless_numpy_representation"


FAR_THRESHOLD_NS = int(50 * 365.25 * 86_400 * 10**9)
NEAR_REF_NS = int(np.datetime64(REF, "ns").astype("int64"))


def _archive_decision(
    case: cases.Case,
    whole: list[str],
    segs: list[dict[str, Any]],
    data_ns: np.ndarray,
    supplement: dict[str, Any],
    fill: int | None,
) -> dict[str, Any]:
    max_ns = int(data_ns.max()) if data_ns.size else None
    min_ns = int(data_ns.min()) if data_ns.size else None
    extent = "empty"
    if max_ns is not None:
        extent = "far" if max_ns > FAR_THRESHOLD_NS else "near"
    fill_present = fill is not None
    fill_covers_far = False
    if fill_present:
        raw_values = np.asarray(
            [
                int(v)
                for v in (_raw_exact(DATA / f"{case.name}_{case.layout}.cdf5")).tolist()
            ]
        )
        fill_covers_far = bool((raw_values == fill).any()) and extent in {"near", "empty"}
    unit_policy = _verdict(case, whole, segs, data_ns)
    if case.calendar == "360_day":
        representation_action = "preserve_original_calendar"
    elif whole:
        representation_action = f"store_lossless_in:{','.join(whole)}"
    elif len(segs) > 1:
        representation_action = "store_lossless_segments_no_single_unit"
    else:
        representation_action = "preserve_int64_ns_only"

    if extent == "far" and fill_present:
        extent_action = "preserve_real_far_record_and_fill_policy"
    elif extent == "far":
        extent_action = "preserve_far_extent"
    elif fill_present:
        extent_action = "fill_extreme_is_missing_not_a_date"
    else:
        extent_action = "near_only"

    return {
        "data_extent": extent,
        "min_nonfill_ns": min_ns,
        "max_nonfill_ns": max_ns,
        "fill_present": fill_present,
        "fill_covers_far_extreme": fill_covers_far,
        "unit_policy": unit_policy,
        "representation_action": representation_action,
        "extent_action": extent_action,
        "action": f"{representation_action} + {extent_action}",
        "action_key": [representation_action, extent_action],
    }


def _supplement_decision(case: cases.Case, supplement: dict[str, Any]) -> dict[str, Any]:
    """Archive decision using only header + the proposed per-block supplement."""
    units = supplement["intersection_units"]
    per_block = supplement["per_block"]
    if case.calendar == "360_day":
        representation = "preserve_original_calendar"
    elif units:
        representation = f"store_lossless_in:{','.join(units)}"
    else:
        # The supplement already proves every block has at least one lossless
        # unit (otherwise that block's gcd/extrema would fail every unit test).
        # A single unit failing across blocks is exactly lossless segmentation.
        representation = "store_lossless_segments_no_single_unit"

    max_ns = supplement["max_nonfill_ns"]
    extent = "empty" if max_ns is None else ("far" if max_ns > FAR_THRESHOLD_NS else "near")
    if extent == "far" and case.fill_code is not None:
        extent_action = "preserve_real_far_record_and_fill_policy"
    elif extent == "far":
        extent_action = "preserve_far_extent"
    elif case.fill_code is not None:
        extent_action = "fill_extreme_is_missing_not_a_date"
    else:
        extent_action = "near_only"
    return {
        "supplement_units": units,
        "data_extent_from_supplement": extent,
        "max_nonfill_ns_from_supplement": max_ns,
        "representation_action": representation,
        "extent_action": extent_action,
        "action_key": [representation, extent_action],
    }


def _supplement_verdict(case: cases.Case, supplement: dict[str, Any]) -> str:
    if case.calendar == "360_day":
        return "preserve_original_calendar"
    units = supplement["intersection_units"]
    if units:
        return f"whole_file_lossless_in:{','.join(units)}"
    # There may still be a per-block unit; segmentation is lossless.
    if all(b["units"] for b in supplement["per_block"]):
        return "lossless_segmentation_only"
    return "no_lossless_numpy_representation"


def _visible_digest(path: Path, window: tuple[int, int]) -> str:
    r = cdf5.Reader.open(path)
    r.window = window
    return r.catalog_digest(window)


def _pair_report(left: cases.Case, right: cases.Case, lpath: Path, rpath: Path, layout: str) -> dict[str, Any]:
    lv = _raw_exact(lpath)
    rv = _raw_exact(rpath)
    diff = np.flatnonzero(lv != rv)
    return {
        "family": left.family,
        "layout": layout,
        "members": [left.name, right.name],
        "visible_summary_equal": _visible_digest(lpath, left.window) == _visible_digest(rpath, right.window),
        "window_values_equal": np.array_equal(
            _read_window(lpath, left.window), _read_window(rpath, right.window)
        ),
        "differing_positions": [
            {
                "position": int(i),
                "left_raw": int(lv[i]),
                "right_raw": int(rv[i]),
                "delta_ns": int(lv[i]) - int(rv[i]),
                "left_block": _block_of(int(i)),
                "right_block": _block_of(int(i)),
            }
            for i in diff
        ],
        "offline_left": _offline(lpath, left),
        "offline_right": _offline(rpath, right),
    }


def _block_of(i: int) -> int | None:
    for b, (a, z) in enumerate(zip(cases.BLOCKS[0], cases.BLOCKS[1], strict=True)):
        if a <= i < z:
            return b
    return None


def _read_window(path: Path, window: tuple[int, int]) -> np.ndarray:
    r = cdf5.Reader.open(path)
    r.recording = False
    return r.read("time", slice(*window))


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    library_probes = _library_probes()
    result: dict[str, Any] = {
        "library_probes": library_probes,
        "meta": {
            "reference": cases.REF,
            "reference_ns": cases.REF_NS,
            "dtype": "int64",
            "unit": "nanoseconds",
            "window": list(cases.WINDOW),
            "python": sys.version,
            "numpy": np.__version__,
            "xarray": xr.__version__,
            "open_engine": "diagnostics.GSB1109.backend.CDF5Backend (real CDF-5 files)",
            "host_backends": "netCDF4/h5netcdf/h5py unavailable on this host",
        },
        "cases": {},
        "pairs": {},
    }

    for case in cases.all_cases():
        key = f"{case.name}_{case.layout}"
        path = DATA / f"{key}.cdf5"
        reader = cdf5.Reader.open(path)
        reader.window = case.window
        entry: dict[str, Any] = {
            "file": path.name,
            "sha256": cdf5.sha256(path),
            "header": {
                "dimensions": reader.dimensions,
                "dtype": reader.variables["time"].dtype.str,
                "shape": list(reader.variables["time"].shape),
                "attrs": cdf5._jsonable(reader.visible_attrs("time")),
                "global_attrs": cdf5._jsonable(reader.global_attrs),
                "header_bytes": reader.header_bytes,
            },
            "block_extrema": reader.block_extrema("time"),
            "visible_summary_sha256": reader.catalog_digest(case.window),
            "online": {},
            "offline": None,
        }

        # 1. Header/metadata inference only: no data reads are needed.
        entry["online"]["metadata_only"] = _mode_evidence(
            path,
            case,
            "metadata_only",
            {"create_default_indexes": False, "decode_times": CFDatetimeCoder(infer_dtype="metadata")},
            window=case.window,
        )
        # 2. Default index creation on.
        entry["online"]["default_index"] = _mode_evidence(
            path, case, "default_index", {}, window=case.window
        )
        # 3. Default index creation off.
        entry["online"]["no_default_index"] = _mode_evidence(
            path, case, "no_default_index", {"create_default_indexes": False}, window=case.window
        )
        # 4. Exact integers, no time decoding, no masking.
        entry["online"]["raw_int64"] = _mode_evidence(
            path,
            case,
            "raw_int64",
            {"create_default_indexes": False, "decode_times": False, "mask_and_scale": False},
            window=case.window,
        )
        # 5. Re-select the same window twice on one lazy handle.
        entry["online"]["reselect_window"] = _reselect_evidence(path, case, case.window)
        # 6. Open and load immediately.
        entry["online"]["load_at_open"] = _mode_evidence(
            path,
            case,
            "load_at_open",
            {"create_default_indexes": False},
            window=case.window,
            load_all=True,
        )

        entry["offline"] = _offline(path, case)
        result["cases"][key] = entry

    for family, variants in (
        ("pair1", ("tail_1ns", "whole_second")),
        ("pair2", ("fill_only", "fill_plus_real_far")),
    ):
        for layout in ("dim", "aux"):
            left = next(c for c in cases.all_cases() if c.name == f"{family}_{variants[0]}" and c.layout == layout)
            right = next(c for c in cases.all_cases() if c.name == f"{family}_{variants[1]}" and c.layout == layout)
            result["pairs"][f"{family}_{layout}"] = _pair_report(
                left, right, DATA / f"{left.name}_{layout}.cdf5", DATA / f"{right.name}_{layout}.cdf5", layout
            )

    (REPORTS / "diagnostic.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    _write_summary(result)
    print(f"wrote {REPORTS / 'diagnostic.json'}")
    return 0


def _library_probes() -> dict[str, Any]:
    """Independent checks that separate archive math from library behavior."""
    import cftime
    from xarray.coding.common import lazy_elemwise_func
    from xarray.coding.times import decode_cf_datetime
    from xarray.core.indexing import LazilyIndexedArray
    from xarray.core.variable import IndexVariable
    from xarray.backends.common import BackendArray
    from xarray.core import indexing

    def off_us(s: str) -> int:
        return (
            int(np.datetime64(s, "s").astype("int64"))
            - int(np.datetime64(REF, "s").astype("int64"))
        ) * 10**6

    far_ns = cases.FAR
    ref_ns = cases.REF_NS
    probes: dict[str, Any] = {
        "math": {
            "far_ns": int(far_ns),
            "ref_ns": int(ref_ns),
            "reference_plus_far_ns": int(ref_ns + far_ns),
            "int64_max": int(np.iinfo("int64").max),
            "relative_timedelta64_ns_ok": bool(np.timedelta64(far_ns, "ns") == np.timedelta64(far_ns, "ns")),
            "absolute_datetime64_ns_overflows": bool(ref_ns + far_ns > np.iinfo("int64").max),
            "unit_divisibility_ns": {u: (far_ns % UNIT_NS[u] == 0) for u in UNITS},
        },
        "cftime_microsecond_header": {},
        "calendar_control": None,
        "silent_object_cast": None,
    }
    # cftime can represent 2300 when the header uses a CF-legal unit.
    us_values = np.array([0, off_us("2300-01-01")], dtype="int64")
    try:
        decoded = decode_cf_datetime(
            us_values, "microseconds since 2100-01-01 00:00:00", "standard", True, "s"
        )
        probes["cftime_microsecond_header"] = {
            "ok": True,
            "values": [str(v) for v in decoded],
        }
    except Exception as exc:  # noqa: BLE001
        probes["cftime_microsecond_header"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # The 360_day control: the same exact int64 means different dates under
    # the standard calendar.  This is a calendar semantics fact, not a dtype
    # or container fact.
    try:
        import cftime

        day59 = 59 * 86_400 * 10**6  # microseconds since 2100-01-01
        as_360 = cftime.num2date(day59, "microseconds since 2100-01-01", "360_day")
        as_std = cftime.num2date(day59, "microseconds since 2100-01-01", "standard")
        probes["calendar_control"] = {
            "offset_us": int(day59),
            "as_360_day": str(as_360),
            "as_standard": str(as_std),
            "same_int64_different_date": str(as_360) != str(as_std),
        }
    except Exception as exc:  # noqa: BLE001
        probes["calendar_control"] = {"error": f"{type(exc).__name__}: {exc}"}

    # A coordinate that declares datetime64[ns] but lazily yields cftime objects
    # (the ordinary fallback for out-of-range dates) is silently cast by
    # IndexVariable.  This is independent of the archive container.
    objects = np.array(
        [cftime.DatetimeGregorian(2100, 1, 1), cftime.DatetimeGregorian(2300, 1, 1)],
        dtype=object,
    )

    class ObjectArray(BackendArray):
        shape = (2,)
        dtype = np.dtype("datetime64[ns]")

        def __getitem__(self, key):
            return indexing.explicit_indexing_adapter(
                key, self.shape, indexing.IndexingSupport.OUTER, self._get
            )

        def _get(self, key):
            return objects[key]

    var = IndexVariable(("time",), LazilyIndexedArray(ObjectArray()), {})
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cast_values = var.values
        probes["silent_object_cast"] = {
            "declared_dtype": str(var.dtype),
            "returned_dtype": str(cast_values.dtype),
            "values": [
                str(np.datetime64(int(v), "ns")) for v in cast_values.astype("int64")
            ],
            # Compare the raw int64, not datetime64 equality: out-of-range
            # wrap-around values can compare equal after another overflow.
            "raw_int64": [int(v) for v in cast_values.astype("int64").tolist()],
            # Avoid datetime64/int64 "literal" conversion: an out-of-range
            # literal wraps to the same bit pattern. Compare the calendar date
            # numpy actually produced against the intended 2300-01-01.
            "corrupted": bool(str(cast_values[1])[:10] != "2300-01-01"),
            "warnings": [str(w.message) for w in caught],
            "plain_xarray_only": True,
        }
    except Exception as exc:  # noqa: BLE001
        probes["silent_object_cast"] = {"error": f"{type(exc).__name__}: {exc}"}
    return probes


def _write_summary(result: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append("GSB1109 archive-time diagnostic summary")
    lines.append(f"reference={result['meta']['reference']} unit=ns dtype=int64 window={result['meta']['window']}")
    lines.append("")
    for key, c in result["cases"].items():
        lines.append(f"[{key}]")
        lines.append(f"  fill={c['header']['attrs'].get('_FillValue')} blocks={[(b['block'], b['min'], b['max']) for b in c['block_extrema']]}")
        for mode, ev in c["online"].items():
            bad = ev.get("earliest_outside_open")
            lines.append(
                f"  {mode}: dtype={ev.get('open_dtype')} err={ev.get('open_error')} "
                f"reads={len(ev.get('reads_on_open') or [])} out_of_window={bool(bad)}"
            )
            if bad:
                e = bad["by_sequence"]
                lines.append(
                    f"    first outside: seq={e['seq']} phase={e['phase']} logical={e['logical_first']} "
                    f"byte={e['byte_first']} (forbidden as evidence)"
                )
        off = c["offline"]
        lines.append(
            f"  offline: units={off['whole_file_units']} segments={[(s['start'], s['stop'], s['chosen_unit']) for s in off['segments']]} "
            f"fill_count={off['fill']['fill_count']} verdict={off['verdict']}"
        )
        lines.append("")
    for key, p in result["pairs"].items():
        lines.append(f"<{key}> visible_summary_equal={p['visible_summary_equal']} window_equal={p['window_values_equal']}")
        lines.append(f"  differing_positions={[(d['position'], d['delta_ns']) for d in p['differing_positions']]}")
        lines.append(f"  left verdict={p['offline_left']['verdict']} right verdict={p['offline_right']['verdict']}")
        lines.append("")
    (REPORTS / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
