"""Regression tests for the effective sample semantics of the 2-sample KS test.

The upstream data is sampled per supplier and keeps duplicate observations of
both historical and current-day amounts. Empty (NULL) amounts are unobserved:
they may only enter the unobserved count and must never participate in the
Kolmogorov-Smirnov statistic, its sample sizes or its decision.

The scenarios below use two samples with the same value domain and mean but
different repetition counts:

* reference sample: ``[0, 1, 1, 1, 2]``
* current sample:   ``[0, 0, 1, 2, 2]``

Each test independently recomputes the test statistic and the participating
sample sizes from cumulative counts in plain Python, instead of trusting the
database row counts.

The tests run against an in-process SQLite database through the regular
datajudge entry points (``TableDataSource``/``DataReference`` and
``BetweenRequirement.add_ks_2sample_constraint``).
"""

from __future__ import annotations

import random
from collections import Counter

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from datajudge import BetweenRequirement
from datajudge.constraints.stats import MIN_SAMPLE_SIZE, KolmogorovSmirnov2Sample
from datajudge.data_source import TableDataSource
from datajudge.db_access import DataReference

SIGNIFICANCE_LEVEL = 0.05
COLUMN = "amount"
DATABASE = "tempdb"

REFERENCE_SAMPLE = [0, 1, 1, 1, 2]
CURRENT_SAMPLE = [0, 0, 1, 2, 2]
FULL_REPEATS = 25
LOW_REPEATS = 2
N_EXTRA_NULLS = 15
N_PADDING_NULLS = 30


@pytest.fixture(scope="module")
def engine():
    # A single shared in-process database so that all connections see the tables.
    sqlite_engine = sa.create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    yield sqlite_engine
    sqlite_engine.dispose()


def _create_table(engine, table_name, values, seed):
    """Create a table and insert the values in a shuffled order."""
    metadata = sa.MetaData()
    table = sa.Table(table_name, metadata, sa.Column(COLUMN, sa.Integer()))
    metadata.create_all(engine)
    rows = [{COLUMN: value} for value in values]
    random.Random(seed).shuffle(rows)
    with engine.begin() as connection:
        connection.execute(table.insert(), rows)
    return table_name


def _references(engine, ref_table, current_table):
    return (
        DataReference(TableDataSource(DATABASE, ref_table), columns=[COLUMN]),
        DataReference(TableDataSource(DATABASE, current_table), columns=[COLUMN]),
    )


def _hand_ks(values1, values2, *, count_nulls_as_observed=False):
    """Independently derive D and the participating sample sizes.

    The empirical distribution functions are built from cumulative occurrence
    counts; by default NULL values are treated as unobserved and excluded.
    Setting ``count_nulls_as_observed`` simulates the regression in which empty
    amounts wrongly enter the sample (NULLs order before every numeric value).
    """

    def cumulative_distribution(values):
        considered = list(values)
        if not count_nulls_as_observed:
            considered = [value for value in considered if value is not None]
        counts = Counter(considered)
        ordered_values = sorted(counts, key=lambda value: (value is not None, value))
        total = len(considered)
        cumulative = 0
        distribution = {}
        for value in ordered_values:
            cumulative += counts[value]
            distribution[value] = cumulative / total
        return distribution, total

    distribution1, n_samples = cumulative_distribution(values1)
    distribution2, m_samples = cumulative_distribution(values2)
    support = sorted(
        set(distribution1) | set(distribution2),
        key=lambda value: (value is not None, value),
    )
    d_statistic = 0.0
    cdf1 = cdf2 = 0.0
    for value in support:
        cdf1 = distribution1.get(value, cdf1)
        cdf2 = distribution2.get(value, cdf2)
        d_statistic = max(d_statistic, abs(cdf1 - cdf2))
    return d_statistic, n_samples, m_samples


def _run_constraint(engine, ref, current, min_sample_size=MIN_SAMPLE_SIZE):
    constraint = KolmogorovSmirnov2Sample(
        ref,
        current,
        significance_level=SIGNIFICANCE_LEVEL,
        min_sample_size=min_sample_size,
    )
    return constraint.test(engine)


