# ETLHelper numeric boundary diagnosis

Run from the repository root with:

```bash
.venv/bin/python diagnostics/numeric_boundary_probe.py
```

Ledger input: `diagnostics/ledger.csv`  
Library under test: `etlhelper.copy_rows` (unchanged)  
Database dependency: none; source/target DBAPI objects are recording stand-ins.

## Ledger

| id | raw text | Decimal value | 2dp display | note |
| --- | --- | --- | --- | --- |
| R-ROUND | 2.346 | 2.346 | 2.35 | same 2 dp display as 2.35; separates source pre-rounding from target-only rounding |
| R-FLOAT-1 | 9007199254740993.01 | 9007199254740993.01 | 9007199254740993.01 | beyond float's exact integer range; differs from R-FLOAT-2 only in the last decimal place |
| R-FLOAT-2 | 9007199254740993.02 | 9007199254740993.02 | 9007199254740993.02 | beyond float's exact integer range; differs from R-FLOAT-1 only in the last decimal place |

## Boundary order

1. `dbapi_source` - raw text from the ledger/source DBAPI.
2. `source_cursor` - the object the source cursor hands to ETLHelper.
3. `row_factory` - ETLHelper's `dict_row_factory` output.
4. `callback` - the existing transform callback output.
5. `bind` - the same object ETLHelper passes to the target cursor.
6. `target_store` - the target column's own 2dp conversion.
7. `target_readback` - a separate read of the stored target value.

## Method and fidelity

- `etlhelper.copy_rows()` is called through its public API; its extract, row-factory, transform, insert-SQL and bind ordering is the library's own.
- Source and target DBAPI connections/cursors are recording stand-ins. The source stand-in parses the ledger with `Decimal` and records the exact object it returns. The target stand-in records the exact parameter ETLHelper hands to `executemany`, applies the declared target-column policy, and then exposes a separate readback.
- `DB_HELPER_FACTORY.from_conn` is temporarily mapped to ETLHelper's own `SQLiteDbHelper` so the unmodified `copy_rows` gets a cursor context manager and named-parameter style. That helper performs no rounding; all rounding is in the labelled stand-in boundaries.
- `target_store` models the stated target-column policy (`HALF_UP` to two places). It is not a claim about any particular database's implicit cast rules.

A type change is not automatically a value loss: a `float` that happens to retain the same decimal text is marked `yes` for text recovery but `NO` for exact value. The exact object type is always recorded.

## Scenario paths

| scenario | role | path |
| --- | --- | --- |
| A-SRC-ROUND | required copy 1 | source_cursor: HALF_UP to 0.01 -> callback identity -> target column: HALF_UP to 0.01 |
| B-TGT-ROUND | required copy 2 | source_cursor: exact Decimal -> callback identity -> target column: HALF_UP to 0.01 |
| R-FLOAT-ROUND | ambiguity probe | source_cursor: exact Decimal -> callback float -> target column: HALF_UP to 0.01 |
| P-DECIMAL | large pair, Decimal retained | source_cursor: exact Decimal -> callback identity -> target column: HALF_UP to 0.01 |
| P-FLOAT | large pair, float callback | source_cursor: exact Decimal -> callback float -> target column: HALF_UP to 0.01 |

## Observations

### A-SRC-ROUND

Source cursor already rounded 2.346 to 2.35.

| row | boundary | Python type | true value (repr) | 2dp display | exact decimal equals input? | decimal text recovers input? | same object as previous |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R-ROUND | dbapi_source | str | `'2.346'` | 2.35 | yes | yes | - |
| R-ROUND | source_cursor | Decimal | `Decimal('2.35')` | 2.35 | **NO** | **NO** | no |
| R-ROUND | row_factory | Decimal | `Decimal('2.35')` | 2.35 | **NO** | **NO** | yes |
| R-ROUND | callback | Decimal | `Decimal('2.35')` | 2.35 | **NO** | **NO** | yes |
| R-ROUND | bind | Decimal | `Decimal('2.35')` | 2.35 | **NO** | **NO** | yes |
| R-ROUND | target_store | Decimal | `Decimal('2.35')` | 2.35 | **NO** | **NO** | no |
| R-ROUND | target_readback | Decimal | `Decimal('2.35')` | 2.35 | **NO** | **NO** | no |

### B-TGT-ROUND

Source cursor and callback kept 2.346; only the target column rounded.

