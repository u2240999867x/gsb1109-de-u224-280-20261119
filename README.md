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

## Declared-partition archives

Archives often embed partition columns such as `date` and `region` in the
directory layout (e.g. `archive/region=us+east/date=2026-01-01/part.parquet`),
while files may also carry their own physical `region` column. The `report`
and `receipt` commands understand a declared business root and reconcile both
sources. Paths outside the business root are ignored, and each directory
segment is percent-decoded exactly once.

```bash
# Measurement projection with ordinary region/date filtering.
$ parquet-tools report ./archive \
    --partition-columns date,region \
    --columns measurement \
    --filter date==2026-01-01 --filter region==us+east \
    --head 100

# Full region reconciliation across every row group (local or S3).
$ parquet-tools receipt s3://bucket-name/archive \
    --filter date==2026-01-02
```

Reconciliation rules:

- A completely missing physical `region` column is filled from the path; a
  physical null is reported separately instead.
- A row group whose trusted statistics prove one non-null region equal to the
  path is accepted without decoding the region column. Missing, mixed or
  null-bearing groups are decoded and checked row by row.
- Report filters only annotate filtered rows; contradictions are never hidden,
  and receipts are never truncated because the caller already sampled enough
  rows. Unfinished (`_incomplete`) and failing files contribute no formal
  report rows, even when their earlier groups matched.
- Repeated partition layers with different values fail with the concrete
  offending path. Reads that declare no partition columns still use the
  original `show`/`csv`/`inspect` behavior unchanged.