def test_original_sample_detects_shape_difference(engine):
    reference_values = REFERENCE_SAMPLE * FULL_REPEATS
    current_values = CURRENT_SAMPLE * FULL_REPEATS
    expected_d, expected_n, expected_m = _hand_ks(reference_values, current_values)
    assert expected_d == pytest.approx(0.2)
    assert (expected_n, expected_m) == (125, 125)

    ref_table = _create_table(engine, "ks_full_ref", reference_values, seed=1)
    current_table = _create_table(engine, "ks_full_cur", current_values, seed=2)
    ref, current = _references(engine, ref_table, current_table)

    (
        d_statistic,
        p_value,
        n_samples,
        m_samples,
        n_total,
        m_total,
        _,
    ) = KolmogorovSmirnov2Sample._calculate_statistic(engine, ref, current)

    assert d_statistic == pytest.approx(expected_d, abs=1e-12)
    assert (n_samples, m_samples) == (expected_n, expected_m)
    assert (n_total, m_total) == (125, 125)
    assert p_value is not None and p_value < SIGNIFICANCE_LEVEL

    result = _run_constraint(engine, ref, current)
    assert result.outcome is False
    assert "rejected" in result.failure_message

    # The public requirement entry point reaches the same decision.
    requirement = BetweenRequirement.from_tables(
        DATABASE, None, ref_table, DATABASE, None, current_table
    )
    requirement.add_ks_2sample_constraint(
        column1=COLUMN,
        column2=COLUMN,
        significance_level=SIGNIFICANCE_LEVEL,
    )
    public_result = requirement[0].test(engine)
    assert public_result.outcome is False


@pytest.mark.parametrize("null_side", ["reference", "current"])
def test_one_sided_nulls_only_change_unobserved_count(engine, null_side):
    reference_values = list(REFERENCE_SAMPLE * FULL_REPEATS)
    current_values = list(CURRENT_SAMPLE * FULL_REPEATS)
    if null_side == "reference":
        reference_values.extend([None] * N_EXTRA_NULLS)
    else:
        current_values.extend([None] * N_EXTRA_NULLS)

    expected_d, expected_n, expected_m = _hand_ks(reference_values, current_values)
    assert expected_d == pytest.approx(0.2)
    assert (expected_n, expected_m) == (125, 125)

    ref_table = _create_table(
        engine, f"ks_null_ref_{null_side}", reference_values, seed=3
    )
    current_table = _create_table(
        engine, f"ks_null_cur_{null_side}", current_values, seed=4
    )
    ref, current = _references(engine, ref_table, current_table)

    (
        d_statistic,
        p_value,
        n_samples,
        m_samples,
        n_total,
        m_total,
        _,
    ) = KolmogorovSmirnov2Sample._calculate_statistic(engine, ref, current)

    assert d_statistic == pytest.approx(expected_d, abs=1e-12)
    assert (n_samples, m_samples) == (125, 125)
    assert (n_total - n_samples, m_total - m_samples) == (
        (N_EXTRA_NULLS, 0) if null_side == "reference" else (0, N_EXTRA_NULLS)
    )
    assert p_value is not None

    result = _run_constraint(engine, ref, current)
    assert result.outcome is False
    assert "rejected" in result.failure_message


def test_swapped_sides_keep_decision_and_swap_participation(engine):
    reference_values = REFERENCE_SAMPLE * FULL_REPEATS
    current_values = CURRENT_SAMPLE * FULL_REPEATS + [None] * N_EXTRA_NULLS
    ref_table = _create_table(engine, "ks_swap_ref", reference_values, seed=5)
    current_table = _create_table(engine, "ks_swap_cur", current_values, seed=6)
    ref, current = _references(engine, ref_table, current_table)

    forward = KolmogorovSmirnov2Sample._calculate_statistic(engine, ref, current)
    swapped = KolmogorovSmirnov2Sample._calculate_statistic(engine, current, ref)

    assert swapped[0] == pytest.approx(forward[0], abs=1e-12)
    assert swapped[0] == pytest.approx(0.2, abs=1e-12)
    # Only the two sides' participation figures are exchanged.
    assert (swapped[2], swapped[3]) == (forward[3], forward[2])
    assert (swapped[4], swapped[5]) == (forward[5], forward[4])
    assert (forward[2], forward[3]) == (125, 125)
    assert (forward[4], forward[5]) == (125, 125 + N_EXTRA_NULLS)
    assert (swapped[4], swapped[5]) == (125 + N_EXTRA_NULLS, 125)

    assert _run_constraint(engine, ref, current).outcome is False
    assert _run_constraint(engine, current, ref).outcome is False


