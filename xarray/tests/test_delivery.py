from __future__ import annotations

import itertools
from collections.abc import Sequence

import numpy as np
import pandas as pd
import pytest

import xarray as xr
from xarray.structure.merge import MergeError

TIMES = pd.to_datetime(["2026-11-24 09:00", "2026-11-24 10:00"])
STATIONS = ["east", "west"]
CAL_7 = pd.Timestamp("2026-11-07")
CAL_9 = pd.Timestamp("2026-11-09")
BATCH_JIA = "jia"
BATCH_YI = "yi"


def make_instrument(
    instrument: str,
    values: np.ndarray,
    license: str,
    spec: str,
    calibration_date: object,
    quota: int | None = None,
) -> xr.Dataset:
    attrs = {
        "license": license,
        "processing_specification": spec,
        "calibration_date": calibration_date,
    }
    if quota is not None:
        attrs["read_quota"] = quota
    return xr.Dataset(
        data_vars={"temperature": (("station", "time"), values)},
        coords={
            "station": STATIONS,
            "time": TIMES,
            "instrument": instrument,
        },
        attrs=attrs,
    )


def instrument_datasets() -> list[xr.Dataset]:
    instrument_a = make_instrument(
        "A",
        np.array([[12.0, np.nan], [14.0, np.nan]]),
        "L1",
        "v1",
        CAL_7,
    )
    instrument_b = make_instrument(
        "B",
        np.array([[12.0, 0.0], [16.0, np.nan]]),
        "L2",
        "v2",
        None,
        quota=1,
    )
    instrument_c = make_instrument(
        "C",
        np.array([[11.0, 13.0], [np.nan, np.nan]]),
        "L3",
        "v2",
        CAL_9,
    )
    return [instrument_a, instrument_b, instrument_c]


def make_manifest(quality_flags: Sequence[bool]) -> xr.Dataset:
    n_rows = len(quality_flags)
    return xr.Dataset(
        data_vars={
            "batch": (
                ["delivery_record"],
                [BATCH_JIA, BATCH_JIA, BATCH_JIA, BATCH_YI][:n_rows],
            ),
            "station": (
                ["delivery_record"],
                ["east", "east", "east", "west"][:n_rows],
            ),
            "time": (
                ["delivery_record"],
                [TIMES[0], TIMES[0], TIMES[1], TIMES[0]][:n_rows],
            ),
            "quality_flagged": (["delivery_record"], list(quality_flags)),
        },
        coords={"delivery_record": [f"r{index + 1}" for index in range(n_rows)]},
    )


def delivery_options(manifest: xr.Dataset) -> xr.DeliveryOptions:
    return xr.DeliveryOptions(
        manifest=manifest,
        instrument_priority=["A", "B", "C"],
        licenses={
            BATCH_JIA: ["L1", "L2", "L3"],
            BATCH_YI: ["L2"],
        },
    )


def delivered(dataset: xr.Dataset, record_id: str) -> dict[str, object]:
    row = dataset.sel(delivery_record=record_id)
    return {
        "temperature": row["temperature"].values.item(),
        "instrument": row["delivery_instrument"].values.item(),
        "license": row["delivery_license"].values.item(),
        "spec": row["processing_specification"].values.item(),
        "calibration_date": row["calibration_date"].values.item(),
        "status": row["delivery_status"].values.item(),
    }


