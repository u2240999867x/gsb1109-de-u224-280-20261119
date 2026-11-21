"""Offline tests for segmented, versioned track archives."""

from __future__ import annotations

import pytest

import pandas as pd
from pyopensky.track_archive import (
    ArchiveStateError,
    Observation,
    TrackArchive,
    TrackSegment,
    apply_changes,
)

ICAO_A = "abc123"
ICAO_B = "def456"


def observation(
    icao24: str,
    second: float,
    *,
    latitude: float | None,
    longitude: float | None,
    source: str = "s1",
    revision: int = 0,
    valid: bool | None = None,
    altitude: float | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "icao24": icao24,
        "time": second,
        "lat": latitude,
        "lon": longitude,
        "alt": altitude,
        "source": source,
        "revision": revision,
    }
    if valid is not None:
        record["valid"] = valid
    return record


def initial_batch() -> list[dict[str, object]]:
    return [
        observation(ICAO_A, 0, latitude=50.0, longitude=4.0),
        observation(
            ICAO_A,
            10,
            latitude=None,
            longitude=None,
            valid=False,
        ),
        observation(ICAO_A, 20, latitude=50.2, longitude=4.2),
        observation(ICAO_A, 30, latitude=50.3, longitude=4.3),
        observation(ICAO_B, 0, latitude=51.0, longitude=4.0),
        observation(ICAO_B, 10, latitude=51.1, longitude=4.1),
        observation(ICAO_B, 20, latitude=51.2, longitude=4.2),
        observation(ICAO_B, 30, latitude=51.3, longitude=4.3),
    ]


def late_batch() -> list[dict[str, object]]:
    return [
        observation(
            ICAO_A,
            10,
            latitude=50.1,
            longitude=4.1,
            source="s1",
            revision=1,
        ),
        observation(
            ICAO_A,
            10,
            latitude=50.1,
            longitude=4.1,
            source="s2",
            revision=1,
        ),
    ]


def revision_batch() -> list[dict[str, object]]:
    return [
        observation(
            ICAO_A,
            10,
            latitude=None,
            longitude=None,
            source="s1",
            revision=2,
            valid=False,
        ),
        observation(
            ICAO_A,
            10,
            latitude=None,
            longitude=None,
            source="s2",
            revision=2,
            valid=False,
        ),
    ]


def segment_times(segments: tuple[TrackSegment, ...]) -> dict[str, list[int]]:
    return {
        segment.segment_id: [
            int(point.time.timestamp()) for point in segment.points
        ]
        for segment in segments
    }


def test_first_batch_splits_at_contact_only_second() -> None:
    archive = TrackArchive(max_gap=10)
    result = archive.write(initial_batch(), batch="first")

    a_segments = [
        segment for segment in result.segments if segment.icao24 == ICAO_A
    ]
    assert [next(iter(segment_times((s,)).values())) for s in a_segments] == [
        [0],
        [20, 30],
    ]
    assert [change.action for change in result.changes] == ["add", "add", "add"]
    assert len(result.segments) == 3
    assert not result.retractions
    assert not result.replacements

    contact = archive.contacts
    assert len(contact) == 1
    assert contact[0].icao24 == ICAO_A
    assert int(contact[0].time.timestamp()) == 10
    assert contact[0].has_position is False
    assert contact[0].batch_id == "first"


def test_late_two_station_position_connects_both_sides_once() -> None:
    archive = TrackArchive(max_gap=10)
    first = archive.write(initial_batch(), batch="first")
    downstream = first.apply({})
    late = archive.write(late_batch(), batch="late")
    downstream = late.apply(downstream)

    a_segments = [
        segment for segment in archive.segments if segment.icao24 == ICAO_A
    ]
    assert len(a_segments) == 1
    assert [int(point.time.timestamp()) for point in a_segments[0].points] == [
        0,
        10,
        20,
        30,
    ]

    bridge = a_segments[0].points[1]
    assert bridge.source_count == 2
    assert bridge.sources == ("s1", "s2")
    assert len(bridge.observations) == 2
    assert bridge.batch_id == "late"

    changes = {change.segment_id: change for change in late.changes}
    assert len(changes) == 2
    assert any(change.action == "replace" for change in changes.values())
    assert any(change.action == "retract" for change in changes.values())
    assert not late.additions
    assert downstream == archive.state


