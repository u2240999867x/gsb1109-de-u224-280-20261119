"""Single-pass intake precheck against a saved Frictionless Table Schema.

The saved contract schema is never modified: every verdict is produced by
reading cells through the schema's own field readers.
"""

from .precheck import (
    CellRejection,
    ColumnProfile,
    RowFinding,
    ScanConfig,
    ScanResult,
    scan_resource,
)

__all__ = [
    "CellRejection",
    "ColumnProfile",
    "RowFinding",
    "ScanConfig",
    "ScanResult",
    "scan_resource",
]