class TestDeliveryMerge:
    def test_original_manifest_selection(self) -> None:
        result = xr.merge(
            instrument_datasets(), delivery=delivery_options(make_manifest([False] * 4))
        )

        assert result.sizes["delivery_record"] == 4
        assert list(result["delivery_record"].values) == ["r1", "r2", "r3", "r4"]

        first = delivered(result, "r1")
        second = delivered(result, "r2")
        third = delivered(result, "r3")
        fourth = delivered(result, "r4")

        assert (first["temperature"], first["instrument"], first["spec"]) == (
            11.0,
            "C",
            "v2",
        )
        assert (second["temperature"], second["instrument"]) == (11.0, "C")
        assert (third["temperature"], third["instrument"]) == (13.0, "C")
        assert (fourth["temperature"], fourth["instrument"], fourth["license"]) == (
            16.0,
            "B",
            "L2",
        )
        assert first["calibration_date"] == CAL_9
        assert pd.isna(fourth["calibration_date"])
        assert all(
            delivered(result, record)["status"] == "delivered"
            for record in ["r1", "r2", "r3", "r4"]
        )

    def test_flagged_row_frees_quota_for_higher_priority(self) -> None:
        manifest = make_manifest([False, False, True, False])
        result = xr.merge(instrument_datasets(), delivery=delivery_options(manifest))

        assert delivered(result, "r1")["instrument"] == "A"
        assert delivered(result, "r2")["instrument"] == "A"
        assert delivered(result, "r1")["spec"] == "v1"
        assert delivered(result, "r1")["temperature"] == 12.0
        flagged = delivered(result, "r3")
        assert flagged["status"] == "quality_flagged"
        assert np.isnan(flagged["temperature"])
        assert flagged["instrument"] is None
        assert delivered(result, "r4")["instrument"] == "B"

    def test_flagged_receiver_row_reuses_single_reading(self) -> None:
        manifest = make_manifest([False, False, False, True])
        result = xr.merge(instrument_datasets(), delivery=delivery_options(manifest))

        assert delivered(result, "r1")["instrument"] == "B"
        assert delivered(result, "r2")["instrument"] == "B"
        assert delivered(result, "r1")["temperature"] == 12.0
        assert delivered(result, "r3")["instrument"] == "C"
        assert delivered(result, "r3")["temperature"] == 13.0
        assert delivered(result, "r4")["status"] == "quality_flagged"

    def test_zero_is_delivered_raw_missing_is_not(self) -> None:
        manifest = xr.Dataset(
            data_vars={
                "batch": (["delivery_record"], [BATCH_JIA, BATCH_JIA]),
                "station": (["delivery_record"], ["east", "east"]),
                "time": (["delivery_record"], [TIMES[1], TIMES[0]]),
                "quality_flagged": (["delivery_record"], [False, False]),
            },
            coords={"delivery_record": ["zero", "missing"]},
        )
        result = xr.merge(instrument_datasets(), delivery=delivery_options(manifest))

        assert delivered(result, "zero")["temperature"] == 0.0
        assert delivered(result, "zero")["instrument"] == "B"
        # A point that no instrument covers is unassignable, not flagged.
        unreachable = xr.Dataset(
            data_vars={
                "batch": (["delivery_record"], [BATCH_YI]),
                "station": (["delivery_record"], ["west"]),
                "time": (["delivery_record"], [TIMES[1]]),
                "quality_flagged": (["delivery_record"], [False]),
            },
            coords={"delivery_record": ["none"]},
        )
        missing_result = xr.merge(
            instrument_datasets(), delivery=delivery_options(unreachable)
        )
        missing_row = delivered(missing_result, "none")
        assert missing_row["status"] == "unassigned"
        assert np.isnan(missing_row["temperature"])
        assert missing_row["instrument"] is None

    def test_quality_flagged_distinct_from_unassigned(self) -> None:
        manifest = xr.Dataset(
            data_vars={
                "batch": (["delivery_record"], [BATCH_YI, BATCH_YI]),
                "station": (["delivery_record"], ["west", "west"]),
                "time": (["delivery_record"], [TIMES[1], TIMES[1]]),
                "quality_flagged": (["delivery_record"], [True, False]),
            },
            coords={"delivery_record": ["flagged", "empty"]},
        )
        result = xr.merge(instrument_datasets(), delivery=delivery_options(manifest))

        assert delivered(result, "flagged")["status"] == "quality_flagged"
        assert delivered(result, "empty")["status"] == "unassigned"
        for record in ["flagged", "empty"]:
            assert np.isnan(delivered(result, record)["temperature"])

    def test_resubmission_reselects_with_fresh_quotas(self) -> None:
        full = xr.merge(
            instrument_datasets(), delivery=delivery_options(make_manifest([False] * 4))
        )
        jia_only_manifest = make_manifest([False, False, False])
        resubmitted = xr.merge(
            instrument_datasets(), delivery=delivery_options(jia_only_manifest)
        )

        assert delivered(resubmitted, "r1")["instrument"] == "B"
        assert delivered(resubmitted, "r2")["instrument"] == "B"
        assert delivered(resubmitted, "r3")["instrument"] == "C"

        # Pulling 甲's rows out of the merged result keeps delivered values.
        jia_rows = full.sel(delivery_record=["r1", "r2", "r3"])
        assert list(jia_rows["delivery_instrument"].values) == ["C", "C", "C"]
        assert list(jia_rows["temperature"].values) == [11.0, 11.0, 13.0]

    def test_invariant_to_input_and_coordinate_order(self) -> None:
        manifest = make_manifest([False] * 4)
        options = delivery_options(manifest)
        reference = xr.merge(instrument_datasets(), delivery=options)

        instrument_a, instrument_b, instrument_c = instrument_datasets()
        reordered = [
            instrument_c.isel(station=[1, 0], time=[1, 0]),
            instrument_a.isel(station=[1, 0], time=[1, 0]),
            instrument_b.isel(station=[1, 0], time=[1, 0]),
        ]
        result = xr.merge(reordered, delivery=options)

        for record_id in reference["delivery_record"].values:
            assert result.sel(delivery_record=record_id).equals(
                reference.sel(delivery_record=record_id)
            )

    def test_only_unflagged_candidate_points_are_read(self) -> None:
        # The flagged row points at coordinates absent from every instrument
        # dataset; selection must never try to read them.
        manifest = make_manifest([False, False, True, False])
        sliced = [
            ds.sel(station=["east", "west"], time=[TIMES[0]])
            for ds in instrument_datasets()
        ]
        result = xr.merge(sliced, delivery=delivery_options(manifest))

        assert delivered(result, "r1")["status"] == "delivered"
        assert delivered(result, "r3")["status"] == "quality_flagged"
        assert delivered(result, "r4")["status"] == "delivered"

    def test_lazy_backend_only_reads_unflagged_points(self) -> None:
        class TrackingArray(np.ndarray):
            accesses: list[object] = []

            def __getitem__(self, key):  # type: ignore[override]
                TrackingArray.accesses.append(key)
                return super().__getitem__(key)

        raw = np.array([[12.0, np.nan], [14.0, np.nan]]).view(TrackingArray)
        dataset = xr.Dataset(
            {"temperature": (("station", "time"), raw)},
            coords={
                "station": STATIONS,
                "time": TIMES,
                "instrument": "A",
            },
            attrs={
                "license": "L1",
                "processing_specification": "v1",
                "calibration_date": CAL_7,
            },
        )
        manifest = xr.Dataset(
            data_vars={
                "batch": (["delivery_record"], [BATCH_JIA, BATCH_JIA]),
                "station": (["delivery_record"], ["east", "west"]),
                "time": (["delivery_record"], [TIMES[0], TIMES[1]]),
                "quality_flagged": (["delivery_record"], [False, True]),
            },
            coords={"delivery_record": ["r1", "r2"]},
        )
        options = xr.DeliveryOptions(
            manifest=manifest,
            instrument_priority=["A"],
            licenses={BATCH_JIA: ["L1"]},
        )
        TrackingArray.accesses.clear()
        result = xr.merge([dataset], delivery=options)

        assert delivered(result, "r1")["temperature"] == 12.0
        assert delivered(result, "r2")["status"] == "quality_flagged"
        assert all(
            np.array_equal(key[0], [0]) and np.array_equal(key[1], [0])
            for key in TrackingArray.accesses
        )

    def test_explicit_read_quotas_argument(self) -> None:
        manifest = make_manifest([False] * 4)
        datasets = instrument_datasets()
        for dataset in datasets:
            dataset.attrs.pop("read_quota", None)
        options = xr.DeliveryOptions(
            manifest=manifest,
            instrument_priority=["A", "B", "C"],
            licenses={
                BATCH_JIA: ["L1", "L2", "L3"],
                BATCH_YI: ["L2"],
            },
            read_quotas={"B": 1},
        )
        result = xr.merge(datasets, delivery=options)

        assert delivered(result, "r1")["instrument"] == "C"
        assert delivered(result, "r4")["instrument"] == "B"

    def test_batch_single_processing_specification_enforced(self) -> None:
        # Two rows for 甲 that can only be served from different specs; the
        # maximizer still delivers both by choosing a shared specification
        # rather than mixing v1 and v2 inside the batch.
        manifest = xr.Dataset(
            data_vars={
                "batch": (["delivery_record"], [BATCH_JIA, BATCH_JIA]),
                "station": (["delivery_record"], ["east", "west"]),
                "time": (["delivery_record"], [TIMES[0], TIMES[0]]),
                "quality_flagged": (["delivery_record"], [False, False]),
            },
            coords={"delivery_record": ["s1", "s2"]},
        )
        result = xr.merge(instrument_datasets(), delivery=delivery_options(manifest))
        specs = {delivered(result, record)["spec"] for record in ["s1", "s2"]}
        assert len(specs) == 1
        assert all(
            delivered(result, record)["status"] == "delivered"
            for record in ["s1", "s2"]
        )

    def test_duplicate_record_ids_rejected(self) -> None:
        manifest = make_manifest([False] * 4).assign_coords(
            delivery_record=["r1", "r1", "r3", "r4"]
        )
        with pytest.raises(MergeError, match="unique"):
            xr.merge(instrument_datasets(), delivery=delivery_options(manifest))

    def test_missing_manifest_column_rejected(self) -> None:
        manifest = make_manifest([False] * 4).drop_vars("quality_flagged")
        with pytest.raises(MergeError, match="quality_flagged"):
            xr.merge(instrument_datasets(), delivery=delivery_options(manifest))

    def test_unknown_instrument_rejected(self) -> None:
        options = xr.DeliveryOptions(
            manifest=make_manifest([False] * 4),
            instrument_priority=["A", "B"],
            licenses={
                BATCH_JIA: ["L1", "L2", "L3"],
                BATCH_YI: ["L2"],
            },
        )
        with pytest.raises(MergeError, match="instrument_priority"):
            xr.merge(instrument_datasets(), delivery=options)

    def test_delivery_rejects_datatree(self) -> None:
        tree = xr.DataTree.from_dict({"/": xr.Dataset()})
        with pytest.raises(TypeError, match="DataTree"):
            xr.merge(
                [tree],
                delivery=delivery_options(make_manifest([False])),
            )

    def test_duplicate_instrument_dataset_rejected(self) -> None:
        datasets = instrument_datasets()
        with pytest.raises(MergeError, match="more than one"):
            xr.merge(
                [datasets[0], datasets[0]],
                delivery=delivery_options(make_manifest([False])),
            )

    def test_integer_record_ids_keep_dtype_and_select_by_id(self) -> None:
        manifest = make_manifest([False] * 4).assign_coords(
            delivery_record=[10, 20, 30, 40]
        )
        result = xr.merge(instrument_datasets(), delivery=delivery_options(manifest))

        assert result["delivery_record"].dtype.kind == "i"
        assert list(result["delivery_record"].values) == [10, 20, 30, 40]
        assert delivered(result, 40)["instrument"] == "B"

    def test_plain_merge_rules_unchanged(self) -> None:
        with pytest.raises(MergeError, match="conflicting values"):
            xr.merge(instrument_datasets())


