# Data Package versions

This page documents how the framework resolves and handles the Data Package
standard versions (v1 and v2).

## Declaring a version

A descriptor declares its version through `$schema`, a JSON Schema profile URL:

```python
package = Package({
    "$schema": "https://datapackage.org/profiles/2.0/datapackage.json",
    "name": "package",
    # ... v2 features are accepted from here on
})
```

The standard profiles are hosted at `https://datapackage.org/profiles/{1.0,2.0}/{datapackage,dataresource,tableschema,tabledialect}.json`.

## Resolution rules

The version of a descriptor is resolved once, top-down, at
descriptor-validation time. In accordance with the specification, only the
root object is expected to have a `$schema` property; all child objects
inherit the resolved version of the root.

The resolved version depends on the descriptor's own declaration:
   - if its `$schema` is a standard `datapackage.org` profile URL, the
     version is resolved to the version in its URL (v1 or v2);
   - if its `$schema` is a custom profile URL, the version resolves to
     `"unknown"`: validation will be lenient and accept a **superset of v1
     and v2** (instead of guessing a standard version);
   - an absent `$schema` is an explicit v1, as mandated by the spec.

| Descriptor state                     | Resolved version        |
| ---                                  | ---                     |
| Under a v1/v2/unknown ancestor       | the ancestor's version  |
| `$schema` standard profile URL       | v1 or v2 (from the URL) |
| `$schema` custom URL                 | unknown (superset)      |
| `$schema` absent                     | v1                      |

### Version gates

v2-specific features apply on explicit v2 version or when version is unknown.
These features may raise errors or be ignored however when validating with
unambiguous v1 version. See table below for examples of behavior changes.

| Feature | v1 | v2 / unknown |
| --- | --- | --- |
| `name` restricted to the v1 pattern | enforced | any string |
| `missingValues` object form `{value, label?}` | rejected | accepted |
| `exclusiveMinimum` / `exclusiveMaximum` | rejected | accepted |

## Detached children keep the inherited version

`Resource` and `Schema` store the version they resolved at import time (a
private `_datapackage_version`). This stored version is threaded top-down at
import, and seeded back into every detached validation, so that:

- `package.validate()` (which validates each resource standalone) agrees with `package.validate_descriptor()`;
- a resource extracted from a package, a `to_copy()` copy, or a transform pipeline step keeps the semantics of its tree instead of re-resolving positionally;
- a path-referenced schema lazily loaded by a resource inherits the resource's version.

Exports stay canonical: by default, exporting the descriptor of children and
reimporting them will change the behavior. The absent `$schema` property of the
children is interpreted as v1 at re-import. Exporting with `upgrade=True`
produces however a standalone descriptor of a child (see below).

## Upgrading to v2

`upgrade()` returns a new instance (the original is untouched) whose descriptor tree is transformed into the v2 form — also available as an export option: `to_descriptor(upgrade=True)`.

- the root gets `$schema` (as its first property) set to the canonical v2 profile of the class (`Package` → `datapackage.json`, `Resource` → `dataresource.json`, `Schema` → `tableschema.json`, `Dialect` → `tabledialect.json`);
- `$schema` is root-only: a child `$schema` is removed (it would be ignored by the inheritance rules anyway);
- the legacy `profile` property is migrated: a custom profile URL is retained
  in `$schema`; `"tabular-data-resource"` becomes `"type": "table"`; other
  values are dropped.

A `$schema` or `profile` pointing to a **custom profile URL is kept as-is**:
an automatic upgrade of a custom profile is currently not supported. Its
upgrade is up to its author. As a consequence, an upgraded custom profile
resolves to `"unknown"` (permissive).

```python
package = Package("package-v1.json")
v2_package = package.upgrade()  # a new, v2 instance
```

## Exporting to a legacy (v1) repository

A v1 repository only understands plain string `missingValues` entries: the v2
object form `{value, label?}` cannot be written into a v1 descriptor, and a
label carries the *reason* of a missing value (e.g. "not measured" vs
"downtime"), which feeds different downstream statistics and must never be
rewritten into another reason. `LegacyExporter` performs a lossless split:

- the plain string tokens stay in `missingValues`, so the v1 reader keeps
  distinguishing real values from missing ones exactly as the v2 descriptor
  does — a cell is missing if and only if its string is one of the tokens;
- the labels move to an `x-missingValueLabels` side-car (a custom property a
  v1 reader ignores), at schema scope and on each field declaring its own
  `missingValues`, keeping every reason recoverable per token and therefore
  per source row;
- rows are written as plain strings: missing inputs are written back as their
  declared token, and a missing input on a required field is additionally
  kept as a traceable rejection record carrying the original reason.

```python
from frictionless import Schema
from frictionless import LegacyExporter

schema = Schema.from_descriptor("schema-v2.json")
exporter = LegacyExporter(schema=schema)
legacy_descriptor = exporter.to_descriptor()  # plain strings + reason side-car
rows = exporter.write_rows(source_rows)
```

The delivered descriptor carries no `$schema` (an absent `$schema` is itself
an explicit v1), reopens unchanged in a v1 repository, and keeps the same
missing and required judgments as the source.

A configured legacy token set is accepted only when it recognizes exactly the
same cells as the v2 descriptor. Any extra token — for instance a legacy
missing word that would turn a real device number into a missing value — or
any dropped token, which would resurrect a documented missing value, is a
conflict: the exporter raises `FrictionlessException` with one reason per
conflict and delivers neither a descriptor nor data, instead of silently
changing the true/missing judgments.

```python
LegacyExporter(
    schema=schema,
    field_missing_values={"device": ["", "9999"]},  # 9999 is a real device number
).to_descriptor()  # raises: the valid value would vanish
```