def test_same_second_station_reports_do_not_create_two_positions() -> None:
    archive = TrackArchive(max_gap=10)
    archive.write(initial_batch(), batch="first")
    result = archive.write(late_batch(), batch="late")

    point_times = [
        int(point.time.timestamp())
        for segment in result.segments
        if segment.icao24 == ICAO_A
        for point in segment.points
    ]
    assert point_times == [0, 10, 20, 30]
    assert len(point_times) == len(set(point_times))

    dataframe = archive.to_dataframe()
    a_frame = dataframe[dataframe["icao24"] == ICAO_A]
    assert a_frame["time"].dt.second.tolist() == [0, 10, 20, 30]
    assert a_frame["source_count"].tolist() == [1, 2, 1, 1]


def test_contact_only_revision_retracts_coordinates_and_splits_again() -> None:
    archive = TrackArchive(max_gap=10)
    first = archive.write(initial_batch(), batch="first")
    downstream = first.apply({})
    late = archive.write(late_batch(), batch="late")
    downstream = late.apply(downstream)

    revised = archive.write(revision_batch(), batch="revise")
    downstream = revised.apply(downstream)

    a_segments = [
        segment for segment in archive.segments if segment.icao24 == ICAO_A
    ]
    assert [
        segment_times((segment,))[segment.segment_id] for segment in a_segments
    ] == [
        [0],
        [20, 30],
    ]
    assert [change.action for change in revised.changes] == [
        "replace",
        "add",
    ]
    assert revised.changes[0].segment_id.startswith(ICAO_A)
    assert revised.changes[1].segment_id.startswith(ICAO_A)
    assert downstream == archive.state
    assert archive.to_dataframe().shape[0] == 7

    assert [(o.source, o.has_position) for o in archive.contacts] == [
        ("s1", False),
        ("s2", False),
    ]
    assert {o.batch_id for o in archive.contacts} == {"revise"}


def test_other_aircraft_and_untouched_segments_are_unchanged() -> None:
    archive = TrackArchive(max_gap=10)
    first = archive.write(initial_batch(), batch="first")
    b_before = {
        segment_id: segment
        for segment_id, segment in archive.state.items()
        if segment.icao24 == ICAO_B
    }
    downstream = first.apply({})

    late = archive.write(late_batch(), batch="late")
    downstream = late.apply(downstream)
    revised = archive.write(revision_batch(), batch="revise")
    downstream = revised.apply(downstream)

    b_after = {
        segment_id: segment
        for segment_id, segment in archive.state.items()
        if segment.icao24 == ICAO_B
    }
    assert b_after == b_before
    assert all(
        change.icao24 == ICAO_A for change in (*late.changes, *revised.changes)
    )
    assert downstream == archive.state


def test_subsecond_arrival_is_measured_on_the_canonical_second() -> None:
    archive = TrackArchive(max_gap=10)
    result = archive.write(
        [
            observation(ICAO_A, 0.2, latitude=1.0, longitude=1.0),
            observation(ICAO_A, 10.8, latitude=1.1, longitude=1.1),
            observation(ICAO_A, 20.1, latitude=1.2, longitude=1.2),
        ],
        batch="subsecond",
    )
    assert len(result.segments) == 1
    assert list(segment_times(result.segments).values()) == [[0, 10, 20]]
    point = result.segments[0].points[1]
    assert point.time == pd.Timestamp(10, unit="s", tz="utc")
    assert point.observed_at == pd.Timestamp(10.8, unit="s", tz="utc")


def test_threshold_is_inclusive_but_larger_gap_splits() -> None:
    archive = TrackArchive(max_gap=10)
    archive.write(
        [observation(ICAO_A, 0, latitude=1.0, longitude=1.0)],
        batch="one",
    )
    exact = archive.write(
        [observation(ICAO_A, 10, latitude=1.1, longitude=1.1)],
        batch="exact",
    )
    assert len(exact.segments) == 1

    beyond = archive.write(
        [observation(ICAO_A, 21, latitude=1.2, longitude=1.2)],
        batch="beyond",
    )
    a_segments = [
        segment for segment in beyond.segments if segment.icao24 == ICAO_A
    ]
    assert [next(iter(segment_times((s,)).values())) for s in a_segments] == [
        [0, 10],
        [21],
    ]


