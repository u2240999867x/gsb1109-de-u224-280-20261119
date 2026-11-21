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
usage: parquet-tools [-h] {show,csv,inspect,stats} ...

parquet CLI tools

positional arguments:
  {show,csv,inspect,stats}
    show              Show human readble format. see `show -h`
    csv               Cat csv style. see `csv -h`
    inspect           Inspect parquet file. see `inspect -h`
    stats             Footer-only column statistics report. see `stats -h`

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

#### Footer-only column statistics report

```bash
$ parquet-tools stats /path/to/parquet --column readings.value
```

Reads only the parquet footer (never data pages) and reports min, max and
null_count per row group and for the whole file, each annotated with an
evidence status (`proven` / `unknown` / `n/a`). Columns are addressed by
their full dot-separated field path (repeatable `--column`); without
`--column` all leaf columns are reported. Unsigned logical types are
decoded with their correct sort order when `column_orders` is declared;
legacy footers with only signed min/max cannot prove unsigned ranges and
are reported as `unknown`.

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
