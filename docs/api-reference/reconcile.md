# `narwhals.reconcile`

Mergeable selection of the currently effective supplier-device record.

For a plain-language explanation of the selection rule, the two missing-time
policies, and the merge guarantees, see the module docstring below.

::: narwhals.reconcile
    handler: python
    options:
      members:
        - ConflictingOccurrenceError
        - DeviceLedger
        - MissingTimePolicy
        - Occurrence
        - ReconcileResult
        - Withdrawal
        - merge_ledgers
        - reconcile
        - reconcile_devices
      show_source: false
      show_bases: false
