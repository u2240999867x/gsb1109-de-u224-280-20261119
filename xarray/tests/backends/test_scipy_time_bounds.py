"""Offline regression tests for time-bounds observation-window corrections.

The meteorological archive workflow exercised here stores ``time`` and its
``time_bounds`` variable on a SciPy NetCDF3 file as signed 32-bit integer
milliseconds on the proleptic Gregorian calendar, each variable carrying its
own missing-value code. Two independent readers exist:

* the warehouse side opens the raw NetCDF3 file and interprets the boundary
  integers against the ``time`` variable's reference epoch, honoring the
  bounds variable's own fill value;
* reviewers open the same file with xarray, which decodes the CF time
  coordinates itself.

Correction tickets are replayed through
``to_netcdf(mode="a", engine="scipy")``. Boundary tickets submit only the
bounds variable and carry no prior encoding; reference-time tickets write the
representative time and both boundary columns together. Every assertion is
made against a file that was closed and reopened, and the expected values are
derived solely from the initial records and the ticket sequence -- never from
the file under test.

Two archive generations must represent the same observations: legacy archives
duplicate the time reference attributes on the bounds variable, new archives
omit them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pytest
from scipy.io import netcdf_file

import xarray as xr
from xarray.tests import requires_scipy

CALENDAR = "proleptic_gregorian"
TIME_FILL = np.int32(-2147483647)
BOUNDS_FILL = np.int32(-2147483646)
Side = Literal[0, 1]
FaultMode = Literal["none", "warehouse_side", "reviewer_side"]

LEFT: Side = 0
RIGHT: Side = 1


def side_name(side: int | None) -> str:
    if side == LEFT:
        return "left"
    if side == RIGHT:
        return "right"
    return "none"


def units_for(epoch: np.datetime64) -> str:
    return f"milliseconds since {np.datetime_as_string(epoch, unit='ms')} UTC"


_EPOCH_RE = re.compile(r"since\s+(.+?)\s*$")


def parse_epoch(units: str) -> np.datetime64:
    """Parse both our ``... UTC`` strings and xarray's ``...+00:00`` format."""
    match = _EPOCH_RE.search(units)
    if match is None:
        raise ValueError(f"could not parse a reference epoch from {units!r}")
    timestamp = match.group(1)
    timestamp = timestamp.removesuffix("UTC").strip().removesuffix("+00:00")
    if "." in timestamp:
        head, fraction = timestamp.split(".")
        timestamp = f"{head}.{fraction[:3]}"
    return np.datetime64(timestamp, "ms")


def to_raw(
    absolute: np.ndarray, epoch: np.datetime64, fill: np.int32
) -> np.ndarray:
    """Encode absolute datetimes as int32 milliseconds relative to epoch."""
    raw = np.full(absolute.shape, int(fill), dtype=np.int32)
    valid = ~np.isnat(absolute)
    offsets = (
        (absolute[valid] - epoch) // np.timedelta64(1, "ms")
    ).astype(np.int64)
    if np.any(offsets == int(fill)):
        raise AssertionError("a valid offset collides with the missing-value code")
    if np.any((offsets < np.iinfo(np.int32).min) | (offsets > np.iinfo(np.int32).max)):
        raise AssertionError("an offset does not fit in signed 32-bit milliseconds")
    raw[valid] = offsets.astype(np.int32)
    return raw


def decode_raw(
    raw: np.ndarray, epoch: np.datetime64, fill: np.int32
) -> np.ndarray:
    absolute = np.full(
        raw.shape, np.datetime64("NaT", "ms"), dtype="datetime64[ms]"
    )
    valid = raw != int(fill)
    absolute[valid] = epoch + raw[valid].astype("timedelta64[ms]")
    return absolute


def same_datetimes(actual: np.ndarray, expected: np.ndarray) -> bool:
    return np.array_equal(
        actual.astype("datetime64[ms]"),
        expected.astype("datetime64[ms]"),
        equal_nan=True,
    )


@dataclass(frozen=True)
class BoundaryTicket:
    """A correction to one side of one record; ``value=None`` sets it missing."""

    ticket_id: str
    record: int
    side: Side
    value: np.datetime64 | None


@dataclass(frozen=True)
class EpochTicket:
    """A reference-time correction; fault modes inject the two mid-way ledgers."""

    ticket_id: str
    new_epoch: np.datetime64
    fault: FaultMode = "none"


Ticket = BoundaryTicket | EpochTicket


@dataclass(frozen=True)
class ArchiveState:
    """The model of truth: absolute observations plus the current reference."""

    epoch: np.datetime64
    time_abs: np.ndarray
    bounds_abs: np.ndarray
    time_fill: np.int32 = TIME_FILL
    bounds_fill: np.int32 = BOUNDS_FILL

    @property
    def n_records(self) -> int:
        return self.time_abs.shape[0]

    def expected_time_raw(self) -> np.ndarray:
        return to_raw(self.time_abs, self.epoch, self.time_fill)

    def expected_bounds_raw(self) -> np.ndarray:
        return to_raw(self.bounds_abs, self.epoch, self.bounds_fill)

    def with_ticket(self, ticket: Ticket) -> ArchiveState:
        if isinstance(ticket, BoundaryTicket):
            bounds = self.bounds_abs.copy()
            bounds[ticket.record, ticket.side] = (
                ticket.value
                if ticket.value is not None
                else np.datetime64("NaT", "ms")
            )
            return replace(self, bounds_abs=bounds)
        return replace(self, epoch=ticket.new_epoch)


