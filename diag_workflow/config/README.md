# Reference configs

These files document the four single-switch interventions in readable form.
`python3 ../drive.py` does not read them directly; it writes the equivalent
config into `evidence/rounds/<case>/config.yaml` so every round is
self-contained and the exact inputs are archived with its trace.

- `baseline.yaml` — checkpoint material still points at `data`, producer
  allow-list is `south`.
- `checkpoint_fixed.yaml` — only the checkpoint material moves to the roster.
- `constraint_only.yaml` — only the producer allow-list widens.
- `both_fixed.yaml` — both switches fixed.
