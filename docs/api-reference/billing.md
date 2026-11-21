# `narwhals.billing`

Contract-driven currency reconciliation for wide sales and procurement tables.
Currencies, fixed-point scales and stable field identities live in batch-scoped
contracts rather than plain column names, so a later contract revision cannot
reinterpret an already-ingested batch and a display-name swap cannot move money
between fields.

::: narwhals.billing
    handler: python
    options:
      members:
        - AMOUNT_COLUMN
        - BATCH_ID_COLUMN
        - BatchContract
        - BatchManifest
        - BillingContractError
        - BillingContracts
        - BillingError
        - CANONICAL_COLUMNS
        - CURRENCY_COLUMN
        - CurrencyDeclaration
        - DECLARED_SCALE_COLUMN
        - FIELD_ID_COLUMN
        - FieldContract
        - IngestionManifests
        - PHYSICAL_NAME_COLUMN
        - PRESENCE_COLUMN
        - PRESENCE_EXPLICIT_NULL
        - PRESENCE_MISSING_COLUMN
        - PRESENCE_PRESENT
        - SOURCE_ROW_COLUMN
        - concat_billings
        - concat_wide
        - expand
        - expand_concatenated
        - ingest
        - lossless_check