@dataclass(frozen=True)
class Finding:
    ticket_id: str
    variable: str
    record: int
    side: int | None
    reader: str
    kind: str
    expected: Any
    actual: Any

    def describe(self) -> str:
        return (
            f"ticket {self.ticket_id!r} variable {self.variable!r} "
            f"record {self.record} side {side_name(self.side)!r}: "
            f"{self.reader} {self.kind} mismatch "
            f"(expected {self.expected!r}, got {self.actual!r})"
        )


@dataclass(frozen=True)
class StepResult:
    ticket: Ticket | None
    findings: list[Finding]


@dataclass(frozen=True)
class ReplayConfig:
    name: str
    legacy: bool
    dataset: str
    epoch_shift_ms: int
    order: int


def build_archive(path: Path, state: ArchiveState, legacy: bool) -> None:
    """Materialize a correct archive for a given model state with raw scipy."""
    with netcdf_file(str(path), "w", version=2, mmap=False) as handle:
        handle.createDimension("time", state.n_records)
        handle.createDimension("nv", 2)
        time_var = handle.createVariable("time", "i4", ("time",))
        time_var.units = units_for(state.epoch)
        time_var.calendar = CALENDAR
        time_var.bounds = "time_bounds"
        time_var._FillValue = np.int32(state.time_fill)
        time_var[:] = state.expected_time_raw().astype(">i4")

        bounds_var = handle.createVariable("time_bounds", "i4", ("time", "nv"))
        bounds_var._FillValue = np.int32(state.bounds_fill)
        if legacy:
            bounds_var.units = units_for(state.epoch)
            bounds_var.calendar = CALENDAR
        bounds_var[:] = state.expected_bounds_raw().astype(">i4")


def read_warehouse(path: Path) -> dict[str, Any]:
    """Read like the warehouse: time epoch, raw integers, each variable's fill."""
    with netcdf_file(str(path), "r", mmap=False) as handle:
        time_var = handle.variables["time"]
        bounds_var = handle.variables["time_bounds"]
        time_units = time_var.units
        if isinstance(time_units, bytes):
            time_units = time_units.decode()
        time_raw = time_var[:].copy().astype(np.int32)
        bounds_raw = bounds_var[:].copy().astype(np.int32)
        time_fill = np.int32(time_var._FillValue)
        bounds_fill = np.int32(bounds_var._FillValue)
        bounds_attrs = {
            key: (value.decode() if isinstance(value, bytes) else value)
            for key, value in bounds_var._attributes.items()
        }
    epoch = parse_epoch(time_units)
    return {
        "epoch": epoch,
        "time_raw": time_raw,
        "bounds_raw": bounds_raw,
        "time_fill": time_fill,
        "bounds_fill": bounds_fill,
        "time_abs": decode_raw(time_raw, epoch, time_fill),
        "bounds_abs": decode_raw(bounds_raw, epoch, bounds_fill),
        "time_missing": time_raw == int(time_fill),
        "bounds_missing": bounds_raw == int(bounds_fill),
        "bounds_units_attr": bounds_attrs.get("units"),
    }


def read_reviewer(path: Path) -> dict[str, Any]:
    """Read like reviewers: plain xarray open of the same closed file."""
    with xr.open_dataset(path, engine="scipy") as dataset:
        time_abs = dataset.time.values.astype("datetime64[ms]")
        bounds_abs = dataset.time_bounds.values.astype("datetime64[ms]")
    return {
        "time_abs": time_abs,
        "bounds_abs": bounds_abs,
        "time_missing": np.isnat(time_abs),
        "bounds_missing": np.isnat(bounds_abs),
    }


def _bounds_encoding(state: ArchiveState) -> dict[str, Any]:
    return {
        "units": units_for(state.epoch),
        "calendar": CALENDAR,
        "_FillValue": np.int32(state.bounds_fill),
        "dtype": np.dtype("int32"),
    }


def _time_encoding(state: ArchiveState) -> dict[str, Any]:
    return {
        "units": units_for(state.epoch),
        "calendar": CALENDAR,
        "_FillValue": np.int32(state.time_fill),
        "dtype": np.dtype("int32"),
    }


def apply_boundary_ticket(
    path: Path, state: ArchiveState, ticket: BoundaryTicket
) -> ArchiveState:
    new_state = state.with_ticket(ticket)
    # Only the bounds variable is submitted and the ticket carries no old
    # encoding; the correct reference comes from the model of truth.
    variable = xr.Variable(
        ("time", "nv"),
        new_state.bounds_abs,
        attrs={},
        encoding=_bounds_encoding(new_state),
    )
    xr.Dataset({"time_bounds": variable}).to_netcdf(
        path, mode="a", engine="scipy"
    )
    return new_state


