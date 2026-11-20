# Auditing Snapshots

The `audit` action compares two Data Package snapshots and reports the changes
that can be replayed against a warehouse by business key. It is intended for
day-over-day reconciliation, not for validating a single package.

```python
from frictionless import audit

report = audit("day-1", "day-2", declaration="audit.json")
```

```bash
frictionless audit day-1 day-2 --declaration audit.json --output report.json
```

## Declaration

The caller declares the correspondence between the two versions. Every column
has a *logical* business name, the source column on each side, and the rule used
to compare values.

```json
{
  "tables": {
    "device": {
      "old": {"resource": "device", "mode": "natural"},
      "new": {"resource": "device", "mode": "surrogate"},
      "key": ["deviceNo"],
      "columns": {
        "deviceNo": {
          "old": "device_no",
          "new": "asset_code",
          "normalize": {
            "old": ["strip", "upper"],
            "new": ["strip", "upper", {"replace": {"pattern": "[^A-Z0-9]+", "with": "-", "literal": false}}]
          },
          "compare": "source"
        },
        "status": {
          "old": "status",
          "new": "state",
          "normalize": ["strip", "lower"],
          "compare": "normalized"
        }
      },
      "references": [
        {"holding": "maintenance", "column": "deviceNo"}
      ]
    }
  }
}
```

- `key` lists the logical columns that form the business key. The same logical
  key may be stored under different physical columns on each side, which is how
  a natural key on one day and a surrogate key on the next are related.
- `mode` records how each version uses its key: `natural`, `surrogate`, or
  `pure`. It is descriptive; the correspondences in `columns` drive the audit.
- `normalize` may be a single list applied to both sides, or an `{"old", "new"}`
  mapping when the two versions need different folding.
- `compare` is `source` (lexical equality) or `normalized` (equality after the
  declared normalization). A difference that the declaration folds away is
  never reported; a difference the declaration keeps is always reported.
- `references` is declared on the *target* table and names the holding table
  and its logical column. The report then links each referencing row to the
  audited state of the key it points at.

## Coverage

A snapshot may only cover part of a source key range. Coverage is read from the
package's `x-audit.coverage` (or `audit.coverage`) metadata for each resource,
and can also be given in the declaration:

```json
"scope": {
  "old": {"complete": true},
  "new": {"complete": false, "segments": [{"from": "D-001", "to": "D-006"}]}
}
```

A deletion is only reported when the key is inside the *new* snapshot's
verified coverage. A key outside the covered segments produces a `suppressed`
record with the source row as evidence instead of a delete. A table declared
`"stats": true` has no such dependency and always reports its own changes.

## Report

Each entry in `tables[].changes` is one of `add`, `delete`, or `update`, and
carries:

- `key` — the normalized token, the logical key values, and the raw values.
- `changed` — per logical column, the old/new values and the comparison basis.
- `evidence` — the physical row number, resource, path, and full row values on
  each side.
- `columns` — the column correspondence and normalization used for that table.

When two distinct source rows collapse onto one normalized key, the key is
reported as `unresolved` with `reason: "duplicate-key"`. Both candidate rows are
kept as evidence and the key is not turned into an update. Referencing rows that
point at that key are reported in `references` with `state: "conflict"`.

Changes are compared by business key, so reordering either table does not alter
the report apart from the physical `rowNumber` pointers.
