from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from narwhals.billing.exceptions import BillingError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

RESERVED_NAMESPACE = "__billing__"
BATCH_ID_COLUMN = f"{RESERVED_NAMESPACE}.batch_id"
SOURCE_ROW_COLUMN = f"{RESERVED_NAMESPACE}.source_row"
FIELD_ID_COLUMN = f"{RESERVED_NAMESPACE}.field_id"
CURRENCY_COLUMN = f"{RESERVED_NAMESPACE}.currency"
DECLARED_SCALE_COLUMN = f"{RESERVED_NAMESPACE}.declared_scale"
PHYSICAL_NAME_COLUMN = f"{RESERVED_NAMESPACE}.physical_name"
PRESENCE_COLUMN = f"{RESERVED_NAMESPACE}.presence"
AMOUNT_COLUMN = f"{RESERVED_NAMESPACE}.amount"

PRESENCE_PRESENT = "present"
PRESENCE_EXPLICIT_NULL = "explicit_null"
PRESENCE_MISSING_COLUMN = "missing_column"

CANONICAL_COLUMNS = (
    BATCH_ID_COLUMN,
    SOURCE_ROW_COLUMN,
    FIELD_ID_COLUMN,
    CURRENCY_COLUMN,
    DECLARED_SCALE_COLUMN,
    PHYSICAL_NAME_COLUMN,
    PRESENCE_COLUMN,
    AMOUNT_COLUMN,
)


def field_column_name(field_id: str) -> str:
    """Return the reserved wide-table column name backing a stable field identity."""
    return f"{RESERVED_NAMESPACE}.field.{field_id}"


@dataclass(frozen=True)
class CurrencyDeclaration:
    """Currency approved for a batch together with its fixed-point scale.

    Arguments:
        currency: ISO-style currency code, interpreted per batch. The same plain
            string in two batches may denote different currencies when the
            contracts differ; the contract is the source of truth.
        scale: Number of fractional digits declared for the currency. Money is
            held losslessly at this scale; narrowing a physical column below this
            scale raises `BillingContractError`.
    """

    currency: str
    scale: int

    def __post_init__(self) -> None:
        if not isinstance(self.currency, str) or not self.currency:
            msg = "currency must be a non-empty string"
            raise BillingError(msg)
        if not isinstance(self.scale, int) or isinstance(self.scale, bool):
            msg = "scale must be an integer"
            raise BillingError(msg)
        if not 0 <= self.scale <= 38:
            msg = "scale must be between 0 and 38 for decimal128 reconciliation"
            raise BillingError(msg)


@dataclass(frozen=True)
class FieldContract:
    """One money field as declared by a batch.

    Arguments:
        field_id: Stable field identity shared across contract revisions. It must
            not rely on a physical display name, because a later contract may keep
            the identity while changing the currency or swapping display names.
        physical_name: Column name in this batch's physical wide table.
        currency: Currency declaration approved for this field in this batch.
    """

    field_id: str
    physical_name: str
    currency: CurrencyDeclaration

    def __post_init__(self) -> None:
        if not isinstance(self.field_id, str) or not self.field_id:
            msg = "field_id must be a non-empty string"
            raise BillingError(msg)
        if "." in self.field_id or self.field_id.startswith(RESERVED_NAMESPACE):
            msg = (
                f"field_id {self.field_id!r} must not use the reserved "
                f"{RESERVED_NAMESPACE!r} namespace or contain '.'"
            )
            raise BillingError(msg)
        if not isinstance(self.physical_name, str) or not self.physical_name:
            msg = "physical_name must be a non-empty string"
            raise BillingError(msg)


@dataclass(frozen=True)
class BatchContract:
    """The full set of money declarations approved for one ingestion batch."""

    batch_id: str
    fields: tuple[FieldContract, ...]

    def __init__(self, batch_id: str, fields: Iterable[FieldContract]) -> None:
        if not isinstance(batch_id, str) or not batch_id:
            msg = "batch_id must be a non-empty string"
            raise BillingError(msg)
        fields_tuple = tuple(fields)
        seen: set[str] = set()
        for field in fields_tuple:
            if field.field_id in seen:
                msg = f"duplicate field_id {field.field_id!r} in batch {batch_id!r}"
                raise BillingError(msg)
            seen.add(field.field_id)
        object.__setattr__(self, "batch_id", batch_id)
        object.__setattr__(self, "fields", fields_tuple)

    @property
    def max_scale(self) -> int:
        """Largest declared scale in the batch; widening to it is always lossless."""
        return max((field.currency.scale for field in self.fields), default=0)

    def field(self, field_id: str) -> FieldContract:
        """Look up a field declaration by its stable identity."""
        for field in self.fields:
            if field.field_id == field_id:
                return field
        msg = f"field_id {field_id!r} is not declared for batch {self.batch_id!r}"
        raise BillingError(msg)


class BillingContracts:
    """Immutable registry of the contracts approved at ingestion time.

    Contracts are keyed by batch id and never reinterpreted: a contract submitted
    later cannot change how an already ingested batch is expanded.
    """

    def __init__(self, contracts: Iterable[BatchContract]) -> None:
        registry: dict[str, BatchContract] = {}
        for contract in contracts:
            if contract.batch_id in registry:
                msg = f"duplicate batch contract {contract.batch_id!r}"
                raise BillingError(msg)
            registry[contract.batch_id] = contract
        self._registry = dict(registry)

    @classmethod
    def from_mapping(
        cls, declarations: Mapping[str, Iterable[FieldContract]]
    ) -> BillingContracts:
        """Build a registry from a mapping of batch id to field declarations."""
        return cls(
            BatchContract(batch_id, tuple(fields))
            for batch_id, fields in declarations.items()
        )

    @property
    def batch_ids(self) -> tuple[str, ...]:
        """Batch ids covered by the registry."""
        return tuple(self._registry)

    def for_batch(self, batch_id: str) -> BatchContract:
        """Return the contract frozen for a batch at ingestion."""
        try:
            return self._registry[batch_id]
        except KeyError:
            msg = (
                f"no ingested contract for batch {batch_id!r}; known batches: "
                f"{list(self._registry)}"
            )
            raise BillingError(msg) from None

    def __contains__(self, batch_id: object) -> bool:
        return batch_id in self._registry

    def __len__(self) -> int:
        return len(self._registry)