def test_quality_records_never_invent_coordinates() -> None:
    archive = TrackArchive(max_gap=10)
    result = archive.write(
        [
            observation(
                ICAO_A,
                0,
                latitude=None,
                longitude=None,
                valid=False,
            ),
            observation(
                ICAO_A,
                5,
                latitude=None,
                longitude=None,
                valid=True,
            ),
            observation(
                ICAO_A,
                5,
                latitude=2.0,
                longitude=2.0,
                source="s2",
                valid=False,
            ),
        ],
        batch="quality",
    )
    assert result.segments == ()
    assert all(
        not point for segment in result.segments for point in segment.points
    )
    assert all(not contact.has_position for contact in archive.contacts)
    assert archive.to_dataframe().empty


def test_triggering_records_point_at_the_supplied_observations() -> None:
    archive = TrackArchive(max_gap=10)
    archive.write(initial_batch(), batch="first")
    late = archive.write(late_batch(), batch="late")
    for change in late.changes:
        assert change.triggering
        assert {record.batch_id for record in change.triggering} == {"late"}
        assert all(
            record.icao24 == change.icao24 for record in change.triggering
        )

    revised = archive.write(revision_batch(), batch="revise")
    for change in revised.changes:
        assert {record.batch_id for record in change.triggering} == {"revise"}
        assert all(record.has_position is False for record in change.triggering)


def test_replaying_changes_matches_full_recompute() -> None:
    archive = TrackArchive(max_gap=10)
    downstream: dict[str, TrackSegment] = {}

    for batch, records in (
        ("first", initial_batch()),
        ("late", late_batch()),
        ("revise", revision_batch()),
    ):
        result = archive.write(records, batch=batch)
        downstream = result.apply(downstream)
        assert downstream == archive.state

    fresh = TrackArchive(max_gap=10)
    fresh.write(
        [*initial_batch(), *late_batch(), *revision_batch()], batch="final"
    )
    fresh_shape = segment_times(fresh.segments)
    archived_shape = segment_times(archive.segments)
    assert fresh_shape == archived_shape
    assert list(fresh_shape.values()) == [[0], [20, 30], [0, 10, 20, 30]]


def test_withdraw_and_restore_replay_same_batch_result() -> None:
    archive = TrackArchive(max_gap=10)
    first = archive.write(initial_batch(), batch="first")
    downstream = first.apply({})
    late = archive.write(late_batch(), batch="late")
    downstream = late.apply(downstream)
    revised = archive.write(revision_batch(), batch="revise")
    downstream = revised.apply(downstream)

    withdrawn = archive.retract_batch("revise")
    downstream = withdrawn.apply(downstream)
    assert downstream == archive.state
    assert [(c.action, c.segment_id) for c in withdrawn.changes] == [
        ("replace", revised.changes[0].segment_id),
        ("retract", revised.changes[1].segment_id),
    ]

    restored = archive.restore_batch("revise")
    downstream = restored.apply(downstream)
    assert downstream == archive.state
    assert [(c.action, c.segment_id) for c in restored.changes] == [
        (c.action, c.segment_id) for c in revised.changes
    ]


def test_observations_keep_actual_time_and_batch_source() -> None:
    archive = TrackArchive(max_gap=10)
    archive.write(initial_batch(), batch="first")
    archive.write(late_batch(), batch="late")
    archive.write(revision_batch(), batch="revise")

    bridge_reports = [
        report
        for report in archive.observations
        if report.icao24 == ICAO_A and report.has_position
    ]
    assert {(report.source, report.batch_id) for report in bridge_reports} == {
        ("s1", "first"),
        ("s1", "late"),
        ("s2", "late"),
    }
    assert all(
        report.time == pd.Timestamp(report.time.timestamp(), unit="s", tz="utc")
        for report in bridge_reports
    )
    assert all(report.time.tzinfo is not None for report in bridge_reports)


