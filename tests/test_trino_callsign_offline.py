"""Offline callsign regressions for ``history`` and ``flightlist``.

The synthetic database stores callsigns exactly like production: eight
characters right-padded with spaces (plus SQL NULL rows). SQLite's built-in
``LIKE`` differs from Trino in two ways, so each offline engine registers a
case-sensitive ``like`` function that treats trailing pad spaces as
insignificant, matching comparisons against padded production values.
"""

from __future__ import annotations

import re
from typing import Any, Iterator

import pytest
from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    Select,
    String,
    Table,
    TextClause,
    create_engine,
    event,
    select,
    update,
)
from sqlalchemy.sql.selectable import Subquery
from sqlalchemy.sql.visitors import iterate
from trino.sqlalchemy.dialect import TrinoDialect

import pandas as pd
from pyopensky.schema import (
    FlightsData4,
    FlightsData5,
    StateVectorsData4,
    UTCTimestampFloat,
    UTCTimestampInteger,
)
from pyopensky.time import to_datetime
from pyopensky.trino import Trino

START = "2025-01-01"
STOP = "2025-01-02"
CALLSIGN_ROUTE = ("EGLL", "LFPG")
OTHER_ROUTE = ("LFPG", "EGLL")
HISTORY_COLUMNS = (
    "time",
    "icao24",
    "callsign",
    "FlightsData4.estdepartureairport",
    "FlightsData4.estarrivalairport",
)
# Distinct observation times keep (time, icao24, callsign) a unique row id.
VECTOR_TIME = {
    "AB_1": "2025-01-01 02:00:00",
    "ABX1": "2025-01-01 02:00:10",
    "abc1": "2025-01-01 02:00:20",
    None: "2025-01-01 02:00:30",
}
FLIGHT_CALLSIGNS = ["AB_1", "ABX1", "ABC", "ABC1", "abc1", None]


def _pad(value: str) -> str:
    return value.ljust(8)


def _trino_like(pattern: str | None, value: str | None) -> int | None:
    """Case-sensitive SQL ``LIKE`` with Trino trailing-pad semantics."""
    if pattern is None or value is None:
        return None
    regex = "".join(
        ".*" if char == "%" else "." if char == "_" else re.escape(char)
        for char in pattern
    )
    return int(re.fullmatch(regex, value.rstrip(" ")) is not None)


class CallsignOfflineTrino(Trino):
    def __init__(self) -> None:
        super().__init__()
        self.database = create_engine("sqlite://").execution_options(
            compiled_cache=None
        )
        event.listen(
            self.database,
            "connect",
            lambda connection, _: connection.create_function(
                "like", 2, _trino_like
            ),
        )
        self.statements: list[Select[Any]] = []
        self.tables: dict[Any, Table] = {}

    def query(
        self,
        query: str | TextClause | Select[Any],
        cached: bool = True,
        compress: bool = False,
    ) -> pd.DataFrame:
        assert isinstance(query, Select)
        self.statements.append(query)
        with self.database.connect() as connection:
            result = connection.execute(query)
            return pd.DataFrame(result.fetchall(), columns=list(result.keys()))