def apply_epoch_ticket(
    path: Path, state: ArchiveState, ticket: EpochTicket
) -> ArchiveState:
    new_state = state.with_ticket(ticket)
    time_variable = xr.Variable(
        ("time",),
        new_state.time_abs,
        attrs={"bounds": "time_bounds"},
        encoding=_time_encoding(new_state),
    )

    if ticket.fault == "none":
        bounds_variable = xr.Variable(
            ("time", "nv"),
            new_state.bounds_abs,
            attrs={},
            encoding=_bounds_encoding(new_state),
        )
    else:
        # The two faulty ledgers disagree on which reader they fool:
        # * warehouse_side: integers stay on the old epoch and the bounds
        #   units attribute stays old too, so xarray still decodes correctly
        #   while the raw-integer/warehouse interpretation is wrong;
        # * reviewer_side: integers are recomputed on the new epoch but the
        #   bounds units attribute is left at the old epoch, so the warehouse
        #   raw read is right while xarray shifts every window.
        raw_epoch = (
            state.epoch if ticket.fault == "warehouse_side" else ticket.new_epoch
        )
        raw = to_raw(new_state.bounds_abs, raw_epoch, state.bounds_fill)
        bounds_variable = xr.Variable(
            ("time", "nv"),
            raw.astype(">i4"),
            attrs={"units": units_for(state.epoch), "calendar": CALENDAR},
            encoding={
                "_FillValue": np.int32(state.bounds_fill),
                "dtype": np.dtype("int32"),
            },
        )

    dataset = xr.Dataset()
    dataset["time"] = time_variable
    dataset["time_bounds"] = bounds_variable
    dataset.to_netcdf(path, mode="a", engine="scipy")
    return new_state


def apply_ticket(path: Path, state: ArchiveState, ticket: Ticket) -> ArchiveState:
    if isinstance(ticket, BoundaryTicket):
        return apply_boundary_ticket(path, state, ticket)
    return apply_epoch_ticket(path, state, ticket)


def _compare_cells(
    ticket_id: str,
    variable: str,
    reader: str,
    kind: str,
    expected: np.ndarray,
    actual: np.ndarray,
) -> list[Finding]:
    findings: list[Finding] = []
    for index in np.ndindex(expected.shape):
        expected_value = expected[index]
        actual_value = actual[index]
        if isinstance(expected_value, np.datetime64):
            mismatch = not (
                np.isnat(expected_value) and np.isnat(actual_value)
            ) and expected_value != actual_value
        else:
            mismatch = expected_value != actual_value
        if mismatch:
            record = index[0]
            side = index[1] if len(index) > 1 else None
            findings.append(
                Finding(
                    ticket_id=ticket_id,
                    variable=variable,
                    record=int(record),
                    side=side,
                    reader=reader,
                    kind=kind,
                    expected=expected_value,
                    actual=actual_value,
                )
            )
    return findings


def verify_step(path: Path, expected: ArchiveState, ticket_id: str) -> list[Finding]:
    """Reopen the closed file and check both readers against model truth."""
    warehouse = read_warehouse(path)
    reviewer = read_reviewer(path)
    findings: list[Finding] = []

    if warehouse["epoch"] != expected.epoch:
        findings.append(
            Finding(
                ticket_id,
                "time",
                record=0,
                side=None,
                reader="warehouse",
                kind="reference_epoch",
                expected=expected.epoch,
                actual=warehouse["epoch"],
            )
        )
    if warehouse["time_fill"] != expected.time_fill:
        findings.append(
            Finding(
                ticket_id,
                "time",
                0,
                None,
                "warehouse",
                "fill_value",
                expected.time_fill,
                warehouse["time_fill"],
            )
        )
    if warehouse["bounds_fill"] != expected.bounds_fill:
        findings.append(
            Finding(
                ticket_id,
                "time_bounds",
                0,
                None,
                "warehouse",
                "fill_value",
                expected.bounds_fill,
                warehouse["bounds_fill"],
            )
        )

    findings += _compare_cells(
        ticket_id,
        "time",
        "warehouse",
        "raw_integer",
        expected.expected_time_raw(),
        warehouse["time_raw"],
    )
    findings += _compare_cells(
        ticket_id,
        "time_bounds",
        "warehouse",
        "raw_integer",
        expected.expected_bounds_raw(),
        warehouse["bounds_raw"],
    )
    findings += _compare_cells(
        ticket_id,
        "time",
        "warehouse",
        "missing_flag",
        np.isnat(expected.time_abs),
        warehouse["time_missing"],
    )
    findings += _compare_cells(
        ticket_id,
        "time_bounds",
        "warehouse",
        "missing_flag",
        np.isnat(expected.bounds_abs),
        warehouse["bounds_missing"],
    )
    findings += _compare_cells(
        ticket_id,
        "time",
        "warehouse",
        "absolute_time",
        expected.time_abs,
        warehouse["time_abs"],
    )
    findings += _compare_cells(
        ticket_id,
        "time_bounds",
        "warehouse",
        "absolute_time",
        expected.bounds_abs,
        warehouse["bounds_abs"],
    )
    findings += _compare_cells(
        ticket_id,
        "time",
        "reviewer",
        "absolute_time",
        expected.time_abs,
        reviewer["time_abs"],
    )
    findings += _compare_cells(
        ticket_id,
        "time_bounds",
        "reviewer",
        "absolute_time",
        expected.bounds_abs,
        reviewer["bounds_abs"],
    )
    findings += _compare_cells(
        ticket_id,
        "time",
        "reviewer",
        "missing_flag",
        np.isnat(expected.time_abs),
        reviewer["time_missing"],
    )
    findings += _compare_cells(
        ticket_id,
        "time_bounds",
        "reviewer",
        "missing_flag",
        np.isnat(expected.bounds_abs),
        reviewer["bounds_missing"],
    )
    return findings


def report(findings: list[Finding]) -> str:
    return "\n".join(finding.describe() for finding in findings)


# ---------------------------------------------------------------------------
# Replayable observation families
# ---------------------------------------------------------------------------


