"""Execute generated queries on synthetic data without OpenSky or its cache."""

from pathlib import Path
from typing import Any, Iterable, Iterator
from unittest.mock import patch

import pytest
from sqlalchemy import (
    Column,
    CursorResult,
    Float,
    Integer,
    MetaData,
    Select,
    String,
    Table,
    TextClause,
    create_engine,
    event,
    func,
    insert,
    or_,
    select,
    tuple_,
)
from trino.sqlalchemy.dialect import TrinoDialect

import pandas as pd
import pyopensky.trino as trino_module
from pyopensky.schema import (
    AcasData4,
    AllcallRepliesData4,
    FlightsData4,
    FlightsData5,
    IdentificationData4,
    OperationalStatusData4,
    PositionData4,
    RollcallRepliesData4,
    StateVectorsData4,
    UTCTimestampFloat,
    UTCTimestampInteger,
    VelocityData4,
)
from pyopensky.time import to_datetime
from pyopensky.trino import Trino

START = "2025-01-01"
STOP = "2025-01-02"
ROUTES = [("KJFK", "EGLL"), ("KMIA", "LFPG")]
COLUMNS = ("time", "icao24", "callsign")
RAW_TABLES = (
    RollcallRepliesData4,
    PositionData4,
    VelocityData4,
    AcasData4,
    AllcallRepliesData4,
    IdentificationData4,
    OperationalStatusData4,
)