def test_low_volume_reports_insufficient_evidence_without_p_value(engine):
    reference_values = REFERENCE_SAMPLE * LOW_REPEATS
    current_values = CURRENT_SAMPLE * LOW_REPEATS
    expected_d, expected_n, expected_m = _hand_ks(reference_values, current_values)
    assert (expected_n, expected_m) == (10, 10)

    ref_table = _create_table(engine, "ks_low_ref", reference_values, seed=7)
    current_table = _create_table(engine, "ks_low_cur", current_values, seed=8)
    ref, current = _references(engine, ref_table, current_table)

    d_statistic, p_value, n_samples, m_samples, n_total, m_total, _ = (
        KolmogorovSmirnov2Sample._calculate_statistic(engine, ref, current)
    )
    assert d_statistic == pytest.approx(expected_d, abs=1e-12)
    assert (n_samples, m_samples) == (10, 10)
    assert (n_total, m_total) == (10, 10)
    assert p_value is None

    result = _run_constraint(engine, ref, current)
    assert result.outcome is False
    assert "Insufficient evidence" in result.failure_message
    assert "10" in result.failure_message


def test_null_padding_cannot_pad_low_volume_into_a_decision(engine):
    # 40 physical rows per side, but only 10 observed values each: the decision
    # must follow the observed count, not the total row count.
    reference_values = REFERENCE_SAMPLE * LOW_REPEATS + [None] * N_PADDING_NULLS
    current_values = CURRENT_SAMPLE * LOW_REPEATS + [None] * N_PADDING_NULLS
    ref_table = _create_table(engine, "ks_low_pad_ref", reference_values, seed=9)
    current_table = _create_table(engine, "ks_low_pad_cur", current_values, seed=10)
    ref, current = _references(engine, ref_table, current_table)

    d_statistic, p_value, n_samples, m_samples, n_total, m_total, _ = (
        KolmogorovSmirnov2Sample._calculate_statistic(engine, ref, current)
    )
    assert d_statistic == pytest.approx(0.2, abs=1e-12)
    assert (n_samples, m_samples) == (10, 10)
    assert (n_total, m_total) == (40, 40)
    assert p_value is None

    result = _run_constraint(engine, ref, current)
    assert result.outcome is False
    assert "Insufficient evidence" in result.failure_message


def test_early_dedup_regression_is_distinguishable(engine):
    # De-duplicating the repeated observations before testing would erase the
    # shape difference: both de-duplicated samples reduce to [0, 1, 2] with
    # identical empirical distributions and only three observations per side.
    reference_values = REFERENCE_SAMPLE * FULL_REPEATS
    current_values = CURRENT_SAMPLE * FULL_REPEATS
    deduped_d, deduped_n, deduped_m = _hand_ks(
        list(set(reference_values)), list(set(current_values))
    )
    assert deduped_d == pytest.approx(0.0)
    assert (deduped_n, deduped_m) == (3, 3)

    ref_table = _create_table(engine, "ks_dedup_ref", reference_values, seed=11)
    current_table = _create_table(engine, "ks_dedup_cur", current_values, seed=12)
    ref, current = _references(engine, ref_table, current_table)
    d_statistic, _, n_samples, m_samples, _, _, _ = (
        KolmogorovSmirnov2Sample._calculate_statistic(engine, ref, current)
    )

    # The library retains the duplicate observations and their weights.
    assert d_statistic == pytest.approx(0.2, abs=1e-12)
    assert d_statistic != pytest.approx(deduped_d)
    assert (n_samples, m_samples) == (125, 125)
    assert _run_constraint(engine, ref, current).outcome is False


