# Relocate

```bash
frictionless relocate left/datapackage.json right/datapackage.json \
  --target published \
  --selection '{"left/datapackage.json": ["orders"], "right/datapackage.json": ["sales"]}'
```

The command publishes the selected resources and the resources their foreign
keys depend on into a new package. It writes the package descriptor and a
`relocation.json` manifest recording the source/target mapping, then prints the
manifest.

Use `--no-dependencies` to refuse rather than carry foreign-key referents, or
`--exclude '<descriptor>::<resource>'` to forbid a specific dependency. Both
forms fail before anything is written when a selected relation cannot be
completed in the new package.

Use `--names '{"left/datapackage.json::codes": "left.codes"}'` to set target
names explicitly.
