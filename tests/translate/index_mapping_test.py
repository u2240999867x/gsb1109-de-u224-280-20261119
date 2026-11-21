"""Tests for `from_native(..., index_mapping=...)` and evidence-based restoration."""

from __future__ import annotations

from typing import Any, cast

import pytest

pytest.importorskip("pandas")
pytest.importorskip("pyarrow")

import pandas as pd
import pyarrow as pa

import narwhals as nw
import narwhals.stable.v1 as nw_v1
import narwhals.stable.v2 as nw_v2
from narwhals.exceptions import ColumnNotFoundError

# Supplier A carries the record keys in a pandas MultiIndex: one named level
# (`batch_id`) and one unnamed level. Its ordinary columns also contain a field
# named `batch_id`, and the index values contain duplicates.
INDEX_VALUES = [("b1", 7), ("b1", 7), ("b2", 9)]


def supplier_a() -> pd.DataFrame:
    index = pd.MultiIndex.from_tuples(INDEX_VALUES, names=["batch_id", None])
    return pd.DataFrame(
        {"batch_id": ["x", "y", "z"], "value": [2.0, 2.5, 3.0], "label": ["p", "q", "r"]},
        index=index,
    )


# Supplier B carries the same business keys in ordinary (Arrow) columns, under
# different names.
def supplier_b() -> pa.Table:
    return pa.table(
        {
            "batch_code": ["b2", "b2"],
            "rec_no": [7, 8],
            "batch_id": ["u", "v"],
            "value": [4.0, 5.0],
            "label": ["s", "t"],
        }
    )


MAPPING_A = {"batch": nw.IndexLevel("batch_id"), "record": nw.IndexLevel(1)}
MAPPING_B = {"batch": nw.IndexColumn("batch_code"), "record": nw.IndexColumn("rec_no")}
CANONICAL = ["batch", "record", "batch_id", "value", "label"]


def business_table() -> nw.DataFrame[pd.DataFrame]:
    """Both suppliers conformed to the same business declaration, then concatenated."""
    frame_a = nw.from_native(supplier_a(), index_mapping=MAPPING_A).select(CANONICAL)
    frame_b = nw.from_native(supplier_b().to_pandas(), index_mapping=MAPPING_B).select(
        CANONICAL
    )
    return nw.concat([frame_a, frame_b])


def test_suppliers_conform_to_same_business_table() -> None:
    frame_a = nw.from_native(supplier_a(), index_mapping=MAPPING_A)
    frame_b = nw.from_native(supplier_b().to_pandas(), index_mapping=MAPPING_B)
    # The mapping does not pollute the business columns: exactly the declared
    # aliases are added, and no source-specific names leak through.
    assert set(frame_a.columns) == set(CANONICAL)
    assert set(frame_b.columns) == set(CANONICAL)
    assert "batch_code" not in frame_b.columns
    assert "rec_no" not in frame_b.columns


def test_end_to_end_restore_with_complete_evidence() -> None:
    product = (
        business_table()
        .select(["record", "batch", "value", "batch_id", "label"])
        .filter(nw.col("value") > 1)
        .rename({"batch": "batch_key", "record": "record_key", "label": "batch"})
    )
    result = nw.to_native(product)
    # Level order, original level names, and duplicate index values are rebuilt
    # from the current rows and the latest column names.
    assert result.index.names == ["batch_id", None]
    assert result.index.tolist() == [
        ("b1", 7),
        ("b1", 7),
        ("b2", 9),
        ("b2", 7),
        ("b2", 8),
    ]
    # The restoration key columns are absorbed back into the index...
    assert result.columns.tolist() == ["value", "batch_id", "batch"]
    # ...while the ordinary column sharing the index level's name is not
    # overwritten, and neither is the display column which took the freed name.
    assert result["batch_id"].tolist() == ["x", "y", "z", "u", "v"]
    assert result["batch"].tolist() == ["p", "q", "r", "s", "t"]


