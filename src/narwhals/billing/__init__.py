"""Currency-aware billing reconciliation on top of the Narwhals API.

The helpers in this namespace turn wide sales/procurement tables into one row
per currency field while keeping every interpretation decision in batch-scoped
contracts rather than in plain column names:

- `BatchContract` / `BillingContracts` freeze the approved currencies, fixed
  point scales and stable field identities at ingestion time. A contract
  revision (for example the same physical column changing from JPY to USD) only
  applies to batches ingested under the new contract.
- `expand` unfolds a single batch according to its approved currency list.
  Missing physical columns, explicit nulls and true zero are kept distinct in a
  reserved `__billing__` namespace that never occupies a business column name.
- `ingest` and `expand_concatenated` give the same rows when wide tables are
  concatenated first and expanded afterwards, routing each batch through the
  contract it was ingested with.
- Narrowing a physical decimal scale is validated before any cast, so both
  supported decimal backends raise `BillingContractError` naming the affected
  batch and declaration instead of silently truncating.

The plain `DataFrame.unpivot` behavior is intentionally left untouched; when no
contract or column list applies, use it exactly as before.
"""

from __future__ import annotations

from narwhals.billing.contracts import (
    AMOUNT_COLUMN,
    BATCH_ID_COLUMN,
    CANONICAL_COLUMNS,
    CURRENCY_COLUMN,
    DECLARED_SCALE_COLUMN,
    FIELD_ID_COLUMN,
    PHYSICAL_NAME_COLUMN,
    PRESENCE_COLUMN,
    PRESENCE_EXPLICIT_NULL,
    PRESENCE_MISSING_COLUMN,
    PRESENCE_PRESENT,
    SOURCE_ROW_COLUMN,
    BatchContract,
    BillingContracts,
    CurrencyDeclaration,
    FieldContract,
)
from narwhals.billing.engine import (
    concat_billings,
    concat_wide,
    expand,
    expand_concatenated,
    ingest,
    lossless_check,
)
from narwhals.billing.exceptions import BillingContractError, BillingError
from narwhals.billing.manifests import BatchManifest, IngestionManifests

__all__ = (
    "AMOUNT_COLUMN",
    "BATCH_ID_COLUMN",
    "CANONICAL_COLUMNS",
    "CURRENCY_COLUMN",
    "DECLARED_SCALE_COLUMN",
    "FIELD_ID_COLUMN",
    "PHYSICAL_NAME_COLUMN",
    "PRESENCE_COLUMN",
    "PRESENCE_EXPLICIT_NULL",
    "PRESENCE_MISSING_COLUMN",
    "PRESENCE_PRESENT",
    "SOURCE_ROW_COLUMN",
    "BatchContract",
    "BatchManifest",
    "BillingContractError",
    "BillingContracts",
    "BillingError",
    "CurrencyDeclaration",
    "FieldContract",
    "IngestionManifests",
    "concat_billings",
    "concat_wide",
    "expand",
    "expand_concatenated",
    "ingest",
    "lossless_check",
)
