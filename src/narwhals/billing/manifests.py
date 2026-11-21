from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from narwhals.billing.exceptions import BillingError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from narwhals.billing.contracts import BatchContract
    from narwhals.dataframe import DataFrame


@dataclass(frozen=True)
class BatchManifest:
    """Physical columns a batch arrived with, recorded at ingestion time.

    Diagonal concatenation null-fills a column missing from some batches, which
    would erase the distinction between a column that was never delivered and a
    column delivered with explicit nulls. The manifest preserves that fact keyed
    by batch id, so concatenated wide tables expand to exactly the same rows as
    per-batch expansion. It deliberately holds plain strings rather than frame
    columns, so filtering, reordering or aliasing the wide table cannot corrupt
    it, and no ordinary business column name is consumed.
    """

    batch_id: str
    present_physical_names: frozenset[str]

    @classmethod
    def from_frame(
        cls, frame: DataFrame[object], contract: BatchContract
    ) -> BatchManifest:
        """Record which declared physical columns the frame actually carries."""
        return cls(
            batch_id=contract.batch_id,
            present_physical_names=frozenset(
                field.physical_name
                for field in contract.fields
                if field.physical_name in frame.columns
            ),
        )


class IngestionManifests:
    """Registry of the physical manifests captured when each batch was ingested."""

    def __init__(self, manifests: Iterable[BatchManifest]) -> None:
        registry: dict[str, BatchManifest] = {}
        for manifest in manifests:
            if manifest.batch_id in registry:
                msg = f"duplicate manifest for batch {manifest.batch_id!r}"
                raise BillingError(msg)
            registry[manifest.batch_id] = manifest
        self._registry = dict(registry)

    @classmethod
    def from_mapping(cls, manifests: Mapping[str, Iterable[str]]) -> IngestionManifests:
        """Build a registry from a mapping of batch id to present column names."""
        return cls(
            BatchManifest(batch_id, frozenset(columns))
            for batch_id, columns in manifests.items()
        )

    def for_batch(self, batch_id: str) -> BatchManifest:
        """Return the manifest frozen for a batch at ingestion."""
        try:
            return self._registry[batch_id]
        except KeyError:
            msg = (
                f"no ingestion manifest for batch {batch_id!r}; known batches: "
                f"{list(self._registry)}"
            )
            raise BillingError(msg) from None

    def __contains__(self, batch_id: object) -> bool:
        return batch_id in self._registry

    def __len__(self) -> int:
        return len(self._registry)