def test_projection_dropped_key_lacks_layer() -> None:
    product_full = (
        business_table()
        .select(["record", "batch", "value", "batch_id", "label"])
        .filter(nw.col("value") > 1)
        .rename({"batch": "batch_key", "record": "record_key", "label": "batch"})
    )
    # Same names and dtypes, but the restoration key was dropped by projection
    # before a display column with the same name (and, by chance, the same
    # values as the original key) was added.
    product_dropped = (
        business_table()
        .select(["record", "batch", "value", "batch_id", "label"])
        .filter(nw.col("value") > 1)
        .rename({"batch": "batch_key", "record": "record_key", "label": "batch"})
        .select(["batch_key", "value", "batch_id", "batch"])
        .with_columns(nw.new_series("record_key", [7, 7, 9, 7, 8], backend="pandas"))
        .select(["record_key", "batch_key", "value", "batch_id", "batch"])
    )
    assert product_full.columns == product_dropped.columns
    assert product_full.collect_schema() == product_dropped.collect_schema()

    result_full = nw.to_native(product_full)
    result_dropped = nw.to_native(product_dropped)

    assert result_full.index.names == ["batch_id", None]
    # The dropped layer is clearly absent, even though a column with the same
    # name and the same values as the original key is present.
    assert result_dropped.index.names == ["batch_id"]
    assert result_dropped.index.tolist() == ["b1", "b1", "b2", "b2", "b2"]
    assert result_dropped["record_key"].tolist() == [7, 7, 9, 7, 8]


def test_concat_and_filter_do_not_copy_old_index() -> None:
    frame = nw.from_native(supplier_a(), index_mapping=MAPPING_A)
    result = nw.to_native(frame.filter(nw.col("value") > 2))
    # The restored index reflects the current rows, not the pre-filter index.
    assert result.index.tolist() == [("b1", 7), ("b2", 9)]

    other = nw.from_native(supplier_a(), index_mapping=MAPPING_A)
    combined = nw.concat([frame, other])
    result = nw.to_native(combined)
    # Concatenation does not copy the old positional index over the records.
    assert result.index.tolist() == INDEX_VALUES + INDEX_VALUES


def test_plain_from_native_to_native_unchanged() -> None:
    native = supplier_a()
    result = nw.to_native(nw.from_native(native))
    # Without a mapping, the index is left exactly as it was.
    assert result.index.names == ["batch_id", None]
    assert result.index.tolist() == INDEX_VALUES
    assert result.columns.tolist() == ["batch_id", "value", "label"]


def test_arrow_column_mapping_roundtrip() -> None:
    table = supplier_b()
    frame = nw.from_native(table, index_mapping=MAPPING_B)
    assert frame.columns == ["batch", "record", "batch_id", "value", "label"]
    result = nw.to_native(frame)
    # Column-carrying sources get their original column names back.
    assert result.column_names == ["batch_code", "rec_no", "batch_id", "value", "label"]
    # The input table was not mutated.
    assert table.column_names == ["batch_code", "rec_no", "batch_id", "value", "label"]


def test_polars_column_mapping_roundtrip() -> None:
    pl = pytest.importorskip("polars")
    frame = nw.from_native(
        pl.DataFrame({"rec_no": [7, 8], "batch_code": ["b1", "b2"], "value": [1.0, 2.0]}),
        index_mapping=MAPPING_B,
    )
    assert frame.columns == ["record", "batch", "value"]
    result = nw.to_native(frame)
    assert result.columns == ["rec_no", "batch_code", "value"]


def test_missing_explicit_alias_conflicts() -> None:
    # A named level whose default alias collides with an ordinary column.
    with pytest.raises(ValueError, match="duplicate column name"):
        nw.from_native(supplier_a(), index_mapping=[nw.IndexLevel("batch_id")])
    # An unnamed level cannot provide a default alias at all.
    with pytest.raises(ValueError, match="explicit alias is required"):
        nw.from_native(supplier_a(), index_mapping=[nw.IndexLevel(1)])


def test_rename_follows_keys_and_freed_name_is_not_evidence() -> None:
    frame = nw.from_native(supplier_a(), index_mapping=MAPPING_A)
    renamed = frame.rename({"batch": "batch_key", "label": "batch"})
    result = nw.to_native(renamed)
    # The evidence follows the renamed key columns; the display column now
    # named `batch` is not treated as a key.
    assert result.index.names == ["batch_id", None]
    assert result.index.tolist() == INDEX_VALUES
    assert result["batch"].tolist() == ["p", "q", "r"]


def test_with_columns_overwrite_loses_evidence() -> None:
    frame = nw.from_native(supplier_a(), index_mapping=MAPPING_A)
    result = nw.to_native(frame.with_columns(nw.lit("?").alias("batch")))
    # The overwritten key can no longer serve as evidence; the surviving
    # (unnamed) layer is still restored.
    assert result.index.names == [None]
    assert result.index.tolist() == [7, 7, 9]
    assert result["batch"].tolist() == ["?", "?", "?"]


def test_drop_and_getitem_lose_evidence() -> None:
    frame = nw.from_native(supplier_a(), index_mapping=MAPPING_A)
    result = nw.to_native(frame.drop("record"))
    assert result.index.names == ["batch_id"]
    result = nw.to_native(frame[["batch", "value"]])
    assert result.index.names == ["batch_id"]
    assert result.columns.tolist() == ["value"]


