"""Case definitions for the archive-time diagnostic.

All offsets are signed 64-bit integer nanoseconds relative to
``2100-01-01T00:00:00``.  Two families are built:

* ``pair1``: same header, same per-block raw extrema, same user window, but
  one file hides a +1 ns residual in the interior of an unread non-monotonic
  block while the other stores that record as a whole second.  Both files
  have a whole-second far extreme touching 2300.
* ``pair2``: both files enable ``_FillValue`` and both have the far extreme
  equal to that fill code.  In one file the real data stop near 2100; in the
  other a real 2300 record sits just below the fill code and is therefore
  hidden by the raw block maximum.

Each family is emitted in two layouts:

* ``dim``: ``time`` is the dimension coordinate of dimension ``time``.
* ``aux``: ``time`` is an auxiliary coordinate on dimension ``record``.

The 360_day control stores a legal 2100-02-30.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from . import cdf5

REF = "2100-01-01T00:00:00"
REF_NS = int(np.datetime64(REF, "ns").astype("int64"))
NS_PER_DAY = 86_400 * 10**9


def day(n: int) -> int:
    """Offset in ns for ``n`` whole days after the reference."""
    return n * NS_PER_DAY


def days_between(a: str, b: str = REF) -> int:
    """Whole-day offset from ``b`` to ``a`` in the proleptic Gregorian calendar."""
    return int(
        (np.datetime64(a, "D") - np.datetime64(b, "D")) / np.timedelta64(1, "D")
    )


FAR = days_between("2300-01-01") * NS_PER_DAY
# Pair 2 keeps the fill-coded far extreme inside datetime64[ns] so that both
# members open identically online; its distinction is fill-vs-real, not the
# absolute-range failure that pair 1 deliberately exercises.
FILL_FAR = days_between("2258-01-01") * NS_PER_DAY
NEAR_LIMIT = 2262  # last whole year fully inside datetime64[ns]

BLOCKS = (np.array([0, 8, 16]), np.array([8, 16, 24]))
WINDOW = (4, 12)


@dataclass(frozen=True)
class Case:
    name: str
    layout: Literal["dim", "aux"]
    calendar: str
    values: np.ndarray
    fill_code: int | None
    window: tuple[int, int]
    block_starts: np.ndarray
    block_stops: np.ndarray
    units: str = "nanoseconds"
    family: str = ""

    @property
    def title(self) -> str:
        # Pair partners must share a title: the title is a header attribute and
        # the two files are required to be indistinguishable from outside.
        return f"GSB1109 {self.family} ({self.layout})"


def _with_blocks(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    starts, stops = BLOCKS
    # The templates are defined for 24 records; keep the table generic so a
    # replay can reshape it.
    if values.size != 24:
        raise ValueError("templates are defined for 24 records")
    return starts.copy(), stops.copy()


def pair1_values(variant: Literal["tail_1ns", "whole_second"]) -> np.ndarray:
    """24 records; only index 14 differs between the variants."""
    v = np.array(
        [
            # block 0: monotonic near records
            day(0), day(1), day(2), day(3), day(4), day(5), day(6), day(7),
            # block 1: non-monotonic; min and max are whole seconds
            day(8), day(9), day(10), day(11), day(6), day(4), day(4), day(14),
            # block 2: non-monotonic and entirely outside the caller window;
            # both extrema are whole seconds and the maximum touches 2300.
            day(16), day(17), day(18), day(19), day(20), day(21), FAR, day(22),
        ],
        dtype=np.int64,
    )
    if variant == "tail_1ns":
        # The hidden residual lives at index 17, inside the unread block 2.
        # It is strictly between the block minimum and maximum, so the raw
        # block extrema are unchanged.
        v[17] = day(17) + 1
    elif variant == "whole_second":
        v[17] = day(17)
    else:
        raise ValueError(variant)
    return v


def pair1_cases(layout: Literal["dim", "aux"]) -> list[Case]:
    out = []
    for variant in ("tail_1ns", "whole_second"):
        starts, stops = _with_blocks(pair1_values(variant))
        out.append(
            Case(
                name=f"pair1_{variant}",
                layout=layout,
                calendar="standard",
                values=pair1_values(variant),
                fill_code=None,
                window=WINDOW,
                block_starts=starts,
                block_stops=stops,
                units="nanoseconds",
                family="pair1",
            )
        )
    return out


def pair2_values(variant: Literal["fill_only", "fill_plus_real_far"]) -> np.ndarray:
    """Both variants have block 2 max == fill code; only the interior differs."""
    v = np.array([day(i) for i in range(24)], dtype=np.int64)
    v[23] = FILL_FAR  # fill code at the far extreme in both variants
    if variant == "fill_plus_real_far":
        # A real far record one day below the fill code.  It is smaller than
        # the fill code, so the raw block maximum stays equal to the fill code
        # and the online summary hides the real far extent.
        v[22] = FILL_FAR - NS_PER_DAY
    elif variant == "fill_only":
        v[22] = day(22)
    else:
        raise ValueError(variant)
    return v


def pair2_cases(layout: Literal["dim", "aux"]) -> list[Case]:
    out = []
    for variant in ("fill_only", "fill_plus_real_far"):
        starts, stops = _with_blocks(pair2_values(variant))
        out.append(
            Case(
                name=f"pair2_{variant}",
                layout=layout,
                calendar="standard",
                values=pair2_values(variant),
                fill_code=FILL_FAR,
                window=WINDOW,
                block_starts=starts,
                block_stops=stops,
                units="nanoseconds",
                family="pair2",
            )
        )
    return out


def cal360_case(layout: Literal["dim", "aux"]) -> Case:
    # Day 59 is 2100-02-30 in the 360_day calendar.  cftime rejects
    # "nanoseconds since ...", so this control deliberately uses the finest
    # CF-legal unit, microseconds, while keeping the same reference instant.
    # The offset in integer microseconds is exactly day(k) // 1000.
    days = (0, 29, 30, 59, 60, 89, 90, 119)
    values = np.array([day(i) // 1000 for i in days], dtype=np.int64)
    return Case(
        name="cal360_feb30",
        layout=layout,
        calendar="360_day",
        values=values,
        fill_code=None,
        window=(2, 5),
        block_starts=np.array([0, 4]),
        block_stops=np.array([4, 8]),
        units="microseconds",
        family="cal360",
    )


def all_cases() -> list[Case]:
    cases: list[Case] = []
    for layout in ("dim", "aux"):
        cases.extend(pair1_cases(layout))
        cases.extend(pair2_cases(layout))
        cases.append(cal360_case(layout))
    return cases


def build(case: Case, path: str, *, block_order: dict[str, list[int]] | None = None) -> dict:
    n = case.values.size
    attrs = {
        "units": f"{case.units} since 2100-01-01 00:00:00",
        "calendar": case.calendar,
        "archive_block_start": case.block_starts,
        "archive_block_stop": case.block_stops,
    }
    if case.fill_code is not None:
        attrs["_FillValue"] = np.int64(case.fill_code)
    if case.layout == "dim":
        dims = {"time": n}
        variables = {
            "time": cdf5.VariableSpec(("time",), case.values, attrs, dtype="i8"),
            "value": cdf5.VariableSpec(
                ("time",), np.arange(n, dtype="f8"), {}, dtype="f8"
            ),
        }
    else:
        dims = {"record": n}
        variables = {
            "time": cdf5.VariableSpec(("record",), case.values, attrs, dtype="i8"),
            "value": cdf5.VariableSpec(
                ("record",),
                np.arange(n, dtype="f8"),
                {"coordinates": "time"},
                dtype="f8",
            ),
        }
    info = cdf5.write_cdf5(
        path,
        dimensions=dims,
        variables=variables,
        global_attrs={"title": case.title},
        block_order=block_order,
    )
    info["sha256"] = cdf5.sha256(path)
    return info
