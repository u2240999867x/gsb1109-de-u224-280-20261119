"""Replayable diagnostics for TableHDU.where row identities.

The synthetic tables and observations here are intentionally independent of
the row-filter implementation.  Nothing in this module changes ``where``.

Run with::

    python -m fitsio.tests.where_row_diagnostic
    python -m fitsio.tests.where_row_diagnostic --json
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import numpy as np

from ..fitslib import FITS, write

THRESHOLD = 5.0
WINDOW = (3, 9)
SCALE = -1.0
ZERO = 11.0

# Primary table.  Duplicate physical brightness values and exact threshold
# values are deliberate.  The independent ROWID column is the source identity.
PRIMARY_ROWIDS = np.array(
    [1001, 1017, 1034, 1051, 1068, 1085, 1102, 1119, 1136, 1153, 1170, 1187],
    dtype="i8",
)
PRIMARY_PHYSICAL = np.array(
    [6.0, 5.0, 8.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 6.0, 5.0, 4.0],
    dtype="f8",
)

# Same shape and same hit counts, but every source identifier is different.
CONTROL_ROWIDS = np.array(
    [7003, 7019, 7036, 7053, 7070, 7087, 7104, 7121, 7138, 7155, 7172, 7189],
    dtype="i8",
)
CONTROL_PHYSICAL = np.array(
    [6.0, 5.0, 8.0, 5.0, 8.0, 3.0, 6.0, 4.0, 7.0, 6.0, 5.0, 4.0],
    dtype="f8",
)


def physical_to_stored(physical):
    """Return the stored value implied by TSCAL/TZERO."""
    return (np.asarray(physical, dtype="f8") - ZERO) / SCALE


def make_table(path, rowids, physical):
    """Write a two-column table with an independent ROWID and a scaled flux.

    ``BRIGHT`` is stored as a float so the public read path and the cfitsio
    predicate both exercise the same TSCAL/TZERO rule.  The stored values are
    ``(physical - TZERO) / TSCAL``.
    """
    physical = np.asarray(physical, dtype="f8")
    rowids = np.asarray(rowids, dtype="i8")
    data = np.zeros(len(rowids), dtype=[("ROWID", "i8"), ("BRIGHT", "f8")])
    data["ROWID"] = rowids
    data["BRIGHT"] = physical_to_stored(physical)
    write(
        path,
        data,
        header={"TSCAL2": SCALE, "TZERO2": ZERO},
        clobber=True,
    )
    return data


def read_stored_columns(hdu):
    """Read the table bytes without applying TSCAL/TZERO.

    This is deliberately a diagnostic read, not a filtering path.  The FITS
    binary table is big endian, so a big-endian dtype decodes the stored
    values directly.  The table schema is fixed by ``make_table``.
    """
    info = hdu.get_info()
    nrows = info["nrows"]
    dtype = np.dtype([("ROWID", ">i8"), ("BRIGHT", ">f8")])
    out = np.zeros(nrows, dtype=dtype)
    hdu._FITS.read_as_rec(hdu._ext + 1, 1, nrows, out)
    return out


def _call_window(hdu, expression, firstrow, lastrow):
    try:
        result = hdu.where(expression, firstrow, lastrow)
    except Exception as exc:  # noqa: BLE001 - diagnostic records the type
        return None, type(exc).__name__, str(exc)
    return np.asarray(result, dtype="i8"), None, None


def observe(path):
    """Return the local observations for one table."""
    with FITS(path) as fits:
        hdu = fits[1]
        data = hdu.read()
        rowids = np.asarray(data["ROWID"], dtype="i8")
        physical = np.asarray(data["BRIGHT"], dtype="f8")
        stored = read_stored_columns(hdu)["BRIGHT"].astype("f8")

        full_ge = hdu.where("BRIGHT >= %r" % THRESHOLD)
        full_gt = hdu.where("BRIGHT > %r" % THRESHOLD)
        full_eq = hdu.where("BRIGHT == %r" % THRESHOLD)
        window_ge = hdu.where("BRIGHT >= %r" % THRESHOLD, *WINDOW)
        window_gt = hdu.where("BRIGHT > %r" % THRESHOLD, *WINDOW)
        window_eq = hdu.where("BRIGHT == %r" % THRESHOLD, *WINDOW)

        stored_full_ge = np.where(stored >= THRESHOLD)[0]
        stored_full_eq = np.where(stored == THRESHOLD)[0]
        stored_window_ge = np.where(
            stored[WINDOW[0] : WINDOW[1]] >= THRESHOLD
        )[0]
        stored_window_eq = np.where(
            stored[WINDOW[0] : WINDOW[1]] == THRESHOLD
        )[0]

        endpoint_ge8 = hdu.where("BRIGHT >= 8.0", *WINDOW)
        endpoint_gt8 = hdu.where("BRIGHT > 8.0", *WINDOW)
        left_inclusive = hdu.where("BRIGHT >= 3.0", WINDOW[0], WINDOW[1])
        right_exclusive = hdu.where("BRIGHT >= 3.0", WINDOW[0], WINDOW[1] - 1)

        no_match_full = hdu.where("BRIGHT > 1.0e99")
        no_match_window = hdu.where("BRIGHT > 1.0e99", WINDOW[0], WINDOW[1])

        _, empty_error_type, empty_error_message = _call_window(
            hdu, "BRIGHT >= %r" % THRESHOLD, WINDOW[0], WINDOW[0]
        )
        _, native_error_type, native_error_message = _call_window_native(
            hdu, "BRIGHT >= %r" % THRESHOLD, WINDOW[0] + 1, 0
        )

    return {
        "rowids": rowids,
        "physical": physical,
        "stored": stored,
        "full_ge": np.asarray(full_ge, dtype="i8"),
        "full_gt": np.asarray(full_gt, dtype="i8"),
        "full_eq": np.asarray(full_eq, dtype="i8"),
        "window_ge": np.asarray(window_ge, dtype="i8"),
        "window_gt": np.asarray(window_gt, dtype="i8"),
        "window_eq": np.asarray(window_eq, dtype="i8"),
        "stored_full_ge": np.asarray(stored_full_ge, dtype="i8"),
        "stored_full_eq": np.asarray(stored_full_eq, dtype="i8"),
        "stored_window_ge": np.asarray(stored_window_ge, dtype="i8"),
        "stored_window_eq": np.asarray(stored_window_eq, dtype="i8"),
        "endpoint_ge8": np.asarray(endpoint_ge8, dtype="i8"),
        "endpoint_gt8": np.asarray(endpoint_gt8, dtype="i8"),
        "left_inclusive": np.asarray(left_inclusive, dtype="i8"),
        "right_exclusive": np.asarray(right_exclusive, dtype="i8"),
        "no_match_full": np.asarray(no_match_full, dtype="i8"),
        "no_match_window": np.asarray(no_match_window, dtype="i8"),
        "empty_error_type": empty_error_type,
        "empty_error_message": empty_error_message,
        "native_error_type": native_error_type,
        "native_error_message": native_error_message,
    }


def _call_window_native(hdu, expression, firstrow, nrows):
    try:
        result = hdu._FITS.where(hdu._ext + 1, expression, firstrow, nrows)
    except Exception as exc:  # noqa: BLE001 - diagnostic records the type
        return None, type(exc).__name__, str(exc)
    return result, None, None


def probe_integer_scaled_read(path):
    """Minimal control for an integer stored column with TSCAL/TZERO.

    The predicate path asks cfitsio to convert with TSCAL/TZERO.  The public
    record-read path chooses the scaled equivalent dtype and then applies the
    scale again in Python.  For an integer stored column those two widths do
    not match in this snapshot, so the two paths disagree.
    """
    rowids = np.arange(600, 606, dtype="i8")
    stored = np.arange(1, 7, dtype="i2")
    data = np.zeros(6, dtype=[("ROWID", "i8"), ("SCALED", "i2")])
    data["ROWID"] = rowids
    data["SCALED"] = stored
    write(
        path,
        data,
        header={"TSCAL2": 0.5, "TZERO2": 1.0},
        clobber=True,
    )
    with FITS(path) as fits:
        hdu = fits[1]
        public = hdu.read()
        where_gt = hdu.where("SCALED > 2.0")
    expected = stored.astype("f8") * 0.5 + 1.0
    return {
        "stored": stored.astype("f8"),
        "expected": expected,
        "read": np.asarray(public["SCALED"], dtype="f8"),
        "read_rowids": np.asarray(public["ROWID"], dtype="i8"),
        "where_gt": np.asarray(where_gt, dtype="i8"),
    }


def _absolute(window_hits, firstrow):
    return firstrow + np.asarray(window_hits, dtype="i8")


def _ids(rowids, indices):
    return rowids[np.asarray(indices, dtype="i8")]


def _fmt(values):
    return "[" + ", ".join(str(int(v)) for v in values) + "]"


def _fmt_float(values):
    return "[" + ", ".join(("%g" % float(v)) for v in values) + "]"


def _overlap(left, right):
    return len(set(int(v) for v in left) & set(int(v) for v in right))


def summarise(obs):
    """Derive the identity-level comparisons used in the report."""
    firstrow = WINDOW[0]
    correct_abs = _absolute(obs["window_ge"], firstrow)
    offset_abs = np.asarray(obs["window_ge"], dtype="i8")
    stored_abs = _absolute(obs["stored_window_ge"], firstrow)
    return {
        "correct_abs": correct_abs,
        "correct_ids": _ids(obs["rowids"], correct_abs),
        "offset_abs": offset_abs,
        "offset_ids": _ids(obs["rowids"], offset_abs),
        "stored_abs": stored_abs,
        "stored_ids": _ids(obs["rowids"], stored_abs),
        "correct_count": int(correct_abs.size),
        "offset_count": int(offset_abs.size),
        "stored_count": int(stored_abs.size),
        "offset_overlap": _overlap(
            _ids(obs["rowids"], correct_abs),
            _ids(obs["rowids"], offset_abs),
        ),
        "stored_overlap": _overlap(
            _ids(obs["rowids"], correct_abs),
            _ids(obs["rowids"], stored_abs),
        ),
    }


def _jsonable(obs):
    out = {}
    for key, value in obs.items():
        if isinstance(value, np.ndarray):
            if value.dtype.kind in "iu":
                out[key] = [int(v) for v in value]
            else:
                out[key] = [float(v) for v in value]
        else:
            out[key] = value
    return out


def build_report():
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = str(Path(tmpdir) / "primary.fits")
        control_path = str(Path(tmpdir) / "control.fits")
        integer_path = str(Path(tmpdir) / "integer.fits")

        make_table(primary_path, PRIMARY_ROWIDS, PRIMARY_PHYSICAL)
        make_table(control_path, CONTROL_ROWIDS, CONTROL_PHYSICAL)

        primary = observe(primary_path)
        control = observe(control_path)
        integer = probe_integer_scaled_read(integer_path)

    return {
        "primary": primary,
        "control": control,
        "integer": integer,
        "primary_summary": summarise(primary),
        "control_summary": summarise(control),
    }


def print_report(report):
    primary = report["primary"]
    control = report["control"]
    ps = report["primary_summary"]
    cs = report["control_summary"]
    firstrow = WINDOW[0]

    print("Synthetic primary table")
    print("  ROWID:   %s" % _fmt(primary["rowids"]))
    print("  physical:%s" % _fmt_float(primary["physical"]))
    print("  stored:  %s" % _fmt_float(primary["stored"]))
    print()
    print("Synthetic control table (different source identifiers)")
    print("  ROWID:   %s" % _fmt(control["rowids"]))
    print("  physical:%s" % _fmt_float(control["physical"]))
    print("  stored:  %s" % _fmt_float(control["stored"]))
    print()

    print("Full table, predicate BRIGHT >= %g" % THRESHOLD)
    print("  primary where:          %s" % _fmt(primary["full_ge"]))
    print(
        "  primary where ROWID:    %s"
        % _fmt(_ids(primary["rowids"], primary["full_ge"]))
    )
    print("  primary stored filter:  %s" % _fmt(primary["stored_full_ge"]))
    print(
        "  primary stored ROWID:   %s"
        % _fmt(_ids(primary["rowids"], primary["stored_full_ge"]))
    )
    print("  control where:          %s" % _fmt(control["full_ge"]))
    print(
        "  control where ROWID:    %s"
        % _fmt(_ids(control["rowids"], control["full_ge"]))
    )
    print(
        "  counts: primary where=%d stored=%d; control where=%d"
        % (
            primary["full_ge"].size,
            primary["stored_full_ge"].size,
            control["full_ge"].size,
        )
    )
    print()

    print(
        "Window [%d, %d), predicate BRIGHT >= %g"
        % (WINDOW[0], WINDOW[1], THRESHOLD)
    )
    print(
        "  primary where:          %s (relative)" % _fmt(primary["window_ge"])
    )
    print("  primary absolute:       %s" % _fmt(ps["correct_abs"]))
    print("  primary ROWID:          %s" % _fmt(ps["correct_ids"]))
    print(
        "  Python physical slice:  %s"
        % _fmt(
            np.where(primary["physical"][WINDOW[0] : WINDOW[1]] >= THRESHOLD)[
                0
            ]
        )
    )
    print(
        "  stored slice filter:    %s (relative)"
        % _fmt(primary["stored_window_ge"])
    )
    print("  stored ROWID:           %s" % _fmt(ps["stored_ids"]))
    print("  offset misread ROWID:   %s" % _fmt(ps["offset_ids"]))
    print(
        "  counts: correct=%d offset=%d stored=%d"
        % (ps["correct_count"], ps["offset_count"], ps["stored_count"])
    )
    print(
        "  overlaps: offset=%d stored=%d"
        % (ps["offset_overlap"], ps["stored_overlap"])
    )
    print()

    print(
        "Control window [%d, %d), predicate BRIGHT >= %g"
        % (WINDOW[0], WINDOW[1], THRESHOLD)
    )
    print(
        "  control where:          %s (relative)" % _fmt(control["window_ge"])
    )
    print("  control absolute:       %s" % _fmt(cs["correct_abs"]))
    print("  control ROWID:          %s" % _fmt(cs["correct_ids"]))
    print("  control stored ROWID:   %s" % _fmt(cs["stored_ids"]))
    print(
        "  counts: correct=%d stored=%d"
        % (cs["correct_count"], cs["stored_count"])
    )
    print()

    print("Threshold and endpoint controls")
    equal_rows = np.where(primary["physical"] == THRESHOLD)[0]
    print(
        "  physical == %.1f:      %s ROWID %s"
        % (
            THRESHOLD,
            _fmt(equal_rows),
            _fmt(_ids(primary["rowids"], equal_rows)),
        )
    )
    print(
        "  >= vs > difference:    %s"
        % _fmt(np.setdiff1d(primary["full_ge"], primary["full_gt"]))
    )
    print(
        "  window left edge BRIGHT=%g: >=8 -> %s, >8 -> %s"
        % (
            primary["physical"][firstrow],
            _fmt(primary["endpoint_ge8"]),
            _fmt(primary["endpoint_gt8"]),
        )
    )
    print("  window [3,8) >=3:      %s" % _fmt(primary["right_exclusive"]))
    print("  window [3,9) >=3:      %s" % _fmt(primary["left_inclusive"]))
    print()

    print("Duplicate-brightness identity control")
    print(
        "  where == %.1f:        %s ROWID %s"
        % (
            THRESHOLD,
            _fmt(primary["full_eq"]),
            _fmt(_ids(primary["rowids"], primary["full_eq"])),
        )
    )
    print(
        "  stored == %.1f:       %s ROWID %s"
        % (
            THRESHOLD,
            _fmt(primary["stored_full_eq"]),
            _fmt(_ids(primary["rowids"], primary["stored_full_eq"])),
        )
    )
    print()

    print("Empty window and true no-match")
    print(
        "  empty window:          %s: %s"
        % (primary["empty_error_type"], primary["empty_error_message"])
    )
    print(
        "  native nrows=0:        %s: %s"
        % (primary["native_error_type"], primary["native_error_message"])
    )
    print(
        "  no-match full:         %s (len=%d)"
        % (_fmt(primary["no_match_full"]), primary["no_match_full"].size)
    )
    print(
        "  no-match window:       %s (len=%d)"
        % (_fmt(primary["no_match_window"]), primary["no_match_window"].size)
    )
    print()

    integer = report["integer"]
    print(
        "Separate read-path control: integer stored column, "
        "TSCAL=0.5 TZERO=1.0"
    )
    print("  stored:                %s" % _fmt_float(integer["stored"]))
    print("  expected physical:     %s" % _fmt_float(integer["expected"]))
    print("  public read:           %s" % _fmt_float(integer["read"]))
    print("  public read ROWID:     %s" % _fmt(integer["read_rowids"]))
    print("  where SCALED > 2.0:    %s (physical)" % _fmt(integer["where_gt"]))
    print()

    print("Result")
    print("  where() returns window-relative, 0-based indices.")
    print("  absolute = firstrow + where(expression, firstrow, lastrow).")
    print("  The predicate compares physical TSCAL/TZERO values.")
    print("  Equal hit counts do not imply the same source rows.")


def _summary_json(summary):
    return {
        key: [int(v) for v in value]
        if isinstance(value, np.ndarray)
        else value
        for key, value in summary.items()
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the observations as JSON instead of text",
    )
    args = parser.parse_args(argv)
    report = build_report()
    if args.json:
        payload = {
            "primary": _jsonable(report["primary"]),
            "control": _jsonable(report["control"]),
            "integer": _jsonable(report["integer"]),
            "primary_summary": _summary_json(report["primary_summary"]),
            "control_summary": _summary_json(report["control_summary"]),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
