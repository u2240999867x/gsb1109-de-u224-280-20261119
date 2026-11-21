"""Regression for OpenSky history daily reports across DST transitions.

The airport keeps local daily reports in ``America/St_Johns``. A report runs
from local midnight to just before the next local midnight, and only state
vectors attached to a confirmed route are admitted.

The hard-coded UTC expectations below are fixed literals derived from the IANA
time zone rules for 2025, never computed from the window passed to the query.
The synthetic database is fully offline: the production ``Trino.history`` SQL
is executed unchanged against SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    TextClause,
    create_engine,
    text,
)
from trino.sqlalchemy.dialect import TrinoDialect

import pandas as pd
from pyopensky.schema import (
    FlightsData4,
    StateVectorsData4,
    UTCTimestampFloat,
    UTCTimestampInteger,
)
from pyopensky.trino import Trino

UTC = ZoneInfo("UTC")
ST_JOHNS = ZoneInfo("America/St_Johns")
ROUTE = ("CYYT", "LFPG")
OTHER_ROUTE = ("CYYT", "EGLL")
COLUMNS = (
    "time",
    "hour",
    "icao24",
    "callsign",
    "FlightsData4.estdepartureairport",
    "FlightsData4.estarrivalairport",
)


def epoch(value: str) -> int:
    """Fixed epoch literal for a UTC timestamp string."""
    return int(pd.Timestamp(value, tz="UTC").timestamp())


# (UTC start, UTC stop, allowed hour floor, exclusive hour ceiling), in epochs.
# Local midnight in America/St_Johns is 03:30 UTC under NST (-03:30) and
# 02:30 UTC under NDT (-02:30); the hour filter may read both edge partitions.
WINDOWS: dict[str, tuple[int, int, int, int]] = {
    "2025-03-08": (
        epoch("2025-03-08 03:30"),
        epoch("2025-03-09 03:30"),
        epoch("2025-03-08 03:00"),
        epoch("2025-03-09 04:00"),
    ),
    "2025-03-09": (
        epoch("2025-03-09 03:30"),
        epoch("2025-03-10 02:30"),
        epoch("2025-03-09 03:00"),
        epoch("2025-03-10 03:00"),
    ),
    "2025-03-10": (
        epoch("2025-03-10 02:30"),
        epoch("2025-03-11 02:30"),
        epoch("2025-03-10 02:00"),
        epoch("2025-03-11 03:00"),
    ),
    "2025-11-01": (
        epoch("2025-11-01 02:30"),
        epoch("2025-11-02 02:30"),
        epoch("2025-11-01 02:00"),
        epoch("2025-11-02 03:00"),
    ),
    "2025-11-02": (
        epoch("2025-11-02 02:30"),
        epoch("2025-11-03 03:30"),
        epoch("2025-11-02 02:00"),
        epoch("2025-11-03 04:00"),
    ),
    "2025-11-03": (
        epoch("2025-11-03 03:30"),
        epoch("2025-11-04 03:30"),
        epoch("2025-11-03 03:00"),
        epoch("2025-11-04 04:00"),
    ),
}

# The incorrect variant treats the local wall clock as if it were UTC, so it
# derives hour partitions from midnight wall-clock readings.
WALL_CLOCK_HOURS: dict[str, tuple[int, int]] = {
    "2025-03-09": (
        epoch("2025-03-09 00:00"),
        epoch("2025-03-10 00:00"),
    ),
    "2025-11-02": (
        epoch("2025-11-02 00:00"),
        epoch("2025-11-03 00:00"),
    ),
}


@dataclass(frozen=True)
class ExpectedReport:
    rows: frozenset[tuple[str, int]]
    hours: frozenset[int]


MARCH_EXPECTED = {
    "2025-03-08": ExpectedReport(
        rows=frozenset({("f10001", epoch("2025-03-09 03:29:58"))}),
        hours=frozenset({epoch("2025-03-09 03:00")}),
    ),
    "2025-03-09": ExpectedReport(
        rows=frozenset(
            {
                ("f10001", epoch("2025-03-09 03:30:00")),
                ("f10001", epoch("2025-03-09 06:29:58")),
                ("f10001", epoch("2025-03-09 06:30:00")),
                ("f10001", epoch("2025-03-09 06:30:02")),
                ("f10002", epoch("2025-03-10 02:29:58")),
            }
        ),
        hours=frozenset(
            {
                epoch("2025-03-09 03:00"),
                epoch("2025-03-09 06:00"),
                epoch("2025-03-10 02:00"),
            }
        ),
    ),
    "2025-03-10": ExpectedReport(
        rows=frozenset(
            {
                ("f10002", epoch("2025-03-10 02:30:00")),
                ("f10002", epoch("2025-03-10 02:30:02")),
                ("f10002", epoch("2025-03-10 03:00:00")),
            }
        ),
        hours=frozenset(
            {
                epoch("2025-03-10 02:00"),
                epoch("2025-03-10 03:00"),
            }
        ),
    ),
}

NOVEMBER_EXPECTED = {
    "2025-11-01": ExpectedReport(
        rows=frozenset({("f20001", epoch("2025-11-02 02:29:58"))}),
        hours=frozenset({epoch("2025-11-02 02:00")}),
    ),
    "2025-11-02": ExpectedReport(
        rows=frozenset(
            {
                ("f20001", epoch("2025-11-02 02:30:00")),
                ("f20001", epoch("2025-11-02 03:59:58")),
                # first local 01:30 (NDT, fold=0) -> 04:00 UTC
                ("f20001", epoch("2025-11-02 04:00:00")),
                ("f20001", epoch("2025-11-02 04:00:02")),
                ("f20002", epoch("2025-11-02 04:59:58")),
                # second local 01:30 (NST, fold=1) -> 05:00 UTC
                ("f20002", epoch("2025-11-02 05:00:00")),
                ("f20002", epoch("2025-11-02 05:00:02")),
                ("f20004", epoch("2025-11-03 03:29:58")),
            }
        ),
        hours=frozenset(
            {
                epoch("2025-11-02 02:00"),
                epoch("2025-11-02 03:00"),
                epoch("2025-11-02 04:00"),
                epoch("2025-11-02 05:00"),
                epoch("2025-11-03 03:00"),
            }
        ),
    ),
    "2025-11-03": ExpectedReport(
        rows=frozenset(
            {
                ("f20004", epoch("2025-11-03 03:30:00")),
                ("f20004", epoch("2025-11-03 03:30:02")),
                ("f20004", epoch("2025-11-03 04:00:00")),
            }
        ),
        hours=frozenset(
            {
                epoch("2025-11-03 03:00"),
                epoch("2025-11-03 04:00"),
            }
        ),
    ),
}

# Fixed inclusive day bounds of the flight subquery (floor of start, ceil of
# stop), proving an overnight flight on the second UTC day is not pruned.
FLIGHT_DAY_BOUNDS = {
    "2025-03-09": (
        epoch("2025-03-09 00:00"),
        epoch("2025-03-11 00:00"),
    ),
    "2025-11-02": (
        epoch("2025-11-02 00:00"),
        epoch("2025-11-04 00:00"),
    ),
}

# (transition day, leaked boundary row, last in-window row of same partition)
HALF_OPEN_CASES = [
    (
        "2025-03-09",
        ("f10002", epoch("2025-03-10 02:30:00")),
        ("f10002", epoch("2025-03-10 02:29:58")),
    ),
    (
        "2025-11-02",
        ("f20004", epoch("2025-11-03 03:30:00")),
        ("f20004", epoch("2025-11-03 03:29:58")),
    ),
]

# (transition day, valid row lost by wall-clock hour partitions)
WALL_CLOCK_CASES = [
    ("2025-03-09", ("f10002", epoch("2025-03-10 02:29:58"))),
    ("2025-11-02", ("f20004", epoch("2025-11-03 03:29:58"))),
]

NON_ROUTE_AIRCRAFT = {"f10003", "f20003"}


class OfflineTrino(Trino):
    def __init__(self) -> None:
        super().__init__()
        self.database = create_engine("sqlite://").execution_options(
            compiled_cache=None
        )
        self.statements: list[Any] = []

    def query(
        self,
        query: str | TextClause | Any,
        cached: bool = True,
        compress: bool = False,
    ) -> pd.DataFrame:
        self.statements.append(query)
        with self.database.connect() as connection:
            result = connection.execute(query)
            return pd.DataFrame(result.fetchall(), columns=list(result.keys()))


def _vector(icao24: str, callsign: str, stamp: str) -> dict[str, Any]:
    value = epoch(stamp)
    return dict(
        icao24=icao24,
        callsign=callsign,
        time=value,
        hour=(value // 3600) * 3600,
        baroaltitude="1000",
        lat="47",
        lon="-52",
    )


def _flight(
    icao24: str,
    callsign: str,
    route: tuple[str, str],
    first_seen: str,
    last_seen: str,
    day: str,
) -> dict[str, Any]:
    return dict(
        icao24=icao24,
        callsign=callsign,
        estdepartureairport=route[0],
        estarrivalairport=route[1],
        firstseen=epoch(first_seen),
        lastseen=epoch(last_seen),
        day=epoch(day),
    )


@pytest.fixture
def stjohns(monkeypatch: pytest.MonkeyPatch) -> Iterator[OfflineTrino]:
    def no_network(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Offline tests must not connect to OpenSky")

    monkeypatch.setattr(Trino, "connect", no_network)
    client = OfflineTrino()
    metadata = MetaData()
    tables = {}
    for schema in (FlightsData4, StateVectorsData4):
        tables[schema] = Table(
            schema.__tablename__,
            metadata,
            *(
                Column(
                    column.name,
                    Integer
                    if isinstance(column.type, (Integer, UTCTimestampInteger))
                    else Float
                    if isinstance(column.type, (Float, UTCTimestampFloat))
                    else String,
                )
                for column in schema.__table__.columns
            ),
        )
    metadata.create_all(client.database)

    flights = [
        _flight(
            "f10001",
            "DSTMARC",
            ROUTE,
            "2025-03-09 03:29:58",
            "2025-03-09 06:30:02",
            "2025-03-09 00:00",
        ),
        # Overnight flight whose UTC day is the second local day.
        _flight(
            "f10002",
            "DSTNITE",
            ROUTE,
            "2025-03-10 01:00",
            "2025-03-10 03:05",
            "2025-03-10 00:00",
        ),
        # Same two seconds around the 06:30 half hour, wrong route.
        _flight(
            "f10003",
            "DSTOTHER",
            OTHER_ROUTE,
            "2025-03-09 06:29",
            "2025-03-09 06:31",
            "2025-03-09 00:00",
        ),
        # First repeated local 01:30 (fold=0, 04:00 UTC).
        _flight(
            "f20001",
            "FALLFST",
            ROUTE,
            "2025-11-02 02:29:58",
            "2025-11-02 04:00:02",
            "2025-11-02 00:00",
        ),
        # Second repeated local 01:30 (fold=1, 05:00 UTC).
        _flight(
            "f20002",
            "FALLSND",
            ROUTE,
            "2025-11-02 04:59:58",
            "2025-11-02 05:00:02",
            "2025-11-02 00:00",
        ),
        # Same two seconds as the first 01:30 occurrence, wrong route.
        _flight(
            "f20003",
            "FALLOTHR",
            OTHER_ROUTE,
            "2025-11-02 03:59",
            "2025-11-02 04:01",
            "2025-11-02 00:00",
        ),
        # Overnight flight whose UTC day is the second local day.
        _flight(
            "f20004",
            "FALLNITE",
            ROUTE,
            "2025-11-03 01:00",
            "2025-11-03 04:05",
            "2025-11-03 00:00",
        ),
    ]

    vectors = [
        # Half-hour boundary seconds one second apart, in one partition.
        _vector("f10001", "DSTMARC", "2025-03-09 03:29:58"),
        _vector("f10001", "DSTMARC", "2025-03-09 03:30:00"),
        _vector("f10001", "DSTMARC", "2025-03-09 06:29:58"),
        _vector("f10001", "DSTMARC", "2025-03-09 06:30:00"),
        _vector("f10001", "DSTMARC", "2025-03-09 06:30:02"),
        # Crosses the local midnight at 02:30 UTC; both sides share a partition.
        _vector("f10002", "DSTNITE", "2025-03-10 02:29:58"),
        _vector("f10002", "DSTNITE", "2025-03-10 02:30:00"),
        _vector("f10002", "DSTNITE", "2025-03-10 02:30:02"),
        _vector("f10002", "DSTNITE", "2025-03-10 03:00:00"),
        # Non-route flight on the exact same two seconds as f10001.
        _vector("f10003", "DSTOTHER", "2025-03-09 06:30:00"),
        _vector("f10003", "DSTOTHER", "2025-03-09 06:30:02"),
        _vector("f20001", "FALLFST", "2025-11-02 02:29:58"),
        _vector("f20001", "FALLFST", "2025-11-02 02:30:00"),
        _vector("f20001", "FALLFST", "2025-11-02 03:59:58"),
        _vector("f20001", "FALLFST", "2025-11-02 04:00:00"),
        _vector("f20001", "FALLFST", "2025-11-02 04:00:02"),
        _vector("f20002", "FALLSND", "2025-11-02 04:59:58"),
        _vector("f20002", "FALLSND", "2025-11-02 05:00:00"),
        _vector("f20002", "FALLSND", "2025-11-02 05:00:02"),
        # Non-route flight on the exact same two seconds as the first 01:30.
        _vector("f20003", "FALLOTHR", "2025-11-02 04:00:00"),
        _vector("f20003", "FALLOTHR", "2025-11-02 04:00:02"),
        # Crosses the local midnight at 03:30 UTC; edge partition 04:00.
        _vector("f20004", "FALLNITE", "2025-11-03 03:29:58"),
        _vector("f20004", "FALLNITE", "2025-11-03 03:30:00"),
        _vector("f20004", "FALLNITE", "2025-11-03 03:30:02"),
        _vector("f20004", "FALLNITE", "2025-11-03 04:00:00"),
    ]

    with client.database.begin() as connection:
        connection.execute(tables[FlightsData4].insert(), flights)
        connection.execute(tables[StateVectorsData4].insert(), vectors)
    yield client
    client.database.dispose()


def epoch_series(values: pd.Series) -> pd.Series:
    return values.astype("int64") // 1_000_000


def row_keys(frame: pd.DataFrame) -> set[tuple[str, int]]:
    return set(zip(frame.icao24, epoch_series(frame.time)))


def hour_values(frame: pd.DataFrame) -> set[int]:
    return set(epoch_series(frame.hour))


def run_report(
    client: OfflineTrino, day: str, *, half_open: bool = True
) -> pd.DataFrame:
    start, stop, _, _ = WINDOWS[day]
    conditions = ()
    if half_open:
        # Report-side half-open upper bound, expressed in the constructed SQL.
        conditions = (
            StateVectorsData4.time < pd.Timestamp(stop, unit="s", tz="UTC"),
        )
    result = client.history(
        start,
        stop,
        *conditions,
        routes=[ROUTE],
        selected_columns=COLUMNS,
        cached=False,
    )
    if result is None:
        return pd.DataFrame(columns=list(COLUMNS))
    return result


def compiled_sql(client: OfflineTrino) -> str:
    return str(
        client.statements[-1].compile(
            dialect=TrinoDialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_fixed_constants_match_st_johns_timezone() -> None:
    # Cross-check only: every report assertion below consumes the fixed
    # literals in WINDOWS, rather than deriving bounds from a tested window.
    for day, (start, stop, hour_floor, hour_ceiling) in WINDOWS.items():
        local_date = date.fromisoformat(day)
        local_midnight = datetime(
            local_date.year, local_date.month, local_date.day, tzinfo=ST_JOHNS
        )
        next_day = local_date + timedelta(days=1)
        next_midnight = datetime(
            next_day.year, next_day.month, next_day.day, tzinfo=ST_JOHNS
        )
        assert int(pd.Timestamp(local_midnight).timestamp()) == start
        assert int(pd.Timestamp(next_midnight).timestamp()) == stop
        assert hour_floor == (start // 3600) * 3600
        assert hour_ceiling == ((stop + 3599) // 3600) * 3600

    first = datetime(2025, 11, 2, 1, 30, fold=0, tzinfo=ST_JOHNS)
    second = datetime(2025, 11, 2, 1, 30, fold=1, tzinfo=ST_JOHNS)
    assert first.astimezone(UTC) == datetime(2025, 11, 2, 4, 0, tzinfo=UTC)
    assert second.astimezone(UTC) == datetime(2025, 11, 2, 5, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (
            ("2025-03-08", "2025-03-09", "2025-03-10"),
            MARCH_EXPECTED,
        ),
        (
            ("2025-11-01", "2025-11-02", "2025-11-03"),
            NOVEMBER_EXPECTED,
        ),
    ],
)
def test_daily_reports_include_states_routes_and_hour_partitions(
    stjohns: OfflineTrino,
    days: tuple[str, str, str],
    expected: dict[str, ExpectedReport],
) -> None:
    reports = {day: run_report(stjohns, day) for day in days}

    for day in days:
        frame = reports[day]
        assert row_keys(frame) == expected[day].rows
        assert hour_values(frame) == expected[day].hours
        # Every admitted state vector is attached to the confirmed route.
        routes = set(
            zip(
                frame.estdepartureairport,
                frame.estarrivalairport,
            )
        )
        assert routes <= {ROUTE}
        assert NON_ROUTE_AIRCRAFT.isdisjoint(frame.icao24)

    # Consecutive reports attribute each in-window row exactly once.
    combined = pd.concat(reports.values())
    counts = (
        combined.assign(epoch=epoch_series(combined.time))
        .groupby(["icao24", "epoch"])
        .size()
    )
    assert (counts == 1).all()


def test_duplicated_local_clock_hours_are_both_valid(
    stjohns: OfflineTrino,
) -> None:
    frame = run_report(stjohns, "2025-11-02")
    fold_vectors = frame[
        frame.icao24.isin(["f20001", "f20002"])
        & epoch_series(frame.time).isin(
            [
                epoch("2025-11-02 04:00:00"),
                epoch("2025-11-02 05:00:00"),
            ]
        )
    ]
    identifiers = set(
        zip(
            fold_vectors.icao24,
            epoch_series(fold_vectors.time),
            epoch_series(fold_vectors.hour),
        )
    )
    assert identifiers == {
        (
            "f20001",
            epoch("2025-11-02 04:00:00"),
            epoch("2025-11-02 04:00"),
        ),
        (
            "f20002",
            epoch("2025-11-02 05:00:00"),
            epoch("2025-11-02 05:00"),
        ),
    }


@pytest.mark.parametrize("day", ["2025-03-09", "2025-11-02"])
def test_history_sql_uses_fixed_utc_window_and_partitions(
    stjohns: OfflineTrino, day: str
) -> None:
    start, stop, hour_floor, hour_ceiling = WINDOWS[day]
    run_report(stjohns, day)
    sql = compiled_sql(stjohns)

    assert f"state_vectors_data4.time >= {start}" in sql
    assert f"state_vectors_data4.time <= {stop}" in sql
    assert f"state_vectors_data4.hour >= {hour_floor}" in sql
    assert f"state_vectors_data4.hour < {hour_ceiling}" in sql
    # One single flight subquery, retaining flights of both UTC days.
    assert sql.count("FROM flights_data4") == 1
    day_floor, day_ceiling = FLIGHT_DAY_BOUNDS[day]
    assert f"flights_data4.day >= {day_floor}" in sql
    assert f"flights_data4.day <= {day_ceiling}" in sql
    assert "IN (('CYYT', 'LFPG'))" in sql


@pytest.mark.parametrize(("day", "leaked", "last_inside"), HALF_OPEN_CASES)
def test_without_half_open_end_the_boundary_row_is_leaked(
    stjohns: OfflineTrino,
    day: str,
    leaked: tuple[str, int],
    last_inside: tuple[str, int],
) -> None:
    correct = run_report(stjohns, day, half_open=True)
    closed = run_report(stjohns, day, half_open=False)

    assert leaked not in row_keys(correct)
    assert leaked in row_keys(closed)
    assert last_inside in row_keys(correct)
    assert len(closed) == len(correct) + 1

    # The leaked end point sits in the same hour partition as the final
    # in-window row, so reading the edge partition alone cannot reject it.
    leaked_hour = (leaked[1] // 3600) * 3600
    inside_hour = (last_inside[1] // 3600) * 3600
    assert leaked_hour == inside_hour
    assert leaked_hour in hour_values(closed)

    # Both queries share start, stop, hour and route filters; the only
    # difference is the report-side half-open upper bound.
    correct_sql = str(
        stjohns.statements[-2].compile(
            dialect=TrinoDialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    closed_sql = str(
        stjohns.statements[-1].compile(
            dialect=TrinoDialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    half_open_predicate = f"state_vectors_data4.time < {WINDOWS[day][1]} AND "
    assert half_open_predicate in correct_sql
    assert correct_sql.replace(half_open_predicate, "") == closed_sql
    start, stop, hour_floor, hour_ceiling = WINDOWS[day]
    for sql in (correct_sql, closed_sql):
        assert f"state_vectors_data4.time >= {start}" in sql
        assert f"state_vectors_data4.time <= {stop}" in sql
        assert f"state_vectors_data4.hour >= {hour_floor}" in sql
        assert f"state_vectors_data4.hour < {hour_ceiling}" in sql


@pytest.mark.parametrize(("day", "lost_row"), WALL_CLOCK_CASES)
def test_wall_clock_hour_partitions_miss_valid_rows(
    stjohns: OfflineTrino,
    day: str,
    lost_row: tuple[str, int],
) -> None:
    start, stop, good_floor, good_ceiling = WINDOWS[day]
    run_report(stjohns, day)
    statement = stjohns.statements[-1]
    correct_sql = str(
        statement.compile(
            dialect=TrinoDialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    wrong_floor, wrong_ceiling = WALL_CLOCK_HOURS[day]
    broken_sql = correct_sql.replace(
        f"state_vectors_data4.hour >= {good_floor}",
        f"state_vectors_data4.hour >= {wrong_floor}",
    ).replace(
        f"state_vectors_data4.hour < {good_ceiling}",
        f"state_vectors_data4.hour < {wrong_ceiling}",
    )

    # The start and stop timestamp bounds are untouched and still correct.
    assert f"state_vectors_data4.time >= {start}" in broken_sql
    assert f"state_vectors_data4.time <= {stop}" in broken_sql

    with stjohns.database.connect() as connection:
        rows = connection.execute(text(broken_sql)).fetchall()
    broken = pd.DataFrame(rows, columns=list(statement.exported_columns.keys()))
    correct = run_report(stjohns, day)

    assert lost_row not in row_keys(broken)
    assert lost_row in row_keys(correct)
    # Other valid rows are still returned, so only partitions were wrong.
    assert len(broken) > 0
    assert set(broken.icao24).issubset(set(correct.icao24))