def _brute_force_reference(
    rows: list[dict[str, object]],
    candidates: list[list[dict[str, object]]],
    priority: list[str],
    quotas: dict[str, int],
) -> list[str | None]:
    """Independent exhaustive reference for the selection optimizer."""
    ranks = {instrument: index for index, instrument in enumerate(priority)}
    empty_rank = len(priority)
    domains = [list(range(-1, len(row_candidates))) for row_candidates in candidates]

    best: tuple[int, list[int]] | None = None
    best_assignment: list[int] = []
    for assignment in itertools.product(*domains):
        used: dict[str, set[tuple[object, object]]] = {}
        specs: dict[object, set[str]] = {}
        count = 0
        for row, choice in zip(rows, assignment, strict=True):
            if row["quality_flagged"]:
                if choice != -1:
                    break
                continue
            if choice == -1:
                continue
            reading = candidates[row["index"]][choice]
            point = (reading["station"], reading["time"])
            instrument = reading["instrument"]
            bucket = used.setdefault(instrument, set())
            if point not in bucket:
                if len(bucket) >= quotas[instrument]:
                    break
                bucket.add(point)
            specs.setdefault(row["batch"], set()).add(reading["spec"])
            if any(len(batch_specs) > 1 for batch_specs in specs.values()):
                break
            count += 1
        else:
            key = (
                count,
                [
                    -empty_rank
                    if choice == -1
                    else -ranks[candidates[index][choice]["instrument"]]
                    for index, choice in enumerate(assignment)
                ],
            )
            if best is None or key > best:
                best = key
                best_assignment = list(assignment)

    assert best is not None
    return [
        None if choice == -1 else candidates[index][choice]["instrument"]
        for index, choice in enumerate(best_assignment)
    ]


