from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import xarray as xr
from xarray.core.radar_settlement import settle_altitude
from xarray.tests import requires_dask

DATE = "2026-11-24 "
SCAN_TIMES = pd.date_range("2026-11-24 01:00", periods=3, freq="h")


def make_dataset() -> xr.Dataset:
    readings = np.array([[1.0, 2.0, np.nan], [4.0, 5.0, 6.0]])
    return xr.Dataset(
        {"reading": (("station", "scan_time"), readings)},
        coords={
            "station": ["A", "B"],
            "scan_time": SCAN_TIMES,
            "measured_altitude": ("station", [400.0, 420.0]),
        },
    )


def make_manifest(
    a_cutoffs: tuple[str, ...] = ("10:00", "12:00", "10:00"),
    b_cutoffs: tuple[str, ...] | None = ("12:00", "12:00", "12:00"),
) -> pd.DataFrame:
    rows = []
    for station, cutoffs in (("A", a_cutoffs), ("B", b_cutoffs)):
        if cutoffs is None:
            continue
        for scan, cutoff in zip(SCAN_TIMES, cutoffs, strict=True):
            rows.append(
                {
                    "station": station,
                    "scan_time": scan,
                    "cutoff": pd.Timestamp(DATE + cutoff),
                }
            )
    return pd.DataFrame(rows)


def make_stations() -> pd.DataFrame:
    return pd.DataFrame({"station": ["A", "B"], "mileage": [40.0, 60.0]})


def make_reports() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "report": "R",
                "version": 1,
                "publish_time": pd.Timestamp(DATE + "08:00"),
                "mileage_min": 30.0,
                "mileage_max": 50.0,
                "scan_min": pd.Timestamp(DATE + "00:00"),
                "scan_max": pd.Timestamp(DATE + "04:00"),
                "altitude": 0.4,
                "unit": "km",
                "unit_verified": True,
                "missing": False,
            },
            {
                "report": "S",
                "version": 1,
                "publish_time": pd.Timestamp(DATE + "09:00"),
                "mileage_min": 30.0,
                "mileage_max": 50.0,
                "scan_min": pd.Timestamp(DATE + "01:00"),
                "scan_max": pd.Timestamp(DATE + "04:00"),
                "altitude": 405.0,
                "unit": "m",
                "unit_verified": True,
                "missing": False,
            },
            {
                "report": "R",
                "version": 2,
                "publish_time": pd.Timestamp(DATE + "12:00"),
                "mileage_min": 50.0,
                "mileage_max": 70.0,
                "scan_min": pd.Timestamp(DATE + "00:00"),
                "scan_max": pd.Timestamp(DATE + "02:00"),
                "altitude": 0.43,
                "unit": "km",
                "unit_verified": True,
                "missing": False,
            },
        ]
    )


def settle(ds: xr.Dataset, manifest: pd.DataFrame) -> xr.Dataset:
    return ds.radar.settle_altitude(
        manifest, make_stations(), make_reports(), priority=["R", "S"]
    )


def test_settle_example(use_dask: bool) -> None:
    ds = make_dataset()
    if use_dask:
        ds = ds.chunk()
    result = settle(ds, make_manifest())

    expected_altitude = np.array([[400.0, 405.0, 400.0], [430.0, 420.0, 420.0]])
    np.testing.assert_allclose(result["settled_altitude"].values, expected_altitude)
    assert result["settled_altitude"].attrs["units"] == "m"
    assert result["settled_altitude"].dims == ("station", "scan_time")

    expected_report = np.array([["R", "S", "R"], ["R", "", ""]], dtype=object)
    np.testing.assert_array_equal(result["settled_report"].values, expected_report)

    expected_version = np.array([[1.0, 1.0, 1.0], [2.0, np.nan, np.nan]])
    np.testing.assert_allclose(
        result["settled_report_version"].values, expected_version
    )

    expected_reason = np.array(
        [["", "", ""], ["", "no_applicable_report", "no_applicable_report"]],
        dtype=object,
    )
    np.testing.assert_array_equal(
        result["settled_fallback_reason"].values, expected_reason
    )

    # the missing radar reading at ("A", 03:00) is still settled
    assert np.isnan(result["reading"].sel(station="A").isel(scan_time=2).values[()])
    assert result["settled_altitude"].sel(station="A").isel(scan_time=2) == 400.0
    # measured altitude stays queryable
    np.testing.assert_allclose(result["measured_altitude"].values, [400.0, 420.0])
    if use_dask:
        # settling must not load the radar readings
        assert result["reading"].chunks is not None


