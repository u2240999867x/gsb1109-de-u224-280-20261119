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

## S3 local-copy reuse

Within a single CLI invocation (one archive task), parquet-tools keeps the
downloaded copy of each S3 object in a task-scoped local cache so that the
schema/inspect read and the data read share one download, and concurrent
processing units reading the same object reuse the same complete copy.

- Versions are identified only by S3-owned evidence: `VersionId` when bucket
  versioning is enabled, otherwise `ETag`. The object key and byte length are
  never treated as version identifiers, so an object replaced under the same
  path with the same length is fetched again as a distinct version.
- A unit that confirmed a version keeps reading its local copy even if the
  background object is replaced later; a new reader picks up the new version.
- Concurrent readers that confirm the same version share a single in-flight
  download. A partial or failed download is never shared and is removed
  immediately; the next read retries cleanly.
- Copies are keyed by `(endpoint, bucket, key)`, so the same bucket/key on a
  different endpoint is downloaded independently.
- The cache lives only for the task and is released when the command ends; it
  is never persisted across tasks.
- When S3 returns no usable version evidence (missing `VersionId`/`ETag`),
  parquet-tools logs why it cannot reuse and downloads without caching rather
  than guessing the version from the name or length.

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
