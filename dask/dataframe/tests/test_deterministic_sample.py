from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import dask.dataframe as dd
from dask.dataframe import DeterministicSampleResult, deterministic_sample


@pytest.fixture
def click_pdf():
    # 10_000 distinct ids in shuffled order
    ids = np.random.default_rng(0).permutation(10_000)
    return pd.DataFrame({"click_id": ids})


def test_same_sample_regardless_of_partitioning(click_pdf):
    result_10 = deterministic_sample(dd.from_pandas(click_pdf, npartitions=10), 200, seed=42, id_column="click_id")
    result_20 = deterministic_sample(dd.from_pandas(click_pdf, npartitions=20), 200, seed=42, id_column="click_id")
    assert result_10.ids == result_20.ids
    assert len(result_10.ids) == 200
    assert len(set(result_10.ids)) == 200
    assert result_10.shortfall == 0
    assert result_10.duplicates == []
    assert result_10.missing is None


def test_same_seed_same_sample(click_pdf):
    ddf = dd.from_pandas(click_pdf, npartitions=10)
    first = deterministic_sample(ddf, 200, seed=7, id_column="click_id")
    second = deterministic_sample(ddf, 200, seed=7, id_column="click_id")
    assert first.ids == second.ids


def test_matches_reference_selection(click_pdf):
    # The distributed tree reduction must select exactly the n ids with the
    # smallest seeded hash, as computed directly on the full pandas frame.
    from dask.dataframe.deterministic_sample import _hash_id

    expected = sorted(click_pdf["click_id"], key=lambda value: (_hash_id(42, value), str(value)))[:200]
    result = deterministic_sample(dd.from_pandas(click_pdf, npartitions=20), 200, seed=42, id_column="click_id")
    assert result.ids == expected


def test_different_seed_different_sample(click_pdf):
    ddf = dd.from_pandas(click_pdf, npartitions=10)
    first = deterministic_sample(ddf, 200, seed=1, id_column="click_id")
    second = deterministic_sample(ddf, 200, seed=2, id_column="click_id")
    assert set(first.ids) != set(second.ids)


def test_exact_count_when_data_is_sufficient(click_pdf):
    ddf = dd.from_pandas(click_pdf, npartitions=10)
    result = deterministic_sample(ddf, 200, seed=42, id_column="click_id")
    assert len(result.ids) == 200
    assert result.shortfall == 0
    assert set(result.ids) <= set(click_pdf["click_id"])


def test_duplicate_ids_are_reported_and_sampled_once():
    pdf = pd.DataFrame({"click_id": list(range(500)) + [0, 1, 2, 250, 250]})
    ddf = dd.from_pandas(pdf, npartitions=10)
    result = deterministic_sample(ddf, 200, seed=42, id_column="click_id")
    assert sorted(result.duplicates) == [0, 1, 2, 250]
    assert len(result.ids) == 200
    assert len(set(result.ids)) == 200
    assert result.shortfall == 0


def test_duplicate_ids_spanning_partitions_are_reported():
    # Same id in every partition: duplicates must be detected globally,
    # not just within a single partition.
    pdf = pd.DataFrame({"click_id": [123] * 10 + list(range(1000, 1300))})
    ddf = dd.from_pandas(pdf, npartitions=10)
    result = deterministic_sample(ddf, 200, seed=42, id_column="click_id")
    assert result.duplicates == [123]
    assert result.ids.count(123) <= 1


def test_missing_expected_ids_are_reported(click_pdf):
    ddf = dd.from_pandas(click_pdf, npartitions=10)
    expected = [0, 5, 10_000, 20_000, 9_999]
    result = deterministic_sample(ddf, 200, seed=42, id_column="click_id", expected_ids=expected)
    assert result.missing == [10_000, 20_000]
    assert len(result.ids) == 200


def test_shortfall_when_not_enough_distinct_ids():
    pdf = pd.DataFrame({"click_id": list(range(50)) * 4})
    ddf = dd.from_pandas(pdf, npartitions=10)
    result = deterministic_sample(ddf, 200, seed=42, id_column="click_id")
    assert len(result.ids) == 50
    assert result.shortfall == 150
    assert sorted(result.duplicates) == list(range(50))


def test_string_ids():
    pdf = pd.DataFrame({"click_id": [f"click-{i}" for i in range(1_000)]})
    ddf10 = dd.from_pandas(pdf, npartitions=10)
    ddf20 = dd.from_pandas(pdf, npartitions=20)
    result_10 = deterministic_sample(ddf10, 200, seed=42, id_column="click_id")
    result_20 = deterministic_sample(ddf20, 200, seed=42, id_column="click_id")
    assert result_10.ids == result_20.ids
    assert len(result_10.ids) == 200


def test_result_type(click_pdf):
    ddf = dd.from_pandas(click_pdf, npartitions=10)
    result = deterministic_sample(ddf, 200, seed=42, id_column="click_id")
    assert isinstance(result, DeterministicSampleResult)


def test_invalid_n(click_pdf):
    ddf = dd.from_pandas(click_pdf, npartitions=10)
    for bad in (0, -1, 1.5, True):
        with pytest.raises(ValueError, match="positive integer"):
            deterministic_sample(ddf, bad, seed=42, id_column="click_id")


def test_numpy_integer_n(click_pdf):
    ddf = dd.from_pandas(click_pdf, npartitions=10)
    result = deterministic_sample(ddf, np.int64(200), seed=42, id_column="click_id")
    assert len(result.ids) == 200


def test_unknown_id_column(click_pdf):
    ddf = dd.from_pandas(click_pdf, npartitions=10)
    with pytest.raises(KeyError, match="not-a-column"):
        deterministic_sample(ddf, 200, seed=42, id_column="not-a-column")
