# station_ingest

Archivable, mergeable ingest progress for multi-collector station data, built
on xarray. Independent collection routes can each absorb part of the backlog,
save to NetCDF, reopen later and merge without ever reviving revoked or
superseded observations.

## Coverage states

Every station/time/variable cell is in one of three states:

- **valid value** — a real reading, where valid zero is stored as `0.0`, not
  missing;
- **explicit missing** — a collector actively reported the cell as missing;
- **uncovered** — no collector currently covers the cell.

A cell enters the consensus grid only when every covering collector reports
the same valid value. Otherwise it is a **conflict**: no consensus value is
stored, and each party's current value and source record are exposed via
`IngestProgress.view(...).conflict_details()`.

## Records

- `Submission(record_id, collector, observations)` — values or explicit
  missing markers for any subset of cells. Submissions take effect in
  `record_id` order, so out-of-order and late delivery converge to the same
  state. A smaller id arriving late never overwrites a larger active record.
- `Revocation(record_id, target_id, collector, cells)` — revokes part of one
  target submission. The cell falls back to the collector's latest
  still-effective earlier submission; with no predecessor it becomes
  uncovered. An explicit missing marker only disappears when its own
  submission is revoked. Revocations may arrive before their target.
- `SealingReceipt(receipt_id, named)` — names a batch of submission and
  revocation ids. Once all named records are present the batch is sealed:
  named submissions can no longer be targeted by new revocations, while the
  same coordinates remain open for fresh submissions. Records not named by a
  receipt may still arrive late, even with smaller ids.

## Archive and merge

`prune_archived()` removes records sealed by complete receipts, keeping only:

- tombstone ids of archived records (needed to reject re-delivery and to stop
  old snapshots from reviving content);
- the current per-cell values and their source records;
- still-pending receipts and still-live records.

Dead values that can never participate again are not retained for recompute.

`IngestProgress.merge(left, right)` is symmetric, commutative for the resulting
ledger state, idempotent and side-effect free. Retained current values from a
pruned side override stale values in an older snapshot, while any later live
delivery still wins. A grid declared up front
(`IngestProgress(stations=..., times=..., variables=...)`) keeps uncovered
coordinates visible in every view.

## NetCDF persistence

`save_progress(progress, path)` and `load_progress(path)` use a flat-table
xarray Dataset (numeric codes plus fixed-width strings) so the default `scipy`
netCDF3 engine works without optional dependencies. Files are ordinary NetCDF
and survive repeated save/load cycles.

```python
from station_ingest import (
    IngestProgress, Submission, Revocation, SealingReceipt,
    load_progress, save_progress, obs_value, obs_missing,
)

progress = IngestProgress(stations=["ST1"], times=["t0", "t1"], variables=["temperature"])
progress.add_submission(Submission(1, "C", {("ST1", "t0", "temperature"): obs_value(0.0)}))
progress.add_submission(Submission(2, "C", {("ST1", "t0", "temperature"): obs_missing()}))
progress.add_revocation(Revocation(3, 2, "C", [("ST1", "t0", "temperature")]))
save_progress(progress, "progress.nc")

reopened = load_progress("progress.nc")
view = reopened.view("temperature")  # status, value, coverage, conflicts
```

## Tests

```bash
python -m pytest -o addopts="" station_ingest/tests
```

`test_scenario.py` reproduces the full narrative: two routes saved
independently, reopened and merged in both orders, sealed and pruned, then
re-merged with older snapshots and a late unnamed submission, with results
checked against a single complete, ordered merge over shuffled, chunked input.