def test_settle_example_all_noon_cutoffs() -> None:
    result = settle(make_dataset(), make_manifest(a_cutoffs=("12:00",) * 3))
    np.testing.assert_allclose(
        result["settled_altitude"].sel(station="A").values, [405.0, 405.0, 405.0]
    )
    np.testing.assert_array_equal(
        result["settled_report"].sel(station="A").values, ["S", "S", "S"]
    )


def test_unsampled_cells_left_empty() -> None:
    result = settle(make_dataset(), make_manifest(b_cutoffs=None))
    settled_b = result["settled_altitude"].sel(station="B")
    assert np.isnan(settled_b.values).all()
    assert (result["settled_report"].sel(station="B").values == "").all()
    assert np.isnan(result["settled_report_version"].sel(station="B").values).all()
    assert not np.isnan(result["settled_altitude"].sel(station="A").values).any()


def test_label_results_order_invariant() -> None:
    base = settle(make_dataset(), make_manifest())

    shuffled = make_dataset().isel(station=[1, 0], scan_time=[2, 1, 0])
    manifest = make_manifest().sample(frac=1.0, random_state=0)
    reports = make_reports().sample(frac=1.0, random_state=1)
    stations = make_stations().sample(frac=1.0, random_state=2)
    result = shuffled.radar.settle_altitude(
        manifest, stations, reports, priority=["R", "S"]
    )
    result = result.sel(station=["A", "B"], scan_time=SCAN_TIMES)

    xr.testing.assert_identical(base.drop_vars("reading"), result.drop_vars("reading"))


def test_measured_altitude_index_preserved() -> None:
    ds = make_dataset().set_xindex("measured_altitude")
    result = settle(ds, make_manifest())
    assert "measured_altitude" in result.indexes
    selected = result.sel(measured_altitude=[400.0])
    assert list(selected["station"].values) == ["A"]


def test_correction_replaces_old_version() -> None:
    # at 12:00 the correction R v2 is published exactly at the cutoff and
    # counts; R v1 stops applying inside its original range immediately
    result = settle(make_dataset(), make_manifest())
    # station A mileage 40 is inside R v1's [30, 50) but not R v2's [50, 70),
    # so the 02:00 cell at cutoff 12:00 falls through to backup report S
    cell = result.sel(station="A", scan_time=SCAN_TIMES[1])
    assert cell["settled_altitude"] == 405.0
    assert cell["settled_report"] == "S"
    # station B mileage 60 is covered by R v2 for the 01:00 scan
    cell = result.sel(station="B", scan_time=SCAN_TIMES[0])
    assert cell["settled_altitude"] == 430.0
    assert cell["settled_report"] == "R"
    assert cell["settled_report_version"] == 2.0


def test_interval_bounds() -> None:
    ds = xr.Dataset(
        coords={
            "station": ["LO", "HI"],
            "scan_time": SCAN_TIMES,
            "measured_altitude": ("station", [100.0, 200.0]),
        }
    )
    stations = pd.DataFrame({"station": ["LO", "HI"], "mileage": [30.0, 50.0]})
    reports = pd.DataFrame(
        [
            {
                "report": "R",
                "version": 1,
                "publish_time": pd.Timestamp(DATE + "08:00"),
                "mileage_min": 30.0,
                "mileage_max": 50.0,
                "scan_min": pd.Timestamp(DATE + "01:00"),
                "scan_max": pd.Timestamp(DATE + "02:00"),
                "altitude": 1.0,
                "unit": "m",
                "unit_verified": True,
                "missing": False,
            }
        ]
    )
    manifest = pd.DataFrame(
        {
            "station": ["LO", "LO", "HI", "HI"],
            "scan_time": [SCAN_TIMES[0], SCAN_TIMES[1]] * 2,
            "cutoff": [pd.Timestamp(DATE + "09:00")] * 4,
        }
    )
    result = ds.radar.settle_altitude(manifest, stations, reports, priority=["R"])
    grid = result["settled_altitude"].values
    # mileage min is inclusive, mileage max exclusive
    assert grid[0, 0] == 1.0
    assert grid[1, 0] == 200.0
    # scan min is inclusive, scan max exclusive
    assert grid[0, 1] == 100.0


