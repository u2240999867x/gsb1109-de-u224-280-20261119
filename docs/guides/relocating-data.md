# Relocating Data

Sometimes a publishable dataset is not a whole source package but a few of its
business tables, gathered from several packages. The `relocate` function copies
the selected tables and the resources they depend on into a new package,
without changing the source packages.

## Selecting Tables

A source package can be selected resource by resource. Foreign keys are followed
inside the package they belong to, so a `codes` dictionary is never taken from
another package just because it has the same name:

```python tabs=Python
from frictionless import relocate

result = relocate(
    "published",
    sources=["left/datapackage.json", "right/datapackage.json"],
    selection={
        "left/datapackage.json": ["orders"],
        "right/datapackage.json": ["sales"],
    },
)
print(result.package.resource_names)
# ['left-biz.codes', 'orders', 'right-biz.codes', 'sales']
```

The two `codes` resources are published under package-qualified names only when
their names would collide. Each business table keeps the foreign key it had in
its own source package, rewritten to the target's identity of that dictionary.

A source resource's schema is kept in its relative form: a schema referenced as
`schemas/orders.schema.json` is published as the same relative path, with its
foreign keys rewritten.

## Dependency Rules

By default, every selected table carries the referents of its foreign keys,
transitively. Two options make the rule explicit:

```python tabs=Python
result = relocate("published", sources=sources, selection=selection,
    include_dependencies=False)

result = relocate("published", sources=sources, selection=selection,
    exclude=["right/datapackage.json::codes"])
```

Both forms refuse a selection whose foreign key cannot be completed inside the
target package. The error names the table and the missing referent, and nothing
is written: an incomplete or silently cross-wired package is never published.

## Naming

Target names are preserved when they are unambiguous. They can be set
explicitly with `names`, keyed by `<source descriptor>::<resource name>`:

```python tabs=Python
result = relocate("published", sources=sources, selection=selection, names={
    "left/datapackage.json::codes": "left.dictionary",
    "right/datapackage.json::codes": "right.dictionary",
})
```

## Reopening And Validating

The result is reopened from its new location and validated as a package, so the
relative paths and the foreign keys are checked outside the source roots:

```python tabs=Python
assert result.valid
assert result.root == "published"
assert result.package.basepath == "published"
```

## Recording The Mapping

`relocate` writes a manifest (`relocation.json` by default) that records, for
every published resource, its source package, source resource, target name,
source and target paths, whether the data was copied or reused, its schema
form, its business properties and its validation:

```python tabs=Python
import json

with open(result.manifest_path) as file:
    manifest = json.load(file)
print(manifest["resources"][0])
```

When two resources genuinely share the same source file, the data material is
copied once and the second resource is recorded as `"material": "reused"`. Each
resource still keeps its own descriptor, schema and business properties.

Source descriptors and data files are only read. Their digests are recorded in
the manifest before the copy and checked again before the new package is
promoted, so a source changed during the operation aborts it.

## Reference

```yaml reference
references:
  - frictionless.relocate
```
