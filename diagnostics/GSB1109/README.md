# GSB1109 archive-time diagnostic

Read `REPORT.md` for the findings. Replay everything with:

```bash
.venv/bin/python diagnostics/GSB1109/run_all.py
```

The run regenerates `data/` (deterministic CDF-5 inputs, sha256-pinned in
`expected/manifest.json`), all `reports/`, and runs the 18 regression tests.
`data/` and `reports/` are reproducible and intentionally not committed.