def test_publish_exactly_at_cutoff_counts() -> None:
    result = settle(make_dataset(), make_manifest())
    # R v2 is published at 12:00 and the B cutoffs are exactly 12:00
    cell = result.sel(station="B", scan_time=SCAN_TIMES[0])
    assert cell["settled_report_version"] == 2.0


def test_fallback_reasons() -> None:
    reports = make_reports()
    missing_report = {
        "report": "M",
        "version": 1,
        "publish_time": pd.Timestamp(DATE + "07:00"),
        "mileage_min": 0.0,
        "mileage_max": 100.0,
        "scan_min": pd.Timestamp(DATE + "00:00"),
        "scan_max": pd.Timestamp(DATE + "06:00"),
        "altitude": 999.0,
        "unit": "m",
        "unit_verified": True,
        "missing": True,
    }
    unverified_report = {
        "report": "U",
        "version": 1,
        "publish_time": pd.Timestamp(DATE + "07:30"),
        "mileage_min": 0.0,
        "mileage_max": 100.0,
        "scan_min": pd.Timestamp(DATE + "00:00"),
        "scan_max": pd.Timestamp(DATE + "06:00"),
        "altitude": 999.0,
        "unit": "ft",
        "unit_verified": False,
        "missing": False,
    }
    reports = pd.concat(
        [pd.DataFrame([missing_report, unverified_report]), reports],
        ignore_index=True,
    )
    ds = make_dataset()
    result = ds.radar.settle_altitude(
        make_manifest(), make_stations(), reports, priority=["M", "U", "R", "S"]
    )
    # the highest-priority report M actively reports missing: fall back to
    # the measured altitude but keep the selected report and the reason
    cell = result.sel(station="A", scan_time=SCAN_TIMES[0])
    assert cell["settled_altitude"] == 400.0
    assert cell["settled_report"] == "M"
    assert cell["settled_report_version"] == 1.0
    assert cell["settled_fallback_reason"] == "report_missing"

    result = ds.radar.settle_altitude(
        make_manifest(), make_stations(), reports, priority=["U", "R", "S"]
    )
    cell = result.sel(station="A", scan_time=SCAN_TIMES[0])
    assert cell["settled_altitude"] == 400.0
    assert cell["settled_report"] == "U"
    assert cell["settled_fallback_reason"] == "unit_unverified"


def test_no_applicable_report_uses_measured_altitude() -> None:
    result = settle(make_dataset(), make_manifest())
    cell = result.sel(station="B", scan_time=SCAN_TIMES[2])
    assert cell["settled_altitude"] == 420.0
    assert cell["settled_report"] == ""
    assert np.isnan(cell["settled_report_version"].values[()])
    assert cell["settled_fallback_reason"] == "no_applicable_report"