def _canonical_dataset() -> tuple[np.datetime64, np.ndarray, np.ndarray]:
    epoch = np.datetime64("2000-01-01T00:00:00.250", "ms")
    time_abs = np.array(
        [
            "2000-01-01T00:00:00.250",
            "2000-01-01T00:00:01.250",
            "2000-01-01T00:00:02.250",
        ],
        dtype="datetime64[ms]",
    )
    bounds_abs = np.array(
        [
            [
                "2000-01-01T00:00:00.250",
                "2000-01-01T00:00:00.750",
            ],
            [
                "2000-01-01T00:00:00.750",
                "NaT",
            ],
            [
                "2000-01-01T00:00:01.750",
                "2000-01-01T00:00:02.750",
            ],
        ],
        dtype="datetime64[ms]",
    )
    return epoch, time_abs, bounds_abs


def _wide_dataset() -> tuple[np.datetime64, np.ndarray, np.ndarray]:
    epoch = np.datetime64("2001-06-15T12:30:00.000", "ms")
    time_abs = np.array(
        [
            "2001-06-15T12:30:00.000",
            "2001-06-15T12:30:01.000",
            "2001-06-15T12:30:02.000",
            "2001-06-15T12:30:03.000",
        ],
        dtype="datetime64[ms]",
    )
    bounds_abs = np.array(
        [
            [
                "2001-06-15T12:29:59.500",
                "2001-06-15T12:30:00.500",
            ],
            [
                "2001-06-15T12:30:00.500",
                "2001-06-15T12:30:01.500",
            ],
            [
                "NaT",
                "2001-06-15T12:30:02.500",
            ],
            [
                "2001-06-15T12:30:02.500",
                "2001-06-15T12:30:03.500",
            ],
        ],
        dtype="datetime64[ms]",
    )
    return epoch, time_abs, bounds_abs


DATASETS = {
    "canonical": _canonical_dataset,
    "wide": _wide_dataset,
}


def initial_state(dataset: str) -> ArchiveState:
    epoch, time_abs, bounds_abs = DATASETS[dataset]()
    return ArchiveState(
        epoch=epoch, time_abs=time_abs.copy(), bounds_abs=bounds_abs.copy()
    )


def _edit_tickets(dataset: str) -> list[BoundaryTicket]:
    # Pure value edits on initially valid cells, on distinct records and sides.
    if dataset == "canonical":
        return [
            BoundaryTicket(
                "EDIT-1",
                0,
                LEFT,
                np.datetime64("1999-12-31T23:59:59.250", "ms"),
            ),
            BoundaryTicket(
                "EDIT-2",
                2,
                RIGHT,
                np.datetime64("2000-01-01T00:00:03.000", "ms"),
            ),
        ]
    return [
        BoundaryTicket(
            "EDIT-1",
            0,
            RIGHT,
            np.datetime64("2001-06-15T12:30:00.750", "ms"),
        ),
        BoundaryTicket(
            "EDIT-2",
            3,
            LEFT,
            np.datetime64("2001-06-15T12:30:02.250", "ms"),
        ),
    ]


def _missing_lifecycle_tickets(dataset: str) -> tuple[
    BoundaryTicket,
    BoundaryTicket,
    BoundaryTicket,
]:
    """Tickets covering both missing-value lifecycles.

    ``set_missing`` invalidates an initially valid cell before the reference
    move. Both restores happen only after the reference has moved and moved
    back: one restores a cell missing from the start, the other the cell that
    was set missing across the reference change.
    """
    if dataset == "canonical":
        set_missing = BoundaryTicket("SET-MISSING", 1, LEFT, None)
        restore_initial = BoundaryTicket(
            "RESTORE-INITIAL",
            1,
            RIGHT,
            np.datetime64("2000-01-01T00:00:01.500", "ms"),
        )
        restore_set = BoundaryTicket(
            "RESTORE-SET",
            1,
            LEFT,
            np.datetime64("2000-01-01T00:00:00.600", "ms"),
        )
    else:
        set_missing = BoundaryTicket("SET-MISSING", 1, RIGHT, None)
        restore_initial = BoundaryTicket(
            "RESTORE-INITIAL",
            2,
            LEFT,
            np.datetime64("2001-06-15T12:30:01.800", "ms"),
        )
        restore_set = BoundaryTicket(
            "RESTORE-SET",
            1,
            RIGHT,
            np.datetime64("2001-06-15T12:30:01.200", "ms"),
        )
    return set_missing, restore_initial, restore_set


def build_history(
    dataset: str, epoch_shift_ms: int, order: int, fault: FaultMode = "none"
) -> list[Ticket]:
    state = initial_state(dataset)
    edits = _edit_tickets(dataset)
    if order == 1:
        edits = list(reversed(edits))
    set_missing, restore_initial, restore_set = _missing_lifecycle_tickets(dataset)
    moved_epoch = state.epoch + np.timedelta64(epoch_shift_ms, "ms")
    return [
        *edits,
        set_missing,
        EpochTicket("EPOCH-MOVE", moved_epoch, fault),
        EpochTicket("EPOCH-BACK", state.epoch, "none"),
        restore_initial,
        restore_set,
    ]


def build_epoch_first_history(
    dataset: str, epoch_shift_ms: int
) -> list[Ticket]:
    """Build a history whose reference moves before any boundary ticket lands.

    On a new archive the bounds variable still has no units attribute when the
    first reference-time correction is written, so the native append-mode
    defect cannot activate: every step must verify cleanly. Legacy archives
    already carry that attribute and still expose the reviewer-side defect.
    """
    state = initial_state(dataset)
    edits = _edit_tickets(dataset)
    set_missing, restore_initial, restore_set = _missing_lifecycle_tickets(dataset)
    moved_epoch = state.epoch + np.timedelta64(epoch_shift_ms, "ms")
    return [
        EpochTicket("EPOCH-MOVE", moved_epoch),
        EpochTicket("EPOCH-BACK", state.epoch, "none"),
        *edits,
        set_missing,
        restore_initial,
        restore_set,
    ]


