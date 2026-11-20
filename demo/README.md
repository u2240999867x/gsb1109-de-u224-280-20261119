# Atomic, streaming Data Package conversion demo

This demo converts an on-disk Frictionless Data Package containing two
dependent resources (`orders` and `items`, linked through `order_id`) while
keeping the source package fully readable the whole time.

## Layout

- `data/source/datapackage.json` — original package descriptor (never mutated)
- `data/source/orders.csv` — 8 orders, primary key `order_id`
- `data/source/items.csv` — 20 line items, foreign key `order_id -> orders`;
  row 19 contains a bad `qty` value (`N/A` for an integer field)
- `conversion_engine.py` — the streaming, staging-based conversion engine
- `run_demo.py` — end-to-end scenario driver with assertions
- `test_conversion_engine.py` — pytest suite

## Guarantees demonstrated

1. **Streaming reads.** Rows come from `resource.row_stream` one at a time;
   the full package is never preloaded into memory (a test patches
   `TableResource.read_rows` to fail if it is called during conversion).
2. **Atomic publication.** Each conversion streams into a private staging
   directory. The output directory and `datapackage.json` are only created
   after both resources are fully written and validate cleanly. A failed run
   removes staging and leaves no partial package, so a downstream consumer can
   never join new orders against old items.
3. **Independent runs.** Every converter opens its own `Package` from the same
   descriptor path: separate generators, stats, schemas and staging dirs.
4. **Join-field migration.** Both schemas rename `order_id` to `order_ref`,
   including `primaryKey` and every `foreignKeys` entry on both sides.
5. **Failure and recovery.** The strict East conversion fails on the bad item
   row and is cleaned up; a later fresh, tolerant run recovers from the same
   source package. The tolerant West conversion finishes and publishes both
   resources.
6. **Early stop is not success.** An abandoned consumer is marked `aborted`
   and leaves nothing on disk.
7. **Stable source.** The original package keeps its old paths, schema and
   row counts (8 orders / 20 items) before, during and after every run.
8. **Standalone output.** Landed packages are reopened from disk alone; they
   do not depend on any exhausted source generator.

## Run

```bash
.venv/bin/python demo/run_demo.py
.venv/bin/python -m pytest demo/ -v
```

Expected row counts after filtering (items follow their parent order's
region, and the bad row is rejected under the tolerant policy):

| Output | orders | items |
|--------|--------|-------|
| west   | 4      | 8     |
| east (recovery) | 4 | 11 |
