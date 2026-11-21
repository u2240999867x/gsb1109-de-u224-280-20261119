# Correction alignment diagnosis

A small, replayable reproduction of a sensor-correction hand-off bug. It does
not modify or monkeypatch the library.

## What it demonstrates

- Two recipes consume the same raw rows and the same correction values:
  - **native**: independent pandas `Series` attached and summed by label;
  - **Narwhals**: the same data passed through `nw.DataFrame.with_columns`,
    where the pandas-like backend resets the operand index and inserts
    positionally.
- Both recipes filter `business_time <= 25` and re-sort by business time.
- The positional path corrupts `S-B` first (`correction=7.0`,
  `adjusted=127.0`), then that row is filtered out.
- The native path stays correct until its final one-row
  `pd.Series([7.0], index=["S-A"])` patch. The patch index is a business label,
  while the target row label is `4102`; pandas aligns by label and writes
  `NaN`.
- The remaining two rows are byte-for-byte identical: two rows, one null at
  `S-A`. Neither the null count nor the apparent index identifies the cause.
- A Polars recipe, which has no pandas index, matches the Narwhals path at
  every stage and acts as the no-index control.
- A duplicate correction input shows two rows (`C-02` and `C-07`) competing for
  `S-B`. A real scalar broadcast (`df.loc[mask, col] = 7.0` / `nw.lit(7.0)`)
  is contrasted with a one-row `Series`, which Narwhals rejects with
  `ShapeError`.
- A business-key merge repairs row/key association (`S-A -> 7.0`) but does not
  invent the missing `S-B` correction.

## Run

From the repository root:

```bash
.venv/bin/python tools/correction_alignment_diag/replay.py
```

Write the JSON report and Markdown diagnosis:

```bash
.venv/bin/python tools/correction_alignment_diag/replay.py \
    --out-dir tools/correction_alignment_diag/artifacts
```

Run the regression checks:

```bash
.venv/bin/python -m pytest tools/correction_alignment_diag/test_replay.py -q
```

The script exits non-zero if a checked invariant changes. Versions used for the
stored artifacts: pandas 3.1.1, Narwhals 2.29.0, Polars 2.1.0.

## Files

- `replay.py`: the complete micro-flow and assertions.
- `test_replay.py`: regression tests around the diagnosis.
- `artifacts/report.json`: generated call-by-call report.
- `artifacts/diagnosis.md`: generated human-readable conclusion and trace
  table.

## Auditable amounts

| business_key | correction | adjusted |
| --- | --- | --- |
| S-A | 7.0 | 107.0 |
| S-B | null (no source correction) | null |
| S-C | 3.0 | 203.0 |
