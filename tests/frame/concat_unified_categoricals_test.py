from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import narwhals as nw
from narwhals.exceptions import (
    CategoricalConflict,
    CategoricalUnificationError,
    InvalidOperationError,
    NarwhalsError,
)
from tests.utils import assert_equal_data

if TYPE_CHECKING:
    from collections.abc import Sequence

pd = pytest.importorskip("pandas")
pa = pytest.importorskip("pyarrow")


def make_pandas_frame(
    labels: Sequence[str | None], categories: list[str], *, ordered: bool = True
) -> nw.DataFrame[Any]:
    return nw.from_native(
        pd.DataFrame(
            {"status": pd.Categorical(labels, categories=categories, ordered=ordered)}
        ),
        eager_only=True,
    )


def make_pyarrow_frame(
    chunks: Sequence[tuple[Sequence[int | None], list[str]]],
) -> nw.DataFrame[Any]:
    arrays = [
        pa.DictionaryArray.from_arrays(
            pa.array(list(codes), type=pa.int32()), pa.array(dictionary)
        )
        for codes, dictionary in chunks
    ]
    table = pa.table({"status": pa.chunked_array(arrays)})
    return nw.from_native(table, eager_only=True)


# Three batches from different channels: each declares the same business labels
# through its own local dictionary encoding. `premium` is declared (by batch 2)
# but never used by any row, `unknown` is an ordinary label, and nulls occur.
PANDAS_BATCHES = (
    (["low", "high", None], ["low", "medium", "high"]),
    (["unknown", "medium"], ["unknown", "low", "medium"]),
    (["medium", None], ["medium", "high", "premium"]),
)
UNIFIED = ("unknown", "low", "medium", "high", "premium")


def pandas_batches() -> list[nw.DataFrame[Any]]:
    return [
        make_pandas_frame(labels, categories) for labels, categories in PANDAS_BATCHES
    ]


def pyarrow_batches() -> list[nw.DataFrame[Any]]:
    return [
        make_pyarrow_frame([([0, 2, None], ["low", "medium", "high"])]),
        # Two chunks with independent local dictionaries.
        make_pyarrow_frame([([0], ["unknown", "low"]), ([1], ["low", "medium"])]),
        make_pyarrow_frame([([0, None], ["medium", "high", "premium"])]),
    ]


def test_unify_compatible_pandas() -> None:
    batches = pandas_batches()
    unification = nw.categorical_unification(batches)

    # Local codes map, by label, onto one stable dictionary; the declared
    # orders of all batches are respected, and the unused `premium` category
    # is still part of the unified dictionary.
    assert unification.categories == {"status": UNIFIED}
    assert unification.mappings == (
        {"status": ((1, 2, 3),)},
        {"status": ((0, 1, 2),)},
        {"status": ((2, 3, 4),)},
    )

    # Swapping the batch order yields the same unified order and labels.
    swapped = nw.categorical_unification(batches[::-1])
    assert swapped.categories == unification.categories

    result = nw.concat_unified_categoricals(batches)
    assert result.schema["status"] == nw.Enum(UNIFIED)
    assert nw.is_ordered_categorical(result["status"])
    assert_equal_data(
        result, {"status": ["low", "high", None, "unknown", "medium", "medium", None]}
    )
    # Local codes are remapped to the unified dictionary; nulls stay null.
    assert result.to_native()["status"].cat.codes.tolist() == [1, 3, -1, 0, 2, 2, -1]
    # `unknown` is an ordinary category and can be queried as usual.
    assert_equal_data(
        result.filter(nw.col("status") == "unknown"), {"status": ["unknown"]}
    )
    # `premium` is a valid category even though no row uses it.
    assert result.filter(nw.col("status") == "premium").shape == (0, 1)


def test_unify_compatible_pyarrow() -> None:
    batches = pyarrow_batches()
    unification = nw.categorical_unification(batches)

    assert unification.categories == {"status": UNIFIED}
    # PyArrow columns declare one dictionary per chunk.
    assert unification.mappings == (
        {"status": ((1, 2, 3),)},
        {"status": ((0, 1), (1, 2))},
        {"status": ((2, 3, 4),)},
    )

    # Swapping batch order and chunk order yields the same unified order.
    rechunked = [
        batches[0],
        make_pyarrow_frame([([1], ["low", "medium"]), ([0], ["unknown", "low"])]),
        batches[2],
    ]
    assert nw.categorical_unification(rechunked).categories == unification.categories
    assert nw.categorical_unification(batches[::-1]).categories == unification.categories

    result = nw.concat_unified_categoricals(batches)
    # PyArrow has no ordered-categorical compute semantics: the column stays a
    # plain `Categorical`, with the unified order recorded in the dictionary.
    assert result.schema["status"] == nw.Categorical()
    assert not nw.is_ordered_categorical(result["status"])
    combined = result.to_native()["status"].combine_chunks()
    assert combined.dictionary.to_pylist() == list(UNIFIED)
    assert combined.indices.to_pylist() == [1, 3, None, 0, 2, 2, None]
    assert_equal_data(
        result, {"status": ["low", "high", None, "unknown", "medium", "medium", None]}
    )
    assert_equal_data(
        result.filter(nw.col("status") == "unknown"), {"status": ["unknown"]}
    )


