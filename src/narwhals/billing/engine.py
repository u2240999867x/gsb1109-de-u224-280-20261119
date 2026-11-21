from __future__ import annotations

from typing import TYPE_CHECKING

import narwhals as nw
import narwhals.dtypes as nwd
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
    RESERVED_NAMESPACE,
    SOURCE_ROW_COLUMN,
    BatchContract,
    BillingContracts,
    FieldContract,
    field_column_name,
)
from narwhals.billing.exceptions import BillingContractError, BillingError

# narwhals-canary: delete this line to confirm you have read the full diff

if TYPE_CHECKING:
    from narwhals.billing.manifests import BatchManifest, IngestionManifests
    from narwhals.dataframe import DataFrame
    from narwhals.typing import DataFrameT

_DECIMAL_PRECISION = 38
_INGEST_STAMPS = (BATCH_ID_COLUMN, SOURCE_ROW_COLUMN)


def _backend_label(frame: DataFrame[object]) -> str:
    return frame.__native_namespace__().__name__


def _overflow_pattern(declared_scale: int) -> str:
    return rf"\.\d{{{declared_scale}}}[1-9]"


def _assert_no_reserved_business_columns(frame: DataFrame[object]) -> None:
    clashes = [
        column
        for column in frame.columns
        if column.startswith(f"{RESERVED_NAMESPACE}.") and column not in _INGEST_STAMPS
    ]
    if clashes:
        msg = (
            "business columns must not use the reserved "
            f"{RESERVED_NAMESPACE!r} namespace, found {clashes}"
        )
        raise BillingError(msg)


def _validate_money_column(
    indexed: DataFrame[object],
    contract: BatchContract,
    field: FieldContract,
    backend: str,
) -> None:
    declared_scale = field.currency.scale
    name = field.physical_name
    dtype = indexed.schema[name]

    if isinstance(dtype, nwd.Decimal):
        if dtype.scale <= declared_scale:
            return
        offenders_frame = indexed.filter(
            nw.col(name).cast(nw.String).str.contains(_overflow_pattern(declared_scale))
        )
        if offenders_frame.shape[0]:
            raise BillingContractError(
                reason="unrepresentable_scale",
                backend=backend,
                batch_id=contract.batch_id,
                field_id=field.field_id,
                currency=field.currency.currency,
                physical_name=name,
                declared_scale=declared_scale,
                physical_scale=dtype.scale,
                offenders=offenders_frame[name].to_list(),
                source_rows=offenders_frame[SOURCE_ROW_COLUMN].to_list(),
            )
        return

    if isinstance(dtype, (nwd.IntegerType, nwd.Unknown)):
        return

    reason = "floating_point" if isinstance(dtype, nwd.FloatType) else "unsupported_dtype"
    raise BillingContractError(
        reason=reason,
        backend=backend,
        batch_id=contract.batch_id,
        field_id=field.field_id,
        currency=field.currency.currency,
        physical_name=name,
        declared_scale=declared_scale,
        physical_scale=getattr(dtype, "scale", None),
    )


def _coerce_money_expr(
    indexed: DataFrame[object],
    contract: BatchContract,
    field: FieldContract,
    backend: str,
) -> nw.Expr:
    _validate_money_column(indexed, contract, field, backend)
    return nw.col(field.physical_name).cast(
        nw.Decimal(_DECIMAL_PRECISION, field.currency.scale)
    )


def _declaration_map(
    frame: DataFrame[object], contract: BatchContract
) -> DataFrame[object]:
    return nw.from_dict(
        {
            FIELD_ID_COLUMN: [field.field_id for field in contract.fields],
            CURRENCY_COLUMN: [field.currency.currency for field in contract.fields],
            DECLARED_SCALE_COLUMN: [field.currency.scale for field in contract.fields],
            PHYSICAL_NAME_COLUMN: [field.physical_name for field in contract.fields],
        },
        backend=frame.__native_namespace__(),
    )


