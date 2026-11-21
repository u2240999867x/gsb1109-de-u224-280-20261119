"""Build the auditable archive delivery from locally recorded reports.

This script performs no network access.  It records the actual history
condition used for every slice, replays two extraction orders, and asserts that
both final deliveries agree by aircraft and observation time before writing the
artefacts under ``artifacts/archive``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
from pyopensky.archive import (
    ArchiveDelivery,
    ArchiveExtractor,
    LocalHistory,
    ReceptionReport,
    ReportPage,
    TimeSlice,
    reports_from_frame,
    utc_slices,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "archive_reports.csv"
DEFAULT_OUTPUT = ROOT / "artifacts" / "archive"
START = "2026-11-18 00:00"
STOP = "2026-11-20 00:00"
DELIVERY_START = "2026-11-18 23:50"
DELIVERY_STOP = "2026-11-19 00:30"


def load_reports(path: Path) -> tuple[ReceptionReport, ...]:
    return reports_from_frame(pd.read_csv(path))


def pages_for(
    reports: tuple[ReceptionReport, ...],
    slices: tuple[TimeSlice, ...],
    *,
    duplicate_midnight: bool = False,
) -> tuple[ReportPage, ...]:
    """Build one page per slice.

    ``duplicate_midnight`` models the archive's cross-midnight overlap: a page
    ending at midnight also contains reports observed just after it, and a page
    starting at midnight also contains reports observed just before it.
    """

    pages: list[ReportPage] = []
    for sequence, time_slice in enumerate(slices):
        window = pd.Timedelta("1m")
        members = [
            report
            for report in reports
            if time_slice.contains(report.observed_at)
        ]
        if duplicate_midnight:
            if (
                time_slice.stop == time_slice.stop.floor("1D")
                and time_slice.stop < slices[-1].stop
            ):
                members.extend(
                    report
                    for report in reports
                    if time_slice.stop
                    <= report.observed_at
                    < time_slice.stop + window
                )
            if (
                time_slice.start == time_slice.start.floor("1D")
                and time_slice.start > slices[0].start
            ):
                members.extend(
                    report
                    for report in reports
                    if time_slice.start - window
                    <= report.observed_at
                    < time_slice.start
                )
        unique: dict[object, ReceptionReport] = {}
        for report in members:
            unique.setdefault(report.ref, report)
        pages.append(
            ReportPage(
                reports=tuple(unique.values()),
                slice=time_slice,
                sequence=sequence,
            )
        )
    return tuple(pages)


def replay_page(
    page: ReportPage,
    report: ReceptionReport,
) -> ReportPage:
    """Add or replace one report in a page, keeping the page's identity."""

    reports = [item for item in page.reports if item.ref != report.ref]
    reports.append(report)
    return ReportPage(
        reports=tuple(reports), slice=page.slice, sequence=page.sequence
    )


