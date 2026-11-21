from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import pytest

import narwhals as nw
from narwhals.exceptions import InvalidOperationError
from tests.utils import assert_equal_data

if TYPE_CHECKING:
    from collections.abc import Iterator

    from narwhals.typing import IntoDType

PLAN_SCHEMA = nw.Schema(
    {
        "left_key": nw.Int64,
        "narrow_null": nw.Int8,
        "bad_text": nw.String,
        "left_payload": nw.Int64,
        "probe_seed": nw.Int64,
        "probe_normal": nw.Int64,
        "probe_bad": nw.Int64,
        "right_key": nw.Int64,
        "right_payload": nw.Int32,
    }
)

NORMAL_SCHEMA = nw.Schema(
    {
        "key": nw.Int64,
        "value": nw.Int16,
        "narrow_null": nw.Int16,
        "right_payload": nw.Int32,
        "left_key": nw.Int64,
        "right_key": nw.Int64,
        "probe": nw.Int64,
    }
)

WIDE_SCHEMA = nw.Schema(
    {
        "key": nw.Int64,
        "value": nw.Int32,
        "narrow_null": nw.Int32,
        "right_payload": nw.Int32,
        "left_key": nw.Int64,
        "right_key": nw.Int64,
        "probe": nw.Int64,
    }
)

BAD_SCHEMA = nw.Schema(
    {
        "key": nw.Int64,
        "value": nw.Int8,
        "narrow_null": nw.Int8,
        "right_payload": nw.Int32,
        "left_key": nw.Int64,
        "right_key": nw.Int64,
        "probe": nw.Int64,
    }
)

# Keys 1 and 3 are left-only, key 2 matched (with null payloads on both sides) and
# key 4 right-only. `narrow_null` and every joined payload are null by construction.
NORMAL_DATA = {
    "key": [1, 2, 3, 4],
    "value": [None, None, None, None],
    "narrow_null": [None, None, None, None],
    "right_payload": [None, None, None, None],
    "left_key": [1, 2, 3, None],
    "right_key": [None, 2, None, 4],
    "probe": [100, 200, 300, None],
}

NORMAL_COUNTS = {"normal": {100: 1, 200: 1, 300: 1}}
BAD_COUNTS = {"bad": {1100: 1, 1200: 1, 1300: 1}}


@dataclass
class Consumption:
    """Count of business values actually evaluated on each branch of a backend."""

    _counts: dict[str, Counter[int]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, branch: str, value: int) -> None:
        with self._lock:
            self._counts.setdefault(branch, Counter())[value] += 1

    def snapshot(self) -> dict[str, dict[int, int]]:
        with self._lock:
            return {branch: dict(counts) for branch, counts in self._counts.items()}


class LazyBackend(Protocol):
    consumption: Consumption

    @property
    def conversion_error(
        self,
    ) -> type[BaseException] | tuple[type[BaseException], ...]: ...

    def frames(self) -> tuple[nw.LazyFrame[Any], nw.LazyFrame[Any]]: ...

    def close(self) -> None: ...


class PolarsBackend:
    conversion_error = (InvalidOperationError,)

    def __init__(self, pl: Any) -> None:
        self._pl = pl
        self.consumption = Consumption()

    def _probe_normal(self, series: Any) -> Any:
        for value in series.to_list():
            self.consumption.record("normal", int(value))
        return series

    def _probe_bad(self, series: Any) -> Any:
        for value in series.to_list():
            self.consumption.record("bad", int(value))
        return series

    def frames(self) -> tuple[nw.LazyFrame[Any], nw.LazyFrame[Any]]:
        pl = self._pl
        left = pl.LazyFrame(
            {
                "left_key": [1, 2, 3],
                "narrow_null": [None, None, None],
                "bad_text": ["1", "oops", "3"],
                "left_payload": [10, 20, 30],
                "probe_seed": [100, 200, 300],
            },
            schema={
                "left_key": pl.Int64,
                "narrow_null": pl.Int8,
                "bad_text": pl.String,
                "left_payload": pl.Int64,
                "probe_seed": pl.Int64,
            },
        ).with_columns(
            pl.col("probe_seed")
            .map_batches(self._probe_normal, return_dtype=pl.Int64)
            .alias("probe_normal"),
            (pl.col("probe_seed") + 1000)
            .map_batches(self._probe_bad, return_dtype=pl.Int64)
            .alias("probe_bad"),
        )
        right = pl.LazyFrame(
            {"right_key": [2, 4], "right_payload": [None, None]},
            schema={"right_key": pl.Int64, "right_payload": pl.Int32},
        )
        return nw.from_native(left), nw.from_native(right)

    def close(self) -> None:
        pass


