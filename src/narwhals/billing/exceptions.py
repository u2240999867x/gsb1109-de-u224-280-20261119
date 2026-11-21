from __future__ import annotations

from typing import TYPE_CHECKING

from narwhals.exceptions import NarwhalsError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from decimal import Decimal
    from typing import Literal

    LossReason = Literal["floating_point", "unsupported_dtype", "unrepresentable_scale"]


class BillingError(NarwhalsError):
    """Base class for errors raised by the billing expansion pipeline."""


class BillingContractError(BillingError):
    """A physical money column cannot satisfy the contract declared for its batch.

    The error is structured rather than message-only so that both supported
    decimal backends report the same affected batch and declaration.
    """

    def __init__(
        self,
        *,
        reason: LossReason,
        backend: str,
        batch_id: str,
        field_id: str,
        currency: str,
        physical_name: str,
        declared_scale: int,
        physical_scale: int | None = None,
        offenders: Sequence[Decimal] = (),
        source_rows: Sequence[int] = (),
    ) -> None:
        self.reason = reason
        self.backend = backend
        self.batch_id = batch_id
        self.field_id = field_id
        self.currency = currency
        self.physical_name = physical_name
        self.declared_scale = declared_scale
        self.physical_scale = physical_scale
        self.offenders = tuple(offenders)
        self.source_rows = tuple(source_rows)

        declaration = (
            f"field_id={field_id!r}, currency={currency!r}, "
            f"declared_scale={declared_scale}"
        )
        if reason == "unrepresentable_scale":
            detail = (
                f"physical scale {physical_scale} cannot be narrowed to declared "
                f"scale {declared_scale} without data loss; offending values "
                f"{list(self.offenders)} at source rows {list(self.source_rows)}"
            )
        elif reason == "floating_point":
            detail = (
                "money columns must be integral or decimal; a floating point column "
                "cannot be reconciled losslessly"
            )
        else:
            detail = (
                f"physical dtype scale {physical_scale} is not integral, decimal or "
                "all-null and cannot be reconciled"
            )
        message = (
            f"[{backend}] batch {batch_id!r} declaration ({declaration}), "
            f"physical column {physical_name!r}: {detail}"
        )
        super().__init__(message)
