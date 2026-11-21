# parquet-tools

![Run Unittest](https://github.com/ktrueda/parquet-tools/workflows/Run%20Unittest/badge.svg)
![Run CLI test](https://github.com/ktrueda/parquet-tools/workflows/Run%20CLI%20test/badge.svg)

This is a pip installable [parquet-tools](https://github.com/apache/parquet-mr).
In other words, parquet-tools is a CLI tools of [Apache Arrow](https://github.com/apache/arrow).
You can show parquet file content/schema on local disk or on Amazon S3.
It is incompatible with original parquet-tools.

## Features

- Read Parquet data (local file or file on S3)
- Read Parquet metadata/schema (local file or file on S3)
- Compare two Parquet footer schemas without reading data pages (`diff`)

## Installation

```bash
$ pip install parquet-tools
```

## Usage

```bash
$ parquet-tools --help
usage: parquet-tools [-h] {show,csv,inspect,diff} ...

parquet CLI tools

positional arguments:
  {show,csv,inspect,diff}
    show              Show human readble format. see `show -h`
    csv               Cat csv style. see `csv -h`
    inspect           Inspect parquet file. see `inspect -h`
    diff              Compare two Parquet footer schemas. see `diff -h`

optional arguments:
  -h, --help          show this help message and exit
```

## Usage Examples

#### Show local parquet file

```bash
$ parquet-tools show test.parquet
+-------+-------+---------+
|   one | two   | three   |
|-------+-------+---------|
|  -1   | foo   | True    |
| nan   | bar   | False   |
|   2.5 | baz   | True    |
+-------+-------+---------+
```

#### Show parquet file on S3

```bash
$ parquet-tools show s3://bucket-name/prefix/*
+-------+-------+---------+
|   one | two   | three   |
|-------+-------+---------|
|  -1   | foo   | True    |
| nan   | bar   | False   |
|   2.5 | baz   | True    |
+-------+-------+---------+
```


#### Inspect parquet file schema

```bash
$ parquet-tools inspect /path/to/parquet
```

<details>

<summary>Inspect output</summary>

```
############ file meta data ############
created_by: parquet-cpp version 1.5.1-SNAPSHOT
num_columns: 3
num_rows: 3
num_row_groups: 1
format_version: 1.0
serialized_size: 2226


############ Columns ############
one
two
three

############ Column(one) ############
name: one
path: one
max_definition_level: 1
max_repetition_level: 0
physical_type: DOUBLE
logical_type: None
converted_type (legacy): NONE

############ Column(two) ############
name: two
path: two
max_definition_level: 1
max_repetition_level: 0
physical_type: BYTE_ARRAY
logical_type: String
converted_type (legacy): UTF8

############ Column(three) ############
name: three
path: three
max_definition_level: 1
max_repetition_level: 0
physical_type: BOOLEAN
logical_type: None
converted_type (legacy): NONE
```
</details>

#### Cat CSV parquet and transform [csvq](https://github.com/mithrandie/csvq)

```bash
$ parquet-tools csv s3://bucket-name/test.parquet |csvq "select one, three where three"
+-------+-------+
|  one  | three |
+-------+-------+
| -1.0  | True  |
| 2.5   | True  |
+-------+-------+
```

## Footer schema diff

`parquet-tools diff OLD NEW` reads only the two Parquet footers (the trailing
Thrift `FileMetaData`) and emits a deterministic JSON structural report. It
never opens data pages, statistics, or dictionary pages, so every conclusion is
marked `"evidence_scope": "footer_only"` and `"data_pages_scanned": false`.

```bash
$ parquet-tools diff old.parquet new.parquet --format json
$ parquet-tools diff old.parquet new.parquet --format text
```

Policy flags are explicit and can be switched off independently:

- `--allow-display-order` / `--no-allow-display-order`: treat a reordering of
  already-paired leaf columns as display-only (`display_only`) or incompatible.
- `--allow-rename-id ID[,ID...]` (repeatable): allow a rename only when both
  sides carry that stable, non-negative field ID. Without this flag a rename is
  incompatible; a rename without a stable ID is `unverifiable`.
- `--json-indent N`: JSON indentation (`0` for one line).

Exit codes: `0` both directions compatible, `1` incompatible (in either
direction), `2` footer cannot decide (including pending identity), `3`
input/footer read error, `4` both directions are compatible but a migration
step is required.

### Identity and LIST rules

Fields are paired conservatively, in this order, and the basis is reported per
pair in `identity_pairs` and per change in `identity.basis`:

1. a **unique field ID** on both sides;
2. the **same single LIST element position** (LIST elements are identified
   positionally, not by their physical name);
3. the **same unique sibling name under an already matched parent**.

A repeated field ID is not identity evidence: every colliding candidate stays
in `pending_confirmations` and no candidate is chosen. Same-named fields with
insufficient IDs likewise remain pending rather than being matched by name.

Both legal LIST encodings are folded to one logical shape before comparison:

```
repeated element;                              # 2-level
repeated group wrapper { optional element; }   # 3-level
```

A wrapper-only difference is reported as `list_wrapper_change` at
`change_level: "wrapper"` with impact `display_only`; it never produces an
element add/remove or a type change. `old`/`new.physical_path` always show the
real footer path, while `logical_path` removes the wrapper (`tags[]`).

Nullability and repetition are compared per node, not just per leaf dtype:

- list element `optional -> required` is
  `list_element_nullability_change` (element level), incompatible in that one
  direction; the reverse direction stays compatible;
- list field `optional -> required` is a separate `repetition_change`;
- moving a repeated level to another node is `repetition_level_relocated`,
  even when the leaf dtype and the numeric max repetition level are unchanged;
- moving a field under another parent is `parent_path_change`.

An identical leaf dtype or an unchanged column index never cancels these
changes. `repetition_level_relocated` lists the actual nodes in
`old_repeated_at` / `new_repeated_at`.

### Report shape

The JSON report contains `inputs` (footer size and SHA-256), `policy`,
`summary.directions` (`old_to_new` and `new_to_old`), `changes`,
`identity_pairs`, and `pending_confirmations`. Changes are sorted by a stable
key (impact, old path, new path, kind, level, physical paths), so the same
inputs and policy produce byte-identical reports.

Each change includes the real `old`/`new` paths, `field_id`, repetition and
level metadata, `identity` (basis, confidence, evidence), both directional
verdicts, `impact` (`display_only`, `metadata_only`, `migration_required`,
`incompatible`, `unverifiable`), `requires_migration`, and
`value_verification`. `may_resolve_violation` means footer evidence alone
cannot rule out a required-value violation; only a data scan could confirm it.
