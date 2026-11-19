from typing import Dict, Optional

from dlt.common.schema.typing import TColumnName, TypedDict


class RelationalNormalizerConfigPropagation(TypedDict, total=False):
    root: Optional[Dict[TColumnName, TColumnName]]
    tables: Optional[Dict[str, Dict[TColumnName, TColumnName]]]


class RelationalNormalizerConfigNestedRowId(TypedDict, total=False):
    # name of the column (normalized identifier expected) holding the
    # business id of a nested row, unique within its parent row
    column: TColumnName


class RelationalNormalizerConfig(TypedDict, total=False):
    max_nesting: Optional[int]
    propagation: Optional[RelationalNormalizerConfigPropagation]
    root_key_propagation: Optional[bool]
    nested_row_id: Optional[RelationalNormalizerConfigNestedRowId]