def replay(
    tmp_path: Path,
    config: ReplayConfig,
    fault: FaultMode = "none",
) -> tuple[ArchiveState, list[StepResult]]:
    state = initial_state(config.dataset)
    path = tmp_path / f"{config.name}.nc"
    build_archive(path, state, config.legacy)
    results = [StepResult(None, verify_step(path, state, "INITIAL"))]
    for ticket in build_history(
        config.dataset, config.epoch_shift_ms, config.order, fault
    ):
        state = apply_ticket(path, state, ticket)
        results.append(
            StepResult(ticket, verify_step(path, state, ticket.ticket_id))
        )
    return state, results


EPOCH_SHIFTS_MS = (500, -750, 1500, -2500)


def replay_configs(legacy: bool) -> list[ReplayConfig]:
    return [
        ReplayConfig(
            name=(
                f"legacy={legacy}-{dataset}-shift={shift}-order={order}"
            ),
            legacy=legacy,
            dataset=dataset,
            epoch_shift_ms=shift,
            order=order,
        )
        for dataset in ("canonical", "wide")
        for shift in EPOCH_SHIFTS_MS
        for order in (0, 1)
    ]


KNOWN_DEFECT_REASON = (
    "known defect: a correct reference-time correction cannot update the "
    "bounds units attribute through scipy append mode (existing attributes "
    "are never rewritten) once that attribute is on disk, while cf_encoder "
    "strips the duplicated bounds time encoding; xarray then keeps decoding "
    "the freshly recomputed integers against the old epoch. The warehouse "
    "raw integers stay correct and the next correct tickets heal the file."
)


def is_known_reviewer_defect(findings: list[Finding]) -> bool:
    """Return whether findings match only the native reviewer-side defect.

    The native defect only ever shifts reviewer-side absolute times.
    """
    return bool(findings) and all(
        finding.reader == "reviewer"
        and finding.variable == "time_bounds"
        and finding.kind == "absolute_time"
        for finding in findings
    )


def check_step(
    path: Path,
    expected: ArchiveState,
    ticket_id: str,
    *,
    tolerate_known_defect: bool = False,
) -> bool:
    """Verify one closed-and-reopened step.

    Returns whether the known reviewer-side defect was observed. Any other
    divergence fails immediately with the first offending ticket, variable,
    record and side spelled out in the message.
    """
    findings = verify_step(path, expected, ticket_id)
    if not findings:
        return False
    if tolerate_known_defect and is_known_reviewer_defect(findings):
        # The warehouse side must still be fully right at this step.
        assert not [
            finding for finding in findings if finding.reader == "warehouse"
        ], report(findings)
        return True
    raise AssertionError(f"first divergence at {ticket_id!r}:\n{report(findings)}")