class DuckDBBackend:
    def __init__(self, duckdb: Any, pa: Any) -> None:
        self._duckdb = duckdb
        self._pa = pa
        self._connection = duckdb.connect()
        self.consumption = Consumption()
        duckdb_types = getattr(duckdb, "sqltypes", None) or duckdb.typing
        self._connection.create_function(
            "probe_normal", self._probe_normal, [duckdb_types.BIGINT], duckdb_types.BIGINT
        )
        self._connection.create_function(
            "probe_bad", self._probe_bad, [duckdb_types.BIGINT], duckdb_types.BIGINT
        )

    @property
    def conversion_error(self) -> type[BaseException] | tuple[type[BaseException], ...]:
        return (getattr(self._duckdb, "Error", Exception),)

    def _probe_normal(self, value: int | None) -> int | None:
        if value is not None:
            self.consumption.record("normal", int(value))
        return value

    def _probe_bad(self, value: int | None) -> int | None:
        if value is not None:
            self.consumption.record("bad", int(value))
        return value

    def frames(self) -> tuple[nw.LazyFrame[Any], nw.LazyFrame[Any]]:
        pa = self._pa
        self._connection.register(
            "lazy_report_left",
            pa.table(
                {
                    "left_key": pa.array([1, 2, 3], pa.int64()),
                    "narrow_null": pa.array([None, None, None], pa.int8()),
                    "bad_text": pa.array(["1", "oops", "3"], pa.string()),
                    "left_payload": pa.array([10, 20, 30], pa.int64()),
                    "probe_seed": pa.array([100, 200, 300], pa.int64()),
                }
            ),
        )
        self._connection.register(
            "lazy_report_right",
            pa.table(
                {
                    "right_key": pa.array([2, 4], pa.int64()),
                    "right_payload": pa.array([None, None], pa.int32()),
                }
            ),
        )
        left = self._connection.sql(
            """
            SELECT left_key, narrow_null, bad_text, left_payload, probe_seed,
                   probe_normal(probe_seed) AS probe_normal,
                   probe_bad(probe_seed + 1000) AS probe_bad
            FROM lazy_report_left
            """
        )
        right = self._connection.sql(
            "SELECT right_key, right_payload FROM lazy_report_right"
        )
        assert left is not None
        assert right is not None
        return nw.from_native(left), nw.from_native(right)

    def close(self) -> None:
        self._connection.close()


@pytest.fixture(params=["polars", "duckdb"])
def lazy_backend(request: pytest.FixtureRequest) -> Iterator[LazyBackend]:
    if request.param == "polars":
        pl = pytest.importorskip("polars")
        backend: LazyBackend = PolarsBackend(pl)
    else:
        duckdb = pytest.importorskip("duckdb")
        pa = pytest.importorskip("pyarrow")
        backend = DuckDBBackend(duckdb, pa)
    try:
        yield backend
    finally:
        backend.close()


@dataclass(frozen=True)
class Report:
    plan: nw.LazyFrame[Any]
    normal: nw.LazyFrame[Any]
    wide: nw.LazyFrame[Any]
    bad: nw.LazyFrame[Any]


def _key() -> nw.Expr:
    return nw.coalesce(nw.col("left_key"), nw.col("right_key")).alias("key")


def _normal_branch(plan: nw.LazyFrame[Any], dtype: IntoDType) -> nw.LazyFrame[Any]:
    value = (
        nw.when(nw.col("right_key").is_null())
        .then(nw.col("narrow_null").cast(dtype))
        .otherwise(nw.col("right_payload").cast(dtype))
        .alias("value")
    )
    return plan.select(
        _key(),
        value,
        nw.col("narrow_null").cast(dtype).alias("narrow_null"),
        nw.col("right_payload").alias("right_payload"),
        nw.col("left_key").alias("left_key"),
        nw.col("right_key").alias("right_key"),
        nw.col("probe_normal").alias("probe"),
    )


def _bad_branch(plan: nw.LazyFrame[Any]) -> nw.LazyFrame[Any]:
    return plan.select(
        _key(),
        nw.col("bad_text").cast(nw.Int8).alias("value"),
        nw.col("narrow_null").alias("narrow_null"),
        nw.col("right_payload").alias("right_payload"),
        nw.col("left_key").alias("left_key"),
        nw.col("right_key").alias("right_key"),
        nw.col("probe_bad").alias("probe"),
    )


