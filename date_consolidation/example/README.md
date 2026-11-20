# Date consolidation demo

Fifteen-minute walk-through of evidence-based date standardization across an
ISO-only headquarters (HQ) and two legacy stores whose records are interleaved
in one CSV.

## Inputs

- `config.yaml` — the only place written date forms are declared.  HQ gets one
  ISO pattern; each store gets a day-first (`%d/%m/%Y`) and a month-first
  (`%m/%d/%Y`) pattern.  No format is ever selected by list position.
- `hq_dates.csv` — HQ submission.  One ISO row and one non-ISO row that no
  configured HQ format accepts.
- `stores_dates.csv` — both stores, same submission version `v7`, interleaved.
  The tail holds two `store_north` rows: one legal only as day-first, then one
  legal only as month-first, breaking that store's single-order promise.

## Run

```bash
python -m date_consolidation.cli date_consolidation/example/config.yaml
```

Outputs land in `date_consolidation/example/output/`:

- `report.txt` — every verdict with the raw text and physical source line,
  plus per source/version consistency and the contradiction with provenance.
- `consolidated.csv` — one row per input record; `standardized_date` is filled
  only where evidence supports it and is written via a frictionless
  `DateField` in the stable ISO format.
- `readback_check.txt` — the standardized column re-read through `DateField`,
  proving the single-format read/write path still works.

## What the evidence shows

- HQ ISO cells are confirmed with format; the non-ISO HQ cell stays empty.
- `09/09/2024` (south) resolves to the same day under both orders and is
  confirmed once south's month-first order is pinned; `07/07/2024` in the
  contradictory north group stays "same day, format unknown" because that
  group never earns a trustworthy pin, though its value is still exported.
- `store_south` is pinned month-first by `04/13/2024`, so its otherwise
  ambiguous `06/04/2024` resolves to 2024-06-04.
- `store_north` is pinned day-first but then contradicted by `02/25/2024`;
  the group is marked not self-consistent and its ambiguous `05/03/2024`
  (2024-03-05 vs 2024-05-03) is left unstandardized.  `store_south` and HQ
  keep their confirmed values because the constraint is scoped per
  source/version.
