"""Reconciliation rules for the auditable trajectory archive."""

from itertools import pairwise
from pathlib import Path

import pytest

import pandas as pd
from pyopensky.archive import (
    ArchiveExtractor,
    LocalHistory,
    ReceptionReport,
    ReportPage,
    TimeSlice,
    history_condition,
    pages_from_frame,
    reconcile_reports,
    reports_from_frame,
    utc_slices,
)

FIXTURE = Path(__file__).parent / "fixtures" / "archive_reports.csv"
START = "2026-11-18 00:00"
STOP = "2026-11-20 00:00"
DELIVERY_START = "2026-11-18 23:50"
DELIVERY_STOP = "2026-11-19 00:30"


def report(
    source: str,
    report_id: str,
    revision: int,
    time: str,
    icao24: str = "abc123",
    lat: float = 52.0,
    lon: float = 4.0,
    callsign: str | None = "SAME123",
) -> ReceptionReport:
    return ReceptionReport(
        source=source,
        report_id=report_id,
        revision=revision,
        icao24=icao24,
        observed_at=pd.Timestamp(time),
        lat=lat,
        lon=lon,
        callsign=callsign,
    )


def page(
    reports: list[ReceptionReport],
    start: str,
    stop: str,
    sequence: int = 0,
) -> ReportPage:
    return ReportPage(
        reports=tuple(reports),
        slice=TimeSlice(start, stop),
        sequence=sequence,
    )


def fixture_pages() -> tuple[ReportPage, ...]:
    return pages_from_frame(pd.read_csv(FIXTURE))


def fixture_reports() -> tuple[ReceptionReport, ...]:
    return reports_from_frame(pd.read_csv(FIXTURE))


def daily_pages(*, duplicate_midnight: bool = False) -> tuple[ReportPage, ...]:
    reports = fixture_reports()
    slices = utc_slices(START, STOP, "1D")
    pages = []
    for sequence, time_slice in enumerate(slices):
        members = [
            item for item in reports if time_slice.contains(item.observed_at)
        ]
        if duplicate_midnight:
            window = pd.Timedelta("1m")
            if (
                time_slice.stop == time_slice.stop.floor("1D")
                and time_slice.stop < slices[-1].stop
            ):
                members.extend(
                    item
                    for item in reports
                    if time_slice.stop
                    <= item.observed_at
                    < time_slice.stop + window
                )
            if (
                time_slice.start == time_slice.start.floor("1D")
                and time_slice.start > slices[0].start
            ):
                members.extend(
                    item
                    for item in reports
                    if time_slice.start - window
                    <= item.observed_at
                    < time_slice.start
                )
        unique = {item.ref: item for item in members}
        pages.append(
            ReportPage(
                reports=tuple(unique.values()),
                slice=time_slice,
                sequence=sequence,
            )
        )
    return tuple(pages)


def six_hour_pages() -> tuple[ReportPage, ...]:
    reports = fixture_reports()
    slices = utc_slices(START, STOP, "6h")
    return tuple(
        ReportPage(
            reports=tuple(
                item
                for item in reports
                if time_slice.contains(item.observed_at)
            ),
            slice=time_slice,
            sequence=sequence,
        )
        for sequence, time_slice in enumerate(slices)
    )


def extract(
    pages: tuple[ReportPage, ...],
    *,
    step: str,
    request_order: str = "forward",
    page_order: str = "forward",
) -> tuple:
    client = LocalHistory(pages)
    delivery = ArchiveExtractor(client, step=step).extract(
        START, STOP, request_order=request_order, page_order=page_order
    )
    return delivery, client


def test_utc_slices_are_contiguous_and_half_open() -> None:
    slices = utc_slices("2026-11-18 23:50", "2026-11-19 00:20", "10min")
    assert [item.label for item in slices] == [
        "2026-11-18T23:50:00Z/2026-11-19T00:00:00Z",
        "2026-11-19T00:00:00Z/2026-11-19T00:10:00Z",
        "2026-11-19T00:10:00Z/2026-11-19T00:20:00Z",
    ]
    assert slices[0].contains("2026-11-18 23:50")
    assert not slices[0].contains("2026-11-19 00:00")
    assert slices[1].contains("2026-11-19 00:00")
    for left, right in pairwise(slices):
        assert left.stop == right.start


def test_history_condition_records_half_open_bounds() -> None:
    condition = history_condition("2026-11-18 23:50", "2026-11-19 00:00")
    assert condition.start_inclusive
    assert not condition.stop_inclusive
    assert "time < 2026-11-19T00:00:00Z" in condition.predicate
    with pytest.raises(ValueError, match="start < stop"):
        history_condition("2026-11-19", "2026-11-19")