def test_invalid_inputs() -> None:
    ds = make_dataset()
    manifest = make_manifest()
    stations = make_stations()
    reports = make_reports()

    duplicate = pd.concat([manifest, manifest.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate"):
        ds.radar.settle_altitude(duplicate, stations, reports, priority=["R", "S"])

    unknown_station = manifest.copy()
    unknown_station.loc[0, "station"] = "Z"
    with pytest.raises(ValueError, match="not present"):
        ds.radar.settle_altitude(
            unknown_station, stations, reports, priority=["R", "S"]
        )

    with pytest.raises(ValueError, match="no mileage"):
        ds.radar.settle_altitude(
            manifest, stations.iloc[:1], reports, priority=["R", "S"]
        )

    bad_unit = reports.copy()
    bad_unit.loc[0, "unit"] = "ft"
    with pytest.raises(ValueError, match="unknown units"):
        ds.radar.settle_altitude(manifest, stations, bad_unit, priority=["R", "S"])

    with pytest.raises(ValueError, match="missing required columns"):
        ds.radar.settle_altitude(
            manifest.drop(columns="cutoff"), stations, reports, priority=["R", "S"]
        )


def naive_settle(
    ds: xr.Dataset,
    manifest: pd.DataFrame,
    stations: pd.DataFrame,
    reports: pd.DataFrame,
    priority: list,
) -> pd.DataFrame:
    mileage = stations.set_index("station")["mileage"]
    measured = ds["measured_altitude"].to_series()
    unit_to_meters = {"m": 1.0, "km": 1000.0}
    by_report = {
        report_id: versions.sort_values(["publish_time", "version"])
        for report_id, versions in reports.groupby("report")
    }
    rows = []
    for cell in manifest.itertuples(index=False):
        station_mileage = mileage[cell.station]
        chosen = None
        for report_id in priority:
            versions = by_report.get(report_id)
            if versions is None:
                continue
            versions = versions[versions["publish_time"] <= cell.cutoff]
            if versions.empty:
                continue
            version = versions.iloc[-1]
            if (
                version.mileage_min <= station_mileage < version.mileage_max
                and version.scan_min <= cell.scan_time < version.scan_max
            ):
                chosen = version
                break
        reason = ""
        if chosen is None:
            altitude = measured[cell.station]
            report_id = ""
            report_version = np.nan
            reason = "no_applicable_report"
        elif chosen.missing:
            altitude = measured[cell.station]
            report_id = chosen.report
            report_version = chosen.version
            reason = "report_missing"
        elif not chosen.unit_verified:
            altitude = measured[cell.station]
            report_id = chosen.report
            report_version = chosen.version
            reason = "unit_unverified"
        else:
            altitude = chosen.altitude * unit_to_meters[chosen.unit]
            report_id = chosen.report
            report_version = chosen.version
        rows.append(
            {
                "station": cell.station,
                "scan_time": cell.scan_time,
                "settled_altitude": altitude,
                "settled_report": report_id,
                "settled_report_version": report_version,
                "settled_fallback_reason": reason,
            }
        )
    return pd.DataFrame(rows)


def test_random_against_naive() -> None:
    rng = np.random.default_rng(42)
    n_stations, n_scans, n_reports = 8, 10, 30
    stations = [f"ST{i}" for i in range(n_stations)]
    scans = pd.date_range("2026-11-24 00:00", periods=n_scans, freq="h")
    measured = rng.uniform(100, 500, n_stations)
    ds = xr.Dataset(
        coords={
            "station": stations,
            "scan_time": scans,
            "measured_altitude": ("station", measured),
        }
    )
    station_rows = pd.DataFrame(
        {"station": stations, "mileage": np.sort(rng.uniform(0, 100, n_stations))}
    )

    report_rows = []
    report_ids = [f"R{i}" for i in range(n_reports)]
    base_time = pd.Timestamp("2026-11-24 00:00")
    for report_id in report_ids:
        publish = base_time + pd.Timedelta(hours=float(rng.uniform(0, 6)))
        for version in range(1, int(rng.integers(1, 4))):
            mlo = rng.uniform(0, 80)
            tlo = base_time + pd.Timedelta(hours=float(rng.uniform(0, 8)))
            report_rows.append(
                {
                    "report": report_id,
                    "version": version,
                    "publish_time": publish,
                    "mileage_min": mlo,
                    "mileage_max": mlo + rng.uniform(1, 40),
                    "scan_min": tlo,
                    "scan_max": tlo + pd.Timedelta(hours=float(rng.uniform(1, 6))),
                    "altitude": float(rng.uniform(100, 500)),
                    "unit": str(rng.choice(["m", "km"])),
                    "unit_verified": bool(rng.random() > 0.1),
                    "missing": bool(rng.random() < 0.1),
                }
            )
            publish += pd.Timedelta(hours=float(rng.uniform(0.5, 3)))
    reports = pd.DataFrame(report_rows)

    manifest = pd.DataFrame(
        [
            {
                "station": station,
                "scan_time": scan,
                "cutoff": base_time + pd.Timedelta(hours=float(rng.uniform(2, 14))),
            }
            for station in stations
            for scan in scans
            if rng.random() > 0.2
        ]
    )
    priority = list(rng.permutation(report_ids))

    result = ds.radar.settle_altitude(
        manifest, station_rows, reports, priority=priority
    )
    expected = naive_settle(ds, manifest, station_rows, reports, priority)

    for row in expected.itertuples(index=False):
        cell = result.sel(station=row.station, scan_time=row.scan_time)
        assert float(cell["settled_altitude"]) == pytest.approx(row.settled_altitude)
        assert str(cell["settled_report"].values[()]) == row.settled_report
        if np.isnan(row.settled_report_version):
            assert np.isnan(cell["settled_report_version"].values[()])
        else:
            assert float(cell["settled_report_version"]) == row.settled_report_version
        assert (
            str(cell["settled_fallback_reason"].values[()])
            == row.settled_fallback_reason
        )


@requires_dask
def test_settle_stays_lazy() -> None:
    ds = make_dataset().chunk({"station": 1})
    result = settle(ds, make_manifest())
    assert result["reading"].chunks is not None
    np.testing.assert_allclose(
        result["settled_altitude"].values,
        [[400.0, 405.0, 400.0], [430.0, 420.0, 420.0]],
    )


@pytest.mark.slow
def test_large_batch() -> None:
    rng = np.random.default_rng(7)
    n_stations, n_scans, n_reports = 500, 400, 20000
    stations = [f"ST{i}" for i in range(n_stations)]
    scans = pd.date_range("2026-11-24 00:00", periods=n_scans, freq="min")
    ds = xr.Dataset(
        coords={
            "station": stations,
            "scan_time": scans,
            "measured_altitude": ("station", rng.uniform(100, 500, n_stations)),
        }
    )
    station_rows = pd.DataFrame(
        {"station": stations, "mileage": np.sort(rng.uniform(0, 500, n_stations))}
    )
    base_time = pd.Timestamp("2026-11-24 00:00")
    report_rows = []
    for i in range(n_reports):
        mlo = rng.uniform(0, 450)
        tlo = rng.uniform(0, n_scans)
        report_rows.append(
            {
                "report": f"R{i}",
                "version": 1,
                "publish_time": base_time
                + pd.Timedelta(hours=float(rng.uniform(0, 12))),
                "mileage_min": mlo,
                "mileage_max": mlo + rng.uniform(1, 100),
                "scan_min": scans[min(int(tlo), n_scans - 1)],
                "scan_max": scans[
                    min(int(tlo) + int(rng.integers(1, 120)), n_scans - 1)
                ],
                "altitude": float(rng.uniform(100, 500)),
                "unit": "m",
                "unit_verified": True,
                "missing": False,
            }
        )
    reports = pd.DataFrame(report_rows)
    cutoffs = [base_time + pd.Timedelta(hours=h) for h in (4.0, 8.0, 12.0)]
    manifest = pd.DataFrame(
        [
            {"station": st, "scan_time": sc, "cutoff": cutoffs[i % 3]}
            for i, (st, sc) in enumerate((st, sc) for st in stations for sc in scans)
        ]
    )
    priority = [f"R{i}" for i in range(n_reports)]
    result = ds.radar.settle_altitude(
        manifest, station_rows, reports, priority=priority
    )
    assert result["settled_altitude"].notnull().all()

    # spot-check a sample of cells against the naive reference
    sample = manifest.sample(50, random_state=0)
    expected = naive_settle(ds, sample, station_rows, reports, priority)
    for row in expected.itertuples(index=False):
        cell = result.sel(station=row.station, scan_time=row.scan_time)
        assert float(cell["settled_altitude"]) == pytest.approx(row.settled_altitude)
        assert str(cell["settled_report"].values[()]) == row.settled_report


def test_function_form() -> None:
    ds = make_dataset()
    result = settle_altitude(
        ds, make_manifest(), make_stations(), make_reports(), priority=["R", "S"]
    )
    assert result["settled_altitude"].sel(station="A").isel(scan_time=0) == 400.0
