"""State vector reconstruction from raw ADS-B messages.

This module provides the Rebuild class for downloading raw ADS-B data from
multiple tables and reconstructing complete state vectors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    import pandas as pd

    from .api import HasBounds
    from .trino import Trino


import numpy as np
import pandas as pd

from .decoders.base import Decoder
from .schema import (
    IdentificationData4,
    PositionData4,
    RollcallRepliesData4,
    VelocityData4,
)
from .time import deltalike, timelike, to_datetime, to_timedelta

VELOCITY_OK = "ok"
VELOCITY_NO_MATCH = "no_match"
VELOCITY_EXPIRED = "expired"
VELOCITY_CONFLICT = "conflict"

VelocityStatus = Literal["ok", "no_match", "expired", "conflict"]


def _normalize_icao24_dtype(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize icao24 column to object dtype for consistent merging.

    :param df: DataFrame with icao24 column
    :return: DataFrame with icao24 as object dtype
    """
    if df is not None and "icao24" in df.columns:
        df = df.copy()
        df["icao24"] = df["icao24"].astype(str)
    return df


def enrich_positions_with_velocity(
    positions: pd.DataFrame,
    velocities: pd.DataFrame | None,
    *,
    max_age: deltalike = 10.0,
    transitions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Attach the last usable velocity of the same phase to each position.

    Positions and velocities carry their own observation timestamps. A
    velocity is only eligible for a position when both rows refer to the same
    aircraft and the same ground or airborne phase. Only velocities observed
    at or before the position time are considered (no future data), and the
    age of the retained velocity is computed from the actual observation
    timestamps. Observations whose age equals ``max_age`` are kept, the limit
    is inclusive.

    Velocity observations with a missing value never provide a velocity:
    a fresher null observation does not replace an older but still valid
    velocity. Two different velocity values observed at the same timestamp
    for the same aircraft create a conflict: without an explicit source
    priority, neither value is picked and the conflict is preserved.

    :param positions: position records, with ``icao24``, ``timestamp`` and
        ``onground`` columns. Any other column is kept untouched.
    :param velocities: velocity records, with ``icao24``, ``timestamp`` and
        ``velocity`` columns, or ``None`` if no velocity is available.
    :param max_age: maximum accepted age of a velocity observation, inclusive.
        Numbers are interpreted as seconds.
    :param transitions: verified state switch points, with ``icao24``,
        ``timestamp`` and ``onground`` columns. From each switch timestamp
        onwards (included), the given state overrides the ``onground`` flag
        observed on position records, which fixes phase boundaries.
    :return: the positions extended with ``velocity``, ``velocity_time``,
        ``velocity_age`` and ``velocity_status`` columns. The status is one
        of ``ok``, ``no_match``, ``expired`` or ``conflict``.
    """
    missing = {"icao24", "timestamp", "onground"}.difference(positions.columns)
    if missing:
        raise ValueError(
            f"positions is missing required columns: {sorted(missing)}"
        )
    age_limit = to_timedelta(max_age)
    if age_limit <= pd.Timedelta(0):
        raise ValueError("max_age must be strictly positive")

    result = positions.copy()
    result["velocity"] = np.nan
    result["velocity_time"] = pd.Series(
        pd.NaT, index=result.index, dtype="datetime64[ns, UTC]"
    )
    result["velocity_age"] = pd.Series(
        pd.NaT, index=result.index, dtype="timedelta64[ns]"
    )
    result["velocity_status"] = pd.Series(
        VELOCITY_NO_MATCH, index=result.index, dtype="object"
    )

    pos = (
        positions[["icao24", "timestamp", "onground"]]
        .copy()
        .reset_index(drop=True)
    )
    pos["icao24"] = pos["icao24"].astype(str)
    pos["onground"] = pos["onground"].astype(bool)
    pos["__row"] = np.arange(len(pos))
    pos = pos.sort_values(["icao24", "timestamp"])

    # Verified switch points override stale onground flags from then on.
    if transitions is not None:
        missing = {"icao24", "timestamp", "onground"}.difference(
            transitions.columns
        )
        if missing:
            raise ValueError(
                f"transitions is missing required columns: {sorted(missing)}"
            )
        switches = transitions[["icao24", "timestamp", "onground"]].copy()
        switches["icao24"] = switches["icao24"].astype(str)
        switches["onground"] = switches["onground"].astype(bool)
        ordered_pos = pos.sort_values("timestamp").reset_index(names="__idx")
        overrides = pd.merge_asof(
            ordered_pos,
            switches.sort_values("timestamp"),
            on="timestamp",
            by="icao24",
            direction="backward",
            suffixes=("", "_switch"),
        )
        switched = overrides["onground_switch"].notna().to_numpy()
        updated = np.where(
            switched,
            overrides["onground_switch"].astype(bool).to_numpy(),
            overrides["onground"].to_numpy(),
        )
        pos.loc[overrides["__idx"], "onground"] = updated
    else:
        switches = None

    # Build one state timeline per aircraft from position observations and
    # verified switch points, the latter also delimit phases in gaps without
    # any position. At a shared timestamp the verified switch wins.
    events = pos[["icao24", "timestamp", "onground"]].copy()
    events["source"] = 0
    if switches is not None:
        switches = switches.assign(source=1)
        events = pd.concat([events, switches], ignore_index=True)
    events = (
        events.sort_values(["icao24", "timestamp", "source"])
        .drop_duplicates(["icao24", "timestamp"], keep="last")
        .reset_index(drop=True)
    )
    changed = events["onground"].ne(
        events.groupby("icao24")["onground"].shift()
    )
    events["phase_id"] = changed.groupby(events["icao24"]).cumsum()
    pos = pos.merge(
        events[["icao24", "timestamp", "phase_id"]],
        on=["icao24", "timestamp"],
        how="left",
    )

    if velocities is None or velocities.shape[0] == 0:
        return result

    missing = {"icao24", "timestamp", "velocity"}.difference(velocities.columns)
    if missing:
        raise ValueError(
            f"velocities is missing required columns: {sorted(missing)}"
        )

    vel = velocities[["icao24", "timestamp", "velocity"]].copy()
    vel["icao24"] = vel["icao24"].astype(str)
    distinct = vel.groupby(["icao24", "timestamp"], sort=False)[
        "velocity"
    ].transform("nunique")
    # A null observation carries no value and never hides an older valid
    # velocity. Two distinct values at the same timestamp are a conflict.
    vel["kind"] = np.select(
        [distinct > 1, vel["velocity"].isna()],
        [VELOCITY_CONFLICT, "null"],
        default=VELOCITY_OK,
    )

    # Each velocity belongs to the phase active at its own observation time:
    # the state of the latest timeline event at or before it. A velocity
    # observed in the gap between two phases therefore stays attached to the
    # earlier phase and can never leak across the state switch.
    vel = vel.sort_values("timestamp").reset_index(drop=True)
    phase_of_velocity = pd.merge_asof(
        vel,
        events[["icao24", "timestamp", "phase_id"]].sort_values("timestamp"),
        on="timestamp",
        by="icao24",
        direction="backward",
    )
    vel["phase_id"] = phase_of_velocity["phase_id"].to_numpy()
    # Velocities observed before the first position of the aircraft have an
    # unknown state: keeping them could leak a ground speed into the first
    # airborne position, so they are dropped.
    first_position = (
        pos.groupby("icao24")["timestamp"].min().rename("first_position")
    )
    vel = vel.merge(first_position, on="icao24", how="left")
    vel = vel[
        vel["phase_id"].notna() & (vel["timestamp"] >= vel["first_position"])
    ].copy()
    vel["phase_id"] = vel["phase_id"].astype(np.int64)
    vel = vel.drop(columns="first_position")

    candidates = (
        vel[vel["kind"].isin([VELOCITY_OK, VELOCITY_CONFLICT])][
            ["icao24", "phase_id", "timestamp", "velocity", "kind"]
        ]
        .rename(columns={"timestamp": "velocity_time"})
        .drop_duplicates(["icao24", "phase_id", "velocity_time", "kind"])
        .sort_values("velocity_time")
    )
    latest = pd.merge_asof(
        pos.sort_values("timestamp"),
        candidates,
        left_on="timestamp",
        right_on="velocity_time",
        by=["icao24", "phase_id"],
        direction="backward",
        tolerance=age_limit,
    )[["__row", "velocity", "velocity_time", "kind"]]

    seen = (
        vel[["icao24", "phase_id", "timestamp"]]
        .rename(columns={"timestamp": "past_time"})
        .drop_duplicates(["icao24", "phase_id", "past_time"])
        .sort_values("past_time")
    )
    past = pd.merge_asof(
        pos.sort_values("timestamp"),
        seen,
        left_on="timestamp",
        right_on="past_time",
        by=["icao24", "phase_id"],
        direction="backward",
    )[["__row", "past_time"]]

    outcomes = (
        pos[["__row", "timestamp"]]
        .merge(latest, on="__row", how="left")
        .merge(past, on="__row", how="left")
        .sort_values("__row")
        .reset_index(drop=True)
    )

    has_ok = outcomes["kind"].eq(VELOCITY_OK).to_numpy()
    has_conflict = outcomes["kind"].eq(VELOCITY_CONFLICT).to_numpy()
    has_past = outcomes["past_time"].notna().to_numpy()
    result["velocity_status"] = np.select(
        [has_ok, has_conflict, has_past],
        [VELOCITY_OK, VELOCITY_CONFLICT, VELOCITY_EXPIRED],
        default=VELOCITY_NO_MATCH,
    )
    result.loc[has_ok, "velocity"] = outcomes.loc[has_ok, "velocity"].to_numpy()
    result.loc[has_ok, "velocity_time"] = outcomes.loc[
        has_ok, "velocity_time"
    ].to_numpy()
    result.loc[has_ok, "velocity_age"] = (
        (outcomes["timestamp"] - outcomes["velocity_time"])
        .loc[has_ok]
        .to_numpy()
    )

    return result


class Rebuild:
    """Class for rebuilding state vectors from raw ADS-B messages.

    This class provides methods to download and decode raw ADS-B messages
    from different data tables and merge them into complete state vectors.

    **Example usage**::

        from pyopensky.trino import Trino
        from pyopensky.rebuild import Rebuild

        trino = Trino()
        rebuild = Rebuild(trino)

        # Without decoding
        data = rebuild.rebuild(
            start="2023-01-03 16:00",
            stop="2023-01-03 20:00",
            icao24="400A0E"
        )

        # With pymodes or rs1090 decoding
        data = rebuild.rebuild(
            start="2023-01-03 16:00",
            stop="2023-01-03 20:00",
            icao24="400A0E",
            decoder="pymodes"  # or "rs1090"
        )

    """

    def __init__(self, trino: Trino) -> None:
        """Initialize Rebuild with a Trino instance.

        :param trino: Trino instance for database queries
        """
        self.trino = trino

    def redecode_position(
        self,
        start: timelike,
        stop: timelike,
        *,
        icao24: str | list[str] | None = None,
        bounds: HasBounds | tuple[float, float, float, float] | None = None,
        cached: bool = True,
        compress: bool = False,
        decoder: Decoder | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame | None:
        """Download and optionally redecode position data from raw messages.

        Downloads position data with raw ADS-B messages, pairs odd/even CPR
        frames, and optionally decodes them using a visitor pattern.

        :param start: start of time range
        :param stop: end of time range
        :param icao24: aircraft transponder code(s)
        :param bounds: geographical footprint
        :param cached: use cached data
        :param compress: compress cache files
        :param decoder: decoder object implementing decode_position(df) method
            that decodes raw messages. If None, returns raw data.

        :return: DataFrame with position data (decoded if visitor provided)

        **Example usage**::

            # Create a pymodes decoder
            from pyopensky.decoders import PyModesDecoder
            decoder = PyModesDecoder()

            # Redecode positions
            pos = rebuild.redecode_position(
                start="2023-01-03 16:00",
                stop="2023-01-03 20:00",
                icao24="400A0E",
                decoder=decoder
            )
        """
        start_ts = to_datetime(start)
        stop_ts = to_datetime(stop)

        if icao24 is None and bounds is None:
            raise ValueError("Either 'icao24' or 'bounds' must be specified")

        # Get icao24s from bounds if needed
        query_icao24 = icao24
        if bounds is not None and icao24 is None:
            pos_sample = self.trino.rawdata(
                start=start_ts,
                stop=stop_ts,
                bounds=bounds,
                Table=PositionData4,
                cached=cached,
                compress=compress,
            )
            if pos_sample is not None and pos_sample.shape[0] > 0:
                query_icao24 = pos_sample["icao24"].unique().tolist()
            else:
                return None

        # Download position data with rawmsg
        pos_data = self.trino.rawdata(
            start=start_ts,
            stop=stop_ts,
            icao24=query_icao24,
            bounds=bounds if icao24 is not None else None,
            Table=PositionData4,
            cached=cached,
            compress=compress,
            extra_columns=(
                PositionData4.lat,
                PositionData4.lon,
                PositionData4.alt,
                PositionData4.groundspeed,
                PositionData4.heading,
                PositionData4.surface,
                PositionData4.nic,
                PositionData4.odd,
            ),
        )

        if pos_data is None or pos_data.shape[0] == 0:
            return None

        # Prepare position data
        pos_data = (
            pos_data.assign(
                timestamp=lambda x: pd.to_datetime(
                    x.mintime, unit="s", utc=True
                )
            )
            .drop_duplicates("rawmsg")
            .rename(
                columns={
                    "surface": "onground",
                    "alt": "altitude",
                    "heading": "track",
                }
            )
            .sort_values("timestamp")
        )

        # Apply decoder if provided
        if decoder is not None:
            pos_data = decoder.decode_position(pos_data)

        if "rawmsg" in pos_data.columns:
            pos_data = pos_data.drop(columns=["rawmsg"])

        return pos_data

    def redecode_velocity(
        self,
        start: timelike,
        stop: timelike,
        *,
        icao24: str | list[str] | None = None,
        cached: bool = True,
        compress: bool = False,
        decoder: Decoder | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame | None:
        """Download and optionally redecode velocity data from raw messages.

        :param decoder: decoder implementing decode_velocity(df) method
        """
        start_ts = to_datetime(start)
        stop_ts = to_datetime(stop)

        if icao24 is None:
            raise ValueError("'icao24' must be specified")

        vel_data = self.trino.rawdata(
            start=start_ts,
            stop=stop_ts,
            icao24=icao24,
            bounds=None,
            Table=VelocityData4,
            cached=cached,
            compress=compress,
            extra_columns=(
                VelocityData4.velocity,
                VelocityData4.heading,
                VelocityData4.vertrate,
                VelocityData4.geominurbaro,
            ),
        )

        if vel_data is None or vel_data.shape[0] == 0:
            return None

        vel_data = (
            vel_data.assign(
                timestamp=lambda x: pd.to_datetime(
                    x.mintime, unit="s", utc=True
                )
            )
            .drop_duplicates("rawmsg")
            .rename(columns={"heading": "track", "vertrate": "vertical_rate"})
            .sort_values("timestamp")
        )

        if decoder is not None:
            vel_data = decoder.decode_velocity(vel_data)

        if "rawmsg" in vel_data.columns:
            vel_data = vel_data.drop(columns=["rawmsg"])

        return vel_data

    def redecode_identification(
        self,
        start: timelike,
        stop: timelike,
        *,
        icao24: str | list[str] | None = None,
        cached: bool = True,
        compress: bool = False,
        decoder: Decoder | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame | None:
        """Download and optionally redecode identification (callsign) data.

        :param decoder: decoder implementing decode_identification(df) method
        """
        start_ts = to_datetime(start)
        stop_ts = to_datetime(stop)

        if icao24 is None:
            raise ValueError("'icao24' must be specified")

        ident_data = self.trino.rawdata(
            start=start_ts,
            stop=stop_ts,
            icao24=icao24,
            bounds=None,
            Table=IdentificationData4,
            cached=cached,
            compress=compress,
            extra_columns=(IdentificationData4.identity,),
        )

        if ident_data is None or ident_data.shape[0] == 0:
            return None

        ident_data = (
            ident_data.assign(
                timestamp=lambda x: pd.to_datetime(
                    x.mintime, unit="s", utc=True
                )
            )
            .rename(columns={"identity": "callsign"})
            .assign(callsign=lambda x: x.callsign.str.replace(" ", ""))
            .sort_values("timestamp")
        )

        if decoder is not None:
            ident_data = decoder.decode_identification(ident_data)

        if "rawmsg" in ident_data.columns:
            ident_data = ident_data.drop(columns=["rawmsg"])

        return ident_data

    def redecode_rollcall(
        self,
        start: timelike,
        stop: timelike,
        *,
        icao24: str | list[str] | None = None,
        cached: bool = True,
        compress: bool = False,
        decoder: Decoder | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame | None:
        """Download and optionally redecode rollcall (squawk) data.

        :param decoder: decoder implementing decode_rollcall(df) method
        """
        start_ts = to_datetime(start)
        stop_ts = to_datetime(stop)

        if icao24 is None:
            raise ValueError("'icao24' must be specified")

        rollcall_data = self.trino.rawdata(
            start=start_ts,
            stop=stop_ts,
            icao24=icao24,
            bounds=None,
            Table=RollcallRepliesData4,
            cached=cached,
            compress=compress,
        )

        if rollcall_data is None or rollcall_data.shape[0] == 0:
            return None

        rollcall_data = rollcall_data.assign(
            timestamp=lambda x: pd.to_datetime(x.mintime, unit="s", utc=True)
        ).sort_values("timestamp")

        if decoder is not None:
            rollcall_data = decoder.decode_rollcall(rollcall_data)

        return rollcall_data

    def rebuild(
        self,
        start: timelike,
        stop: timelike,
        *,
        icao24: str | list[str] | None = None,
        bounds: HasBounds | tuple[float, float, float, float] | None = None,
        cached: bool = True,
        compress: bool = False,
        decoder: Decoder | Literal["pymodes", "rs1090"] | None = None,
        include_rollcall: bool = False,
        transitions: pd.DataFrame | None = None,
        velocity_max_age: deltalike = 10.0,
        **kwargs: Any,
    ) -> pd.DataFrame | None:
        """Rebuild state vectors by downloading and merging all data tables.

        This method downloads data from position, velocity, identification,
        and optionally rollcall tables, then merges them using time-based
        joins to reconstruct complete state vectors.

        :param start: start of time range
        :param stop: end of time range
        :param icao24: aircraft transponder code(s)
        :param bounds: geographical footprint
        :param cached: use cached data
        :param compress: compress cache files
        :param decoder: decoder for decoding raw messages (e.g. PyModesDecoder)
        :param include_rollcall: include rollcall (squawk) data in merge
        :param transitions: verified state switch points with ``icao24``,
            ``timestamp`` and ``onground`` columns, used to delimit ground
            and airborne phases when attaching the last usable velocity
        :param velocity_max_age: inclusive maximum age, in seconds by
            default, of a velocity observation attached to a position

        :return: DataFrame with merged state vectors

        **Example usage**::

            # Without decoding (use database decoded values)
            data = rebuild.rebuild(
                start="2023-01-03 16:00",
                stop="2023-01-03 20:00",
                icao24="400A0E"
            )

            # With pymodes or rs1090 decoding
            data = rebuild.rebuild(
                start="2023-01-03 16:00",
                stop="2023-01-03 20:00",
                icao24="400A0E",
                decoder="pymodes"  # or "rs1090"
            )
        """
        if isinstance(decoder, str):
            if decoder == "pymodes":
                from .decoders.pymodes import PyModesDecoder

                decoder = PyModesDecoder()
            elif decoder == "rs1090":
                from .decoders.rs1090 import Rs1090Decoder

                decoder = Rs1090Decoder()
            else:
                raise ValueError(
                    f"Unknown decoder string: {decoder}. "
                    "Use 'pymodes' or 'rs1090' or your own instance."
                )

        # Get position data
        pos_data = self.redecode_position(
            start=start,
            stop=stop,
            icao24=icao24,
            bounds=bounds,
            cached=cached,
            compress=compress,
            decoder=decoder,
        )

        if pos_data is None or pos_data.shape[0] == 0:
            return None

        # Normalize icao24 dtype for consistent merging
        pos_data = _normalize_icao24_dtype(pos_data)

        # Get icao24s from position data for other queries
        query_icao24 = pos_data["icao24"].unique().tolist()

        # Get velocity data
        vel_data = self.redecode_velocity(
            start=start,
            stop=stop,
            icao24=query_icao24,
            cached=cached,
            compress=compress,
            decoder=decoder,
        )

        # Merge velocity data
        if vel_data is not None and vel_data.shape[0] > 0:
            vel_data = _normalize_icao24_dtype(vel_data)
            pos_data = pd.merge_asof(
                pos_data,
                vel_data[
                    [
                        "timestamp",
                        "icao24",
                        "track",
                        "vertical_rate",
                        "geominurbaro",
                    ]
                ],
                on="timestamp",
                by="icao24",
                direction="nearest",
                tolerance=cast(pd.Timedelta, pd.Timedelta(seconds=5)),
                suffixes=("_pos", "_vel"),
            )

            # Combine overlapping columns
            if (
                "track_pos" in pos_data.columns
                and "track_vel" in pos_data.columns
            ):
                track_pos = pos_data["track_pos"]
                track_vel = pos_data["track_vel"]
                pos_data["track"] = np.where(
                    pd.isna(track_pos), track_vel, track_pos
                )
                pos_data = pos_data.drop(
                    columns=["track_pos", "track_vel"], errors="ignore"
                )
            elif "track_pos" in pos_data.columns:
                pos_data["track"] = pos_data["track_pos"]
                pos_data = pos_data.drop(columns=["track_pos"], errors="ignore")

            # Calculate geoaltitude
            if (
                "geominurbaro" in pos_data.columns
                and "altitude" in pos_data.columns
            ):
                pos_data["geoaltitude"] = (
                    pos_data["altitude"] + pos_data["geominurbaro"]
                )

            # Attach the last usable velocity of the same ground or airborne
            # phase, along with its observation time, age and match status.
            pos_data = enrich_positions_with_velocity(
                pos_data,
                vel_data,
                max_age=velocity_max_age,
                transitions=transitions,
            )
        else:
            pos_data = enrich_positions_with_velocity(
                pos_data,
                None,
                max_age=velocity_max_age,
                transitions=transitions,
            )

        # Get identification data
        ident_data = self.redecode_identification(
            start=start,
            stop=stop,
            icao24=query_icao24,
            cached=cached,
            compress=compress,
            decoder=decoder,
        )

        # Merge identification data
        if ident_data is not None and ident_data.shape[0] > 0:
            ident_data = _normalize_icao24_dtype(ident_data)
            pos_data = pd.merge_asof(
                pos_data,
                ident_data[["timestamp", "icao24", "callsign"]],
                on="timestamp",
                by="icao24",
                direction="nearest",
                tolerance=cast(pd.Timedelta, pd.Timedelta(seconds=5)),
            )

        # Optionally get and merge rollcall data
        rollcall_data = self.redecode_rollcall(
            start=start,
            stop=stop,
            icao24=query_icao24,
            cached=cached,
            compress=compress,
            decoder=decoder,
        )

        if rollcall_data is not None and rollcall_data.shape[0] > 0:
            rollcall_data = _normalize_icao24_dtype(rollcall_data)
            if include_rollcall:
                pos_data = pd.merge_asof(
                    pos_data,
                    rollcall_data,
                    on="timestamp",
                    by="icao24",
                    direction="nearest",
                    tolerance=cast(pd.Timedelta, pd.Timedelta(seconds=5)),
                )
            else:
                pos_data = pd.concat([pos_data, rollcall_data], axis=0)

        return pos_data.sort_values(["icao24", "timestamp"])