def build_report(backend: LazyBackend) -> Report:
    left, right = backend.frames()
    plan = left.join(
        right, left_on="left_key", right_on="right_key", how="full", suffix="_right"
    )
    return Report(
        plan=plan,
        normal=_normal_branch(plan, nw.Int16),
        wide=_normal_branch(plan, nw.Int32),
        bad=_bad_branch(plan),
    )


def assert_schema(frame: nw.LazyFrame[Any], expected: nw.Schema) -> None:
    assert frame.collect_schema() == expected


def assert_normal_result(result: nw.DataFrame[Any], expected: nw.Schema) -> None:
    assert result.collect_schema() == expected
    assert_equal_data(result.sort("key"), NORMAL_DATA)


def test_report_schema_queries_are_lazy_and_branch_local(
    lazy_backend: LazyBackend,
) -> None:
    report = build_report(lazy_backend)

    # Interleave the original plan and both branches, in both orders. Branch
    # schemas must not inherit the plan's or each other's declarations.
    assert_schema(report.plan, PLAN_SCHEMA)
    assert_schema(report.bad, BAD_SCHEMA)
    assert_schema(report.normal, NORMAL_SCHEMA)
    assert_schema(report.plan, PLAN_SCHEMA)
    assert_schema(report.normal, NORMAL_SCHEMA)
    assert_schema(report.bad, BAD_SCHEMA)
    assert_schema(report.wide, WIDE_SCHEMA)
    assert_schema(report.normal, NORMAL_SCHEMA)
    assert_schema(report.wide, WIDE_SCHEMA)

    # The not-yet-collected bad text must not be evaluated while resolving any
    # of those schemas, and no business batch may have been consumed either.
    assert lazy_backend.consumption.snapshot() == {}


def test_normal_branch_collects_hand_written_table(lazy_backend: LazyBackend) -> None:
    report = build_report(lazy_backend)

    assert_schema(report.normal, NORMAL_SCHEMA)
    assert lazy_backend.consumption.snapshot() == {}

    result = report.normal.collect()
    assert_normal_result(result, NORMAL_SCHEMA)
    assert lazy_backend.consumption.snapshot() == NORMAL_COUNTS


def test_bad_branch_fails_only_on_collect_and_does_not_pollute_normal(
    lazy_backend: LazyBackend,
) -> None:
    report = build_report(lazy_backend)

    assert_schema(report.bad, BAD_SCHEMA)
    assert_schema(report.normal, NORMAL_SCHEMA)
    assert lazy_backend.consumption.snapshot() == {}

    assert_normal_result(report.normal.collect(), NORMAL_SCHEMA)
    assert lazy_backend.consumption.snapshot() == NORMAL_COUNTS

    # The declaration is valid, so only the value conversion can fail when the
    # branch is actually evaluated.
    with pytest.raises(lazy_backend.conversion_error, match="oops"):
        report.bad.collect()

    snapshot = lazy_backend.consumption.snapshot()
    # Engines may abort the failing projection before or after its probe column
    # is evaluated, but a probe may never observe a business row more than once.
    bad_counts = snapshot.pop("bad", {})
    assert set(bad_counts) <= set(BAD_COUNTS["bad"])
    assert all(count == 1 for count in bad_counts.values())
    # Whatever happened to the bad branch, the normal branch's business input
    # was not evaluated a second time by it.
    assert snapshot == NORMAL_COUNTS

    # A failed value conversion in one branch must leave the other branch's
    # declaration and successful result untouched.
    assert_schema(report.normal, NORMAL_SCHEMA)
    assert_normal_result(report.normal.collect(), NORMAL_SCHEMA)
    assert lazy_backend.consumption.snapshot() == {
        "normal": {100: 2, 200: 2, 300: 2},
        "bad": bad_counts,
    }


def test_widened_normal_branch_reuses_same_null_business_input(
    lazy_backend: LazyBackend,
) -> None:
    report = build_report(lazy_backend)

    assert_schema(report.normal, NORMAL_SCHEMA)
    assert_schema(report.wide, WIDE_SCHEMA)
    assert_schema(report.normal, NORMAL_SCHEMA)
    assert lazy_backend.consumption.snapshot() == {}

    assert_normal_result(report.normal.collect(), NORMAL_SCHEMA)
    assert_normal_result(report.wide.collect(), WIDE_SCHEMA)
    assert lazy_backend.consumption.snapshot() == {"normal": {100: 2, 200: 2, 300: 2}}
