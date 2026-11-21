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

## Installation

```bash
$ pip install parquet-tools
```

## Usage

```bash
$ parquet-tools --help
usage: parquet-tools [-h] {show,csv,inspect} ...

parquet CLI tools

positional arguments:
  {show,csv,inspect}
    show              Show human readble format. see `show -h`
    csv               Cat csv style. see `csv -h`
    inspect           Inspect parquet file. see `inspect -h`

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

## Structured-column CSV handoff

Nested parquet tables (structs, lists of structs, fixed-scale decimals) cannot
survive a plain flat CSV without losing their definitions. The
`parquet_tools.commands.handoff` package adds a versioned handoff format on
top of ordinary CSV files.

- Every batch is written as `<name>.csv` plus `<name>.sidecar.json`. The CSV
  is plain text and is split with the standard library `csv` module only;
  each cell holds one JSON value, which is reinterpreted using the table
  definition carried by the sidecar.
- The sidecar embeds the exact Arrow schema the batch was exported under
  (base64 Arrow IPC), the full list of declared field paths, and SHA-256
  checksums binding the CSV bytes to that schema and version.
- A `SchemaRegistry` records every definition ever used. Evolution is
  restricted to backward-compatible changes: adding nullable struct members
  and widening decimal precision/scale. The union shape is used to read
  earlier batches, filling new members with nulls; nothing is ever guessed
  from the newest column layout.
- A batch whose version is missing from the registry, whose embedded schema
  differs from the registry copy, or whose checksums do not verify, is
  rejected as a whole while every other batch still reconciles. Reading a
  batch under its original version keeps "never declared", "explicit null"
  and "actual value" distinguishable.
- The existing flat `csv` command output is unchanged.

```python
from parquet_tools.commands.handoff import (
    SchemaRegistry, write_batch, read_handoff, physical_missing_paths)

registry = SchemaRegistry()
registry.add('v1', schema_v1)
write_batch(table_v1, './handoff', 'batch1', 'v1', registry)

registry.add('v2', schema_v2)          # nullable struct member added, decimal scale raised
write_batch(table_v2, './handoff', 'batch2', 'v2', registry)

# receiver: delivery order may differ from export order
result = read_handoff('./handoff', registry,
                      delivery_order=['batch2.csv', 'batch1.csv'])
result.table                 # all verifiable rows on the union schema
result.rejected_rows         # rows that could not be verified under their own version
```

