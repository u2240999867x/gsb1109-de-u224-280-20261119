"""
Streaming string-budget validation for supplier catalog ingestion.

`validate_string_budgets()` streams a JSON catalog document and enforces
caller-declared budgets on the decoded length of selected string fields
(e.g. every ``title`` inside each element of an ``orders`` array):

* a per-field budget: the maximum number of decoded Unicode code points
  allowed in any single occurrence of the field, and/or
* a per-order quota: the maximum combined number of decoded code points
  across all occurrences of the field within the same order.

Lengths are measured in decoded Unicode code points: a surrogate pair
escape (``\\ud83d\\ude00``), the same character written directly as UTF-8
and an escaped control character (``\\n``) each count as one code point.
Keys are matched after decoding, so a key written as ``\\u0074itle``
matches the field name ``title``.

Validation stops at the first confirmable failure: an over-budget value
is rejected as soon as the first over-budget code point has been decoded,
without waiting for the end of the string, and no further data is pulled
from the underlying stream. A character that cannot yet be legally
decoded (e.g. an invalid escape or an unpaired surrogate) is reported as
an escape error, never as a budget violation.

Budget enforcement relies on the pure-python tokenizer
(`json_stream.tokenizer.tokenize`), which is used regardless of whether
the optional Rust tokenizer extension is installed.
"""
from typing import NamedTuple, Optional

from json_stream.iterators import ensure_file
from json_stream.tokenizer import TokenType, tokenize

OPERATOR = TokenType.OPERATOR
STRING = TokenType.STRING
COLON = (OPERATOR, ":")


class StringBudgetError(ValueError):
    """
    Raised when a budgeted string violates its budget or cannot be decoded.

    Attributes:
        kind: one of `FIELD_LIMIT`, `ORDER_QUOTA` or `ESCAPE`
        path: full path of the offending value, e.g. ``("orders", 1, "title")``
        order_index: index of the order within the orders array
        occurrence: 1-based occurrence of the field within that order
        limit: the configured budget that was exceeded (None for escape errors)
        detail: the underlying decode error message (escape errors only)
    """

    FIELD_LIMIT = "field_limit"
    ORDER_QUOTA = "order_quota"
    ESCAPE = "escape"

    def __init__(self, kind, path, order_index, occurrence, limit=None, detail=None):
        self.kind = kind
        self.path = path
        self.order_index = order_index
        self.occurrence = occurrence
        self.limit = limit
        self.detail = detail
        location = f"order {order_index}, field occurrence {occurrence}, path {path!r}"
        if kind == self.FIELD_LIMIT:
            message = f"decoded string exceeds per-field budget of {limit} characters ({location})"
        elif kind == self.ORDER_QUOTA:
            message = f"decoded strings exceed order quota of {limit} characters ({location})"
        else:
            message = f"invalid escape in budgeted string ({location}): {detail}"
        super().__init__(message)


class FieldReport(NamedTuple):
    """Accounting for a single occurrence of a budgeted field."""
    order_index: int
    occurrence: int
    path: tuple
    length: int  # decoded length in code points


class OrderReport(NamedTuple):
    """Accounting for a single order: its budgeted fields and their total."""
    order_index: int
    fields: tuple  # tuple of FieldReport
    total: int  # combined decoded length of the budgeted fields


def validate_string_budgets(fp_or_iterable, *, orders_path=("orders",), field="title",
                            field_limit=None, order_quota=None):
    """
    Stream a supplier catalog and enforce decoded-string budgets.

    Args:
        fp_or_iterable: file-like object or iterable of `bytes`/`str` chunks
        orders_path: path of the orders array within the document; each
            element of that array is an "order" with its own quota
        field: (decoded) key name of the budgeted string fields; every
            occurrence of this key anywhere inside an order is budgeted
        field_limit: maximum decoded code points allowed per field value
        order_quota: maximum combined decoded code points per order

    Returns:
        a list of `OrderReport`, one per order, in document order

    Raises:
        StringBudgetError: on the first confirmable budget violation or
            invalid escape inside a budgeted string
        ValueError: on other malformed JSON
    """
    fp = ensure_file(fp_or_iterable)
    validator = _BudgetValidator(tuple(orders_path), field, field_limit, order_quota)
    token_stream = tokenize(fp, string_hook=validator.string_char)
    return validator.run(token_stream)


