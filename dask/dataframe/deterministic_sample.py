from __future__ import annotations

import hashlib
import numbers
from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pandas as pd

from dask.base import compute

if TYPE_CHECKING:
    from dask.dataframe.dask_expr._collection import DataFrame

__all__ = ["DeterministicSampleResult", "deterministic_sample"]


@dataclass
class DeterministicSampleResult:
    """Outcome of :func:`deterministic_sample`.

    Attributes
    ----------
    ids : list
        The sampled record ids, in deterministic selection order (ascending
        seeded hash). Contains exactly ``n`` entries whenever the data holds
        at least ``n`` distinct ids.
    duplicates : list
        Record ids that appear more than once in the input. Duplicated ids
        are only sampled once.
    missing : list or None
        Entries of ``expected_ids`` that do not occur in the input, in the
        order they were given. ``None`` when no ``expected_ids`` were passed.
    shortfall : int
        How many ids fewer than requested were sampled because the input
        does not contain enough distinct ids. Zero when the sample is full.
    """

    ids: list
    duplicates: list
    missing: list | None
    shortfall: int


def _hash_id(seed: Hashable, value: Any) -> int:
    """Stable 64-bit hash of a record id, independent of hash randomization."""
    key = f"{seed}\x1f{type(value).__name__}\x1f{value}".encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "little")


def _take_hash_candidates(df: pd.DataFrame, id_column: str, n: int, seed: Hashable) -> pd.DataFrame:
    """Keep the ``n`` distinct ids with the smallest seeded hash in a partition."""
    ids = df[id_column]
    out = pd.DataFrame(
        {
            "id": ids,
            # Pin to uint64: the hashes span the full 64-bit range and an
            # inferred int64/uint64 mix could be upcast to float on concat.
            "_hash": ids.map(lambda value: _hash_id(seed, value)).astype("uint64"),
            "_idstr": ids.map(str),
        }
    )
    out = out.drop_duplicates(subset="id")
    out = out.sort_values(["_hash", "_idstr"], kind="mergesort")
    return out.head(n)


def _combine_hash_candidates(parts: pd.DataFrame, n: int) -> pd.DataFrame:
    """Merge per-partition candidates, keeping the ``n`` smallest hashes."""
    parts = parts.drop_duplicates(subset="id")
    parts = parts.sort_values(["_hash", "_idstr"], kind="mergesort")
    return parts.head(n)


def deterministic_sample(
    df: DataFrame,
    n: int,
    *,
    seed: Hashable,
    id_column: str,
    expected_ids: Iterable[Hashable] | None = None,
) -> DeterministicSampleResult:
    """Draw exactly ``n`` record ids, reproducibly and partition-independently.

    Each record id is assigned a stable hash derived from ``seed`` and the id
    itself, and the ``n`` ids with the smallest hash values are selected.
    Because the selection depends only on ``(seed, id)`` — never on row
    position or partition layout — the same data yields the same sample no
    matter how many partitions it is split into.

    The selection runs as a tree reduction: every partition keeps only its
    ``n`` best candidates and intermediate results are merged pair-wise, so
    no single task (and never the driver) has to hold all records. Duplicate
    and missing ids are reported through the returned result object.

    This function triggers computation and returns concrete results.

    Parameters
    ----------
    df : DataFrame
        The input dask DataFrame.
    n : int
        Number of record ids to sample. Must be positive.
    seed : Hashable
        Seed for the stable hash. The same seed and the same set of record
        ids always produce the same sample.
    id_column : str
        Name of the column holding the record ids.
    expected_ids : Iterable, optional
        If given, the ids expected to be present in the data. Any expected
        id that is missing from the data is reported in
        :attr:`DeterministicSampleResult.missing`.

    Returns
    -------
    DeterministicSampleResult
        Named result with the sampled ``ids`` plus data-quality reports
        (``duplicates``, ``missing``, ``shortfall``).

    See Also
    --------
    DataFrame.sample : Approximate, partition-dependent random sampling.

    Examples
    --------
    >>> import pandas as pd
    >>> import dask.dataframe as dd
    >>> ddf = dd.from_pandas(pd.DataFrame({"click_id": range(10_000)}), npartitions=10)
    >>> result = dd.deterministic_sample(ddf, 200, seed=42, id_column="click_id")
    >>> len(result.ids)
    200
    >>> result.shortfall
    0

    The sample does not depend on how the same records are partitioned:

    >>> ddf20 = dd.from_pandas(pd.DataFrame({"click_id": range(10_000)}), npartitions=20)
    >>> dd.deterministic_sample(ddf20, 200, seed=42, id_column="click_id").ids == result.ids
    True
    """
    if not isinstance(n, numbers.Integral) or isinstance(n, bool) or n < 1:
        raise ValueError(f"n must be a positive integer, got {n!r}")
    n = int(n)
    if id_column not in df.columns:
        raise KeyError(f"id_column {id_column!r} is not a column of the DataFrame")

    # Build meta through the chunk function itself so dtypes always match.
    meta = _take_hash_candidates(df[[id_column]]._meta_nonempty, id_column, n=1, seed=seed).iloc[:0]

    sampled = df[[id_column]].reduction(
        _take_hash_candidates,
        combine=_combine_hash_candidates,
        aggregate=_combine_hash_candidates,
        meta=meta,
        token="deterministic-sample",
        chunk_kwargs={"id_column": id_column, "n": n, "seed": seed},
        combine_kwargs={"n": n},
        aggregate_kwargs={"n": n},
    )

    # Distributed data-quality checks on the id column only. The final
    # filtering happens on the workers, so only the (small) reports —
    # not the records — are brought back to the driver.
    id_counts = df[id_column].value_counts()
    duplicate_ids = id_counts[id_counts > 1].index
    distinct_count = id_counts.size

    if expected_ids is not None:
        expected = list(dict.fromkeys(expected_ids))
        present = df[id_column][df[id_column].isin(expected)].drop_duplicates()
        sampled_pdf, dup_index, n_distinct, present_ids = compute(sampled, duplicate_ids, distinct_count, present)
        present_set = set(present_ids.tolist())
        missing = [expected_id for expected_id in expected if expected_id not in present_set]
    else:
        sampled_pdf, dup_index, n_distinct = compute(sampled, duplicate_ids, distinct_count)
        missing = None

    return DeterministicSampleResult(
        ids=sampled_pdf["id"].tolist(),
        duplicates=dup_index.tolist(),
        missing=missing,
        shortfall=max(0, n - int(n_distinct)),
    )
