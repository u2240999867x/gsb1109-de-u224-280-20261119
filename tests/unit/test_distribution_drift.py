import csv
from pathlib import Path

import pytest
import sqlalchemy as sa

from datajudge.constraints.stats import (
    DEFAULT_MIN_SAMPLES,
    KolmogorovSmirnov2Sample,
)
from datajudge.data_source import TableDataSource
from datajudge.db_access import DataReference, apply_patches
from datajudge.requirements import BetweenRequirement

EXAMPLE_DIR = (
    Path(__file__).parents[2] / "docs" / "examples" / "distribution_drift"
)
SIGNIFICANCE_LEVEL = 0.05

# Expected results of the checked-in CSV files. Keeping them here makes a
# change in the example data or in the KS implementation visible immediately.
CASES = [
    pytest.param(
        "baseline.csv",
        "baseline.csv",
        True,
        (40, 40),
        0.0,
        id="identical",
    ),
    pytest.param(
        "baseline.csv",
        "location_shift.csv",
        False,
        (40, 40),
        0.5,
        id="location-shift-same-shape",
    ),
    pytest.param(
        "baseline.csv",
        "shape_change.csv",
        False,
        (40, 40),
        0.45,
        id="shape-change-same-range",
    ),
    pytest.param(
        "insufficient_baseline.csv",
        "insufficient_current.csv",
        False,
        (10, 10),
        None,
        id="insufficient-samples",
    ),
]


def _read_values(filename: str) -> list[int]:
    with (EXAMPLE_DIR / filename).open(newline="", encoding="utf-8") as handle:
        return [int(row["value"]) for row in csv.DictReader(handle)]


@pytest.fixture()
def engine():
    engine = sa.create_engine("sqlite:///:memory:")
    apply_patches(engine)

    filenames = {
        filename
        for case in CASES
        for filename in (case.values[0], case.values[1])
        if isinstance(filename, str)
    }
    with engine.begin() as connection:
        for filename in filenames:
            table_name = filename.removesuffix(".csv")
            connection.execute(
                sa.text(f'CREATE TABLE "{table_name}" ("value" INTEGER NOT NULL)')
            )
            connection.execute(
                sa.text(f'INSERT INTO "{table_name}" ("value") VALUES (:value)'),
                [{"value": value} for value in _read_values(filename)],
            )
    return engine


def _requirement(filename1: str, filename2: str) -> BetweenRequirement:
    requirement = BetweenRequirement.from_tables(
        db_name1="sqlite",
        schema_name1=None,
        table_name1=filename1.removesuffix(".csv"),
        db_name2="sqlite",
        schema_name2=None,
        table_name2=filename2.removesuffix(".csv"),
    )
    requirement.add_ks_2sample_constraint(
        column1="value",
        column2="value",
        significance_level=SIGNIFICANCE_LEVEL,
    )
    return requirement


def _calculate_d(filename1: str, filename2: str, engine) -> float:
    data_source1 = TableDataSource("sqlite", filename1.removesuffix(".csv"))
    data_source2 = TableDataSource("sqlite", filename2.removesuffix(".csv"))
    ref1 = DataReference(data_source1, ["value"])
    ref2 = DataReference(data_source2, ["value"])
    d_statistic, *_ = KolmogorovSmirnov2Sample._calculate_statistic(
        engine, ref1, ref2
    )
    return d_statistic


@pytest.mark.parametrize(
    "filename1, filename2, expected_outcome, expected_sample_sizes, expected_d",
    CASES,
)
def test_distribution_drift_verdict_is_reproducible(
    engine,
    filename1,
    filename2,
    expected_outcome,
    expected_sample_sizes,
    expected_d,
):
    if expected_d is not None:
        assert _calculate_d(filename1, filename2, engine) == pytest.approx(
            expected_d, abs=1e-12
        )

    result = _requirement(filename1, filename2)[0].test(engine)

    assert result.outcome is expected_outcome, result.failure_message
    assert result.sample_sizes == expected_sample_sizes


def test_repeated_runs_give_identical_verdicts_and_sample_sizes(engine):
    for case in CASES:
        filename1, filename2, expected_outcome, expected_sample_sizes, _ = (
            case.values
        )
        results = [
            _requirement(filename1, filename2)[0].test(engine) for _ in range(3)
        ]

        assert {result.outcome for result in results} == {expected_outcome}
        assert {result.sample_sizes for result in results} == {
            expected_sample_sizes
        }
        assert len({result.failure_message for result in results}) == 1


def test_location_only_change_fails_but_ranges_differ(engine):
    baseline = _read_values("baseline.csv")
    shifted = _read_values("location_shift.csv")

    assert len(baseline) == len(shifted)
    # A pure location shift: the sorted shape is untouched.
    assert shifted == [value + 20 for value in baseline]
    assert sum(shifted) / len(shifted) > sum(baseline) / len(baseline)

    result = _requirement("baseline.csv", "location_shift.csv")[0].test(engine)
    assert not result.outcome


def test_shape_change_keeps_value_range(engine):
    baseline = _read_values("baseline.csv")
    changed = _read_values("shape_change.csv")

    assert len(baseline) == len(changed)
    assert min(baseline) == min(changed)
    assert max(baseline) == max(changed)
    assert sorted(baseline) != sorted(changed)

    result = _requirement("baseline.csv", "shape_change.csv")[0].test(engine)
    assert not result.outcome


def test_insufficient_samples_fail_despite_identical_distribution(engine):
    baseline = _read_values("insufficient_baseline.csv")
    current = _read_values("insufficient_current.csv")

    assert baseline == current
    assert _calculate_d(
        "insufficient_baseline.csv", "insufficient_current.csv", engine
    ) == pytest.approx(0.0, abs=1e-12)

    result = _requirement(
        "insufficient_baseline.csv", "insufficient_current.csv"
    )[0].test(engine)

    assert result.sample_sizes == (10, 10)
    assert not result.outcome
    assert result.failure_message is not None
    assert "Insufficient sample size" in result.failure_message
    assert "10 and 10 rows" in result.failure_message
    assert f"at least {DEFAULT_MIN_SAMPLES} per sample" in result.failure_message


def test_min_samples_threshold_is_configurable(engine):
    requirement = BetweenRequirement.from_tables(
        db_name1="sqlite",
        schema_name1=None,
        table_name1="insufficient_baseline",
        db_name2="sqlite",
        schema_name2=None,
        table_name2="insufficient_current",
    )
    requirement.add_ks_2sample_constraint(
        column1="value",
        column2="value",
        significance_level=SIGNIFICANCE_LEVEL,
        min_samples=10,
    )

    result = requirement[0].test(engine)

    assert result.outcome
    assert result.sample_sizes == (10, 10)


def test_invalid_min_samples_rejected():
    requirement = BetweenRequirement.from_tables(
        "sqlite", None, "a", "sqlite", None, "b"
    )

    with pytest.raises(ValueError, match="min_samples"):
        requirement.add_ks_2sample_constraint(
            column1="value", column2="value", min_samples=0
        )