| row | boundary | Python type | true value (repr) | 2dp display | exact decimal equals input? | decimal text recovers input? | same object as previous |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R-ROUND | dbapi_source | str | `'2.346'` | 2.35 | yes | yes | - |
| R-ROUND | source_cursor | Decimal | `Decimal('2.346')` | 2.35 | yes | yes | no |
| R-ROUND | row_factory | Decimal | `Decimal('2.346')` | 2.35 | yes | yes | yes |
| R-ROUND | callback | Decimal | `Decimal('2.346')` | 2.35 | yes | yes | yes |
| R-ROUND | bind | Decimal | `Decimal('2.346')` | 2.35 | yes | yes | yes |
| R-ROUND | target_store | Decimal | `Decimal('2.35')` | 2.35 | **NO** | **NO** | no |
| R-ROUND | target_readback | Decimal | `Decimal('2.35')` | 2.35 | **NO** | **NO** | no |

### R-FLOAT-ROUND

Same 2.346 reading through a float callback.

| row | boundary | Python type | true value (repr) | 2dp display | exact decimal equals input? | decimal text recovers input? | same object as previous |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R-ROUND | dbapi_source | str | `'2.346'` | 2.35 | yes | yes | - |
| R-ROUND | source_cursor | Decimal | `Decimal('2.346')` | 2.35 | yes | yes | no |
| R-ROUND | row_factory | Decimal | `Decimal('2.346')` | 2.35 | yes | yes | yes |
| R-ROUND | callback | float | `2.346` | 2.35 | **NO** | yes | no |
| R-ROUND | bind | float | `2.346` | 2.35 | **NO** | yes | yes |
| R-ROUND | target_store | Decimal | `Decimal('2.35')` | 2.35 | **NO** | **NO** | no |
| R-ROUND | target_readback | Decimal | `Decimal('2.35')` | 2.35 | **NO** | **NO** | no |

### P-DECIMAL

Both large readings stay Decimal end to end.

| row | boundary | Python type | true value (repr) | 2dp display | exact decimal equals input? | decimal text recovers input? | same object as previous |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R-FLOAT-1 | dbapi_source | str | `'9007199254740993.01'` | 9007199254740993.01 | yes | yes | - |
| R-FLOAT-1 | source_cursor | Decimal | `Decimal('9007199254740993.01')` | 9007199254740993.01 | yes | yes | no |
| R-FLOAT-1 | row_factory | Decimal | `Decimal('9007199254740993.01')` | 9007199254740993.01 | yes | yes | yes |
| R-FLOAT-1 | callback | Decimal | `Decimal('9007199254740993.01')` | 9007199254740993.01 | yes | yes | yes |
| R-FLOAT-1 | bind | Decimal | `Decimal('9007199254740993.01')` | 9007199254740993.01 | yes | yes | yes |
| R-FLOAT-1 | target_store | Decimal | `Decimal('9007199254740993.01')` | 9007199254740993.01 | yes | yes | no |
| R-FLOAT-1 | target_readback | Decimal | `Decimal('9007199254740993.01')` | 9007199254740993.01 | yes | yes | no |
| R-FLOAT-2 | dbapi_source | str | `'9007199254740993.02'` | 9007199254740993.02 | yes | yes | - |
| R-FLOAT-2 | source_cursor | Decimal | `Decimal('9007199254740993.02')` | 9007199254740993.02 | yes | yes | no |
| R-FLOAT-2 | row_factory | Decimal | `Decimal('9007199254740993.02')` | 9007199254740993.02 | yes | yes | yes |
| R-FLOAT-2 | callback | Decimal | `Decimal('9007199254740993.02')` | 9007199254740993.02 | yes | yes | yes |
| R-FLOAT-2 | bind | Decimal | `Decimal('9007199254740993.02')` | 9007199254740993.02 | yes | yes | yes |
| R-FLOAT-2 | target_store | Decimal | `Decimal('9007199254740993.02')` | 9007199254740993.02 | yes | yes | no |
| R-FLOAT-2 | target_readback | Decimal | `Decimal('9007199254740993.02')` | 9007199254740993.02 | yes | yes | no |

### P-FLOAT

Both large readings pass through a float callback.

