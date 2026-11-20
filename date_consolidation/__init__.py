"""Evidence-based consolidation of date columns from HQ and legacy stores.

The package keeps every legal reading of a raw date cell and only promotes a
value into the standardized column when the batch evidence actually supports
it.  A configured format list is never treated as a default choice.
"""

from .config import Config, FileSpec, FormatSpec, SourceConfig, load_config
from .consolidate import Record, consolidate
from .group import GroupState
from .interp import Reading, interpret_cell

__all__ = [
    "Config",
    "FileSpec",
    "FormatSpec",
    "GroupState",
    "Reading",
    "Record",
    "SourceConfig",
    "consolidate",
    "interpret_cell",
    "load_config",
]
