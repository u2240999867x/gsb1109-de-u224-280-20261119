from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from benedict.core.standardize import (
        StandardizationConflict,
        StandardizationPlan,
    )


class ExtrasRequireModuleNotFoundError(ModuleNotFoundError):
    def __init__(self, *, target: str) -> None:
        message = (
            f"Extras require '[{target}]' module not found, "
            f"you can install it by running: "
            f"'python -m pip install \"python-benedict[{target}]\"'"
        )
        super().__init__(message)


class StandardizationConflictError(ValueError):
    """
    Raised by strict key standardization when two or more keys of the
    same mapping would be renamed to the same standardized key.

    The computed :class:`~benedict.core.standardize.StandardizationPlan`
    is attached as ``plan`` so that the non-conflicting parts of the
    result can still be inspected while the original input is left
    untouched.
    """

    def __init__(self, plan: StandardizationPlan) -> None:
        self.plan = plan
        conflicts: Sequence[StandardizationConflict] = plan.conflicts
        details = "; ".join(
            f"{conflict.standardized_key!r} <= "
            + ", ".join(repr(path) for path in conflict.original_paths)
            for conflict in conflicts
        )
        message = (
            "Strict standardization would produce conflicting keys: "
            f"{details}. The original input has not been modified."
        )
        super().__init__(message)