def _amount_expressions(
    indexed: DataFrame[object],
    frame: DataFrame[object],
    contract: BatchContract,
    backend: str,
    manifest: BatchManifest | None,
) -> tuple[list[nw.Expr], list[str]]:
    missing_field_ids: list[str] = []
    amount_exprs: list[nw.Expr] = []
    for field in contract.fields:
        amount_name = field_column_name(field.field_id)
        if manifest is not None:
            delivered = field.physical_name in manifest.present_physical_names
        else:
            delivered = field.physical_name in frame.columns
        if delivered and field.physical_name not in frame.columns:
            msg = (
                f"manifest for batch {contract.batch_id!r} declares "
                f"{field.physical_name!r} as delivered, but the column is absent "
                "from the frame"
            )
            raise BillingError(msg)
        if delivered:
            amount_exprs.append(
                _coerce_money_expr(indexed, contract, field, backend).alias(amount_name)
            )
        else:
            missing_field_ids.append(field.field_id)
            amount_exprs.append(
                nw.lit(None)
                .cast(nw.Decimal(_DECIMAL_PRECISION, field.currency.scale))
                .alias(amount_name)
            )
    return amount_exprs, missing_field_ids


def expand(
    frame: DataFrameT,
    contract: BatchContract,
    *,
    index_columns: list[str] | None = None,
    manifest: BatchManifest | None = None,
) -> DataFrameT:
    """Expand one batch's wide table into one row per declared currency field.

    Currencies and scales come exclusively from the batch contract; plain column
    names are never interpreted as currencies. Physical columns that are absent,
    explicitly null, or hold a true zero remain distinguishable through the
    reserved presence column. Coercion to the declared scale is validated as
    lossless first, so both decimal backends reject the same declaration instead
    of silently truncating.

    Arguments:
        frame: Eager Narwhals wide table for one ingestion batch.
        contract: The contract approved for this batch at ingestion time.
        index_columns: Business columns carried onto every expanded row. When
            `None`, every column that is not a declared physical money column is
            carried, mirroring `unpivot` without an explicit column list.
        manifest: Ingestion-time record of the physical columns the batch
            arrived with. Required when expanding a partition of a concatenated
            table, where diagonal null-filling otherwise hides a never-delivered
            column. When `None`, presence is read directly from `frame.columns`.

    Returns:
        A long reconciliation frame whose reserved `__billing__.*` columns never
        collide with ordinary business column names.

    Examples:
        >>> from decimal import Decimal
        >>> import pyarrow as pa
        >>> import narwhals as nw
        >>> from narwhals import billing as nb
        >>> contract = nb.BatchContract(
        ...     "b1",
        ...     [
        ...         nb.FieldContract(
        ...             "f_amount", "amount", nb.CurrencyDeclaration("USD", 2)
        ...         )
        ...     ],
        ... )
        >>> frame = nw.from_native(
        ...     pa.table(
        ...         {"amount": pa.array([Decimal("12.30"), None], pa.decimal128(38, 2))}
        ...     )
        ... )
        >>> result = nb.expand(frame, contract)
        >>> result[nb.AMOUNT_COLUMN].to_list()
        [Decimal('12.30'), None]
        >>> result[nb.PRESENCE_COLUMN].to_list()
        ['present', 'explicit_null']
    """
    _assert_no_reserved_business_columns(frame)
    backend = _backend_label(frame)

    if SOURCE_ROW_COLUMN in frame.columns:
        indexed = frame
    else:
        indexed = frame.with_row_index(SOURCE_ROW_COLUMN)
    physical_names = {field.physical_name for field in contract.fields}
    if index_columns is None:
        carried = [
            column
            for column in indexed.columns
            if column not in _INGEST_STAMPS and column not in physical_names
        ]
    else:
        missing = [column for column in index_columns if column not in frame.columns]
        if missing:
            msg = f"index columns {missing} are not present in the frame"
            raise BillingError(msg)
        carried = [column for column in index_columns if column not in _INGEST_STAMPS]

    if not contract.fields:
        empty_schema = {
            **{column: frame.schema[column] for column in carried},
            BATCH_ID_COLUMN: nw.String(),
            SOURCE_ROW_COLUMN: nw.Int64(),
            FIELD_ID_COLUMN: nw.String(),
            CURRENCY_COLUMN: nw.String(),
            DECLARED_SCALE_COLUMN: nw.Int64(),
            PHYSICAL_NAME_COLUMN: nw.String(),
            PRESENCE_COLUMN: nw.String(),
            AMOUNT_COLUMN: nw.Decimal(_DECIMAL_PRECISION, 0),
        }
        empty_data = {column: [] for column in [*carried, *CANONICAL_COLUMNS]}
        return nw.from_dict(
            empty_data, schema=empty_schema, backend=frame.__native_namespace__()
        )

    amount_exprs, missing_field_ids = _amount_expressions(
        indexed, frame, contract, backend, manifest
    )

    amount_columns = [field_column_name(field.field_id) for field in contract.fields]
    unpivoted = (
        indexed.with_columns(amount_exprs)
        .unpivot(
            on=amount_columns,
            index=[SOURCE_ROW_COLUMN, *carried],
            variable_name=FIELD_ID_COLUMN,
            value_name=AMOUNT_COLUMN,
        )
        .with_columns(
            nw.col(FIELD_ID_COLUMN)
            .str.replace(f"^{RESERVED_NAMESPACE}\\.field\\.", "")
            .alias(FIELD_ID_COLUMN)
        )
        .join(_declaration_map(frame, contract), on=FIELD_ID_COLUMN, how="left")
    )

    missing_predicate = (
        nw.col(FIELD_ID_COLUMN).is_in(missing_field_ids)
        if missing_field_ids
        else nw.lit(False)
    )
    reconciled = unpivoted.with_columns(
        nw.when(missing_predicate)
        .then(nw.lit(PRESENCE_MISSING_COLUMN))
        .when(nw.col(AMOUNT_COLUMN).is_null())
        .then(nw.lit(PRESENCE_EXPLICIT_NULL))
        .otherwise(nw.lit(PRESENCE_PRESENT))
        .alias(PRESENCE_COLUMN),
        nw.lit(contract.batch_id).alias(BATCH_ID_COLUMN),
    )
    return reconciled.select(*carried, *CANONICAL_COLUMNS)


