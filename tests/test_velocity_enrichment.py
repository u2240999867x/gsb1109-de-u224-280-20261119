"""Offline tests for phase-aware velocity enrichment of rebuilt positions."""

from typing import Any

import pytest

import numpy as np
import pandas as pd
from pyopensky.rebuild import (
    VELOCITY_CONFLICT,
    VELOCITY_EXPIRED,
    VELOCITY_NO_MATCH,
    VELOCITY_OK,
    Rebuild,
    enrich_positions_with_velocity,
)
from pyopensky.schema import (
    IdentificationData4,
    PositionData4,
    RollcallRepliesData4,
    VelocityData4,
)

T0 = pd.Timestamp("2025-06-01 00:00:00", tz="UTC")


def ts(seconds: int) -> pd.Timestamp:
    return T0 + pd.Timedelta(seconds=seconds)


def position_frame(rows: list[tuple[str, int, bool]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "icao24": [icao24 for icao24, _, _ in rows],
            "timestamp": [ts(second) for _, second, _ in rows],
            "onground": [onground for _, _, onground in rows],
            "lat": [float(index) for index in range(len(rows))],
            "lon": [float(index) + 0.5 for index in range(len(rows))],
            "altitude": [1000.0 + index for index in range(len(rows))],
            "track": [10.0 * index for index in range(len(rows))],
        }
    )


