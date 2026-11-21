'''Structured-column CSV handoff format.

The CSV payload stays plain text (split with the standard ``csv`` module);
business values are JSON cells interpreted through the Arrow schema carried
by each batch sidecar and recorded in the :class:`SchemaRegistry`.
'''

from .handoff import (BatchDescriptor, BatchReadout, BatchVerificationError,
                      HandoffResult, RejectedRow, discover_batches,
                      physical_missing_paths, read_batch, read_handoff,
                      try_read_batch, value_at, verify_sidecar, write_batch)
from .registry import (HandoffError, IncompatibleSchemaError,
                       SchemaMismatchError, SchemaRegistry, SchemaVersion,
                       UnregisteredVersionError, all_field_paths,
                       decode_schema, encode_schema, ensure_subset_compatible,
                       merge_schemas, path_is_declared, widen_table)

__all__ = [
    'BatchDescriptor',
    'BatchReadout',
    'BatchVerificationError',
    'HandoffError',
    'HandoffResult',
    'IncompatibleSchemaError',
    'RejectedRow',
    'SchemaMismatchError',
    'SchemaRegistry',
    'SchemaVersion',
    'UnregisteredVersionError',
    'all_field_paths',
    'decode_schema',
    'discover_batches',
    'encode_schema',
    'ensure_subset_compatible',
    'merge_schemas',
    'path_is_declared',
    'physical_missing_paths',
    'read_batch',
    'read_handoff',
    'try_read_batch',
    'value_at',
    'verify_sidecar',
    'widen_table',
    'write_batch',
]
