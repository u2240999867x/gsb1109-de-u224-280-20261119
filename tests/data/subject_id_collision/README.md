# Subject ID collision probe

A minimal dataset for investigating why some reports merge subjects `007` and
`7` into a single identifier while others keep them apart.

## Layout

```
dataset_description.json
code/
  subject_str.json        # subject pattern is identical to the int variant,
  subject_int.json        #   only dtype differs: str vs int
sub-007/anat/...          # raw root, default bids config (subject -> str)
sub-7/anat/...
sub-17/anat/...           # distractor: proves an unanchored "7" over-matches
derivatives/strproc/      # subject captured with dtype="str"
derivatives/intproc/      # subject captured with dtype="int"
```

Every root contains the same three subject labels and a tiny `T1w` pair
(`.nii.gz` + `.json`).  The JSON sidecars exist so `BIDSReport` can be run
without touching real imaging data.

The two configs are identical except for `entities[0].dtype`.  They live under
`code/` because that directory is ignored by the indexer.

## Replay

```bash
.venv/bin/python -m pytest src/bids/layout/tests/test_subject_id_collision.py -q
```

The test module wires the derivatives up explicitly:

```python
from bids import BIDSLayout

root = 'tests/data/subject_id_collision'
parent = BIDSLayout(root, validate=False, config=root + '/code/subject_str.json')
for name in ('strproc', 'intproc'):
    parent.derivatives[name] = BIDSLayout(
        root + '/derivatives/' + name,
        is_derivative=True,
        config=root + '/code/subject_' + ('int' if name == 'intproc' else 'str') + '.json',
        sources=parent,
    )
```

`derivatives=True` cannot be used for this: the indexer always loads each
derivative directory's own `layout_config.json`, so two different dtypes for
the same entity raise `UNIQUE constraint failed: entities.name` at index time.

## What reproduces, and where

| Probe | strproc | intproc |
| --- | --- | --- |
| `get(subject='007')` files | `sub-007` | `sub-007` + `sub-7` |
| `get(subject='7')` files | `sub-7` | `sub-007` + `sub-7` |
| `get(subject='^007$', regex_search=True)` | `sub-007` | `sub-007` |
| `get(subject='7', regex_search=True)` | `007`,`7`,`17` | `007`,`7`,`17` |
| `Tag._dtype` | `str` | `int` |
| `Tag._value` | `'007'`,`'7'`,`'17'` | `'007'`,`'7'`,`'17'` |
| restored value | `str` | `PaddedInt` |

The int root folds `007` and `7` at the SQL layer:

```sql
-- strproc
... AND tags_1._value IN (?)
-- intproc
... AND CAST(tags_1._value AS INTEGER) IN (?)
```

with bindings `('subject', '007')` vs `('subject', 007)` (the latter is a
`PaddedInt`).  SQLite compares the *integer* parameter against the cast column,
so both `'007'` and `'7'` match.  The regex branch emits

```sql
... AND (tags_1._value REGEXP ?)
```

for both roots and binds the original `'^007$'` / `'7'` string, so the int root
never applies integer equality on that path.

## Summary / dedup behaviour

`return_type='id'` collects matched subject values and then runs
`natural_sort(list(set(results)))`.  `PaddedInt` hashes like `int`, so
`PaddedInt('007')` and the *integer* `7` collapse, but `PaddedInt('007')` and
the *string* `'007'` do not.  Consequences:

- exact `007` over all scopes returns 4 imaging files (int root contributes 2)
  but summarizes to 2 ids: `007` (`PaddedInt`) and `'007'` (`str`);
- unanchored `7` over all scopes returns 9 imaging files yet summarizes to 5
  ids (`007`, `'007'`, `'7'`, `'17'`, `17`);
- swapping the two roots' load order leaves every file result unchanged, but
  the order of the id list can change because `_get_layouts_in_scope` returns
  `list(set(layouts))`;
- `BIDSReport.generate()` keys on those ids, so `007`/`7` string ids and the
  int id produce identical per-subject file sets; five ids collapse to two
  report texts.  `generate_from_files()` has no id key and raises `TypeError`
  while sorting `PaddedInt` against `str`.

## Save / reopen

Only one configuration survives a round-trip:

- Parent records explicit derivative paths
  (`BIDSLayout(root, derivatives=[...])`): `save()` writes child indexes to
  `<db>/<pipeline>/`, reopening reuses them, the int dtype is preserved.
- Parent records `derivatives=True`: `save()` still writes `<db>/<pipeline>/`,
  but reopening looks for `<db>/derivatives/<pipeline>/`, finds nothing, and
  re-indexes each child with the default config, turning every subject back
  into `str`.  This combination is *not* reproducible from the existing
  database.
- `reset_database=True` rebuilds in place and overwrites the typed child
  indexes with whatever config the new call provides.

Capture a typed database before any reset (or copy it outside the dataset) if
you need to re-run the diagnostics; once reset re-indexes a child, the original
`Tag._dtype` values are gone and cannot be recovered from that file.

Also note `save()` into a non-empty database raises
`OperationalError: table associations already exists`; use a fresh directory.
