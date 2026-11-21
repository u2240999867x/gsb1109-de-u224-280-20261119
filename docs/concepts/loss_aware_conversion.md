# Loss-aware conversion

Narwhals' [`convert`](../api-reference/convert.md) helper applies one declared
cleaning rule across pandas, PyArrow, and lazy backends. Unlike `cast`, it
keeps the successfully converted values and reports every rejected cell.

```python exec="yes" source="above" session="convert"
import narwhals as nw

rule = nw.convert.ConversionRule(
    {"amount": nw.Decimal(5, 2)},
    source_id="record_id",
)
rule
```

The rule is validated before any data is read: only fixed-width integers
(`Int8` through `Int64`, `UInt8` through `UInt64`) and instantiated
`nw.Decimal(precision, scale)` targets are accepted. An unsupported target or
source dtype raises while planning the conversion.

## Results

Applying the rule to an eager frame returns a
[`ConversionResult`](../api-reference/convert.md):

```python exec="yes" source="above" session="convert"
import pandas as pd

df = nw.from_native(
    pd.DataFrame(
        {
            "record_id": ["a", "b", "c", "d"],
            "amount": ["1.20", "1.234", "oops", None],
        }
    ).convert_dtypes(dtype_backend="pyarrow")
)
result = nw.convert.convert(df, rule)
result.values.to_dict(as_series=False)
```

`values` keeps the frame shape and replaces rejected cells with null. The
original null is not treated as a failure:

```python exec="yes" source="above" session="convert"
result.failures.to_dict(as_series=False)
```

Each failure names the business record, the affected source column, the declared
reason, the target dtype, and the original value. A row which fails in two
columns appears twice, once per column. Because the record id travels with the
evidence, duplicate pandas index labels cannot merge two distinct business rows.

## Lazy frames

Applying the rule to a lazy frame returns a `LazyConversionResult`. Its
`collect_schema` and `failure_schema` are computed from the declared plan; they
do not read data.

```python exec="yes" source="above" session="convert"
import polars as pl

lazy = nw.from_native(pl.LazyFrame({"record_id": ["a"], "amount": ["1.20"]}))
lazy_result = nw.convert.convert(lazy, rule)
lazy_result.collect_schema()
```

Calling `collect` materializes both `values` and `failures`. Only then does the
underlying engine read the source rows.