| row | boundary | Python type | true value (repr) | 2dp display | exact decimal equals input? | decimal text recovers input? | same object as previous |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R-FLOAT-1 | dbapi_source | str | `'9007199254740993.01'` | 9007199254740993.01 | yes | yes | - |
| R-FLOAT-1 | source_cursor | Decimal | `Decimal('9007199254740993.01')` | 9007199254740993.01 | yes | yes | no |
| R-FLOAT-1 | row_factory | Decimal | `Decimal('9007199254740993.01')` | 9007199254740993.01 | yes | yes | yes |
| R-FLOAT-1 | callback | float | `9007199254740994.0` | 9007199254740994.00 | **NO** | **NO** | no |
| R-FLOAT-1 | bind | float | `9007199254740994.0` | 9007199254740994.00 | **NO** | **NO** | yes |
| R-FLOAT-1 | target_store | Decimal | `Decimal('9007199254740994.00')` | 9007199254740994.00 | **NO** | **NO** | no |
| R-FLOAT-1 | target_readback | Decimal | `Decimal('9007199254740994.00')` | 9007199254740994.00 | **NO** | **NO** | no |
| R-FLOAT-2 | dbapi_source | str | `'9007199254740993.02'` | 9007199254740993.02 | yes | yes | - |
| R-FLOAT-2 | source_cursor | Decimal | `Decimal('9007199254740993.02')` | 9007199254740993.02 | yes | yes | no |
| R-FLOAT-2 | row_factory | Decimal | `Decimal('9007199254740993.02')` | 9007199254740993.02 | yes | yes | yes |
| R-FLOAT-2 | callback | float | `9007199254740994.0` | 9007199254740994.00 | **NO** | **NO** | no |
| R-FLOAT-2 | bind | float | `9007199254740994.0` | 9007199254740994.00 | **NO** | **NO** | yes |
| R-FLOAT-2 | target_store | Decimal | `Decimal('9007199254740994.00')` | 9007199254740994.00 | **NO** | **NO** | no |
| R-FLOAT-2 | target_readback | Decimal | `Decimal('9007199254740994.00')` | 9007199254740994.00 | **NO** | **NO** | no |

## Last recoverable and first lost boundary

| scenario | row | last text-recoverable boundary | first text lost | last exact-value boundary | first exact-value lost | meaning |
| --- | --- | --- | --- | --- | --- | --- |
| A-SRC-ROUND | R-ROUND | `dbapi_source` | `source_cursor` | `dbapi_source` | `source_cursor` | Source cursor already rounded 2.346 to 2.35. |
| B-TGT-ROUND | R-ROUND | `bind` | `target_store` | `bind` | `target_store` | Source cursor and callback kept 2.346; only the target column rounded. |
| R-FLOAT-ROUND | R-ROUND | `bind` | `target_store` | `row_factory` | `callback` | Same 2.346 reading through a float callback. |
| P-DECIMAL | R-FLOAT-1 | `target_readback` | (no loss) | `target_readback` | (no loss) | Both large readings stay Decimal end to end. |
| P-DECIMAL | R-FLOAT-2 | `target_readback` | (no loss) | `target_readback` | (no loss) | Both large readings stay Decimal end to end. |
| P-FLOAT | R-FLOAT-1 | `row_factory` | `callback` | `row_factory` | `callback` | Both large readings pass through a float callback. |
| P-FLOAT | R-FLOAT-2 | `row_factory` | `callback` | `row_factory` | `callback` | Both large readings pass through a float callback. |

The text boundary is what a decimal ledger can replay. The exact boundary is stricter: `Decimal(2.346)` from a Python float is `2.345999999999999...`, even though `repr()` is `2.346`.

## Pair separation: Decimal retained vs float callback

| path | first boundary where the pair is equal | last boundary where the pair differs | values at each boundary |
| --- | --- | --- | --- |
| P-DECIMAL | (never collapsed) | `target_readback` | dbapi_source: 9007199254740993.01 / 9007199254740993.02<br>source_cursor: 9007199254740993.01 / 9007199254740993.02<br>row_factory: 9007199254740993.01 / 9007199254740993.02<br>callback: 9007199254740993.01 / 9007199254740993.02<br>bind: 9007199254740993.01 / 9007199254740993.02<br>target_store: 9007199254740993.01 / 9007199254740993.02<br>target_readback: 9007199254740993.01 / 9007199254740993.02 |
| P-FLOAT | `callback` | `row_factory` | dbapi_source: 9007199254740993.01 / 9007199254740993.02<br>source_cursor: 9007199254740993.01 / 9007199254740993.02<br>row_factory: 9007199254740993.01 / 9007199254740993.02<br>callback: 9007199254740994.0 / 9007199254740994.0<br>bind: 9007199254740994.0 / 9007199254740994.0<br>target_store: 9007199254740994.00 / 9007199254740994.00<br>target_readback: 9007199254740994.00 / 9007199254740994.00 |