@pytest.mark.parametrize("seed", range(20))
def test_optimizer_matches_exhaustive_reference(seed: int) -> None:
    rng = np.random.default_rng(seed)
    instruments = ["A", "B", "C"]
    specs = {"A": "v1", "B": "v2", "C": "v2"}
    licenses = {"A": "L1", "B": "L2", "C": "L3"}
    stations = ["east", "west"]
    times = list(TIMES)
    quotas = {instrument: int(rng.integers(1, 4)) for instrument in instruments}

    n_rows = int(rng.integers(1, 7))
    rows: list[dict[str, object]] = [
        {
            "index": index,
            "record": f"r{index}",
            "batch": BATCH_JIA if rng.random() < 0.6 else BATCH_YI,
            "station": stations[int(rng.integers(len(stations)))],
            "time": times[int(rng.integers(len(times)))],
            "quality_flagged": bool(rng.random() < 0.25),
        }
        for index in range(n_rows)
    ]

    availability: dict[str, dict[tuple[object, object], float]] = {}
    for instrument in instruments:
        points: dict[tuple[object, object], float] = {}
        for station in stations:
            for time in times:
                draw = rng.random()
                if draw < 0.3:
                    continue
                points[(station, time)] = (
                    np.nan if draw < 0.45 else float(rng.integers(0, 20))
                )
        availability[instrument] = points

    accepted = {
        BATCH_JIA: {"L1", "L2", "L3"},
        BATCH_YI: {"L2"},
    }
    candidates: list[list[dict[str, object]]] = []
    for row in rows:
        row_candidates: list[dict[str, object]] = []
        if not row["quality_flagged"]:
            for instrument in instruments:
                value = availability[instrument].get((row["station"], row["time"]))
                if value is None or np.isnan(value):
                    continue
                if licenses[instrument] not in accepted[row["batch"]]:
                    continue
                row_candidates.append(
                    {
                        "instrument": instrument,
                        "station": row["station"],
                        "time": row["time"],
                        "spec": specs[instrument],
                    }
                )
        candidates.append(row_candidates)

    expected = _brute_force_reference(rows, candidates, instruments, quotas)

    datasets: list[xr.Dataset] = []
    for instrument in instruments:
        grid = np.full((len(stations), len(times)), np.nan)
        for (station, time), value in availability[instrument].items():
            grid[stations.index(station), times.index(time)] = value
        datasets.append(
            make_instrument(
                instrument,
                grid,
                licenses[instrument],
                specs[instrument],
                CAL_7,
            )
        )
    manifest = xr.Dataset(
        data_vars={
            "batch": (
                ["delivery_record"],
                [row["batch"] for row in rows],
            ),
            "station": (
                ["delivery_record"],
                [row["station"] for row in rows],
            ),
            "time": (
                ["delivery_record"],
                [row["time"] for row in rows],
            ),
            "quality_flagged": (
                ["delivery_record"],
                [row["quality_flagged"] for row in rows],
            ),
        },
        coords={"delivery_record": [row["record"] for row in rows]},
    )
    options = xr.DeliveryOptions(
        manifest=manifest,
        instrument_priority=instruments,
        licenses=accepted,
        read_quotas=quotas,
    )
    result = xr.merge(datasets, delivery=options)
    actual = [
        result["delivery_instrument"].sel(delivery_record=row["record"]).item()
        for row in rows
    ]
    assert actual == expected
