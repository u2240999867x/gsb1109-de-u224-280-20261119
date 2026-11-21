# `narwhals.plan`

Schema-only join planning. `NamePlan` mirrors a chain of `join`/`select`/`rename`
operations using only column names, so a report that joins several tables can
confirm its output column names - and the original table and column each of
them comes from - before any data is read.

::: narwhals.plan
    handler: python
    options:
      members:
        - ColumnSource
        - NamePlan
        - PlannedFrame
      show_root_heading: false
      show_source: false
      show_bases: false