## Two copies with the same target value

| scenario | source cursor value | bind parameter | 2dp display | target readback |
| --- | --- | --- | --- | --- |
| A-SRC-ROUND | `Decimal('2.35')` | `Decimal('2.35')` | 2.35 | `Decimal('2.35')` |
| B-TGT-ROUND | `Decimal('2.346')` | `Decimal('2.346')` | 2.35 | `Decimal('2.35')` |
| R-FLOAT-ROUND | `Decimal('2.346')` | `2.346` | 2.35 | `Decimal('2.35')` |

`A-SRC-ROUND` and `B-TGT-ROUND` both read back `Decimal('2.35')`: the target records are equal. They differ at `source_cursor` and at `bind`.

## ETLHelper DEBUG log view

| scenario | raw `First row` message | 2dp formatter view |
| --- | --- | --- |
| A-SRC-ROUND | `First row: {'id': 'R-ROUND', 'reading': Decimal('2.35')}` | `First row: {'id': 'R-ROUND', 'reading': 2.35}` |
| B-TGT-ROUND | `First row: {'id': 'R-ROUND', 'reading': Decimal('2.346')}` | `First row: {'id': 'R-ROUND', 'reading': 2.35}` |
| R-FLOAT-ROUND | `First row: {'id': 'R-ROUND', 'reading': 2.346}` | `First row: {'id': 'R-ROUND', 'reading': 2.35}` |
| P-DECIMAL | `First row: {'id': 'R-FLOAT-1', 'reading': Decimal('9007199254740993.01')}` | `First row: {'id': 'R-FLOAT-1', 'reading': 9007199254740993.01}` |
| P-FLOAT | `First row: {'id': 'R-FLOAT-1', 'reading': 9007199254740994.0}` | `First row: {'id': 'R-FLOAT-1', 'reading': 9007199254740994.00}` |

ETLHelper logs only `First row` per `executemany` call, so the second member of the large-reading pair is not in the log at all. The 2dp view hides A/B/R-FLOAT differences that are present in the real objects.

## What the target value alone can and cannot tell you

For `R-ROUND` with only `target readback = 2.35`, the following paths remain compatible:

- `A-SRC-ROUND`: source cursor rounded `2.346 -> 2.35`.
- `B-TGT-ROUND`: source cursor and bind kept `2.346`; target column rounded it to `2.35`.
- `R-FLOAT-ROUND` also lands on `2.35` numerically, but its callback object is a `float`, not `Decimal`.

Minimum additional observations to separate them:

1. Look at `source_cursor`: A is `Decimal('2.35')`; B and R-FLOAT are `Decimal('2.346')`. This separates A.
2. Look at the `callback` output or `bind` type: B is `Decimal('2.346')`; R-FLOAT is `float 2.346`. This separates B from the float path.

For the large pair, a target readback of `9007199254740994.00` for both rows is compatible with the float path (`P-FLOAT`) and incompatible with the Decimal path (`P-DECIMAL`). The minimum extra boundary is `row_factory` (distinct `Decimal('...993.01')` / `Decimal('...993.02')`) or `callback` (both `float 9007199254740994.0`).

## Conclusion

- Source pre-rounding: the first irreversible boundary is `source_cursor`; the raw `dbapi_source` text is the last place the original is recoverable.
- Target-only 2dp quantization: the first irreversible boundary is `target_store`; `bind` is the last place the original is recoverable.
- Float callback: for the large pair the first irreversible boundary is `callback`; `row_factory` is the last place the two inputs are distinct. For `2.346` the float's text still reads `2.346`, but its exact value is already `Decimal(2.346) = Decimal('2.346000000000000085265128291212022304534912109375')` at `callback`; the 2dp loss appears later at `target_store`. The type change is at `callback` even when the text boundary looks safe.
- Equal target records and equal 2dp log displays are therefore not sufficient evidence. Read `source_cursor`, `callback`/`bind`, and `target_readback` separately.

No ETLHelper numeric conversion policy was changed: all rounding in this report occurs in the recording stand-ins, and `copy_rows` is called through its public API.
