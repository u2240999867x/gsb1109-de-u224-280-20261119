# Copyright 2024-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you
# may not use this file except in compliance with the License.  You
# may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.


"""Constants, types, and classes shared across Bulk Write API implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

from bson.objectid import ObjectId
from bson.raw_bson import RawBSONDocument
from pymongo.common import (
    validate_is_document_type,
    validate_ok_for_replace,
    validate_ok_for_update,
)
from pymongo.errors import BulkWriteError, OperationFailure, PyMongoError
from pymongo.helpers_shared import _get_wce_doc
from pymongo.message import (
    _DELETE,
    _INSERT,
    _UPDATE,
    _BulkWriteContext,
    _EncryptedBulkWriteContext,
)
from pymongo.results import (
    BulkWriteOperationOutcome,
    BulkWriteOutcomeStatus,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, MutableMapping

    from pymongo.typings import _AgnosticCollection, _DocumentOut, _DocumentType, _Pipeline


_DELETE_ALL: int = 0
_DELETE_ONE: int = 1

_OP_TYPE_NAMES: tuple[str, str, str] = ("insert", "update", "delete")

# For backwards compatibility. See MongoDB src/mongo/base/error_codes.err
_BAD_VALUE: int = 2
_UNKNOWN_ERROR: int = 8
_WRITE_CONCERN_ERROR: int = 64

_COMMANDS: tuple[str, str, str] = ("insert", "update", "delete")


class _Run:
    """Represents a batch of write operations."""

    def __init__(self, op_type: int) -> None:
        """Initialize a new Run object."""
        self.op_type: int = op_type
        self.index_map: list[int] = []
        self.ops: list[Any] = []
        self.idx_offset: int = 0

    def index(self, idx: int) -> int:
        """Get the original index of an operation in this run.

        :param idx: The Run index that maps to the original index.
        """
        return self.index_map[idx]

    def add(self, original_index: int, operation: Any) -> None:
        """Add an operation to this Run instance.

        :param original_index: The original index of this operation
            within a larger bulk operation.
        :param operation: The operation document.
        """
        self.index_map.append(original_index)
        self.ops.append(operation)


class _OperationLedger:
    """Track what each command proves about the operations it carried.

    The ledger is indexed by original input position, so two commands that
    report position ``0`` resolve to the two different input operations that
    each command actually carried.
    """

    def __init__(self, ops: list[tuple[int, Mapping[str, Any]]]) -> None:
        self._entries = [
            BulkWriteOperationOutcome(index, _OP_TYPE_NAMES[op_type])
            for index, (op_type, _) in enumerate(ops)
        ]

    def entries(self) -> list[BulkWriteOperationOutcome]:
        return self._entries

    def copies(self) -> list[BulkWriteOperationOutcome]:
        return [entry._copy() for entry in self._entries]

    def mark_attempted(self, run: _Run, offset: int, count: int) -> None:
        """Mark the operations a new acknowledged attempt will carry as unknown."""
        for run_idx in range(offset, offset + count):
            self._entries[run.index(run_idx)]._mark_attempted()

    def mark_not_sent(self, run: _Run, offset: int, count: int) -> None:
        """Mark operations as provably never handed to the server."""
        for run_idx in range(offset, offset + count):
            self._entries[run.index(run_idx)]._set(BulkWriteOutcomeStatus.NOT_SENT)

    def record(
        self,
        run: _Run,
        offset: int,
        count: int,
        result: Mapping[str, Any],
        ordered: bool = False,
    ) -> None:
        """Record per-operation evidence from one authoritative command reply.

        ``run`` is the run whose operations the command carried, ``offset`` is
        the run index of the first operation sent, and ``count`` is the number
        of operations the command actually carried.
        """
        sent = [run.index(run_idx) for run_idx in range(offset, offset + count)]
        wce = _get_wce_doc(result)

        failed: dict[int, Mapping[str, Any]] = {}
        failed_positions: list[int] = []
        for doc in result.get("writeErrors") or ():
            position = doc.get("index", doc.get("idx"))
            if position is None or not (0 <= position < count):
                continue
            failed[sent[position]] = doc
            failed_positions.append(position)

        upserted: dict[int, Any] = {}
        for doc in result.get("upserted") or ():
            position = doc.get("index", doc.get("idx"))
            if position is None or not (0 <= position < count):
                continue
            upserted[sent[position]] = doc.get("_id")

        # Raw write command replies report counts in ``n``. ``nModified`` is
        # only present for update commands, and ``upserted`` only for upserts.
        affected = result.get("n", 0)

        not_executed: set[int] = set()
        if ordered and failed_positions:
            # An ordered command stops at its first error; later operations in
            # the same command were received but not executed.
            first_failed = min(failed_positions)
            for position in range(first_failed + 1, count):
                not_executed.add(sent[position])

        # An insert document carries a client-known _id. If every operation the
        # command executed is accounted for by n, each executed insert is a
        # proven insertion of that specific document. A partial count proves no
        # particular document. Counts for updates and deletes never prove which
        # record changed, so they stay ACKNOWLEDGED.
        executed = count - len(failed) - len(not_executed)
        proven_insert = run.op_type == _INSERT and affected == executed

        for position, original_index in enumerate(sent):
            entry = self._entries[original_index]
            inserted_id = None
            if original_index in failed:
                status = BulkWriteOutcomeStatus.WRITE_ERROR
            elif original_index in not_executed:
                status = BulkWriteOutcomeStatus.NOT_EXECUTED
            elif wce:
                status = BulkWriteOutcomeStatus.WRITE_CONCERN_ERROR
            elif original_index in upserted:
                status = BulkWriteOutcomeStatus.PROVEN
            elif proven_insert:
                status = BulkWriteOutcomeStatus.PROVEN
                inserted_id = run.ops[offset + position].get("_id")
            else:
                status = BulkWriteOutcomeStatus.ACKNOWLEDGED
            entry._set(
                status,
                write_error=failed.get(original_index),
                upserted_id=upserted.get(original_index),
                inserted_id=inserted_id,
                write_concern_error=wce,
            )


def _attach_operation_outcomes(
    exc: BaseException, operation_outcomes: list[BulkWriteOperationOutcome]
) -> None:
    """Attach per-operation evidence to a bulk write exception, when possible.

    ``BulkWriteError`` already carries outcomes from the merge path. Any other
    exception raised while a bulk write was in flight (for example a network
    failure before a command reached the wire) gets them attached here, so the
    caller can still see which operations were never sent.
    """
    if isinstance(exc, BulkWriteError) and exc.operation_outcomes:
        return
    if isinstance(exc, PyMongoError):
        exc._operation_outcomes = tuple(operation_outcomes)


def _merge_command(
    run: _Run,
    full_result: MutableMapping[str, Any],
    offset: int,
    result: Mapping[str, Any],
) -> None:
    """Merge a write command result into the full bulk result."""
    affected = result.get("n", 0)

    if run.op_type == _INSERT:
        full_result["nInserted"] += affected

    elif run.op_type == _DELETE:
        full_result["nRemoved"] += affected

    elif run.op_type == _UPDATE:
        upserted = result.get("upserted")
        if upserted:
            n_upserted = len(upserted)
            for doc in upserted:
                # Leave the server response intact for APM and for the
                # per-operation ledger, which reads command-local positions.
                merged = doc.copy()
                merged["index"] = run.index(doc["index"] + offset)
                full_result["upserted"].append(merged)
            full_result["nUpserted"] += n_upserted
            full_result["nMatched"] += affected - n_upserted
        else:
            full_result["nMatched"] += affected
        full_result["nModified"] += result["nModified"]

    write_errors = result.get("writeErrors")
    if write_errors:
        for doc in write_errors:
            # Leave the server response intact for APM.
            replacement = doc.copy()
            idx = doc["index"] + offset
            replacement["index"] = run.index(idx)
            # Add the failed operation to the error document.
            replacement["op"] = run.ops[idx]
            full_result["writeErrors"].append(replacement)

    wce = _get_wce_doc(result)
    if wce:
        full_result["writeConcernErrors"].append(wce)


def _raise_bulk_write_error(
    full_result: _DocumentOut,
    operation_outcomes: list[BulkWriteOperationOutcome] | None = None,
) -> NoReturn:
    """Raise a BulkWriteError from the full bulk api result."""
    # retryWrites on MMAPv1 should raise an actionable error.
    if full_result["writeErrors"]:
        full_result["writeErrors"].sort(key=lambda error: error["index"])
        err = full_result["writeErrors"][0]
        code = err["code"]
        msg = err["errmsg"]
        if code == 20 and msg.startswith("Transaction numbers"):
            errmsg = (
                "This MongoDB deployment does not support "
                "retryable writes. Please add retryWrites=false "
                "to your connection string."
            )
            raise OperationFailure(errmsg, code, full_result)
    raise BulkWriteError(full_result, operation_outcomes)


class _AgnosticBulkBase:
    """The private guts of the bulk write API."""

    def __init__(
        self,
        collection: _AgnosticCollection[_DocumentType],
        ordered: bool,
        bypass_document_validation: bool | None,
        comment: str | None = None,
        let: Any | None = None,
    ) -> None:
        """Initialize an _AgnosticBulkBase instance."""
        self.collection = collection.with_options(
            codec_options=collection.codec_options._replace(
                unicode_decode_error_handler="replace", document_class=dict
            )
        )
        self.let = let
        if self.let is not None:
            validate_is_document_type("let", self.let)
        self.comment: str | None = comment
        self.ordered = ordered
        self.ops: list[tuple[int, Mapping[str, Any]]] = []
        self.executed = False
        self.bypass_doc_val = bypass_document_validation
        self.uses_collation = False
        self.uses_array_filters = False
        self.uses_sort = False
        self.is_retryable = True
        self.retrying = False
        self.started_retryable_write = False
        # Extra state so that we know where to pick up on a retry attempt.
        self.current_run = None
        self.next_run = None
        self.is_encrypted = False
        # Per-operation evidence, keyed by original input position.
        self.ledger: _OperationLedger | None = None

    def operation_outcomes(self) -> list[BulkWriteOperationOutcome]:
        """Return the per-operation evidence collected so far."""
        return self.operation_outcomes_ledger().entries()

    def operation_outcomes_ledger(self) -> _OperationLedger:
        """Return the ledger tracking per-operation evidence."""
        if self.ledger is None:
            self.ledger = _OperationLedger(self.ops)
        return self.ledger

    @property
    def bulk_ctx_class(self) -> type[_BulkWriteContext]:
        encrypter = self.collection.database.client._encrypter
        if encrypter and not encrypter._bypass_auto_encryption:
            self.is_encrypted = True
            return _EncryptedBulkWriteContext
        else:
            self.is_encrypted = False
            return _BulkWriteContext

    def add_insert(self, document: _DocumentOut) -> None:
        """Add an insert document to the list of ops."""
        validate_is_document_type("document", document)
        # Generate ObjectId client side.
        if not (isinstance(document, RawBSONDocument) or "_id" in document):
            document["_id"] = ObjectId()
        self.ops.append((_INSERT, document))

    def add_update(
        self,
        selector: Mapping[str, Any],
        update: Mapping[str, Any] | _Pipeline,
        multi: bool,
        upsert: bool | None,
        collation: Mapping[str, Any] | None = None,
        array_filters: list[Mapping[str, Any]] | None = None,
        hint: str | dict[str, Any] | None = None,
        sort: Mapping[str, Any] | None = None,
    ) -> None:
        """Create an update document and add it to the list of ops."""
        validate_ok_for_update(update)
        cmd: dict[str, Any] = {"q": selector, "u": update, "multi": multi}
        if upsert is not None:
            cmd["upsert"] = upsert
        if collation is not None:
            self.uses_collation = True
            cmd["collation"] = collation
        if array_filters is not None:
            self.uses_array_filters = True
            cmd["arrayFilters"] = array_filters
        if hint is not None:
            cmd["hint"] = hint
        if sort is not None:
            self.uses_sort = True
            cmd["sort"] = sort
        if multi:
            # A bulk_write containing an update_many is not retryable.
            self.is_retryable = False
        self.ops.append((_UPDATE, cmd))

    def add_replace(
        self,
        selector: Mapping[str, Any],
        replacement: Mapping[str, Any],
        upsert: bool | None,
        collation: Mapping[str, Any] | None = None,
        hint: str | dict[str, Any] | None = None,
        sort: Mapping[str, Any] | None = None,
    ) -> None:
        """Create a replace document and add it to the list of ops."""
        validate_ok_for_replace(replacement)
        cmd: dict[str, Any] = {"q": selector, "u": replacement}
        if upsert is not None:
            cmd["upsert"] = upsert
        if collation is not None:
            self.uses_collation = True
            cmd["collation"] = collation
        if hint is not None:
            cmd["hint"] = hint
        if sort is not None:
            self.uses_sort = True
            cmd["sort"] = sort
        self.ops.append((_UPDATE, cmd))

    def add_delete(
        self,
        selector: Mapping[str, Any],
        limit: int,
        collation: Mapping[str, Any] | None = None,
        hint: str | dict[str, Any] | None = None,
    ) -> None:
        """Create a delete document and add it to the list of ops."""
        cmd: dict[str, Any] = {"q": selector, "limit": limit}
        if collation is not None:
            self.uses_collation = True
            cmd["collation"] = collation
        if hint is not None:
            cmd["hint"] = hint
        if limit == _DELETE_ALL:
            # A bulk_write containing a delete_many is not retryable.
            self.is_retryable = False
        self.ops.append((_DELETE, cmd))

    def gen_ordered(self) -> Iterator[_Run | None]:
        """Generate batches of operations, batched by type of operation.

        The batches are in the order **provided**.
        """
        run = None
        for idx, (op_type, operation) in enumerate(self.ops):
            if run is None:
                run = _Run(op_type)
            elif run.op_type != op_type:
                yield run
                run = _Run(op_type)
            run.add(idx, operation)
        yield run

    def gen_unordered(self) -> Iterator[_Run]:
        """Generate batches of operations, batched by type of operation, in arbitrary order."""
        operations = [_Run(_INSERT), _Run(_UPDATE), _Run(_DELETE)]
        for idx, (op_type, operation) in enumerate(self.ops):
            operations[op_type].add(idx, operation)

        for run in operations:
            if run.ops:
                yield run