def test_cycle_reports_minimal_conflicts_pandas() -> None:
    # Each pair of batches is compatible, but jointly they form a cycle:
    # low < medium < high < deprecated < low. `deprecated` is declared (and
    # takes part in the conflict) without being used by any row.
    batch_0 = make_pandas_frame(["low"], ["low", "medium"])
    batch_1 = make_pandas_frame(["medium"], ["medium", "high"])
    batch_2 = make_pandas_frame(["high"], ["high", "deprecated", "low"])
    for compatible in ((batch_0, batch_1), (batch_0, batch_2), (batch_1, batch_2)):
        nw.categorical_unification(compatible)

    with pytest.raises(CategoricalUnificationError) as excinfo:
        nw.categorical_unification([batch_0, batch_1, batch_2])
    exc = excinfo.value
    assert isinstance(exc, NarwhalsError)
    assert exc.column == "status"
    # The reported conflicts are exactly the relations of the minimal cycle,
    # each annotated with the batches which declared it.
    assert set(exc.conflicts) == {
        CategoricalConflict("status", "low", "medium", (0,)),
        CategoricalConflict("status", "medium", "high", (1,)),
        CategoricalConflict("status", "high", "deprecated", (2,)),
        CategoricalConflict("status", "deprecated", "low", (2,)),
    }
    message = str(exc)
    assert "'deprecated' precedes 'low' (declared by input(s) 2)" in message
    assert "'low' precedes 'medium' (declared by input(s) 0)" in message

    with pytest.raises(CategoricalUnificationError):
        nw.concat_unified_categoricals([batch_0, batch_1, batch_2])


def test_cycle_reports_minimal_conflicts_pyarrow() -> None:
    batch_0 = make_pyarrow_frame([([0], ["low", "medium"])])
    batch_1 = make_pyarrow_frame([([0], ["medium", "high"])])
    batch_2 = make_pyarrow_frame([([0], ["high", "deprecated", "low"])])
    with pytest.raises(CategoricalUnificationError) as excinfo:
        nw.concat_unified_categoricals([batch_0, batch_1, batch_2])
    assert {conflict.before for conflict in excinfo.value.conflicts} == {
        "low",
        "medium",
        "high",
        "deprecated",
    }


def test_cycle_with_acyclic_tail() -> None:
    # `a1`/`a2` only take part in an acyclic chain hanging off the cycle, so
    # the reported minimal cycle must not include them.
    batch_0 = make_pandas_frame(["x"], ["x", "y"])
    batch_1 = make_pandas_frame(["y"], ["y", "x"])
    batch_2 = make_pandas_frame(["a1"], ["x", "a1", "a2"])
    with pytest.raises(CategoricalUnificationError) as excinfo:
        nw.categorical_unification([batch_0, batch_1, batch_2])
    assert set(excinfo.value.conflicts) == {
        CategoricalConflict("status", "x", "y", (0,)),
        CategoricalConflict("status", "y", "x", (1,)),
    }


def test_empty_categories() -> None:
    # Dictionaries without any labels still unify (to an empty dictionary),
    # and every row stays null.
    df_a = make_pandas_frame([None, None], [])
    df_b = make_pandas_frame([None], [])
    unification = nw.categorical_unification([df_a, df_b])
    assert unification.categories == {"status": ()}
    result = nw.concat_unified_categoricals([df_a, df_b])
    assert result["status"].is_null().to_list() == [True, True, True]

    table_a = make_pyarrow_frame([([None, None], [])])
    table_b = make_pyarrow_frame([([None], [])])
    assert nw.categorical_unification([table_a, table_b]).categories == {"status": ()}
    result = nw.concat_unified_categoricals([table_a, table_b])
    assert result["status"].is_null().to_list() == [True, True, True]