def _widen_money_columns(frames: list[DataFrameT]) -> list[DataFrameT]:
    column_names = {column for frame in frames for column in frame.columns}
    aligned = frames
    for column in column_names:
        holders = [frame for frame in frames if column in frame.columns]
        if len(holders) < 2:
            continue
        if not all(
            isinstance(frame.schema[column], (nwd.Decimal, nwd.IntegerType))
            for frame in holders
        ):
            continue
        decimal_holders = [
            frame for frame in holders if isinstance(frame.schema[column], nwd.Decimal)
        ]
        if not decimal_holders:
            continue
        target_scale = max(frame.schema[column].scale for frame in decimal_holders)
        aligned = [
            (
                frame.with_columns(
                    nw.col(column)
                    .cast(nw.Decimal(_DECIMAL_PRECISION, target_scale))
                    .alias(column)
                )
                if column in frame.columns
                else frame
            )
            for frame in aligned
        ]
    return aligned


def concat_billings(frames: list[DataFrameT]) -> DataFrameT:
    """Concatenate canonical frames produced by `expand`.

    The reserved amount column is widened losslessly to the largest declared
    scale across the batches; business columns absent from a batch arrive
    null-filled. This mirrors the alignment `expand_concatenated` performs
    internally, keeping the expand-then-concat and concat-then-expand paths
    identical.
    """
    if not frames:
        msg = "concat_billings requires at least one frame"
        raise BillingError(msg)
    target_scale = max(frame.schema[AMOUNT_COLUMN].scale for frame in frames)
    aligned = [
        frame.with_columns(
            nw.col(AMOUNT_COLUMN)
            .cast(nw.Decimal(_DECIMAL_PRECISION, target_scale))
            .alias(AMOUNT_COLUMN)
        )
        for frame in frames
    ]
    return nw.concat(aligned, how="diagonal")