def test_concat_with_unmapped_frame_does_not_restore() -> None:
    frame = nw.from_native(supplier_a(), index_mapping=MAPPING_A).select(CANONICAL[:4])
    unmapped = nw.from_native(
        pd.DataFrame(
            {"batch": ["b3"], "record": [1], "batch_id": ["w"], "value": [9.0]}
        )
    )
    result = nw.to_native(nw.concat([frame, unmapped]))
    assert result.index.tolist() == [0, 1, 2, 0]


def test_concat_incompatible_mappings_raises() -> None:
    frame_a = nw.from_native(supplier_a(), index_mapping=MAPPING_A).select(
        ["batch", "batch_id"]
    )
    frame_b = nw.from_native(
        pd.DataFrame({"value": [1.0], "batch_id": ["q"]}),
        index_mapping={"batch": nw.IndexColumn("value")},
    ).select(["batch", "batch_id"])
    with pytest.raises(ValueError, match="incompatible `index_mapping`"):
        nw.concat([frame_a, frame_b])


def test_input_is_not_mutated() -> None:
    native = supplier_a()
    nw.from_native(native, index_mapping=MAPPING_A)
    assert native.columns.tolist() == ["batch_id", "value", "label"]
    assert native.index.tolist() == INDEX_VALUES


def test_stable_namespaces_reject_index_mapping() -> None:
    for version in (nw_v1, nw_v2):
        from_native = cast(Any, version.from_native)
        with pytest.raises(TypeError, match="index_mapping"):
            from_native(supplier_a(), index_mapping=MAPPING_A)


def test_invalid_mappings_raise() -> None:
    native = supplier_a()
    with pytest.raises(ValueError, match="No index level named 'nope'"):
        nw.from_native(native, index_mapping={"k": nw.IndexLevel("nope")})
    with pytest.raises(ValueError, match="out of range"):
        nw.from_native(native, index_mapping={"k": nw.IndexLevel(5)})
    with pytest.raises(ColumnNotFoundError, match="'nope'"):
        nw.from_native(native, index_mapping={"k": nw.IndexColumn("nope")})
    with pytest.raises(ValueError, match="aliases must be unique"):
        nw.from_native(
            native, index_mapping=[nw.IndexColumn("value"), nw.IndexColumn("value")]
        )
    with pytest.raises(TypeError, match="nw.IndexLevel"):
        nw.from_native(native, index_mapping={"k": "batch_id"})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="series_only"):
        nw.from_native(native, index_mapping=MAPPING_A, series_only=True)  # type: ignore[call-overload]


def test_index_level_on_arrow_raises() -> None:
    with pytest.raises(TypeError, match="pandas-like"):
        nw.from_native(supplier_b(), index_mapping={"k": nw.IndexLevel(0)})


def test_lazy_frame_raises() -> None:
    pl = pytest.importorskip("polars")
    with pytest.raises(TypeError, match="only supported for eager"):
        nw.from_native(pl.LazyFrame({"a": [1]}), index_mapping={"k": nw.IndexColumn("a")})


def test_pandas_column_mapping_roundtrip() -> None:
    frame = nw.from_native(supplier_b().to_pandas(), index_mapping=MAPPING_B)
    result = nw.to_native(frame)
    assert result.columns.tolist() == [
        "batch_code",
        "rec_no",
        "batch_id",
        "value",
        "label",
    ]


def test_identity_alias_is_a_noop_rename() -> None:
    native = pd.DataFrame({"value": [1, 2], "other": [3.0, 4.0]})
    frame = nw.from_native(native, index_mapping={"value": nw.IndexColumn("value")})
    assert frame.columns == ["value", "other"]
    assert nw.to_native(frame).columns.tolist() == ["value", "other"]

    table = pa.table({"value": [1, 2], "other": [3.0, 4.0]})
    frame = nw.from_native(table, index_mapping={"value": nw.IndexColumn("value")})
    assert nw.to_native(frame).column_names == ["value", "other"]


def test_negative_level_position() -> None:
    frame = nw.from_native(supplier_a(), index_mapping={"record": nw.IndexLevel(-1)})
    result = nw.to_native(frame)
    assert result.index.names == [None]
    assert result.index.tolist() == [7, 7, 9]