def velocity_frame(rows: list[tuple[str, int, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "icao24": [icao24 for icao24, _, _ in rows],
            "timestamp": [ts(second) for _, second, _ in rows],
            "velocity": [value for _, _, value in rows],
        }
    )


@pytest.fixture
def positions() -> pd.DataFrame:
    # Aircraft aaaaaa: ground, first airborne phase, landing, second takeoff.
    # Aircraft bbbbbb: simultaneous airborne data of another aircraft.
    return position_frame(
        [
            ("aaaaaa", 0, True),
            ("aaaaaa", 2, True),  # last ground position before takeoff
            ("aaaaaa", 10, False),  # first airborne position
            ("aaaaaa", 20, False),
            ("aaaaaa", 30, False),
            ("aaaaaa", 40, False),
            ("aaaaaa", 60, False),
            ("aaaaaa", 100, True),  # landed
            ("aaaaaa", 110, True),
            ("aaaaaa", 120, False),  # takeoff again
            ("aaaaaa", 130, False),
            ("bbbbbb", 10, False),
            ("bbbbbb", 20, False),
        ]
    )


@pytest.fixture
def velocities() -> pd.DataFrame:
    return velocity_frame(
        [
            # Ground speed of aaaaaa, close to the last ground position.
            ("aaaaaa", 1, 10.0),
            # Airborne velocities only arrive after the first airborne pos.
            ("aaaaaa", 15, 150.0),
            ("aaaaaa", 18, np.nan),  # fresher null observation
            ("aaaaaa", 20, 160.0),  # age 10 s at t=30: exactly at the limit
            ("aaaaaa", 26, np.nan),
            # Same timestamp, two different values: unresolved conflict.
            ("aaaaaa", 38, 200.0),
            ("aaaaaa", 38, 210.0),
            ("aaaaaa", 45, 190.0),  # only stale observation left at t=60
            # Ground speed in the landing phase.
            ("aaaaaa", 105, 5.0),
            # Second aircraft, same window.
            ("bbbbbb", 15, 300.0),
            ("bbbbbb", 20, 310.0),
        ]
    )


def row(df: pd.DataFrame, icao24: str, second: int) -> pd.Series:
    mask = (df["icao24"] == icao24) & (df["timestamp"] == ts(second))
    return df.loc[mask].iloc[0]


def test_takeoff_narrative(
    positions: pd.DataFrame, velocities: pd.DataFrame
) -> None:
    result = enrich_positions_with_velocity(positions, velocities, max_age=10.0)

    first_air = row(result, "aaaaaa", 10)
    # No future velocity and no previous ground velocity may fill this row.
    assert first_air["velocity_status"] == VELOCITY_NO_MATCH
    assert pd.isna(first_air["velocity"])
    assert pd.isna(first_air["velocity_time"])
    assert pd.isna(first_air["velocity_age"])

    ground = row(result, "aaaaaa", 2)
    assert ground["velocity_status"] == VELOCITY_OK
    assert ground["velocity"] == 10.0
    assert ground["velocity_time"] == ts(1)
    assert ground["velocity_age"] == pd.Timedelta(seconds=1)

    second_air = row(result, "aaaaaa", 20)
    assert second_air["velocity_status"] == VELOCITY_OK
    assert second_air["velocity"] == 160.0
    assert second_air["velocity_time"] == ts(20)
    assert second_air["velocity_age"] == pd.Timedelta(0)

    # The newer nulls do not hide the valid velocity, age 10 s is included.
    boundary = row(result, "aaaaaa", 30)
    assert boundary["velocity_status"] == VELOCITY_OK
    assert boundary["velocity"] == 160.0
    assert boundary["velocity_time"] == ts(20)
    assert boundary["velocity_age"] == pd.Timedelta(seconds=10)

    conflict = row(result, "aaaaaa", 40)
    assert conflict["velocity_status"] == VELOCITY_CONFLICT
    assert pd.isna(conflict["velocity"])
    assert pd.isna(conflict["velocity_age"])

    expired = row(result, "aaaaaa", 60)
    assert expired["velocity_status"] == VELOCITY_EXPIRED
    assert pd.isna(expired["velocity"])

    # Landing phase: airborne velocities cannot cross the state switch.
    assert row(result, "aaaaaa", 100)["velocity_status"] == VELOCITY_NO_MATCH
    landed = row(result, "aaaaaa", 110)
    assert landed["velocity_status"] == VELOCITY_OK
    assert landed["velocity"] == 5.0

    # Second takeoff: the previous airborne phase is unreachable.
    for second in (120, 130):
        again = row(result, "aaaaaa", second)
        assert again["velocity_status"] == VELOCITY_NO_MATCH
        assert pd.isna(again["velocity"])


def test_second_aircraft_is_independent(
    positions: pd.DataFrame, velocities: pd.DataFrame
) -> None:
    result = enrich_positions_with_velocity(positions, velocities, max_age=10.0)

    assert row(result, "bbbbbb", 10)["velocity_status"] == VELOCITY_NO_MATCH
    second = row(result, "bbbbbb", 20)
    assert second["velocity_status"] == VELOCITY_OK
    assert second["velocity"] == 310.0
    assert second["velocity_age"] == pd.Timedelta(0)


def test_input_order_does_not_resolve_conflict(
    positions: pd.DataFrame, velocities: pd.DataFrame
) -> None:
    shuffled = velocities.sample(frac=1.0, random_state=4)
    ordered = enrich_positions_with_velocity(
        positions, velocities, max_age=10.0
    )
    shuffled_result = enrich_positions_with_velocity(
        positions, shuffled, max_age=10.0
    )
    pd.testing.assert_frame_equal(
        ordered.sort_values(["icao24", "timestamp"]).reset_index(drop=True),
        shuffled_result.sort_values(["icao24", "timestamp"]).reset_index(
            drop=True
        ),
    )
    assert row(shuffled_result, "aaaaaa", 40)["velocity_status"] == (
        VELOCITY_CONFLICT
    )


def test_existing_position_fields_are_kept(
    positions: pd.DataFrame, velocities: pd.DataFrame
) -> None:
    result = enrich_positions_with_velocity(positions, velocities, max_age=10.0)
    for column in ("lat", "lon", "altitude", "track", "onground"):
        pd.testing.assert_series_equal(
            result[column].reset_index(drop=True),
            positions[column].reset_index(drop=True),
            check_names=False,
        )
    assert len(result) == len(positions)


def test_no_velocity_data(positions: pd.DataFrame) -> None:
    for velocities in (None, pd.DataFrame()):
        result = enrich_positions_with_velocity(
            positions, velocities, max_age=10.0
        )
        assert set(result["velocity_status"]) == {VELOCITY_NO_MATCH}
        assert result["velocity"].isna().all()
        assert result["velocity_time"].isna().all()
        assert result["velocity_age"].isna().all()


def test_max_age_timedelta_and_string(
    positions: pd.DataFrame, velocities: pd.DataFrame
) -> None:
    from_timedelta = enrich_positions_with_velocity(
        positions, velocities, max_age=pd.Timedelta(seconds=10)
    )
    from_string = enrich_positions_with_velocity(
        positions, velocities, max_age="10s"
    )
    pd.testing.assert_frame_equal(from_timedelta, from_string)
    assert row(from_timedelta, "aaaaaa", 30)["velocity_status"] == VELOCITY_OK

    stricter = enrich_positions_with_velocity(
        positions, velocities, max_age=9.0
    )
    # Age 10 s now falls outside the limit.
    assert row(stricter, "aaaaaa", 30)["velocity_status"] == VELOCITY_EXPIRED


def test_verified_transitions_override_onground() -> None:
    # The t=10 position still carries a ground flag although the verified
    # takeoff happened at t=5. The first airborne position must not reuse the
    # ground speed, and the later airborne velocity belongs to the air phase.
    positions = position_frame(
        [("cccccc", 0, True), ("cccccc", 10, True), ("cccccc", 20, False)]
    )
    velocities = velocity_frame([("cccccc", 2, 10.0), ("cccccc", 12, 100.0)])
    transitions = pd.DataFrame(
        {
            "icao24": ["cccccc"],
            "timestamp": [ts(5)],
            "onground": [False],
        }
    )

    without = enrich_positions_with_velocity(
        positions, velocities, max_age=10.0
    )
    # Without the switch, t=10 is ground and inherits the ground speed.
    assert row(without, "cccccc", 10)["velocity"] == 10.0
    # The t=12 observation stays in the ground phase per the raw flags, so
    # the first airborne position at t=20 has no airborne velocity.
    assert row(without, "cccccc", 20)["velocity_status"] == VELOCITY_NO_MATCH

    with_transition = enrich_positions_with_velocity(
        positions,
        velocities,
        max_age=10.0,
        transitions=transitions,
    )
    switched = row(with_transition, "cccccc", 10)
    assert switched["velocity_status"] == VELOCITY_NO_MATCH
    assert pd.isna(switched["velocity"])
    airborne = row(with_transition, "cccccc", 20)
    assert airborne["velocity_status"] == VELOCITY_OK
    assert airborne["velocity"] == 100.0
    assert airborne["velocity_age"] == pd.Timedelta(seconds=8)


def test_velocity_before_first_position_is_ignored() -> None:
    # A velocity arriving before any position has no verifiable state and
    # must not leak into the first (possibly airborne) position.
    positions = position_frame([("dddddd", 10, False), ("dddddd", 20, False)])
    velocities = velocity_frame([("dddddd", 5, 20.0), ("dddddd", 15, 140.0)])
    result = enrich_positions_with_velocity(positions, velocities, max_age=10.0)
    first = row(result, "dddddd", 10)
    assert first["velocity_status"] == VELOCITY_NO_MATCH
    assert pd.isna(first["velocity"])
    second = row(result, "dddddd", 20)
    assert second["velocity_status"] == VELOCITY_OK
    assert second["velocity"] == 140.0


def test_invalid_arguments(
    positions: pd.DataFrame, velocities: pd.DataFrame
) -> None:
    with pytest.raises(ValueError, match="max_age"):
        enrich_positions_with_velocity(positions, velocities, max_age=0)
    with pytest.raises(ValueError, match="positions is missing"):
        enrich_positions_with_velocity(
            positions.drop(columns="onground"), velocities
        )
    with pytest.raises(ValueError, match="velocities is missing"):
        enrich_positions_with_velocity(
            positions, velocities.drop(columns="velocity")
        )


class FakeTrino:
    """Minimal Trino stand-in returning crafted raw tables."""

    def __init__(
        self,
        positions: pd.DataFrame,
        velocities: pd.DataFrame,
        ident: pd.DataFrame,
    ) -> None:
        self.positions = positions
        self.velocities = velocities
        self.ident = ident

    def rawdata(self, **kwargs: Any) -> pd.DataFrame | None:
        table = kwargs["Table"]
        if table is PositionData4:
            return self.positions
        if table is VelocityData4:
            return self.velocities
        if table is IdentificationData4:
            return self.ident
        if table is RollcallRepliesData4:
            return None
        raise AssertionError(f"unexpected table {table}")


def test_rebuild_integration(
    positions: pd.DataFrame, velocities: pd.DataFrame
) -> None:
    epoch = T0.timestamp()
    raw_positions = positions.rename(
        columns={"onground": "surface", "altitude": "alt", "track": "heading"}
    ).copy()
    raw_positions["mintime"] = (
        raw_positions["timestamp"] - T0
    ).dt.total_seconds() + epoch
    raw_positions["rawmsg"] = [
        f"msg-{index:02d}" for index in range(len(raw_positions))
    ]
    raw_positions["nic"] = 0
    raw_positions["odd"] = False

    raw_velocities = velocities.copy()
    raw_velocities["mintime"] = (
        raw_velocities["timestamp"] - T0
    ).dt.total_seconds() + epoch
    raw_velocities["rawmsg"] = [
        f"vel-{index:02d}" for index in range(len(raw_velocities))
    ]
    raw_velocities["heading"] = 90.0
    raw_velocities["vertrate"] = 0.0
    raw_velocities["geominurbaro"] = 100.0

    ident = pd.DataFrame(
        {
            "icao24": ["aaaaaa", "bbbbbb"],
            "mintime": [epoch, epoch],
            "rawmsg": ["id-0", "id-1"],
            "identity": ["TEST0001", "TEST0002"],
        }
    )

    rebuild = Rebuild(
        FakeTrino(raw_positions, raw_velocities, ident)  # type: ignore[arg-type]
    )
    result = rebuild.rebuild(
        T0,
        ts(140),
        icao24=["aaaaaa", "bbbbbb"],
        velocity_max_age=10.0,
        cached=False,
    )

    assert result is not None
    assert {
        "velocity",
        "velocity_time",
        "velocity_age",
        "velocity_status",
    }.issubset(result.columns)
    # Original position fields survive the full pipeline.
    assert {"lat", "lon", "altitude", "callsign"}.issubset(result.columns)
    assert row(result, "aaaaaa", 10)["velocity_status"] == VELOCITY_NO_MATCH
    assert row(result, "aaaaaa", 30)["velocity"] == 160.0
    assert row(result, "aaaaaa", 40)["velocity_status"] == VELOCITY_CONFLICT
    assert row(result, "aaaaaa", 60)["velocity_status"] == VELOCITY_EXPIRED
    assert row(result, "bbbbbb", 20)["velocity"] == 310.0