def concat_wide(frames: list[DataFrameT]) -> DataFrameT:
    """Vertically concatenate batch wide tables after lossless scale alignment.

    Shared physical columns are widened to the largest physical decimal scale,
    which is always lossless; columns absent from a batch arrive null-filled.
    Money columns still keep their per-batch contracts, so the concatenated frame
    can only be interpreted together with its `BillingContracts` registry.
    """
    if not frames:
        msg = "concat_wide requires at least one frame"
        raise BillingError(msg)
    return nw.concat(_widen_money_columns(frames), how="diagonal")


def ingest(frame: DataFrameT, contract: BatchContract) -> DataFrameT:
    """Stamp a freshly received wide table with its ingested batch id.

    The stamp lives in the reserved `__billing__` namespace so it never occupies
    an ordinary business column name. Pair it with
    `BatchManifest.from_frame` to preserve which physical columns arrived.
    """
    _assert_no_reserved_business_columns(frame)
    return frame.with_row_index(SOURCE_ROW_COLUMN).with_columns(
        nw.lit(contract.batch_id).alias(BATCH_ID_COLUMN)
    )


def expand_concatenated(
    wide: DataFrameT,
    contracts: BillingContracts,
    manifests: IngestionManifests,
    *,
    index_columns: list[str] | None = None,
) -> DataFrameT:
    """Expand a wide table concatenated from several already-ingested batches.

    Rows are routed to the contract their batch was ingested with; a contract
    submitted later has no effect on earlier batches. Each partition goes through
    the same lossless path as `expand`, with its ingestion manifest restoring the
    missing-column versus explicit-null distinction that diagonal null-filling
    erases. Partitions are concatenated after widening every amount to the
    global maximum declared scale.
    """
    if BATCH_ID_COLUMN not in wide.columns:
        msg = (
            f"concatenated wide table must carry {BATCH_ID_COLUMN!r}; "
            "stamp batches with ingest() first"
        )
        raise BillingError(msg)

    batch_ids = wide.select(nw.col(BATCH_ID_COLUMN).unique())[BATCH_ID_COLUMN].to_list()
    partitions: list[DataFrameT] = []
    max_declared_scale = 0
    for batch_id in batch_ids:
        contract = contracts.for_batch(batch_id)
        manifest = manifests.for_batch(batch_id)
        max_declared_scale = max(max_declared_scale, contract.max_scale)
        partition = wide.filter(nw.col(BATCH_ID_COLUMN) == batch_id).drop(BATCH_ID_COLUMN)
        partitions.append(
            expand(partition, contract, index_columns=index_columns, manifest=manifest)
        )

    aligned = [
        partition.with_columns(
            nw.col(AMOUNT_COLUMN)
            .cast(nw.Decimal(_DECIMAL_PRECISION, max_declared_scale))
            .alias(AMOUNT_COLUMN)
        )
        for partition in partitions
    ]
    return nw.concat(aligned, how="diagonal")


def lossless_check(frame: DataFrame[object], contract: BatchContract) -> None:
    """Validate every physical money column against its contract without expanding.

    Raises `BillingContractError` for the first declaration that cannot be held
    losslessly, reporting the affected batch and declaration on either backend.
    """
    _assert_no_reserved_business_columns(frame)
    backend = _backend_label(frame)
    if SOURCE_ROW_COLUMN in frame.columns:
        indexed = frame
    else:
        indexed = frame.with_row_index(SOURCE_ROW_COLUMN)
    for field in contract.fields:
        if field.physical_name in indexed.columns:
            _validate_money_column(indexed, contract, field, backend)