@requires_scipy
class TestTimeBoundsCorrections:
    @pytest.mark.parametrize(
        "config",
        replay_configs(legacy=False) + replay_configs(legacy=True),
        ids=lambda config: config.name,
    )
    def test_correction_history_replays(self, tmp_path: Path, config: ReplayConfig) -> None:
        # Replayable family: both archive generations, both boundary-edit
        # orders, and reference moves in both directions and magnitudes. The
        # findings of every step were computed against the model-of-truth
        # state captured at that step, never from a later file.
        _, results = replay(tmp_path, config)
        defect_seen = False
        for result in results:
            ticket_id = (
                result.ticket.ticket_id if result.ticket is not None else "INITIAL"
            )
            if not result.findings:
                continue
            if isinstance(result.ticket, EpochTicket) and is_known_reviewer_defect(
                result.findings
            ):
                # The warehouse side must stay fully right at this step.
                assert not [
                    finding
                    for finding in result.findings
                    if finding.reader == "warehouse"
                ], report(result.findings)
                defect_seen = True
                continue
            raise AssertionError(
                f"first divergence at {ticket_id!r}:\n{report(result.findings)}"
            )
        if defect_seen:
            pytest.xfail(KNOWN_DEFECT_REASON)

    def test_canonical_example_integer_anchors(self, tmp_path: Path) -> None:
        # Hard-coded anchors from the workflow description: the corrected left
        # boundary is -1000 ms relative to the first reference and becomes
        # -1500 ms once the reference moves forward by 500 ms.
        state = initial_state("canonical")
        path = tmp_path / "anchors.nc"
        build_archive(path, state, legacy=False)

        first = BoundaryTicket(
            "EDIT-1",
            0,
            LEFT,
            np.datetime64("1999-12-31T23:59:59.250", "ms"),
        )
        state = apply_ticket(path, state, first)
        warehouse = read_warehouse(path)
        assert warehouse["bounds_raw"][0, LEFT] == -1000
        assert not warehouse["bounds_missing"][0, LEFT]
        assert check_step(path, state, "EDIT-1") is False

        moved_epoch = np.datetime64("2000-01-01T00:00:00.750", "ms")
        state = apply_ticket(path, state, EpochTicket("EPOCH-MOVE", moved_epoch))
        warehouse = read_warehouse(path)
        # Raw integers and missing flags are correct on both epoch tickets.
        assert warehouse["bounds_raw"][0, LEFT] == -1500
        assert warehouse["epoch"] == moved_epoch
        defect_seen = check_step(
            path, state, "EPOCH-MOVE", tolerate_known_defect=True
        )

        state = apply_ticket(
            path,
            state,
            EpochTicket(
                "EPOCH-BACK",
                np.datetime64("2000-01-01T00:00:00.250", "ms"),
            ),
        )
        restore = BoundaryTicket(
            "RESTORE-INITIAL",
            1,
            RIGHT,
            np.datetime64("2000-01-01T00:00:01.500", "ms"),
        )
        state = apply_ticket(path, state, restore)
        warehouse = read_warehouse(path)
        assert warehouse["bounds_raw"][1, RIGHT] == 1250
        assert not warehouse["bounds_missing"][1, RIGHT]
        assert check_step(path, state, "RESTORE-INITIAL") is False
        if defect_seen:
            pytest.xfail(KNOWN_DEFECT_REASON)

    @pytest.mark.parametrize("dataset", ["canonical", "wide"])
    def test_epoch_first_history_fully_green_on_new_archive(
        self, tmp_path: Path, dataset: str
    ) -> None:
        # Normal control: when the reference moves before any boundary ticket
        # writes a units attribute onto the bounds variable, a new archive has
        # no stale attribute to preserve, so every step -- including both
        # reference moves in either direction and magnitude -- verifies on
        # both readers with no tolerated failure.
        for shift in EPOCH_SHIFTS_MS:
            state = initial_state(dataset)
            path = tmp_path / f"epoch-first-{dataset}-{shift}.nc"
            build_archive(path, state, legacy=False)
            for ticket in build_epoch_first_history(dataset, shift):
                state = apply_ticket(path, state, ticket)
                assert verify_step(
                    path, state, ticket.ticket_id
                ) == [], f"shift={shift} ticket={ticket.ticket_id}"

    @pytest.mark.parametrize("dataset", ["canonical", "wide"])
    def test_epoch_first_history_legacy_only_fails_at_move(
        self, tmp_path: Path, dataset: str
    ) -> None:
        # On legacy archives the pre-existing bounds units attribute makes the
        # first reference move expose the native reviewer defect; the warehouse
        # side stays correct and every later step verifies cleanly.
        defect_seen = False
        for shift in EPOCH_SHIFTS_MS:
            state = initial_state(dataset)
            path = tmp_path / f"epoch-first-legacy-{dataset}-{shift}.nc"
            build_archive(path, state, legacy=True)
            for ticket in build_epoch_first_history(dataset, shift):
                state = apply_ticket(path, state, ticket)
                defect_seen |= check_step(
                    path,
                    state,
                    ticket.ticket_id,
                    tolerate_known_defect=isinstance(ticket, EpochTicket),
                )
        if defect_seen:
            pytest.xfail(KNOWN_DEFECT_REASON)

    @pytest.mark.parametrize("dataset", ["canonical", "wide"])
    def test_two_generations_equivalent(
        self, tmp_path: Path, dataset: str
    ) -> None:
        state = initial_state(dataset)
        legacy_path = tmp_path / "legacy.nc"
        new_path = tmp_path / "new.nc"
        build_archive(legacy_path, state, legacy=True)
        build_archive(new_path, state, legacy=False)

        legacy_warehouse = read_warehouse(legacy_path)
        new_warehouse = read_warehouse(new_path)
        np.testing.assert_array_equal(
            legacy_warehouse["bounds_raw"], new_warehouse["bounds_raw"]
        )
        np.testing.assert_array_equal(
            legacy_warehouse["time_raw"], new_warehouse["time_raw"]
        )
        assert legacy_warehouse["bounds_units_attr"] is not None
        assert new_warehouse["bounds_units_attr"] is None

        legacy_reviewer = read_reviewer(legacy_path)
        new_reviewer = read_reviewer(new_path)
        assert same_datetimes(
            legacy_reviewer["bounds_abs"], new_reviewer["bounds_abs"]
        )
        assert same_datetimes(
            legacy_reviewer["time_abs"], new_reviewer["time_abs"]
        )
        assert same_datetimes(legacy_reviewer["bounds_abs"], state.bounds_abs)

    def test_histories_converge_on_same_observations(
        self, tmp_path: Path
    ) -> None:
        # Two histories move the reference along different paths and apply the
        # boundary edits in different orders, but end at the same epoch with
        # the same absolute observations.
        base = initial_state("canonical")
        tickets_a = build_history("canonical", epoch_shift_ms=500, order=0)
        tickets_b = build_history("canonical", epoch_shift_ms=-1500, order=1)

        def run(name: str, tickets: list[Ticket]) -> ArchiveState:
            state = base
            path = tmp_path / f"{name}.nc"
            build_archive(path, state, legacy=False)
            for ticket in tickets:
                state = apply_ticket(path, state, ticket)
                check_step(
                    path,
                    state,
                    ticket.ticket_id,
                    tolerate_known_defect=isinstance(ticket, EpochTicket),
                )
            return state

        state_a = run("history_a", tickets_a)
        state_b = run("history_b", tickets_b)
        assert state_a.epoch == state_b.epoch == base.epoch
        assert same_datetimes(state_a.bounds_abs, state_b.bounds_abs)
        assert same_datetimes(state_a.time_abs, state_b.time_abs)

        warehouse_a = read_warehouse(tmp_path / "history_a.nc")
        warehouse_b = read_warehouse(tmp_path / "history_b.nc")
        np.testing.assert_array_equal(
            warehouse_a["bounds_raw"], warehouse_b["bounds_raw"]
        )
        np.testing.assert_array_equal(
            warehouse_a["time_raw"], warehouse_b["time_raw"]
        )
        reviewer_a = read_reviewer(tmp_path / "history_a.nc")
        reviewer_b = read_reviewer(tmp_path / "history_b.nc")
        assert same_datetimes(reviewer_a["bounds_abs"], reviewer_b["bounds_abs"])

    @pytest.mark.parametrize("legacy", [False, True], ids=["new", "legacy"])
    @pytest.mark.parametrize("resume_at", range(1, 8))
    def test_resume_from_expected_archive(
        self, tmp_path: Path, legacy: bool, resume_at: int
    ) -> None:
        # Rebuild the archive that *should* exist at any step purely from the
        # model of truth, then continue with the remaining tickets. A corrupted
        # earlier file never feeds the expectations of a later step.
        state = initial_state("canonical")
        tickets = build_history("canonical", epoch_shift_ms=750, order=0)
        expected_states = [state]
        for ticket in tickets:
            state = state.with_ticket(ticket)
            expected_states.append(state)

        resume_state = expected_states[resume_at]
        path = tmp_path / "resumed.nc"
        build_archive(path, resume_state, legacy=legacy)
        defect_seen = check_step(
            path, resume_state, f"RESUME-{resume_at}"
        )
        current = resume_state
        for ticket in tickets[resume_at:]:
            current = apply_ticket(path, current, ticket)
            defect_seen |= check_step(
                path,
                current,
                ticket.ticket_id,
                tolerate_known_defect=isinstance(ticket, EpochTicket),
            )
        if defect_seen:
            pytest.xfail(KNOWN_DEFECT_REASON)

    @pytest.mark.parametrize("legacy", [False, True], ids=["new", "legacy"])
    @pytest.mark.parametrize("dataset", ["canonical", "wide"])
    @pytest.mark.parametrize("shift", EPOCH_SHIFTS_MS)
    def test_warehouse_side_fault_detected_at_first_ticket(
        self, tmp_path: Path, legacy: bool, dataset: str, shift: int
    ) -> None:
        config = ReplayConfig(
            name=f"warehouse-fault-{legacy}-{dataset}-{shift}",
            legacy=legacy,
            dataset=dataset,
            epoch_shift_ms=shift,
            order=0,
        )
        _, results = replay(tmp_path, config, fault="warehouse_side")
        by_ticket = {
            result.ticket.ticket_id: result.findings
            for result in results
            if result.ticket is not None
        }
        for result in results:
            if (
                result.ticket is None
                or result.ticket.ticket_id != "EPOCH-MOVE"
            ):
                assert not result.findings, report(result.findings)

        first = by_ticket["EPOCH-MOVE"]
        assert first, "the faulty epoch ticket must be detected"
        assert {finding.ticket_id for finding in first} == {"EPOCH-MOVE"}
        assert {finding.variable for finding in first} == {"time_bounds"}
        assert {finding.reader for finding in first} == {"warehouse"}
        # The reviewer still decodes correctly; fill values and missing flags
        # are untouched, so only raw integers and their interpretation diverge.
        assert not any(finding.reader == "reviewer" for finding in first)
        assert not any(
            finding.kind in {"missing_flag", "fill_value"} for finding in first
        )
        assert {finding.kind for finding in first} == {
            "raw_integer",
            "absolute_time",
        }
        anchor = next(
            finding
            for finding in first
            if finding.record == 0 and finding.side == LEFT
        )
        assert anchor.kind == "raw_integer"
        assert anchor.expected != anchor.actual

        self._assert_fault_history_heals(config, results, tmp_path)

    @pytest.mark.parametrize("legacy", [False, True], ids=["new", "legacy"])
    @pytest.mark.parametrize("dataset", ["canonical", "wide"])
    @pytest.mark.parametrize("shift", EPOCH_SHIFTS_MS)
    def test_reviewer_side_fault_detected_at_first_ticket(
        self, tmp_path: Path, legacy: bool, dataset: str, shift: int
    ) -> None:
        config = ReplayConfig(
            name=f"reviewer-fault-{legacy}-{dataset}-{shift}",
            legacy=legacy,
            dataset=dataset,
            epoch_shift_ms=shift,
            order=0,
        )
        _, results = replay(tmp_path, config, fault="reviewer_side")
        by_ticket = {
            result.ticket.ticket_id: result.findings
            for result in results
            if result.ticket is not None
        }
        for result in results:
            if (
                result.ticket is None
                or result.ticket.ticket_id != "EPOCH-MOVE"
            ):
                assert not result.findings, report(result.findings)

        first = by_ticket["EPOCH-MOVE"]
        assert first
        assert {finding.reader for finding in first} == {"reviewer"}
        assert {finding.variable for finding in first} == {"time_bounds"}
        # The warehouse raw integers, fill values and missing flags are right.
        assert {finding.kind for finding in first} == {"absolute_time"}
        anchor = next(
            finding
            for finding in first
            if finding.record == 0 and finding.side == LEFT
        )
        assert anchor.reader == "reviewer"
        assert anchor.expected != anchor.actual

        self._assert_fault_history_heals(config, results, tmp_path)

    def _assert_fault_history_heals(
        self,
        config: ReplayConfig,
        results: list[StepResult],
        tmp_path: Path,
    ) -> None:
        # Both injected ledgers return to fully correct readings on both
        # readers after the later correct tickets, including the missing-cell
        # restores that span the reference-time round trip.
        for result in results:
            if result.ticket is None or result.ticket.ticket_id == "EPOCH-MOVE":
                continue
            assert not result.findings, report(result.findings)
        final = results[-1]
        assert final.ticket is not None and not final.findings

        state = initial_state(config.dataset)
        for ticket in build_history(
            config.dataset, config.epoch_shift_ms, config.order
        ):
            state = state.with_ticket(ticket)
        assert verify_step(
            tmp_path / f"{config.name}.nc", state, "FINAL"
        ) == []

    @pytest.mark.xfail(strict=True, reason=KNOWN_DEFECT_REASON)
    @pytest.mark.parametrize("legacy", [False, True], ids=["new", "legacy"])
    @pytest.mark.parametrize("dataset", ["canonical", "wide"])
    def test_known_defect_single_step_marker(
        self, tmp_path: Path, legacy: bool, dataset: str
    ) -> None:
        # Dedicated single-step marker: an otherwise correct reference-time
        # correction on an archive whose bounds already carry units. Strict
        # xfail flips to an XPASS failure once the defect is fixed, signaling
        # that this marker and the tolerant checks can be tightened.
        state = initial_state(dataset)
        path = tmp_path / "known-defect.nc"
        build_archive(path, state, legacy=legacy)
        # A boundary ticket first puts (or confirms) units on the bounds
        # variable, matching the real replay ordering.
        edit = _edit_tickets(dataset)[0]
        state = apply_ticket(path, state, edit)
        assert verify_step(path, state, edit.ticket_id) == []
        moved_epoch = state.epoch + np.timedelta64(500, "ms")
        state = apply_ticket(
            path, state, EpochTicket("EPOCH-MOVE", moved_epoch)
        )
        findings = verify_step(path, state, "EPOCH-MOVE")
        assert not [
            finding for finding in findings if finding.reader == "reviewer"
        ], report(findings)

    def test_known_defect_step_other_reader_and_controls(
        self, tmp_path: Path
    ) -> None:
        # At the defective step the warehouse raw integers, fill values and
        # absolute times are still correct on both generations, even though
        # the reviewer side diverges.
        state = initial_state("canonical")
        moved_epoch = state.epoch + np.timedelta64(500, "ms")
        for legacy in (False, True):
            path = tmp_path / f"defective-{legacy}.nc"
            build_archive(path, state, legacy=legacy)
            edit = _edit_tickets("canonical")[0]
            stepped = apply_ticket(path, state, edit)
            stepped = apply_ticket(
                path, stepped, EpochTicket("EPOCH-MOVE", moved_epoch)
            )
            findings = verify_step(path, stepped, "EPOCH-MOVE")
            assert not [
                finding for finding in findings if finding.reader == "warehouse"
            ], report(findings)
            assert is_known_reviewer_defect(findings)

        # Normal control: a correctly rebuilt new archive at that same step,
        # replayed through every remaining ticket (reference move back plus
        # the missing-cell restores), stays fully green on both readers.
        correct_state = state.with_ticket(
            EpochTicket("EPOCH-MOVE", moved_epoch)
        )
        resume_path = tmp_path / "control-resume.nc"
        build_archive(resume_path, correct_state, legacy=False)
        current = correct_state
        for ticket in build_history("canonical", 500, 0)[4:]:
            current = apply_ticket(resume_path, current, ticket)
            assert verify_step(
                resume_path, current, ticket.ticket_id
            ) == [], report(verify_step(resume_path, current, ticket.ticket_id))
        assert verify_step(resume_path, current, "FINAL") == []

    def test_missing_value_codes_remain_distinct_and_preserved(
        self, tmp_path: Path
    ) -> None:
        config = ReplayConfig(
            name="fills",
            legacy=False,
            dataset="canonical",
            epoch_shift_ms=-750,
            order=0,
        )
        _, results = replay(tmp_path, config)
        # Only the known reviewer defect at the epoch step may be present.
        for result in results:
            if result.findings:
                assert isinstance(result.ticket, EpochTicket)
                assert is_known_reviewer_defect(result.findings)
        warehouse = read_warehouse(tmp_path / "fills.nc")
        assert warehouse["time_fill"] == TIME_FILL
        assert warehouse["bounds_fill"] == BOUNDS_FILL
        assert warehouse["time_fill"] != warehouse["bounds_fill"]
        # SET-MISSING invalidated a cell across the reference round trip and
        # RESTORE-SET brought it back; RESTORE-INITIAL recovered the cell that
        # was missing in the original archive. Both end valid, neither carries
        # a foreign fill code.
        assert not warehouse["bounds_missing"][1, LEFT]
        assert not warehouse["bounds_missing"][1, RIGHT]
        assert warehouse["bounds_raw"][1, LEFT] != int(BOUNDS_FILL)
        assert warehouse["bounds_raw"][1, RIGHT] != int(BOUNDS_FILL)

    def test_boundary_ticket_leaves_time_variable_untouched(
        self, tmp_path: Path
    ) -> None:
        state = initial_state("canonical")
        path = tmp_path / "bounds-only.nc"
        build_archive(path, state, legacy=False)
        before = read_warehouse(path)
        ticket = BoundaryTicket(
            "EDIT-1",
            0,
            LEFT,
            np.datetime64("1999-12-31T23:59:59.250", "ms"),
        )
        new_state = apply_ticket(path, state, ticket)
        after = read_warehouse(path)
        np.testing.assert_array_equal(before["time_raw"], after["time_raw"])
        assert before["epoch"] == after["epoch"]
        assert verify_step(path, new_state, "EDIT-1") == []