def test_tie_break() -> None:
    # `apple` and `zebra` are never ordered relative to each other, so the
    # business tie-break rule decides.
    batch_0 = make_pandas_frame(["low"], ["low", "high"])
    batch_1 = make_pandas_frame(["apple"], ["apple", "low"])
    batch_2 = make_pandas_frame(["zebra"], ["zebra", "high"])

    unification = nw.categorical_unification([batch_0, batch_1, batch_2])
    assert unification.categories == {"status": ("apple", "low", "zebra", "high")}

    unification = nw.categorical_unification(
        [batch_0, batch_1, batch_2], tie_break=lambda label: label[::-1]
    )
    assert unification.categories == {"status": ("zebra", "apple", "low", "high")}

    with pytest.raises(TypeError, match="tie_break"):
        nw.categorical_unification([batch_0], tie_break=42)  # type: ignore[arg-type]


def test_unordered_and_ordered_inputs_unify() -> None:
    ordered = make_pandas_frame(["low"], ["low", "high"], ordered=True)
    unordered = make_pandas_frame(["high"], ["high", "low"], ordered=False)
    assert ordered.schema["status"] == nw.Enum(["low", "high"])
    assert unordered.schema["status"] == nw.Categorical()
    with pytest.raises(CategoricalUnificationError):
        # `ordered` declares low < high, `unordered` declares high < low.
        nw.categorical_unification([ordered, unordered])

    unordered_compatible = make_pandas_frame(["low"], ["low", "high"], ordered=False)
    result = nw.concat_unified_categoricals([ordered, unordered_compatible])
    assert result.schema["status"] == nw.Enum(["low", "high"])


def test_multiple_categorical_columns() -> None:
    def make(
        labels: list[str], categories: list[str], hazards: list[str]
    ) -> nw.DataFrame[Any]:
        return nw.from_native(
            pd.DataFrame(
                {
                    "status": pd.Categorical(labels, categories=categories, ordered=True),
                    "hazard": pd.Categorical(
                        hazards, categories=["low", "high"], ordered=True
                    ),
                }
            ),
            eager_only=True,
        )

    df_a = make(["low"], ["low", "high"], ["low"])
    df_b = make(["unknown"], ["unknown", "low"], ["high"])
    unification = nw.categorical_unification([df_a, df_b])
    assert unification.categories == {
        "status": ("unknown", "low", "high"),
        "hazard": ("low", "high"),
    }
    result = nw.concat_unified_categoricals([df_a, df_b])
    assert_equal_data(result, {"status": ["low", "unknown"], "hazard": ["low", "high"]})


def test_no_categorical_columns() -> None:
    df_a = nw.from_native(pd.DataFrame({"a": [1, 2]}), eager_only=True)
    df_b = nw.from_native(pd.DataFrame({"a": [3]}), eager_only=True)
    result = nw.concat_unified_categoricals([df_a, df_b])
    assert_equal_data(result, {"a": [1, 2, 3]})
    unification = nw.categorical_unification([df_a, df_b])
    assert unification.categories == {}
    assert unification.mappings == ({}, {})


def test_invalid_items() -> None:
    with pytest.raises(ValueError, match="No items"):
        nw.categorical_unification([])
    with pytest.raises(ValueError, match="No items"):
        nw.concat_unified_categoricals([])

    df = make_pandas_frame(["low"], ["low", "high"])
    with pytest.raises(InvalidOperationError, match="eager"):
        nw.categorical_unification([df.lazy()])  # type: ignore[list-item]
    with pytest.raises(InvalidOperationError, match="eager"):
        nw.concat_unified_categoricals([df.lazy()])  # type: ignore[list-item]


def test_mismatched_columns() -> None:
    categorical = make_pandas_frame(["low"], ["low", "high"])
    strings = nw.from_native(pd.DataFrame({"status": ["low"]}), eager_only=True)
    with pytest.raises(InvalidOperationError, match="non-categorical"):
        nw.categorical_unification([categorical, strings])

    missing = nw.from_native(pd.DataFrame({"other": ["low"]}), eager_only=True)
    with pytest.raises(InvalidOperationError, match="missing from input 1"):
        nw.categorical_unification([categorical, missing])


def test_non_string_categories() -> None:
    df = nw.from_native(
        pd.DataFrame({"status": pd.Categorical([1, 2], categories=[1, 2, 3])}),
        eager_only=True,
    )
    with pytest.raises(InvalidOperationError, match="string labels"):
        nw.categorical_unification([df])


def test_unsupported_backend() -> None:
    pl = pytest.importorskip("polars")
    df = nw.from_native(
        pl.DataFrame({"status": pl.Series(["a"], dtype=pl.Categorical)}), eager_only=True
    )
    with pytest.raises(NotImplementedError, match="pandas-like"):
        nw.categorical_unification([df])
