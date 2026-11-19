# Distribution drift on a small dataset

This example checks distribution drift with the two-sample
Kolmogorov-Smirnov (KS) test on four hand-sized, checked-in cases. The
data lives next to this file as CSV, is loaded into an in-memory SQLite
database, and is small enough to inspect by eye.

All files share one integer column, `value`:

| file                        | rows | min | max | mean |
| --------------------------- | ---- | --- | --- | ---- |
| `baseline.csv`              | 40   | 0   | 39  | 19.5 |
| `location_shift.csv`        | 40   | 20  | 59  | 39.5 |
| `shape_change.csv`          | 40   | 0   | 39  | 19.5 |
| `insufficient_baseline.csv` | 10   | 0   | 9   | 4.5  |
| `insufficient_current.csv`  | 10   | 0   | 9   | 4.5  |

The cases deliberately separate the two kinds of drift:

- **Location only** (`location_shift.csv`): every value of `baseline.csv` is
  shifted by +20. The shape and spread are unchanged.
- **Shape only** (`shape_change.csv`): the range is the same as the baseline
  (`0` to `39`), but the mass moves to both ends of the range. A min/max or
  range check cannot see this; a distribution test can.
- **Insufficient sample** (`insufficient_*.csv`): two identical 10-row
  samples. Their KS statistic is exactly `0`, so a test that only looks at the
  statistic or a missing p-value would pass them. They must not pass.

Run the specification with

```bash
pytest docs/examples/distribution_drift/specification.py -v
```

Expected and reproducible results:

| comparison                                        | rows  | D     | verdict  |
| ------------------------------------------------- | ----- | ----- | -------- |
| `baseline` vs `baseline`                          | 40/40 | 0.00  | passes   |
| `baseline` vs `location_shift`                    | 40/40 | 0.50  | fails    |
| `baseline` vs `shape_change`                      | 40/40 | 0.45  | fails    |
| `insufficient_baseline` vs `insufficient_current` | 10/10 | 0.00  | fails: insufficient sample |

At a significance level of 0.05 the critical D for 40 + 40 rows is about
0.304, so both drift cases are far outside the acceptance region. No random
numbers are involved; re-running produces the same statistic, sample sizes and
verdicts.

The insufficient case fails with

```text
Insufficient sample size for the 2-sample Kolmogorov-Smirnov test:
got 10 and 10 rows, but at least 35 per sample are required (observed d=0.0).
```

The default `min_samples=35` matches the point below which the p-value
approximation is considered unreliable. Both sample sizes are also exposed on
the result as `TestResult.sample_sizes`, so a report can show exactly how much
data a verdict was based on.

```python
from datajudge import BetweenRequirement

requirement = BetweenRequirement.from_tables(
    db_name1="sqlite",
    schema_name1=None,
    table_name1="baseline",
    db_name2="sqlite",
    schema_name2=None,
    table_name2="shape_change",
)
requirement.add_ks_2sample_constraint(
    column1="value",
    column2="value",
    significance_level=0.05,
    min_samples=35,
)

result = requirement[0].test(engine)
assert result.sample_sizes == (40, 40)
# This particular comparison fails as intended; the point is that the result
# always reports the sample sizes the verdict was based on.
assert not result.outcome
```

If a smaller sample is genuinely acceptable for a use case, lower
`min_samples` explicitly. A missing p-value is never interpreted as a pass:
without enough data the constraint fails as inconclusive.
