"""A runnable distribution-drift specification.

    pytest docs/examples/distribution_drift/specification.py -v

Each case loads its values from the CSV files next to this file into an
in-memory SQLite database, so every run sees exactly the same data and
produces the same verdicts and sample sizes.
"""

import csv
from pathlib import Path

import pytest
import sqlalchemy as sa

from datajudge import BetweenRequirement
from datajudge.pytest_integration import collect_data_tests

DATA_DIR = Path(__file__).parent
SIGNIFICANCE_LEVEL = 0.05
MIN_SAMPLES = 35

CASES = [
    pytest.param(
        "baseline.csv",
        "baseline.csv",
        True,
        (40, 40),
        id="same-distribution",
    ),
    pytest.param(
        "baseline.csv",
        "location_shift.csv",
        False,
        (40, 40),
        id="location-shift-same-shape",
    ),
    pytest.param(
        "baseline.csv",
        "shape_change.csv",
        False,
        (40, 40),
        id="shape-change-same-range",
    ),
    pytest.param(
        "insufficient_baseline.csv",
        "insufficient_current.csv",
        False,
        (10, 10),
        id="insufficient-samples",
    ),
]


def _read_values(filename: str) -> list[int]:
    with (DATA_DIR / filename).open(newline="", encoding="utf-8") as handle:
        return [int(row["value"]) for row in csv.DictReader(handle)]


@pytest.fixture(scope="module")
def datajudge_engine():
    engine = sa.create_engine("sqlite:///:memory:")
    filenames = sorted(
        {filename for case in CASES for filename in case.values[:2]}
    )
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
        min_samples=MIN_SAMPLES,
        name=f"{filename1} -> {filename2}",
    )
    return requirement


@pytest.mark.parametrize(
    "filename1, filename2, expected_outcome, expected_sample_sizes", CASES
)
def test_distribution_drift(
    datajudge_engine,
    filename1,
    filename2,
    expected_outcome,
    expected_sample_sizes,
):
    result = _requirement(filename1, filename2)[0].test(datajudge_engine)

    assert result.outcome is expected_outcome, result.failure_message
    assert result.sample_sizes == expected_sample_sizes
    if min(expected_sample_sizes) < MIN_SAMPLES:
        assert result.failure_message is not None
        assert "Insufficient sample size" in result.failure_message


# The same requirements can also be handed to pytest's regular integration
# helper. The identical case below is the one expected to pass.
identical_requirement = _requirement("baseline.csv", "baseline.csv")
test_constraint = collect_data_tests([identical_requirement])
