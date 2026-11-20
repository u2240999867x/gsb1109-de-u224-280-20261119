# Descriptor compatibility

Before replacing a Data Package descriptor with a new version, it helps to know
whether readers of one version can still use data written by the other. A
clean parse is not enough: a new `missingValues` entry can turn a string code
into a missing value, a narrowed `minimum`/`maximum` range can reject records
that used to pass, and a new nullable column can make an old reader reject the
whole table while both tables still read row by row.

The compatibility audit replays the publisher's raw records through each
allowed read/write direction and compares the effective values and the
cross-table reconciliation against the old descriptor's reading. It never
modifies or publishes the descriptors.

## Contract

```json
{
  "name": "equipment-maintenance",
  "old": {"package": {"resources": []}},
  "new": {"package": {"$schema": "https://datapackage.org/profiles/2.0/datapackage.json", "resources": []}},
  "directions": ["old-write/new-read", "new-write/old-read"],
  "records": {
    "equipment": [
      {"id": "eq-9999", "values": {"device_code": "9999", "device_name": "Pump A"}}
    ]
  },
  "domains": {
    "equipment": {"device_code": {"values": ["9999", "1000"]}}
  },
  "customConstraints": [
    {
      "table": "equipment",
      "field": "device_code",
      "constraint": "externalAssetRegistry",
      "custom": true,
      "covered": false,
      "values": null
    }
  ]
}
```

- `old` / `new` accept an inline package descriptor or a path to one. A
  resource may carry only a schema: the audit uses the schemas and foreign
  keys, and adds the replay data itself.
- `directions` lists the allowed combinations: `old-write/new-read`,
  `new-write/old-read`, `old-write/old-read`, and `new-write/new-read`. The
  default is the two cross-version directions. The baseline is always the old
  descriptor's reading, so `new-write/new-read` still reports where the new
  schema changes the old valid values.
- `records` are the raw cells, kept verbatim. An omitted field is `absent`,
  not an explicitly blank cell; the report calls this out.
- `domains` declare the actual values and ranges the publisher supplied.
- `customConstraints` are constraints the standard evidence does not cover. A
  custom constraint is only certified when `covered` is true and `values`
  (or `domain`) declares the domain used to establish coverage; a list is an
  enumeration, a dict a value/range declaration. Otherwise the audit reports
  that compatibility cannot be certified from these inputs.

## Report

Each direction reports four outcomes per record:

- `rejected`: the writer or reader raised an error, with the original record,
  the wire cells, and the actual header/row/write errors.
- `changed`: the record reads, but an effective value changed (e.g. a valid
  `9999` key becomes missing) or the cross-table reconciliation changed (e.g.
  a `linked` relation becomes `unassociated`). A column the schema adds is
  reported as `fieldEvidence` (e.g. `field-added`), not as a value change.
  Write-side rewrites are kept separately in `writeChanges`: declaring
  `9999` missing makes a writer emit the blank token even for a value that is
  still a valid key.
- `compatible`: both the values and the relation outcome are preserved.
- `not-certifiable`: the record is clean, but a custom constraint outside the
  supplied value domain applies.

A direction is only `certifiable` when some records are compatible and no
custom constraint is left uncovered. The certified range names the exact
records, fields, values, domains, and relationships it covers. A clean row
read with no errors does not certify compatibility on its own.

## Python

```python
from frictionless import CompatibilityAudit

report = CompatibilityAudit.from_descriptor(
    "data/compatibility/equipment-maintenance.json"
).run()
print(report.status)
print(report.to_json())
```

## Console

```bash
frictionless compatibility data/compatibility/equipment-maintenance.json
frictionless compatibility data/compatibility/equipment-maintenance.json --json
```

The command exits non-zero unless every allowed direction is compatible, so a
publisher can use it as a release gate. `--json`/`--yaml` emit the machine
readable report; `--output` writes it to a file. The report contains the
input digests and raw records, so the same inputs can reproduce the same
differences.