def test_revision_two_wins_and_revision_one_is_superseded() -> None:
    pages = daily_pages()
    delivery = reconcile_reports(pages)
    observation = delivery.observation_index()[
        ("abc123", pd.Timestamp("2026-11-18T23:59:30Z"))
    ]
    assert (observation.lat, observation.lon) == (52.1, 4.1)
    assert "station-a:R7@2" in {ref.label for ref in observation.reports}
    assert len(delivery.superseded) == 1
    superseded = delivery.superseded[0]
    assert superseded.report.revision == 1
    assert superseded.superseded_by == 2
    assert (superseded.report.lat, superseded.report.lon) == (52.0, 4.0)


def test_independent_station_confirms_revision_two() -> None:
    delivery = reconcile_reports(daily_pages())
    observation = delivery.observation_index()[
        ("abc123", pd.Timestamp("2026-11-18T23:59:30Z"))
    ]
    assert observation.confirmed_by == 2
    assert observation.sources == ("station-a", "station-b")
    assert {ref.label for ref in observation.reports} == {
        "station-a:R7@2",
        "station-b:B17@1",
    }


def test_third_station_conflict_is_retained_with_provenance() -> None:
    delivery = reconcile_reports(daily_pages())
    observation = delivery.observation_index()[
        ("abc123", pd.Timestamp("2026-11-18T23:59:30Z"))
    ]
    assert len(observation.conflicts) == 1
    conflict = observation.conflicts[0]
    assert (conflict.lat, conflict.lon) == (52.3, 4.3)
    assert conflict.sources == ("station-c",)
    assert conflict.reports == (
        delivery.provenance[
            [
                item.report.report_id == "C3" for item in delivery.provenance
            ].index(True)
        ].ref,
    )
    assert delivery.conflicts_frame().shape[0] == 1


def test_higher_revision_does_not_override_confirmation() -> None:
    delivery = reconcile_reports(daily_pages())
    observation = delivery.observation_index()[
        ("abc123", pd.Timestamp("2026-11-18T23:59:30Z"))
    ]
    assert observation.lat == 52.1
    assert "station-c:C3@9" not in {ref.label for ref in observation.reports}
    assert observation.conflicts[0].reports[0].revision == 9


def test_callsign_is_not_aircraft_identity() -> None:
    delivery = reconcile_reports(daily_pages()).restrict(
        DELIVERY_START, DELIVERY_STOP
    )
    index = delivery.observation_index()
    first = index[("abc123", pd.Timestamp("2026-11-18T23:59:30Z"))]
    second = index[("def456", pd.Timestamp("2026-11-18T23:59:30Z"))]
    assert first.callsign == second.callsign == "SAME123"
    assert first.lat != second.lat
    assert len(delivery.observations) == 8


def test_duplicate_page_is_not_a_second_station() -> None:
    pages = daily_pages(duplicate_midnight=True)
    delivery = reconcile_reports(pages)
    observation = delivery.observation_index()[
        ("abc123", pd.Timestamp("2026-11-18T23:59:30Z"))
    ]
    assert observation.sources == ("station-a", "station-b")
    r7 = next(
        item for item in delivery.provenance if item.report.report_id == "R7"
    )
    assert len(r7.slices) == 2
    assert r7.occurrences == 2


def test_raw_occurrences_keep_every_slice() -> None:
    delivery = reconcile_reports(daily_pages(duplicate_midnight=True))
    frame = delivery.raw_frame()
    rows = frame[
        (frame.source == "station-a")
        & (frame.report_id == "R7")
        & (frame.revision == 1)
    ]
    assert len(rows) == 2
    assert rows.slice_start.nunique() == 2


def test_superseded_reports_are_searchable_but_not_observations() -> None:
    delivery = reconcile_reports(daily_pages())
    frame = delivery.superseded_frame()
    assert list(frame.report_id) == ["R7"]
    assert list(frame.revision) == [1]
    assert list(frame.lat) == [52.0]
    observed_refs = {
        ref
        for observation in delivery.observations
        for ref in observation.reports
    }
    superseded_ref = (
        frame.iloc[0].source,
        frame.iloc[0].report_id,
        frame.iloc[0].revision,
    )
    assert superseded_ref not in {
        (ref.source, ref.report_id, ref.revision) for ref in observed_refs
    }
    assert "station-a:R7@1" not in {
        ref.label
        for observation in delivery.observations
        for ref in observation.reports
    }