class OfflineTrino(Trino):
    def __init__(self) -> None:
        super().__init__()
        self.database = create_engine("sqlite://").execution_options(
            compiled_cache=None
        )
        self.statements: list[Select[Any]] = []
        self.options: list[tuple[bool, bool]] = []

    def query(
        self,
        query: str | TextClause | Select[Any],
        cached: bool = True,
        compress: bool = False,
    ) -> pd.DataFrame:
        assert isinstance(query, Select)
        self.statements.append(query)
        self.options.append((cached, compress))
        with self.database.connect() as connection:
            result = connection.execute(query)
            return pd.DataFrame(result.fetchall(), columns=list(result.keys()))


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> Iterator[OfflineTrino]:
    def no_network(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Offline tests must not connect to OpenSky")

    monkeypatch.setattr(Trino, "connect", no_network)
    client = OfflineTrino()
    metadata = MetaData()
    tables = {}
    for schema in (FlightsData4, FlightsData5, StateVectorsData4, *RAW_TABLES):
        # SQLite does not support the ARRAY types in the production schema.
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
    epoch = int(to_datetime(START).timestamp())
    flights = [
        dict(
            icao24=f"a0000{index}",
            callsign=f"TEST{index}".ljust(8),
            estdepartureairport=departure,
            estarrivalairport=arrival,
            firstseen=epoch + 3600,
            lastseen=epoch + 10800,
            day=epoch,
            estarrivalairporthorizdistance=index * 100,
        )
        for index, (departure, arrival) in enumerate(
            [*ROUTES, ("KJFK", "LFPG"), ("KMIA", "EGLL"), ("EGLL", "KJFK")]
        )
    ]
    # Two samples per flight, plus one outside each flight's time interval.
    vectors = [
        dict(
            icao24=flight["icao24"],
            callsign=flight["callsign"],
            time=epoch + offset,
            hour=epoch + offset // 3600 * 3600,
            baroaltitude="1000",
            lat="50",
            lon="0",
        )
        for flight in flights
        for offset in (7200, 7210, 14400)
    ]
    messages: list[dict[str, Any]] = [
        dict(
            icao24=flight["icao24"],
            mintime=epoch + offset,
            hour=epoch + offset // 3600 * 3600,
            rawmsg="1234567890abcd",
            msgcount=2,
        )
        for flight in flights
        for offset in (7200, 7210, 14400)
    ]
    messages.append({**messages[0], "rawmsg": None})
    with client.database.begin() as connection:
        connection.execute(tables[FlightsData4].insert(), flights)
        connection.execute(tables[FlightsData5].insert(), flights)
        connection.execute(tables[StateVectorsData4].insert(), vectors)
        for raw_table in RAW_TABLES:
            connection.execute(tables[raw_table].insert(), messages)
    yield client
    client.database.dispose()


def insert_dateline_observations(
    offline: OfflineTrino, *, reverse: bool = False
) -> None:
    """Add a small route with observations on both sides of the dateline."""
    epoch = int(to_datetime(START).timestamp())
    call = "EDGE1".ljust(8)
    other = "EDGE2".ljust(8)
    metadata = MetaData()
    flights_table = Table(
        "flights_data4", metadata, autoload_with=offline.database
    )
    vectors_table = Table(
        "state_vectors_data4", metadata, autoload_with=offline.database
    )
    with offline.database.begin() as connection:
        connection.exec_driver_sql("DELETE FROM flights_data4")
        connection.exec_driver_sql("DELETE FROM state_vectors_data4")
        connection.execute(
            insert(flights_table),
            [
                dict(
                    icao24="a10000",
                    callsign=call,
                    estdepartureairport="KJFK",
                    estarrivalairport="EGLL",
                    firstseen=epoch + 3600,
                    lastseen=epoch + 10800,
                    day=epoch,
                ),
                dict(
                    icao24="a20000",
                    callsign=other,
                    estdepartureairport="KJFK",
                    estarrivalairport="EGLL",
                    firstseen=epoch + 3600,
                    lastseen=epoch + 10800,
                    day=epoch,
                ),
                dict(
                    icao24="a30000",
                    callsign="EDGE3".ljust(8),
                    estdepartureairport="EGLL",
                    estarrivalairport="KJFK",
                    firstseen=epoch + 3600,
                    lastseen=epoch + 10800,
                    day=epoch,
                ),
            ],
        )
        observations = [
            # Earliest sample: the same observation at both sides of 180.
            ("a10000", call, 7300, "10", "-180"),
            ("a10000", call, 7300, "10", "180"),
            # Same aircraft and time, different latitude.
            ("a10000", call, 7300, "20", "175"),
            # Another aircraft at the same time and latitude.
            ("a20000", other, 7300, "10", "-175"),
            # Outside the longitude window.
            ("a10000", call, 7290, "10", "160"),
            # Outside the latitude window.
            ("a10000", call, 7280, "40", "175"),
            # Outside the time window.
            ("a10000", call, 90000, "10", "175"),
            # Route mismatch: same callsign on another route.
            ("a30000", "EDGE3".ljust(8), 7300, "10", "175"),
        ]
        connection.execute(
            insert(vectors_table),
            [
                dict(
                    icao24=icao24,
                    callsign=callsign,
                    time=epoch + offset,
                    hour=epoch + offset // 3600 * 3600,
                    baroaltitude="1000",
                    lat=lat,
                    lon=lon,
                )
                for icao24, callsign, offset, lat, lon in (
                    reversed(observations) if reverse else observations
                )
            ],
        )


def test_antimeridian_selects_both_sides_and_normalizes(
    offline: OfflineTrino,
) -> None:
    insert_dateline_observations(offline)
    result = offline.history(
        START,
        STOP,
        routes=ROUTES,
        bounds=(170, -20, -170, 30),
        antimeridian=True,
        selected_columns=("time", "icao24", "callsign", "lat", "lon"),
        cached=False,
    )
    assert result is not None
    # -180/+180 is one observation, represented by +180. Other observations
    # and the other aircraft stay. Excluded longitudes, latitudes, times and
    # the other route are absent.
    assert list(result.lon) == [180.0, 175.0, -175.0]
    assert list(result.lat) == [10.0, 20.0, 10.0]
    assert list(result.callsign) == ["EDGE1", "EDGE1", "EDGE2"]
    assert result.time.is_monotonic_increasing
    assert_one_join(offline)
    sql = str(
        offline.statements[-1].compile(
            dialect=TrinoDialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert (
        "state_vectors_data4.lon >= 170 OR state_vectors_data4.lon <= -170"
    ) in sql
    assert "row_number() OVER" in sql
    assert "LIMIT" not in sql


@pytest.mark.parametrize("limit", [1, 2, 3, 5])
def test_antimeridian_limit_ignores_column_projection(
    offline: OfflineTrino, limit: int
) -> None:
    insert_dateline_observations(offline)
    result = offline.history(
        START,
        STOP,
        routes=ROUTES,
        bounds=(170, -20, -170, 30),
        antimeridian=True,
        selected_columns=("time", "callsign"),
        limit=limit,
        cached=False,
    )
    full = offline.history(
        START,
        STOP,
        routes=ROUTES,
        bounds=(170, -20, -170, 30),
        antimeridian=True,
        selected_columns=("time", "icao24", "callsign", "lat", "lon"),
        cached=False,
    )
    assert result is not None and full is not None
    assert list(result.columns) == ["time", "callsign"]
    assert len(result) == min(limit, len(full))
    pd.testing.assert_frame_equal(
        result[["time", "callsign"]].reset_index(drop=True),
        full[["time", "callsign"]].head(limit).reset_index(drop=True),
    )


def test_antimeridian_icao24_and_time_filters(offline: OfflineTrino) -> None:
    insert_dateline_observations(offline)
    result = offline.history(
        START,
        STOP,
        routes=ROUTES,
        bounds=(170, -20, -170, 30),
        antimeridian=True,
        icao24="A20000",
        selected_columns=("time", "icao24", "lat", "lon"),
        cached=False,
    )
    assert result is not None
    assert list(result.icao24) == ["a20000"]
    assert list(result.lon) == [-175.0]

    result = offline.history(
        "2025-01-01 02:01",
        "2025-01-01 02:01:45",
        routes=ROUTES,
        bounds=(170, -20, -170, 30),
        antimeridian=True,
        selected_columns=("time", "icao24", "lat", "lon"),
        cached=False,
    )
    assert result is not None
    assert list(result.lon) == [180.0, 175.0, -175.0]
    assert set(result.time) == {pd.Timestamp("2025-01-01 02:01:40", tz="UTC")}


def test_antimeridian_rejects_rectangular_bounds(offline: OfflineTrino) -> None:
    with pytest.raises(ValueError, match="west > east"):
        offline.history(
            START,
            STOP,
            bounds=(1, 2, 3, 4),
            antimeridian=True,
        )
    with pytest.raises(ValueError, match="requires bounds"):
        offline.history(START, STOP, antimeridian=True)


def test_antimeridian_is_independent_of_insertion_order(
    offline: OfflineTrino,
) -> None:
    insert_dateline_observations(offline)
    arguments = dict(
        routes=ROUTES,
        bounds=(170, -20, -170, 30),
        antimeridian=True,
        selected_columns=("time", "icao24", "callsign", "lat", "lon"),
        limit=2,
        cached=False,
    )
    before = offline.history(START, STOP, **arguments)
    insert_dateline_observations(offline, reverse=True)
    after = offline.history(START, STOP, **arguments)
    assert before is not None and after is not None
    pd.testing.assert_frame_equal(before, after)
    assert list(after.lon) == [180.0, 175.0]


def test_rectangular_history_unchanged_by_antimeridian_option(
    offline: OfflineTrino,
) -> None:
    insert_dateline_observations(offline)
    # Without the explicit option, west > east keeps the original rectangle
    # semantics and therefore matches no longitude.
    expected = offline.history(
        START,
        STOP,
        bounds=(170, -20, -170, 30),
        selected_columns=("time", "lat", "lon"),
    )
    assert expected is None
    result = offline.history(
        START,
        STOP,
        icao24="A10000",
        bounds=(170, -20, 180, 30),
        selected_columns=("time", "lat", "lon"),
    )
    assert result is not None
    assert list(result.columns) == ["time", "lat", "lon"]
    assert set(zip(result.lat, result.lon)) == {
        (10.0, 180.0),
        (20.0, 175.0),
    }
    sql = str(offline.statements[-1])
    assert "row_number()" not in sql
    assert "ORDER BY" not in sql


def assert_one_join(client: OfflineTrino) -> None:
    statement = client.statements[-1]
    assert len(statement.get_final_froms()) == 1
    sql = str(statement.compile(dialect=TrinoDialect()))
    assert sql.count("FROM flights_data4") == 1
    assert "flights_data4.day >=" in sql
    assert "state_vectors_data4.hour <" in sql


def test_history_exact_routes(offline: OfflineTrino) -> None:
    result = offline.history(
        START, STOP, routes=ROUTES, selected_columns=COLUMNS, cached=False
    )
    assert result is not None
    assert len(result) == 4
    assert set(result.icao24) == {"a00000", "a00001"}
    assert offline.options == [(False, False)]
    assert_one_join(offline)
    sql = str(
        offline.statements[-1].compile(
            dialect=TrinoDialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "IN (('KJFK', 'EGLL'), ('KMIA', 'LFPG'))" in sql


def test_history_routes_match_individual_queries(offline: OfflineTrino) -> None:
    separate = [
        offline.history(
            START,
            STOP,
            departure_airport=departure,
            arrival_airport=arrival,
            selected_columns=COLUMNS,
        )
        for departure, arrival in ROUTES
    ]
    batched = offline.history(
        START, STOP, routes=ROUTES, selected_columns=COLUMNS
    )
    assert batched is not None
    expected = pd.concat([df for df in separate if df is not None])
    pd.testing.assert_frame_equal(
        batched.sort_values(list(COLUMNS)).reset_index(drop=True),
        expected.sort_values(list(COLUMNS)).reset_index(drop=True),
    )


@pytest.mark.parametrize("routes", [[], [("XXXX", "YYYY")]])
def test_no_matching_routes(
    offline: OfflineTrino, routes: list[tuple[str, str]]
) -> None:
    assert offline.history(START, STOP, routes=routes) is None
    assert offline.flightlist(START, STOP, routes=routes) is None


def test_duplicate_routes_do_not_duplicate_rows(offline: OfflineTrino) -> None:
    result = offline.history(START, STOP, routes=[ROUTES[0], ROUTES[0]])
    assert result is not None
    assert len(result) == 2


def test_routes_are_exact_not_wildcards(offline: OfflineTrino) -> None:
    assert offline.history(START, STOP, routes=[("K%", "EGLL")]) is None


def test_airport_lists_select_all_combinations(offline: OfflineTrino) -> None:
    result = offline.history(
        START,
        STOP,
        departure_airport=["KJFK", "KMIA"],
        arrival_airport=["EGLL", "LFPG"],
    )
    assert result is not None
    assert set(result.icao24) == {"a00000", "a00001", "a00002", "a00003"}


def test_routes_intersect_other_filters(offline: OfflineTrino) -> None:
    result = offline.history(
        START,
        STOP,
        StateVectorsData4.time < "2025-01-01 02:00:05",
        routes=ROUTES,
        airport=["KJFK"],
        icao24="A00000",
        callsign="TEST0",
        bounds=(-1, 49, 1, 51),
        selected_columns=COLUMNS,
        limit=1,
    )
    assert result is not None
    assert list(result.icao24) == ["a00000"]
    assert_one_join(offline)


@pytest.mark.parametrize("airport", [None, "KJFK"])
def test_flight_conditions_use_existing_alias(
    offline: OfflineTrino, airport: str | None
) -> None:
    result = offline.history(
        START,
        STOP,
        tuple_(
            FlightsData4.estdepartureairport, FlightsData4.estarrivalairport
        ).in_([ROUTES[0]]),
        airport=airport,
        selected_columns=COLUMNS,
    )
    assert result is not None
    assert list(result.icao24) == ["a00000", "a00000"]
    assert_one_join(offline)


def test_unprojected_flight_columns_and_nested_conditions(
    offline: OfflineTrino,
) -> None:
    result = offline.history(
        START,
        STOP,
        or_(
            func.coalesce(FlightsData4.estarrivalairporthorizdistance, 0)
            == 100,
            (FlightsData4.day == START)
            & (StateVectorsData4.icao24 == "a00000"),
        ),
        routes=ROUTES,
        selected_columns=COLUMNS,
    )
    assert result is not None
    assert set(result.icao24) == {"a00000", "a00001"}
    assert len(result) == 4
    assert_one_join(offline)


@pytest.mark.parametrize("routes", [None, ROUTES])
def test_flight_schema_attributes_in_selected_columns(
    offline: OfflineTrino, routes: list[tuple[str, str]] | None
) -> None:
    result = offline.history(
        START,
        STOP,
        routes=routes,
        selected_columns=(
            StateVectorsData4.icao24,
            FlightsData4.estdepartureairport,
            FlightsData4.day,
            "FlightsData4.estarrivalairporthorizdistance",
        ),
    )
    assert result is not None
    assert len(result) == (10 if routes is None else 4)
    expected = {"KJFK", "KMIA", "EGLL"} if routes is None else {"KJFK", "KMIA"}
    assert set(result.estdepartureairport) == expected
    assert set(result.estarrivalairporthorizdistance) == (
        {0, 100, 200, 300, 400} if routes is None else {0, 100}
    )
    assert_one_join(offline)


@pytest.mark.parametrize("column", ["firstseen", "FlightsData4.firstseen"])
def test_selecting_flight_names_enables_join(
    offline: OfflineTrino, column: str
) -> None:
    result = offline.history(START, STOP, selected_columns=("icao24", column))
    assert result is not None
    assert len(result) == 10
    assert "firstseen" in result.columns
    assert_one_join(offline)


def test_plain_history_does_not_join_flights(offline: OfflineTrino) -> None:
    result = offline.history(START, STOP, selected_columns=COLUMNS)
    assert result is not None
    assert len(result) == 15
    assert "flights_data4" not in str(offline.statements[-1])


def test_routes_keep_requested_time_window(offline: OfflineTrino) -> None:
    result = offline.history(
        "2025-01-01 02:00:05",
        "2025-01-01 02:00:15",
        routes=ROUTES,
        time_buffer="12h",
        selected_columns=COLUMNS,
    )
    assert result is not None
    assert len(result) == 2
    assert all(result.time == pd.Timestamp("2025-01-01 02:00:10", tz="UTC"))


@pytest.mark.parametrize("table", [FlightsData4, FlightsData5])
def test_flightlist_routes(
    offline: OfflineTrino, table: type[FlightsData4] | type[FlightsData5]
) -> None:
    result = offline.flightlist(START, STOP, routes=ROUTES, Table=table)
    assert result is not None
    assert set(zip(result.departure, result.arrival)) == set(ROUTES)
    assert len(result) == 2
    filtered = offline.flightlist(
        START, STOP, routes=ROUTES, departure_airport="KJFK", Table=table
    )
    assert filtered is not None
    assert list(filtered.icao24) == ["a00000"]


@pytest.mark.parametrize("method", ["history", "flightlist"])
def test_existing_airport_conflicts(offline: OfflineTrino, method: str) -> None:
    with pytest.raises(RuntimeError, match="airport may not be set"):
        getattr(offline, method)(
            START, STOP, routes=ROUTES, airport="KJFK", arrival_airport="EGLL"
        )


@pytest.mark.parametrize("table", RAW_TABLES)
def test_rawdata_exact_routes(offline: OfflineTrino, table: Any) -> None:
    result = offline.rawdata(
        START, STOP, routes=ROUTES, Table=table, cached=False
    )
    assert result is not None
    assert len(result) == 4
    assert set(result.icao24) == {"a00000", "a00001"}
    assert result.rawmsg.notna().all()
    assert offline.options == [(False, False)]
    statement = offline.statements[-1]
    assert len(statement.get_final_froms()) == 1
    sql = str(statement.compile(dialect=TrinoDialect()))
    assert sql.count("FROM flights_data4") == 1
    assert "flights_data4.day >=" in sql
    assert f"{table.__tablename__}.hour <" in sql


def test_rawdata_routes_match_individual_queries(offline: OfflineTrino) -> None:
    separate = [
        offline.rawdata(
            START, STOP, departure_airport=departure, arrival_airport=arrival
        )
        for departure, arrival in ROUTES
    ]
    batched = offline.rawdata(START, STOP, routes=ROUTES)
    assert batched is not None
    expected = pd.concat([df for df in separate if df is not None])
    keys = ["icao24", "mintime"]
    pd.testing.assert_frame_equal(
        batched.sort_values(keys).reset_index(drop=True),
        expected.sort_values(keys).reset_index(drop=True),
    )


@pytest.mark.parametrize("routes", [[], [("XXXX", "YYYY")], [("K%", "EGLL")]])
def test_rawdata_no_matching_routes(
    offline: OfflineTrino, routes: list[tuple[str, str]]
) -> None:
    assert offline.rawdata(START, STOP, routes=routes) is None


def test_rawdata_duplicate_routes(offline: OfflineTrino) -> None:
    result = offline.rawdata(START, STOP, routes=[ROUTES[0], ROUTES[0]])
    assert result is not None
    assert len(result) == 2


def test_rawdata_routes_intersect_filters(offline: OfflineTrino) -> None:
    result = offline.rawdata(
        START,
        STOP,
        RollcallRepliesData4.mintime < "2025-01-01 02:00:05",
        routes=ROUTES,
        departure_airport=["KJFK"],
        icao24="A00000",
        limit=1,
    )
    assert result is not None
    assert list(result.icao24) == ["a00000"]


@pytest.mark.parametrize("airport", [None, "KJFK"])
def test_rawdata_flight_conditions_use_alias(
    offline: OfflineTrino, airport: str | None
) -> None:
    result = offline.rawdata(
        START,
        STOP,
        or_(
            func.coalesce(FlightsData4.estarrivalairporthorizdistance, 0)
            == 100,
            (FlightsData4.day == START)
            & (RollcallRepliesData4.icao24 == "a00000"),
        ),
        airport=airport,
    )
    assert result is not None
    expected = {"a00000", "a00001"} if airport is None else {"a00000"}
    assert set(result.icao24) == expected
    assert len(result) == 2 * len(expected)
    assert len(offline.statements[-1].get_final_froms()) == 1


@pytest.mark.parametrize("routes", [None, ROUTES])
def test_rawdata_flight_extra_columns(
    offline: OfflineTrino, routes: list[tuple[str, str]] | None
) -> None:
    result = offline.rawdata(
        START,
        STOP,
        routes=routes,
        extra_columns=(
            FlightsData4.estdepartureairport,
            FlightsData4.day,
            RollcallRepliesData4.msgcount,
        ),
    )
    assert result is not None
    assert len(result) == (10 if routes is None else 4)
    assert result.columns.is_unique
    assert result.msgcount.eq(2).all()
    assert "estdepartureairport" in result.columns
    assert "day" in result.columns
    assert len(offline.statements[-1].get_final_froms()) == 1


def test_rawdata_plain_query_does_not_join_flights(
    offline: OfflineTrino,
) -> None:
    result = offline.rawdata(
        START, STOP, extra_columns=(RollcallRepliesData4.msgcount,)
    )
    assert result is not None
    assert len(result) == 15
    assert list(result.columns) == ["mintime", "rawmsg", "icao24", "msgcount"]
    assert "flights_data4" not in str(offline.statements[-1])


@pytest.mark.parametrize(
    "selection", [{"callsign": "TEST0"}, {"bounds": (-1, 49, 1, 51)}]
)
def test_rawdata_existing_state_vector_selection(
    offline: OfflineTrino, selection: dict[str, Any]
) -> None:
    result = offline.rawdata(START, STOP, **selection)
    assert result is not None
    assert len(result) == (3 if "callsign" in selection else 15)
    sql = str(offline.statements[-1])
    assert "state_vectors_data4" in sql
    assert "flights_data4" not in sql


@pytest.mark.parametrize(
    "selection", [{"callsign": "TEST0"}, {"bounds": (-1, 49, 1, 51)}]
)
def test_rawdata_routes_keep_selection_restrictions(
    offline: OfflineTrino, selection: dict[str, Any]
) -> None:
    with pytest.raises(RuntimeError, match="Filter on only one among"):
        offline.rawdata(START, STOP, routes=ROUTES, **selection)
    assert not offline.statements


def test_session_properties_are_copied_and_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    properties = {"join_distribution_type": "BROADCAST"}
    client = Trino(session_properties=properties)
    properties["join_distribution_type"] = "PARTITIONED"
    monkeypatch.setattr(client, "token", lambda: None)
    with patch.object(trino_module, "create_engine") as factory:
        client.engine()
        forwarded = factory.call_args.kwargs["connect_args"][
            "session_properties"
        ]
        assert forwarded == {"join_distribution_type": "BROADCAST"}
        forwarded["join_distribution_type"] = "AUTOMATIC"
        client.engine()
        assert factory.call_args.kwargs["connect_args"][
            "session_properties"
        ] == {"join_distribution_type": "BROADCAST"}


def test_default_session_properties_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = Trino(), Trino()
    assert first.session_properties == second.session_properties == {}
    monkeypatch.setattr(second, "token", lambda: None)
    with patch.object(trino_module, "create_engine") as factory:
        second.engine()
        assert (
            factory.call_args.kwargs["connect_args"]["session_properties"] == {}
        )
    first.session_properties["join_distribution_type"] = "BROADCAST"
    assert second.session_properties == {}


def test_session_properties_separate_cached_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(trino_module, "cache_path", tmp_path)

    def process_result(
        self: Trino, result: CursorResult[Any], batch_size: int = 50_000
    ) -> Iterable[pd.DataFrame]:
        yield pd.DataFrame(result.fetchall(), columns=list(result.keys()))

    monkeypatch.setattr(Trino, "process_result", process_result)
    configurations = [
        {},
        {"join_distribution_type": "BROADCAST"},
        {"join_distribution_type": "BROADCAST", "query_max_run_time": "1m"},
        {"query_max_run_time": "1m", "join_distribution_type": "BROADCAST"},
    ]
    connections = []
    for configuration in configurations:
        client = Trino(session_properties=configuration)
        engine = create_engine("sqlite://")

        @event.listens_for(engine, "connect")
        def register(connection: Any, record: Any) -> None:
            connections.append(connection)
            connection.create_function(
                "session_property",
                1,
                lambda key: client.session_properties.get(key, "AUTOMATIC"),
            )

        monkeypatch.setattr(client, "connect", engine.connect)
        try:
            result = client.query(
                "SELECT session_property('join_distribution_type') AS value"
            )
            assert result.value.iloc[0] == configuration.get(
                "join_distribution_type", "AUTOMATIC"
            )
        finally:
            engine.dispose()
    # Reordered properties reuse the third query's result, without connecting.
    assert len(connections) == 3
    assert len(list(tmp_path.glob("*.parquet"))) == 3


@pytest.mark.parametrize("method", ["history", "rawdata"])
@pytest.mark.parametrize("routes", [None, [ROUTES[0]]])
@pytest.mark.parametrize("outer_flight_filter", [False, True])
def test_independent_flight_subquery_is_preserved(
    offline: OfflineTrino,
    method: str,
    routes: list[tuple[str, str]] | None,
    outer_flight_filter: bool,
) -> None:
    # Select an aircraft by a previous flight outside the outer time window.
    with offline.database.begin() as connection:
        connection.execute(
            insert(FlightsData4).values(
                icao24="a00000",
                callsign="PAST",
                firstseen="2024-01-01 01:00",
                lastseen="2024-01-01 03:00",
                day="2024-01-01",
                estdepartureairport="LFPG",
                estarrivalairport="EGLL",
            )
        )
    table = StateVectorsData4 if method == "history" else RollcallRepliesData4
    condition = table.icao24.in_(
        select(FlightsData4.icao24).where(
            FlightsData4.day == "2024-01-01",
            FlightsData4.estdepartureairport == "LFPG",
        )
    )
    if outer_flight_filter:
        condition &= FlightsData4.day == START
    result = getattr(offline, method)(START, STOP, condition, routes=routes)
    assert result is not None
    assert set(result.icao24) == {"a00000"}
    # Without an explicit flight selection, keep all three samples/messages.
    has_join = routes is not None or outer_flight_filter
    assert len(result) == (2 if has_join else 3)
    statement = offline.statements[-1]
    assert len(statement.get_final_froms()) == 1
    sql = str(statement.compile(dialect=TrinoDialect()))
    assert sql.count("FROM flights_data4") == (2 if has_join else 1)