@pytest.fixture
def callsign_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[CallsignOfflineTrino]:
    def no_network(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Offline tests must not connect to OpenSky")

    monkeypatch.setattr(Trino, "connect", no_network)
    client = CallsignOfflineTrino()
    metadata = MetaData()
    for schema in (FlightsData4, FlightsData5, StateVectorsData4):
        client.tables[schema] = Table(
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
    flight_window = dict(firstseen=epoch + 3600, lastseen=epoch + 10800)

    def flight(callsign: str | None, route: tuple[str, str]) -> dict[str, Any]:
        return dict(
            icao24="a00001",
            callsign=None if callsign is None else _pad(callsign),
            estdepartureairport=route[0],
            estarrivalairport=route[1],
            day=epoch,
            **flight_window,
        )

    flights = [
        flight(callsign, CALLSIGN_ROUTE) for callsign in FLIGHT_CALLSIGNS
    ]
    # The same callsign flies a second, differently routed flight segment.
    flights.append(flight("AB_1", OTHER_ROUTE))
    vectors = [
        dict(
            icao24="a00001",
            callsign=None if callsign is None else _pad(callsign),
            time=int(pd.Timestamp(VECTOR_TIME[callsign], tz="UTC").timestamp()),
            hour=epoch + 7200,
            lat="50",
            lon="0",
            baroaltitude="1000",
        )
        for callsign in ("AB_1", "ABX1", "abc1", None)
    ]
    with client.database.begin() as connection:
        connection.execute(client.tables[FlightsData4].insert(), flights)
        connection.execute(client.tables[FlightsData5].insert(), flights)
        connection.execute(client.tables[StateVectorsData4].insert(), vectors)
    yield client
    client.database.dispose()


def compiled_sql(statement: Select[Any]) -> str:
    return str(
        statement.compile(
            dialect=TrinoDialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def flight_subquery(statement: Select[Any]) -> Subquery:
    seen: set[int] = set()
    for element in iterate(statement):
        if (
            isinstance(element, Subquery)
            and FlightsData4.__table__ in element.element.get_final_froms()
            and id(element) not in seen
        ):
            seen.add(id(element))
            return element
    raise AssertionError("statement has no flights_data4 subquery")


def _naive_time(value: Any) -> str:
    return str(pd.Timestamp(value).tz_localize(None))


def inner_identities(
    client: CallsignOfflineTrino, statement: Select[Any]
) -> list[tuple[str, str | None, str, str]]:
    subquery = flight_subquery(statement)
    query = select(
        subquery.c.icao24,
        subquery.c.callsign,
        subquery.c.estdepartureairport,
        subquery.c.estarrivalairport,
    )
    with client.database.connect() as connection:
        rows = connection.execute(query).fetchall()
    return sorted(
        ((row[0], row[1], row[2], row[3]) for row in rows),
        key=lambda row: (row[1] is None, row[1] or ""),
    )


def outer_identities(
    client: CallsignOfflineTrino, callsign: str | list[str] | None
) -> list[tuple[str, str, str | None]]:
    start = to_datetime(START)
    stop = to_datetime(STOP)
    statement = select(
        StateVectorsData4.time,
        StateVectorsData4.icao24,
        StateVectorsData4.callsign,
    ).where(
        StateVectorsData4.time >= start,
        StateVectorsData4.time <= stop,
        StateVectorsData4.hour >= start.floor("1h"),
        StateVectorsData4.hour < stop.ceil("1h"),
    )
    statement = client.stmt_where_str(
        statement, callsign, StateVectorsData4.callsign
    )
    with client.database.connect() as connection:
        rows = connection.execute(statement).fetchall()
    return sorted(
        ((_naive_time(row[0]), row[1], row[2]) for row in rows),
        key=lambda row: row[2] or "",
    )


def history_rows(result: pd.DataFrame) -> set[tuple[str, str, str, str]]:
    return {
        (
            _naive_time(time),
            callsign,
            departure,
            arrival,
        )
        for time, callsign, departure, arrival in zip(
            result.time,
            result.callsign,
            result.estdepartureairport,
            result.estarrivalairport,
        )
    }


def flight_rows(result: pd.DataFrame) -> set[tuple[str | None, str, str]]:
    return {
        (
            None if pd.isna(callsign) else callsign,
            departure,
            arrival,
        )
        for callsign, departure, arrival in zip(
            result.callsign, result.departure, result.arrival
        )
    }


HISTORY_EXPECTED = [
    pytest.param(
        ["AB_1", "ABC"],
        {(VECTOR_TIME["AB_1"], "AB_1", *CALLSIGN_ROUTE)},
        id="list_exact",
    ),
    pytest.param(
        "AB_%",
        {
            (VECTOR_TIME["AB_1"], "AB_1", *CALLSIGN_ROUTE),
            (VECTOR_TIME["ABX1"], "ABX1", *CALLSIGN_ROUTE),
        },
        id="single_like_prefix",
    ),
    pytest.param(
        "AB_1",
        {
            (VECTOR_TIME["AB_1"], "AB_1", *CALLSIGN_ROUTE),
            (VECTOR_TIME["ABX1"], "ABX1", *CALLSIGN_ROUTE),
        },
        id="single_like_underscore",
    ),
    pytest.param(
        ["AB_1"],
        {(VECTOR_TIME["AB_1"], "AB_1", *CALLSIGN_ROUTE)},
        id="list_single_literal_underscore",
    ),
    pytest.param(
        "ABX1",
        {(VECTOR_TIME["ABX1"], "ABX1", *CALLSIGN_ROUTE)},
        id="single_exact",
    ),
    pytest.param([], None, id="empty_list"),
    pytest.param(
        None,
        {
            (VECTOR_TIME["AB_1"], "AB_1", *CALLSIGN_ROUTE),
            (VECTOR_TIME["ABX1"], "ABX1", *CALLSIGN_ROUTE),
            (VECTOR_TIME["abc1"], "abc1", *CALLSIGN_ROUTE),
        },
        id="unset",
    ),
]


@pytest.mark.parametrize("callsign, expected", HISTORY_EXPECTED)
def test_history_callsign_filters(
    callsign_offline: CallsignOfflineTrino,
    callsign: str | list[str] | None,
    expected: set[tuple[str, str, str, str]] | None,
) -> None:
    result = callsign_offline.history(
        START,
        STOP,
        routes=[CALLSIGN_ROUTE],
        callsign=callsign,
        selected_columns=HISTORY_COLUMNS,
    )
    if expected is None:
        assert result is None
        return
    assert result is not None
    assert history_rows(result) == expected
    assert set(result.icao24) == {"a00001"}


FLIGHTLIST_EXPECTED = [
    pytest.param(
        ["AB_1", "ABC"],
        {("AB_1", *CALLSIGN_ROUTE), ("ABC", *CALLSIGN_ROUTE)},
        id="list_exact",
    ),
    pytest.param(
        "AB_%",
        {
            ("AB_1", *CALLSIGN_ROUTE),
            ("ABX1", *CALLSIGN_ROUTE),
            ("ABC", *CALLSIGN_ROUTE),
            ("ABC1", *CALLSIGN_ROUTE),
        },
        id="single_like_prefix",
    ),
    pytest.param(
        "AB_1",
        {
            ("AB_1", *CALLSIGN_ROUTE),
            ("ABX1", *CALLSIGN_ROUTE),
            ("ABC1", *CALLSIGN_ROUTE),
        },
        id="single_like_underscore",
    ),
    pytest.param(
        ["AB_1"],
        {("AB_1", *CALLSIGN_ROUTE)},
        id="list_single_literal_underscore",
    ),
    pytest.param(
        "ABX1",
        {("ABX1", *CALLSIGN_ROUTE)},
        id="single_exact",
    ),
    pytest.param([], None, id="empty_list"),
    pytest.param(
        None,
        {
            ("AB_1", *CALLSIGN_ROUTE),
            ("ABX1", *CALLSIGN_ROUTE),
            ("ABC", *CALLSIGN_ROUTE),
            ("ABC1", *CALLSIGN_ROUTE),
            ("abc1", *CALLSIGN_ROUTE),
            (None, *CALLSIGN_ROUTE),
        },
        id="unset",
    ),
]


@pytest.mark.parametrize("table", [FlightsData4, FlightsData5])
@pytest.mark.parametrize("callsign, expected", FLIGHTLIST_EXPECTED)
def test_flightlist_callsign_filters(
    callsign_offline: CallsignOfflineTrino,
    table: type[FlightsData4] | type[FlightsData5],
    callsign: str | list[str] | None,
    expected: set[tuple[str | None, str, str]] | None,
) -> None:
    result = callsign_offline.flightlist(
        START,
        STOP,
        routes=[CALLSIGN_ROUTE],
        callsign=callsign,
        Table=table,
    )
    if expected is None:
        assert result is None
        return
    assert result is not None
    assert flight_rows(result) == expected


def test_other_route_keeps_same_callsign_as_distinct_flight(
    callsign_offline: CallsignOfflineTrino,
) -> None:
    result = callsign_offline.history(
        START,
        STOP,
        routes=[OTHER_ROUTE],
        callsign=["AB_1"],
        selected_columns=HISTORY_COLUMNS,
    )
    assert result is not None
    assert history_rows(result) == {(VECTOR_TIME["AB_1"], "AB_1", *OTHER_ROUTE)}


def test_segment_vectors_depend_on_flight_identity_not_just_window(
    callsign_offline: CallsignOfflineTrino,
) -> None:
    vectors = callsign_offline.tables[StateVectorsData4]
    flights = callsign_offline.tables[FlightsData4]
    epoch = int(to_datetime(START).timestamp())
    with callsign_offline.database.connect() as connection:
        in_segment = connection.execute(
            select(vectors.c.callsign)
            .where(
                vectors.c.icao24 == "a00001",
                vectors.c.time >= epoch + 3600,
                vectors.c.time <= epoch + 10800,
            )
            .order_by(vectors.c.time)
        ).fetchall()
        flight_identities = connection.execute(
            select(flights.c.callsign).where(
                flights.c.estdepartureairport == CALLSIGN_ROUTE[0],
                flights.c.estarrivalairport == CALLSIGN_ROUTE[1],
            )
        ).fetchall()
    # Both vectors are observed by the same aircraft inside the segment.
    assert {row[0] for row in in_segment} >= {_pad("AB_1"), _pad("ABX1")}
    # The route has flight records for both callsigns, but the literal list
    # selects only the AB_1 identity, so ABX1 must not join the result.
    assert {row[0] for row in flight_identities} >= {
        _pad("AB_1"),
        _pad("ABX1"),
    }
    result = callsign_offline.history(
        START,
        STOP,
        routes=[CALLSIGN_ROUTE],
        callsign=["AB_1"],
        selected_columns=HISTORY_COLUMNS,
    )
    assert result is not None
    assert set(result.callsign) == {"AB_1"}


def test_route_query_executes_list_then_like_sql(
    callsign_offline: CallsignOfflineTrino,
) -> None:
    list_result = callsign_offline.history(
        START,
        STOP,
        routes=[CALLSIGN_ROUTE],
        callsign=["AB_1", "ABC"],
        selected_columns=HISTORY_COLUMNS,
    )
    list_sql = compiled_sql(callsign_offline.statements[-1])
    assert "IN ('AB_1    ', 'ABC     ')" in list_sql
    assert "callsign LIKE" not in list_sql
    assert list_result is not None
    assert history_rows(list_result) == {
        (VECTOR_TIME["AB_1"], "AB_1", *CALLSIGN_ROUTE)
    }

    like_result = callsign_offline.history(
        START,
        STOP,
        routes=[CALLSIGN_ROUTE],
        callsign="AB_%",
        selected_columns=HISTORY_COLUMNS,
    )
    like_sql = compiled_sql(callsign_offline.statements[-1])
    assert like_sql.count("callsign LIKE 'AB_%'") == 2
    assert "callsign IN" not in like_sql
    assert like_result is not None
    assert history_rows(like_result) == {
        (VECTOR_TIME["AB_1"], "AB_1", *CALLSIGN_ROUTE),
        (VECTOR_TIME["ABX1"], "ABX1", *CALLSIGN_ROUTE),
    }


def test_underscore_is_wildcard_for_scalar_but_literal_in_list(
    callsign_offline: CallsignOfflineTrino,
) -> None:
    scalar = callsign_offline.history(
        START,
        STOP,
        routes=[CALLSIGN_ROUTE],
        callsign="AB_1",
        selected_columns=HISTORY_COLUMNS,
    )
    scalar_sql = compiled_sql(callsign_offline.statements[-1])
    assert scalar is not None
    assert history_rows(scalar) == {
        (VECTOR_TIME["AB_1"], "AB_1", *CALLSIGN_ROUTE),
        (VECTOR_TIME["ABX1"], "ABX1", *CALLSIGN_ROUTE),
    }
    assert scalar_sql.count("callsign LIKE 'AB_1'") == 2

    listed = callsign_offline.history(
        START,
        STOP,
        routes=[CALLSIGN_ROUTE],
        callsign=["AB_1"],
        selected_columns=HISTORY_COLUMNS,
    )
    list_sql = compiled_sql(callsign_offline.statements[-1])
    assert listed is not None
    assert history_rows(listed) == {
        (VECTOR_TIME["AB_1"], "AB_1", *CALLSIGN_ROUTE)
    }
    assert "IN ('AB_1    ')" in list_sql

    exact = callsign_offline.history(
        START,
        STOP,
        routes=[CALLSIGN_ROUTE],
        callsign="ABX1",
        selected_columns=HISTORY_COLUMNS,
    )
    exact_sql = compiled_sql(callsign_offline.statements[-1])
    assert exact is not None
    assert history_rows(exact) == {
        (VECTOR_TIME["ABX1"], "ABX1", *CALLSIGN_ROUTE)
    }
    assert exact_sql.count("= 'ABX1    '") == 2


def test_like_follows_case_sensitive_production_semantics(
    callsign_offline: CallsignOfflineTrino,
) -> None:
    upper = callsign_offline.history(
        START,
        STOP,
        routes=[CALLSIGN_ROUTE],
        callsign="AB_1",
        selected_columns=HISTORY_COLUMNS,
    )
    lower = callsign_offline.history(
        START,
        STOP,
        routes=[CALLSIGN_ROUTE],
        callsign="ab_1",
        selected_columns=HISTORY_COLUMNS,
    )
    assert upper is not None and lower is not None
    assert set(upper.callsign) == {"AB_1", "ABX1"}
    assert set(lower.callsign) == {"abc1"}
    assert "LIKE 'ab_1'" in compiled_sql(callsign_offline.statements[-1])


def test_null_callsign_rows(
    callsign_offline: CallsignOfflineTrino,
) -> None:
    flights = callsign_offline.flightlist(START, STOP, routes=[CALLSIGN_ROUTE])
    assert flights is not None
    assert flights.callsign.isna().sum() == 1
    filtered_flights = callsign_offline.flightlist(
        START, STOP, routes=[CALLSIGN_ROUTE], callsign="AB%"
    )
    assert filtered_flights is not None
    assert not filtered_flights.callsign.isna().any()

    vectors = callsign_offline.history(
        START,
        STOP,
        routes=[CALLSIGN_ROUTE],
        selected_columns=HISTORY_COLUMNS,
    )
    assert vectors is not None
    assert not vectors.callsign.isna().any()
    assert len(vectors) == 3


@pytest.mark.parametrize(
    "callsign",
    [["AB_1"], "AB_%", "ABX1"],
    ids=["list", "like", "exact"],
)
def test_callsign_filter_binds_flight_subquery_and_outer_query(
    callsign_offline: CallsignOfflineTrino,
    callsign: str | list[str],
) -> None:
    result = callsign_offline.history(
        START,
        STOP,
        routes=[CALLSIGN_ROUTE],
        callsign=callsign,
        selected_columns=HISTORY_COLUMNS,
    )
    statement = callsign_offline.statements[-1]
    assert result is not None

    inner = inner_identities(callsign_offline, statement)
    assert inner, "flight subquery must keep its own callsign predicate"
    assert all((row[2], row[3]) == CALLSIGN_ROUTE for row in inner)
    assert {row[1] for row in inner}.isdisjoint({None})

    outer = outer_identities(callsign_offline, callsign)
    assert outer, "outer state-vector query must keep its callsign predicate"
    assert {row[2] for row in outer}.isdisjoint({None})


def test_inner_filter_mutation_vanishes_flight_without_changing_sql(
    callsign_offline: CallsignOfflineTrino,
) -> None:
    baseline = callsign_offline.history(
        START,
        STOP,
        routes=[CALLSIGN_ROUTE],
        callsign=["AB_1"],
        selected_columns=HISTORY_COLUMNS,
    )
    assert baseline is not None and len(baseline) == 1
    base_sql = compiled_sql(callsign_offline.statements[-1])

    flights = callsign_offline.tables[FlightsData4]
    with callsign_offline.database.begin() as connection:
        connection.execute(
            update(flights)
            .where(
                flights.c.callsign == _pad("AB_1"),
                flights.c.estdepartureairport == CALLSIGN_ROUTE[0],
            )
            .values(callsign=_pad("ZZZZ"))
        )
    try:
        mutated = callsign_offline.history(
            START,
            STOP,
            routes=[CALLSIGN_ROUTE],
            callsign=["AB_1"],
            selected_columns=HISTORY_COLUMNS,
        )
        mutated_statement = callsign_offline.statements[-1]
        assert mutated is None
        assert compiled_sql(mutated_statement) == base_sql
        assert inner_identities(callsign_offline, mutated_statement) == []
        assert outer_identities(callsign_offline, ["AB_1"]) == [
            (VECTOR_TIME["AB_1"], "a00001", "AB_1")
        ]
    finally:
        with callsign_offline.database.begin() as connection:
            connection.execute(
                update(flights)
                .where(flights.c.callsign == _pad("ZZZZ"))
                .values(callsign=_pad("AB_1"))
            )


def test_outer_filter_mutation_vanishes_vector_without_changing_sql(
    callsign_offline: CallsignOfflineTrino,
) -> None:
    baseline = callsign_offline.history(
        START,
        STOP,
        routes=[CALLSIGN_ROUTE],
        callsign=["AB_1"],
        selected_columns=HISTORY_COLUMNS,
    )
    assert baseline is not None and len(baseline) == 1
    base_sql = compiled_sql(callsign_offline.statements[-1])

    vectors = callsign_offline.tables[StateVectorsData4]
    with callsign_offline.database.begin() as connection:
        connection.execute(
            update(vectors)
            .where(vectors.c.callsign == _pad("AB_1"))
            .values(callsign=_pad("ZZZZ"))
        )
    try:
        mutated = callsign_offline.history(
            START,
            STOP,
            routes=[CALLSIGN_ROUTE],
            callsign=["AB_1"],
            selected_columns=HISTORY_COLUMNS,
        )
        mutated_statement = callsign_offline.statements[-1]
        assert mutated is None
        assert compiled_sql(mutated_statement) == base_sql
        assert inner_identities(callsign_offline, mutated_statement) == [
            ("a00001", "AB_1", *CALLSIGN_ROUTE)
        ]
        assert outer_identities(callsign_offline, ["AB_1"]) == []
    finally:
        with callsign_offline.database.begin() as connection:
            connection.execute(
                update(vectors)
                .where(vectors.c.callsign == _pad("ZZZZ"))
                .values(callsign=_pad("AB_1"))
            )