def test_boundary_is_half_open_in_final_delivery() -> None:
    delivery = reconcile_reports(daily_pages())
    final = delivery.restrict(DELIVERY_START, DELIVERY_STOP)
    keys = set(final.observation_index())
    assert ("ghi789", pd.Timestamp("2026-11-18T23:50:00Z")) in keys
    assert ("ghi789", pd.Timestamp("2026-11-19T00:30:00Z")) not in keys
    assert ("ghi789", pd.Timestamp("2026-11-19T00:00:00Z")) in keys


def test_conflict_does_not_block_other_observations() -> None:
    final = reconcile_reports(daily_pages()).restrict(
        DELIVERY_START, DELIVERY_STOP
    )
    assert len(final.observations) == 8
    assert ("jkl012", pd.Timestamp("2026-11-19T00:29:59Z")) in set(
        final.observation_index()
    )


def test_extraction_records_conditions_for_both_orders() -> None:
    pages = six_hour_pages()
    forward, forward_client = extract(pages, step="6h", request_order="forward")
    reverse, reverse_client = extract(pages, step="6h", request_order="reverse")
    forward.assert_consistent_with(reverse)
    assert [item.start for item in forward_client.requests] == sorted(
        item.start for item in forward_client.requests
    )
    assert [item.start for item in reverse_client.requests] == sorted(
        (item.start for item in reverse_client.requests), reverse=True
    )
    for condition in forward_client.requests + reverse_client.requests:
        assert condition.start_inclusive
        assert not condition.stop_inclusive
        assert "time <" in condition.predicate


def test_page_order_does_not_change_delivery() -> None:
    pages = six_hour_pages()
    forward, _ = extract(
        pages, step="6h", request_order="forward", page_order="forward"
    )
    reverse, _ = extract(
        pages, step="6h", request_order="forward", page_order="reverse"
    )
    forward.assert_consistent_with(reverse)


def test_replayed_old_revision_keeps_final_payload_and_provenance() -> None:
    pages = six_hour_pages()
    baseline, _ = extract(pages, step="6h")
    old = next(
        item
        for item in fixture_reports()
        if item.source == "station-a"
        and item.report_id == "R7"
        and item.revision == 1
    )
    after_midnight = next(
        index
        for index, item in enumerate(pages)
        if item.slice is not None
        and item.slice.start == pd.Timestamp("2026-11-19T00:00:00Z")
    )
    replayed = list(pages)
    target = replayed[after_midnight]
    replayed[after_midnight] = ReportPage(
        reports=tuple(
            [item for item in target.reports if item.ref != old.ref] + [old]
        ),
        slice=target.slice,
        sequence=target.sequence,
    )
    delivery, _ = extract(tuple(replayed), step="6h", page_order="reverse")
    delivery.assert_consistent_with(baseline)
    r7_old = next(
        item
        for item in delivery.provenance
        if item.report.report_id == "R7" and item.report.revision == 1
    )
    assert r7_old.status == "superseded"
    assert len(r7_old.slices) == 2
    assert r7_old.occurrences >= 2


def test_reports_from_frame_requires_provenance_columns() -> None:
    with pytest.raises(ValueError, match="missing report provenance"):
        reports_from_frame(pd.DataFrame({"time": ["2026-11-19"]}))


def test_pages_from_frame_requires_slice_columns() -> None:
    frame = pd.read_csv(FIXTURE)
    with pytest.raises(ValueError, match="slice_start"):
        pages_from_frame(frame)


def test_local_history_returns_only_recorded_slice() -> None:
    pages = (
        page(
            [report("a", "1", 1, "2026-11-18T23:59:30Z")],
            "2026-11-18",
            "2026-11-19",
        ),
        page(
            [report("a", "2", 1, "2026-11-19T00:00:00Z")],
            "2026-11-19",
            "2026-11-20",
        ),
    )
    client = LocalHistory(pages)
    frame = client.history("2026-11-18", "2026-11-19")
    assert frame is not None
    assert list(frame.report_id) == ["1"]
    assert client.requests[0].label == (
        "2026-11-18T00:00:00Z/2026-11-19T00:00:00Z"
    )


def test_two_deliveries_agree_on_every_auditable_table() -> None:
    pages = six_hour_pages()
    forward, _ = extract(
        pages, step="6h", request_order="forward", page_order="forward"
    )
    reverse, _ = extract(
        pages, step="6h", request_order="reverse", page_order="reverse"
    )
    forward.assert_consistent_with(reverse)
    for name in ("observations", "conflicts", "superseded", "provenance"):
        pd.testing.assert_frame_equal(
            forward.frames()[name].reset_index(drop=True),
            reverse.frames()[name].reset_index(drop=True),
            check_dtype=False,
        )
