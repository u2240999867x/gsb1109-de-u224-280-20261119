# 'dicts' is imported before 'core' on purpose, importing 'core' first triggers
# a circular import: core.unflatten -> dicts.keylist -> dicts.
from benedict.dicts import benedict  # noqa: I001
from benedict.core.source_map import MergeResult, SourceNode, SourceMap
from benedict.metadata import (
    __author__,
    __copyright__,
    __description__,
    __license__,
    __title__,
    __version__,
)

__all__ = [
    "benedict",
    "MergeResult",
    "SourceMap",
    "SourceNode",
    "__author__",
    "__copyright__",
    "__description__",
    "__license__",
    "__title__",
    "__version__",
]