class _BudgetValidator:
    def __init__(self, orders_path, field, field_limit, order_quota):
        self.orders_path = orders_path
        self.field = field
        self.field_limit = field_limit
        self.order_quota = order_quota
        # armed state: set while the value of a budgeted field is decoded
        self.armed = False
        self.arm_path = None
        self.arm_order_index = None
        self.arm_occurrence = None
        self.field_count = 0
        # per-order state (reset when an adjacent order is entered)
        self.order_index: Optional[int] = None
        self.order_count = 0
        self.occurrence = 0
        self._orders = []

    # called by the tokenizer for every decoded string character
    def string_char(self, char):
        if not self.armed:
            return
        self.field_count += 1
        self.order_count += 1
        if self.field_limit is not None and self.field_count > self.field_limit:
            raise StringBudgetError(
                StringBudgetError.FIELD_LIMIT, self.arm_path,
                self.arm_order_index, self.arm_occurrence, limit=self.field_limit,
            )
        if self.order_quota is not None and self.order_count > self.order_quota:
            raise StringBudgetError(
                StringBudgetError.ORDER_QUOTA, self.arm_path,
                self.arm_order_index, self.arm_occurrence, limit=self.order_quota,
            )

    def run(self, stream):
        try:
            token_type, token = next(stream)
        except StopIteration:
            raise ValueError('No JSON value found') from None
        self._parse_value(stream, (), token_type, token)
        return [
            OrderReport(order_index, tuple(fields), sum(field.length for field in fields))
            for order_index, fields in self._orders
        ]

    def _next(self, stream):
        try:
            return next(stream)
        except StopIteration:
            raise ValueError("Unexpected end of file") from None

    def _next_value_token(self, stream, armed):
        # pull the first token of a field value; if the field is budgeted the
        # hook is armed so a string token is validated as it is decoded
        if armed:
            self.armed = True
            self.field_count = 0
        try:
            return next(stream)
        except StopIteration:
            raise ValueError("Unexpected end of file") from None
        except StringBudgetError:
            raise
        except ValueError as e:
            if armed:
                raise StringBudgetError(
                    StringBudgetError.ESCAPE, self.arm_path,
                    self.arm_order_index, self.arm_occurrence, detail=e.args[0],
                ) from e
            raise
        finally:
            self.armed = False

    def _order_of(self, value_path):
        # the order a value belongs to, or None if it is not inside an order
        depth = len(self.orders_path)
        if len(value_path) < depth + 2:
            return None
        if value_path[:depth] != self.orders_path:
            return None
        order_index = value_path[depth]
        if not isinstance(order_index, int) or isinstance(order_index, bool):
            return None
        return order_index

    def _start_order(self, index):
        self.order_index = index
        self.order_count = 0
        self.occurrence = 0
        self._orders.append((index, []))

    def _record_field(self, length):
        self._orders[-1][1].append(
            FieldReport(self.arm_order_index, self.arm_occurrence, self.arm_path, length)
        )

    def _parse_value(self, stream, path, token_type, token):
        if token_type == OPERATOR:
            if token == '{':
                self._parse_object(stream, path)
            elif token == '[':
                self._parse_array(stream, path)
            else:
                raise ValueError(f"Expecting value, got {token}")

    def _parse_object(self, stream, path):
        first = True
        while True:
            token_type, token = self._next(stream)
            if first:
                if token_type == OPERATOR and token == '}':
                    return
                if token_type != STRING:
                    raise ValueError(f"Expecting string key or }}, got {token!r}")
                first = False
            else:
                if token_type != OPERATOR or token not in ',}':
                    raise ValueError(f"Expecting comma or }}, got {token!r}")
                if token == '}':
                    return
                token_type, token = self._next(stream)
                if token_type != STRING:
                    raise ValueError(f"Expecting string key after comma, got {token!r}")
            key = token
            if self._next(stream) != COLON:
                raise ValueError("Expecting :")
            self._parse_member_value(stream, path, key)

    def _parse_member_value(self, stream, path, key):
        key_path = path + (key,)
        order_index = self._order_of(key_path) if key == self.field else None
        budgeted = order_index is not None
        if budgeted:
            self.occurrence += 1
            self.arm_path = key_path
            self.arm_order_index = order_index
            self.arm_occurrence = self.occurrence
        token_type, token = self._next_value_token(stream, budgeted)
        if token_type == OPERATOR:
            if token == '{':
                self._parse_object(stream, key_path)
            elif token == '[':
                self._parse_array(stream, key_path)
            else:
                raise ValueError(f"Expecting value, got {token}")
        elif budgeted and token_type == STRING:
            self._record_field(len(token))

    def _parse_array(self, stream, path):
        is_orders = path == self.orders_path
        index = 0
        first = True
        while True:
            token_type, token = self._next(stream)
            if first:
                if token_type == OPERATOR and token == ']':
                    return
                first = False
            else:
                if token_type != OPERATOR or token not in ',]':
                    raise ValueError(f"Expecting comma or ], got {token!r}")
                if token == ']':
                    return
                token_type, token = self._next(stream)
                if token_type == OPERATOR and token not in '{[':
                    raise ValueError(f"Expecting value after comma, got {token}")
            child_path = path + (index,)
            if is_orders:
                self._start_order(index)
            self._parse_value(stream, child_path, token_type, token)
            index += 1
