# What about the pandas Index?

There are two types of pandas users:

- The ones who make full use of the Index's power.
- The `.reset_index(drop=True)` ones, who would rather not think about the Index.

Narwhals aims to accommodate both!

- If you'd rather not think about the Index, then don't
  worry: it's not part of the Narwhals public API, and you'll never have to worry about
  resetting the index or about pandas doing funky index alignment for you.
- If you want your library to cater to Index powerusers who would be very angry if you reset
  their beautiful Index on their behalf, then don't worry: Narwhals makes certain promises
  with regards to the Index.

Let's learn about what Narwhals promises.

## 1. Narwhals will preserve your index for common dataframe operations

```python exec="yes" source="above" session="ex1"
import narwhals as nw
from narwhals.typing import IntoDataFrameT, IntoLazyFrameT


def my_func(df: IntoDataFrameT | IntoLazyFrameT) -> IntoDataFrameT | IntoLazyFrameT:
    df = nw.from_native(df)
    df = df.with_columns(a_plus_one=nw.col("a") + 1)
    return nw.to_native(df)
```

Let's start with a dataframe with an Index with values `[7, 8, 9]`.

```python exec="yes" source="material-block" result="python" session="ex1"
import pandas as pd

df = pd.DataFrame({"a": [2, 1, 3], "b": [3, 5, -3]}, index=[7, 8, 9])
print(my_func(df))
```

Note how the result still has the original index - Narwhals did not modify
it. Narwhals will preserve your original index for most common dataframe
operations. However, Narwhals will _not_ preserve the original index for
`DataFrame.group_by`, because there, overlapping index and column names
raise errors.

## 2. Index alignment follows the left-hand-rule

pandas automatically aligns indices for users. For example:

```python exec="yes" source="above" session="ex2"
import pandas as pd

df_pd = pd.DataFrame({"a": [2, 1, 3], "b": [4, 5, 6]})
s_pd = df_pd["a"].sort_values()
df_pd["a_sorted"] = s_pd
```

Reading the code, you might expect that `'a_sorted'` will contain the
values `[1, 2, 3]`.

**However**, here's what actually happens:

```python exec="yes" source="material-block" session="ex2" result="python"
print(df_pd)
```

In other words, pandas' index alignment undid the `sort_values` operation!

Narwhals, on the other hand, preserves the index of the left-hand-side argument.
Everything else will be inserted positionally, just like Polars would do:

```python exec="yes" source="material-block" session="ex2" result="python"
import narwhals as nw

df = nw.from_native(df_pd)
s = nw.from_native(s_pd, allow_series=True)
df = df.with_columns(a_sorted=s.sort())
print(nw.to_native(df))
```

If you keep these two rules in mind, then Narwhals will both help you avoid
Index-related surprises whilst letting you preserve the Index for the subset
of your users who consciously make great use of it.

## 3. Explicitly mapped keys can be restored as an Index

Some data suppliers carry business keys in the pandas (Multi)Index, others in
ordinary columns. If you declare, explicitly, where each restoration key lives,
then Narwhals can conform both kinds of sources to the same business table, and
restore the pandas Index from those keys later:

```python exec="yes" source="above" session="ex3"
import pandas as pd
import narwhals as nw

df_pd = pd.DataFrame(
    {"value": [10.0, 20.0]},
    index=pd.MultiIndex.from_tuples([("a", 1), ("a", 2)], names=["batch_id", None]),
)
df = nw.from_native(
    df_pd,
    index_mapping={"batch": nw.IndexLevel("batch_id"), "record": nw.IndexLevel(1)},
)
```

The mapped index levels become ordinary columns, so you can clean, reshape, and
concatenate just like with any other business table:

```python exec="yes" source="material-block" session="ex3" result="python"
print(df.columns)
```

Narwhals tracks the keys through operations such as `select`, `rename`,
`filter`, and `concat`. If the evidence is still complete when you convert
back, `to_native` rebuilds the index levels - in their original order, with
their original names - from the current rows and the latest column names:

```python exec="yes" source="material-block" session="ex3" result="python"
df = df.filter(nw.col("value") > 15).rename({"batch": "batch_key"})
print(nw.to_native(df))
```

If a restoration key was dropped along the way (for example by a projection),
then the corresponding layer is simply absent from the result - a column which
merely shares a name (and even values) with a dropped key is never used as
evidence. Without an `index_mapping`, `from_native`/`to_native` behave exactly
as described in the sections above.

