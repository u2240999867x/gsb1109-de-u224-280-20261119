from typing import Any

from dlt.common.exceptions import DltException


class NormalizerException(DltException):
    pass


class InvalidJsonNormalizer(NormalizerException):
    def __init__(self, required_normalizer: str, present_normalizer: str) -> None:
        self.required_normalizer = required_normalizer
        self.present_normalizer = present_normalizer
        super().__init__(
            f"Operation requires `{required_normalizer}` normalizer while"
            f" `{present_normalizer}` normalizer is present"
        )


class NestedRowIdConflictException(NormalizerException):
    """Raised when several nested rows under the same parent share the configured
    business id. The same id is allowed in different parent rows."""

    def __init__(
        self,
        table_name: str,
        parent_row_id: str,
        id_column: str,
        id_value: Any,
    ) -> None:
        self.table_name = table_name
        self.parent_row_id = parent_row_id
        self.id_column = id_column
        self.id_value = id_value
        super().__init__(
            f"Nested rows in table `{table_name}` under parent `{parent_row_id}` share the"
            f" same `{id_column}` value `{id_value}`. The id must be unique within a parent"
            " row; the same value is allowed under different parent rows."
        )