def test_nulls_in_sample_regression_is_distinguishable(engine):
    reference_values = REFERENCE_SAMPLE * FULL_REPEATS
    current_values = CURRENT_SAMPLE * FULL_REPEATS + [None] * N_EXTRA_NULLS

    # Treating NULLs as observed (and letting them sort first) shifts the
    # empirical distribution and produces a different, inflated statistic.
    corrupted_d, corrupted_n, corrupted_m = _hand_ks(
        reference_values, current_values, count_nulls_as_observed=True
    )
    assert corrupted_d != pytest.approx(0.2)
    assert corrupted_m == 125 + N_EXTRA_NULLS

    ref_table = _create_table(engine, "ks_nullbug_ref", reference_values, seed=13)
    current_table = _create_table(engine, "ks_nullbug_cur", current_values, seed=14)
    ref, current = _references(engine, ref_table, current_table)
    d_statistic, _, n_samples, m_samples, _, m_total, _ = (
        KolmogorovSmirnov2Sample._calculate_statistic(engine, ref, current)
    )

    assert d_statistic == pytest.approx(0.2, abs=1e-12)
    assert d_statistic != pytest.approx(corrupted_d)
    assert (n_samples, m_samples) == (125, 125)
    assert m_total == 125 + N_EXTRA_NULLS


def test_total_row_count_decision_regression_is_distinguishable(engine):
    # With 40 physical rows per side a total-row-count implementation would
    # clear the minimum of 35, take a p-value based decision and wrongly pass.
    reference_values = REFERENCE_SAMPLE * LOW_REPEATS + [None] * N_PADDING_NULLS
    current_values = CURRENT_SAMPLE * LOW_REPEATS + [None] * N_PADDING_NULLS
    ref_table = _create_table(engine, "ks_rowcount_ref", reference_values, seed=15)
    current_table = _create_table(engine, "ks_rowcount_cur", current_values, seed=16)
    ref, current = _references(engine, ref_table, current_table)

    d_statistic, _, n_samples, m_samples, n_total, m_total, _ = (
        KolmogorovSmirnov2Sample._calculate_statistic(engine, ref, current)
    )
    assert (n_total, m_total) == (40, 40)
    assert min(n_total, m_total) >= MIN_SAMPLE_SIZE
    assert min(n_samples, m_samples) < MIN_SAMPLE_SIZE

    # A row-count implementation would accept and emit a p-value on the 40 rows.
    row_count_accepts = KolmogorovSmirnov2Sample._check_acceptance(
        d_statistic, n_total, m_total, SIGNIFICANCE_LEVEL
    )
    row_count_p_value = KolmogorovSmirnov2Sample._approximate_p_value(
        d_statistic, n_total, m_total
    )
    assert row_count_accepts is True
    assert row_count_p_value > SIGNIFICANCE_LEVEL

    # The library instead follows the observed counts and abstains.
    result = _run_constraint(engine, ref, current)
    assert result.outcome is False
    assert "Insufficient evidence" in result.failure_message
    assert "p-value" in result.failure_message


def test_all_null_side_reports_insufficient_evidence_without_crashing(engine):
    # A side whose amounts are all NULL contributes zero observed values, so no
    # statistic can be computed; this must still surface as insufficient
    # evidence rather than a database error.
    reference_values = [None] * 40
    current_values = CURRENT_SAMPLE * FULL_REPEATS
    ref_table = _create_table(engine, "ks_allnull_ref", reference_values, seed=17)
    current_table = _create_table(engine, "ks_allnull_cur", current_values, seed=18)
    ref, current = _references(engine, ref_table, current_table)

    d_statistic, p_value, n_samples, m_samples, n_total, m_total, _ = (
        KolmogorovSmirnov2Sample._calculate_statistic(engine, ref, current)
    )
    assert d_statistic is None
    assert p_value is None
    assert (n_samples, m_samples) == (0, 125)
    assert (n_total, m_total) == (40, 125)

    result = _run_constraint(engine, ref, current)
    assert result.outcome is False
    assert "Insufficient evidence" in result.failure_message
    assert "0" in result.failure_message
