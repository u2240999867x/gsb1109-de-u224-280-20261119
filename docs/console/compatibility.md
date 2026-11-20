---
script:
  basepath: data
---

# Compatibility

```markdown remark type=warning
For the concepts behind the audit, read the [Descriptor compatibility](../advanced/compatibility.html) page.
```

The `compatibility` command replays raw records through the allowed read/write directions and reports rejected records, records that read but change values or cross-table relationships, and the range that can be certified. It exits non-zero unless every direction is compatible.

```bash script tabs=CLI
frictionless compatibility compatibility/equipment-maintenance.json
```

Use `--json` or `--yaml` for a machine readable report, and `--output` to write it to a file:

```bash script tabs=CLI
frictionless compatibility compatibility/equipment-maintenance-silent.json --json
```