def test_shuffled_batches_produce_stable_final_segments() -> None:
    records = [*initial_batch(), *late_batch(), *revision_batch()]
    orders = [records, list(reversed(records)), records[3:] + records[:3]]
    shapes = []
    for order in orders:
        archive = TrackArchive(max_gap=10)
        archive.write(order, batch="all")
        shapes.append(
            [
                [int(point.time.timestamp()) for point in segment.points]
                for segment in archive.segments
            ]
        )
    assert shapes[0] == shapes[1] == shapes[2]


def test_from_rebuild_accepts_timestamp_and_sensors() -> None:
    frame = pd.DataFrame(
        [
            {
                "icao24": ICAO_A,
                "timestamp": pd.Timestamp(0, unit="s", tz="utc"),
                "lat": 1.0,
                "lon": 1.0,
                "sensors": [{"serial": 1, "mintime": 0.0, "maxtime": 0.0}],
            },
            {
                "icao24": ICAO_A,
                "timestamp": pd.Timestamp(5, unit="s", tz="utc"),
                "lat": 1.1,
                "lon": 1.1,
                "sensors": [
                    {"serial": 1, "mintime": 5.0, "maxtime": 5.0},
                    {"serial": 2, "mintime": 5.0, "maxtime": 5.0},
                ],
            },
        ]
    )
    archive = TrackArchive.from_rebuild(frame, max_gap=10, batch="rebuild")
    assert len(archive.segments) == 1
    assert archive.segments[0].points[1].source_count == 2


def test_apply_rejects_out_of_order_or_divergent_state() -> None:
    archive = TrackArchive(max_gap=10)
    first = archive.write(
        [observation(ICAO_A, 0, latitude=1.0, longitude=1.0)], batch="one"
    )
    downstream = first.apply({})
    with pytest.raises(ArchiveStateError):
        apply_changes(downstream, first.changes)

    second = archive.write(
        [observation(ICAO_A, 5, latitude=1.2, longitude=1.2)], batch="two"
    )
    assert [change.action for change in second.changes] == ["replace"]
    with pytest.raises(ArchiveStateError):
        apply_changes({}, second.changes)

    downstream = second.apply(downstream)
    assert downstream == archive.state

    third = archive.write(
        [observation(ICAO_A, 7, latitude=1.3, longitude=1.3)], batch="three"
    )
    assert [change.action for change in third.changes] == ["replace"]
    diverged = dict(downstream)
    old_id = third.changes[0].segment_id
    old = diverged[old_id]
    diverged[old_id] = TrackSegment(
        segment_id=old.segment_id,
        icao24=old.icao24,
        points=(old.points[0],),
    )
    with pytest.raises(ArchiveStateError):
        apply_changes(diverged, third.changes)


def test_duplicate_batch_id_is_rejected_without_mutation() -> None:
    archive = TrackArchive(max_gap=10)
    archive.write(initial_batch(), batch="first")
    before = archive.state
    with pytest.raises(ValueError):
        archive.write(initial_batch(), batch="first")
    assert archive.state == before


def test_malformed_batch_is_rejected_without_mutation() -> None:
    archive = TrackArchive(max_gap=10)
    archive.write(initial_batch(), batch="first")
    before = archive.state
    with pytest.raises(ValueError):
        archive.write(
            [observation(ICAO_A, 40, latitude=200.0, longitude=0.0)],
            batch="bad",
        )
    assert archive.state == before
    assert archive.batch_ids == ("first",)


def test_observation_dataclass_is_accepted() -> None:
    record = Observation(
        icao24=ICAO_A,
        time=pd.Timestamp(0, unit="s", tz="utc"),
        latitude=1.0,
        longitude=2.0,
        altitude=300.0,
        source="s1",
        report_id="r1",
        revision=1,
        batch_id="ignored",
        has_position=True,
    )
    archive = TrackArchive(max_gap=10)
    result = archive.write([record], batch="direct")
    assert len(result.segments) == 1
    assert result.segments[0].points[0].observations[0].batch_id == "direct"