def test_all_keys_lost_returns_plain_native() -> None:
    frame = nw.from_native(supplier_a(), index_mapping=MAPPING_A)
    result = nw.to_native(frame.select(["value", "batch_id"]))
    assert result.index.tolist() == [0, 1, 2]

    arrow_frame = nw.from_native(supplier_b(), index_mapping=MAPPING_B)
    result_arrow = nw.to_native(arrow_frame.select(["value", "batch_id"]))
    assert result_arrow.column_names == ["value", "batch_id"]

    pl = pytest.importorskip("polars")
    polars_frame = nw.from_native(
        pl.DataFrame({"rec_no": [7], "value": [1.0]}),
        index_mapping={"record": nw.IndexColumn("rec_no")},
    )
    assert nw.to_native(polars_frame.select("value")).columns == ["value"]


def test_select_with_col_expressions() -> None:
    frame = nw.from_native(supplier_a(), index_mapping=MAPPING_A)
    # Bare `nw.col` selections keep the evidence.
    result = nw.to_native(
        frame.select(nw.col("batch"), nw.col("record"), "value", "batch_id", "label")
    )
    assert result.index.names == ["batch_id", None]
    # A computed expression reusing a key's name is not evidence.
    result = nw.to_native(
        frame.select((nw.col("batch").first()).alias("batch"), "record", "value")
    )
    assert result.index.names == [None]


def test_with_columns_variants() -> None:
    frame = nw.from_native(supplier_a(), index_mapping=MAPPING_A)
    # Identity re-selections keep the evidence.
    kept = nw.to_native(frame.with_columns("batch", nw.col("record")))
    assert kept.index.names == ["batch_id", None]
    # Overwriting via keyword loses the evidence for that key.
    lost = nw.to_native(frame.with_columns(batch=nw.lit("?")))
    assert lost.index.names == [None]
    # Overwriting via a computed expression loses the evidence for that key.
    lost = nw.to_native(frame.with_columns(nw.col("record") + 1))
    assert lost.index.names == ["batch_id"]
    # Literals can't be told apart from key overwrites, so the evidence is
    # conservatively dropped.
    lost = nw.to_native(frame.with_columns(nw.lit(1)))
    assert lost.index.tolist() == [0, 1, 2]
    # Same for a Series whose name isn't a string.
    series = nw.from_native(pd.Series([1, 2, 3], name=0), series_only=True)
    lost = nw.to_native(frame.with_columns(series))
    assert lost.index.tolist() == [0, 1, 2]


def test_rename_after_loss_keeps_loss() -> None:
    frame = nw.from_native(supplier_a(), index_mapping=MAPPING_A)
    result = nw.to_native(frame.drop("record").rename({"batch": "batch_key"}))
    assert result.index.names == ["batch_id"]


def test_concat_merges_column_mappings() -> None:
    frame_b = nw.from_native(supplier_b().to_pandas(), index_mapping=MAPPING_B).select(
        CANONICAL
    )
    result = nw.to_native(nw.concat([frame_b, frame_b]))
    assert "batch_code" in result.columns
    assert "rec_no" in result.columns


def test_concat_with_key_lost_in_one_frame() -> None:
    frame = nw.from_native(supplier_a(), index_mapping=MAPPING_A).select(CANONICAL)
    overwritten = frame.with_columns(nw.lit(0).alias("record"))
    result = nw.to_native(nw.concat([frame, overwritten]))
    # The key whose evidence was lost in one input stays lost.
    assert result.index.names == ["batch_id"]


def test_column_mapped_twice_raises() -> None:
    native = pd.DataFrame({"value": [1, 2]})
    with pytest.raises(ValueError, match="mapped more than once"):
        nw.from_native(
            native,
            index_mapping={"a": nw.IndexColumn("value"), "b": nw.IndexColumn("value")},
        )
    with pytest.raises(ValueError, match="mapped more than once"):
        nw.from_native(
            pa.table({"value": [1, 2]}),
            index_mapping={"a": nw.IndexColumn("value"), "b": nw.IndexColumn("value")},
        )


def test_non_string_alias_raises() -> None:
    with pytest.raises(TypeError, match="aliases must be strings"):
        nw.from_native(supplier_a(), index_mapping={1: nw.IndexLevel(0)})  # type: ignore[dict-item]


def test_missing_column_on_arrow_raises() -> None:
    with pytest.raises(ColumnNotFoundError, match="'nope'"):
        nw.from_native(supplier_b(), index_mapping={"k": nw.IndexColumn("nope")})


def test_level_mapped_twice_raises() -> None:
    with pytest.raises(ValueError, match="mapped more than once"):
        nw.from_native(
            supplier_a(),
            index_mapping={"a": nw.IndexLevel(0), "b": nw.IndexLevel("batch_id")},
        )