def run(
    pages: tuple[ReportPage, ...],
    *,
    request_order: str,
    page_order: str,
    step: str,
) -> tuple[ArchiveDelivery, LocalHistory]:
    client = LocalHistory(pages)
    extractor = ArchiveExtractor(client, step=step)
    delivery = extractor.extract(
        START, STOP, request_order=request_order, page_order=page_order
    )
    assert client.requests, "the history client must have been queried"
    for request in client.requests:
        assert request.start_inclusive
        assert not request.stop_inclusive
    return delivery, client


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)

    reports = load_reports(FIXTURE)
    daily_slices = utc_slices(START, STOP, "1D")
    short_slices = utc_slices(START, STOP, "6h")

    daily = pages_for(reports, daily_slices, duplicate_midnight=True)
    short = pages_for(reports, short_slices)
    short_overlap = pages_for(reports, short_slices, duplicate_midnight=True)
    daily_forward, daily_forward_client = run(
        daily, request_order="forward", page_order="forward", step="1D"
    )
    daily_reverse, daily_reverse_client = run(
        daily, request_order="reverse", page_order="reverse", step="1D"
    )
    daily_forward.assert_consistent_with(daily_reverse)

    short_forward, short_forward_client = run(
        short, request_order="forward", page_order="forward", step="6h"
    )
    short_reverse, short_reverse_client = run(
        short, request_order="reverse", page_order="reverse", step="6h"
    )
    short_forward.assert_consistent_with(short_reverse)
    daily_forward.assert_consistent_with(short_forward)

    overlap_forward, _ = run(
        short_overlap,
        request_order="forward",
        page_order="reverse",
        step="6h",
    )
    overlap_forward.assert_consistent_with(daily_forward)

    # Later pages return first and the page just after midnight re-reports R7
    # revision 1 with the old coordinate, after revision 2 was already
    # recorded before midnight.
    r7_old = next(
        report
        for report in reports
        if report.source == "station-a"
        and report.report_id == "R7"
        and report.revision == 1
    )
    after_midnight = next(
        index
        for index, page in enumerate(short_overlap)
        if page.slice is not None
        and page.slice.start == pd.Timestamp("2026-11-19T00:00:00Z")
    )
    replay_pages = tuple(
        replay_page(page, r7_old) if index == after_midnight else page
        for index, page in enumerate(short_overlap)
    )
    replay_forward, replay_client = run(
        replay_pages,
        request_order="forward",
        page_order="reverse",
        step="6h",
    )
    replay_reverse, _ = run(
        replay_pages,
        request_order="reverse",
        page_order="forward",
        step="6h",
    )
    replay_forward.assert_consistent_with(replay_reverse)
    daily_forward.assert_consistent_with(replay_forward)

    final = replay_forward.restrict(DELIVERY_START, DELIVERY_STOP)
    alternate = replay_reverse.restrict(DELIVERY_START, DELIVERY_STOP)

    for name, frame in final.frames().items():
        frame.to_csv(output / f"{name}.csv", index=False)
    final.raw_frame().to_csv(output / "raw-reports.csv", index=False)
    (output / "manifest.json").write_text(final.to_json())

    for name, delivery in (
        ("forward", final),
        ("reverse", alternate),
    ):
        directory = output / "deliveries" / name
        directory.mkdir(parents=True, exist_ok=True)
        for table, frame in delivery.frames().items():
            frame.to_csv(directory / f"{table}.csv", index=False)
        delivery.raw_frame().to_csv(directory / "raw-reports.csv", index=False)

    def request_payload(client: LocalHistory) -> dict[str, object]:
        return {
            "order": [request.label for request in client.requests],
            "requests": [request.to_dict() for request in client.requests],
        }

    request_log = {
        "start": START,
        "stop": STOP,
        "half_open": True,
        "delivery_window": {"start": DELIVERY_START, "stop": DELIVERY_STOP},
        "daily_slices": {
            "step": "1D",
            "forward": request_payload(daily_forward_client),
            "reverse": request_payload(daily_reverse_client),
        },
        "short_slices": {
            "step": "6h",
            "forward": request_payload(short_forward_client),
            "reverse": request_payload(short_reverse_client),
            "replay": request_payload(replay_client),
        },
    }
    (output / "history-request-log.json").write_text(
        json.dumps(request_log, indent=2, sort_keys=True)
    )

    primary = final.observation_index()[
        ("abc123", pd.Timestamp("2026-11-18T23:59:30Z"))
    ]
    r7_old = next(
        item
        for item in final.provenance
        if item.report.report_id == "R7" and item.report.revision == 1
    )
    windowed = {
        "daily_forward": daily_forward.restrict(DELIVERY_START, DELIVERY_STOP),
        "daily_reverse": daily_reverse.restrict(DELIVERY_START, DELIVERY_STOP),
        "short_forward": short_forward.restrict(DELIVERY_START, DELIVERY_STOP),
        "short_reverse": short_reverse.restrict(DELIVERY_START, DELIVERY_STOP),
        "replay_forward": replay_forward.restrict(
            DELIVERY_START, DELIVERY_STOP
        ),
        "replay_reverse": replay_reverse.restrict(
            DELIVERY_START, DELIVERY_STOP
        ),
    }
    for delivery in windowed.values():
        delivery.assert_consistent_with(final)
    verification = {
        "final_delivery_identical": {
            name: delivery.canonical_observations()
            == final.canonical_observations()
            for name, delivery in windowed.items()
        },
        "r7": {
            "observation": {
                "icao24": primary.icao24,
                "observed_at": primary.observed_at.isoformat(),
                "lat": primary.lat,
                "lon": primary.lon,
                "sources": list(primary.sources),
                "reports": [ref.label for ref in primary.reports],
            },
            "superseded_revision": r7_old.report.revision,
            "superseded_by": r7_old.superseded_by,
            "appeared_in_slices": list(r7_old.slices),
            "occurrences": r7_old.occurrences,
            "in_observations": any(
                ref.source == "station-a"
                and ref.report_id == "R7"
                and ref.revision == 1
                for observation in final.observations
                for ref in observation.reports
            ),
        },
        "conflict": [
            {
                "lat": conflict.lat,
                "lon": conflict.lon,
                "sources": list(conflict.sources),
                "reports": [ref.label for ref in conflict.reports],
            }
            for conflict in primary.conflicts
        ],
        "shared_callsign_aircraft": [
            observation.icao24
            for observation in final.observations
            if observation.callsign == "SAME123"
        ],
        "stop_boundary": {
            "stop": DELIVERY_STOP,
            "stop_included": [
                [icao24, timestamp]
                for icao24, timestamp in final.canonical_observations()
                if timestamp == "2026-11-19T00:30:00Z"
            ],
            "just_before_stop": [
                [icao24, timestamp]
                for icao24, timestamp in final.canonical_observations()
                if timestamp == "2026-11-19T00:29:59Z"
            ],
        },
    }
    (output / "verification.json").write_text(
        json.dumps(verification, indent=2, sort_keys=True)
    )
    print(final.to_json())


if __name__ == "__main__":
    main()
