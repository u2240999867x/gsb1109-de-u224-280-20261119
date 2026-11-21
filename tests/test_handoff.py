import csv
import hashlib
import io
import json
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from parquet_tools.commands.handoff import (
    BatchVerificationError,
    HandoffError,
    IncompatibleSchemaError,
    SchemaMismatchError,
    SchemaRegistry,
    UnregisteredVersionError,
    all_field_paths,
    discover_batches,
    encode_schema,
    ensure_subset_compatible,
    merge_schemas,
    path_is_declared,
    physical_missing_paths,
    read_batch,
    read_handoff,
    try_read_batch,
    value_at,
    write_batch,
)
from parquet_tools.commands.handoff.handoff import _validate_paths


V1 = 'v1-2026-01-01'
V2 = 'v2-2026-02-01'


def _v1_schema():
    event = pa.struct([('code', pa.string()), ('qty', pa.int32())])
    return pa.schema([
        ('id', pa.int64()),
        ('amount', pa.decimal128(18, 2)),
        ('note', pa.string()),
        ('payload', pa.struct([('events', pa.list_(event))])),
    ])


def _v2_schema():
    event = pa.struct([
        ('code', pa.string()),
        ('qty', pa.int32()),
        ('label', pa.string()),
    ])
    return pa.schema([
        ('id', pa.int64()),
        ('amount', pa.decimal128(20, 4)),
        ('note', pa.string()),
        ('payload', pa.struct([('events', pa.list_(event))])),
    ])


@pytest.fixture
def registry():
    registry = SchemaRegistry()
    registry.add(V1, _v1_schema())
    registry.add(V2, _v2_schema())
    return registry


@pytest.fixture
def workspace(tmp_path):
    parquet_dir = tmp_path / 'parquet'
    handoff_dir = tmp_path / 'handoff'
    parquet_dir.mkdir()
    handoff_dir.mkdir()
    return parquet_dir, handoff_dir


def _batch1_rows():
    return [
        {'id': 1, 'amount': Decimal('12.50'), 'note': 'plain',
         'payload': {'events': [{'code': 'A', 'qty': 1}, None]}},
        {'id': 2, 'amount': Decimal('3.00'), 'note': None, 'payload': None},
        {'id': 3, 'amount': Decimal('4.00'), 'note': 'empty list',
         'payload': {'events': []}},
        {'id': 4, 'amount': Decimal('5.00'), 'note': 'null-only list',
         'payload': {'events': [None]}},
    ]


def _batch2_rows():
    return [
        {'id': 10,
         'amount': Decimal('9007199254740993.1234'),
         'note': 'He said "hi", then\nline two',
         'payload': {'events': [{'code': 'B', 'qty': 2, 'label': None}]}},
        {'id': 11, 'amount': Decimal('7.5000'), 'note': 'x',
         'payload': {'events': [{'code': 'C', 'qty': 3, 'label': 'VIP'}]}},
        {'id': 12, 'amount': Decimal('100.0000'), 'note': 'y', 'payload': None},
    ]


def _write_parquet(path, rows, schema):
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def test_full_handoff_round_trip(registry, workspace):
    parquet_dir, handoff_dir = workspace

    # --- sender side: parquet sources, including physically incomplete files ---
    b1_path = parquet_dir / 'batch1.parquet'
    _write_parquet(b1_path, _batch1_rows(), _v1_schema())

    # batch 1 source parquet with a physically missing top-level column
    v1_without_note = pa.schema([
        field for field in _v1_schema() if field.name != 'note'
    ])
    b1_missing_path = parquet_dir / 'batch1_missing_note.parquet'
    note_less_rows = [
        {'id': 5, 'amount': Decimal('9.99'),
         'payload': {'events': [{'code': 'D', 'qty': 4}]}},
    ]
    _write_parquet(b1_missing_path, note_less_rows, v1_without_note)
    assert physical_missing_paths(str(b1_missing_path), _v1_schema()) == ['note']

    # batch 2 source whose physical structure still lacks the new member
    v2_event = pa.struct([
        ('code', pa.string()), ('qty', pa.int32()), ('label', pa.string())])
    v1_event = pa.struct([('code', pa.string()), ('qty', pa.int32())])
    b2_old_shape_path = parquet_dir / 'batch2_old_shape.parquet'
    _write_parquet(
        b2_old_shape_path,
        [{'id': 20, 'amount': Decimal('8.25'), 'note': 'legacy shape',
          'payload': {'events': [{'code': 'E', 'qty': 5}]}}],
        pa.schema([
            ('id', pa.int64()),
            ('amount', pa.decimal128(18, 2)),
            ('note', pa.string()),
            ('payload', pa.struct([('events', pa.list_(v1_event))])),
        ]))
    missing = physical_missing_paths(str(b2_old_shape_path), _v2_schema())
    assert 'payload.events[].label' in missing
    ensure_subset_compatible(pq.read_schema(b2_old_shape_path), _v2_schema())

    # --- export old batch first, under the old definition ---
    table_b1 = pq.read_table(b1_path)
    desc_b1 = write_batch(table_b1, str(handoff_dir), 'batch1', V1, registry)
    b1_csv = handoff_dir / 'batch1.csv'
    b1_bytes_before = b1_csv.read_bytes()

    # the physically incomplete parquet is exported under v1 too: the missing
    # column is projected to null, while its physical absence stays recorded
    table_gap = pq.read_table(b1_missing_path, schema=_v1_schema())
    desc_gap = write_batch(table_gap, str(handoff_dir), 'batch1_gap', V1, registry)
    gap_rows = read_batch(desc_gap.csv_path, registry).table.to_pylist()
    assert gap_rows[0]['note'] is None and 'note' in gap_rows[0]
    assert physical_missing_paths(str(b1_missing_path), _v1_schema()) == ['note']

    # --- then evolve the definition and export batch 2 under the new one ---
    table_b2 = pa.Table.from_pylist(_batch2_rows(), schema=_v2_schema())
    desc_b2 = write_batch(table_b2, str(handoff_dir), 'batch2', V2, registry)

    # second batch-2 file, projected from the physically older parquet
    table_b2_legacy = pq.read_table(b2_old_shape_path, schema=_v2_schema())
    desc_b2_legacy = write_batch(
        table_b2_legacy, str(handoff_dir), 'batch2_legacy', V2, registry)

    # old CSV stays byte-for-byte untouched once newer batches are exported
    assert b1_csv.read_bytes() == b1_bytes_before
    assert desc_b1.version == V1 and desc_b2.version == V2

    # every batch's exact definition is recoverable
    for descriptor in (desc_b1, desc_b2, desc_b2_legacy):
        sidecar = json.loads(Path(descriptor.sidecar_path).read_text())
        assert registry.get(descriptor.version).schema.equals(
            read_batch(descriptor.csv_path, registry).declared_schema)
        assert sidecar['field_paths'] == all_field_paths(
            registry.get(descriptor.version).schema)
    v1_paths = json.loads(Path(desc_b1.sidecar_path).read_text())['field_paths']
    assert 'payload.events[].label' not in v1_paths
    assert 'payload.events[].label' in all_field_paths(_v2_schema())

    # --- receiver holds both early CSVs, then restores rows in swapped order ---
    result = read_handoff(
        str(handoff_dir), registry,
        delivery_order=['batch2.csv', 'batch2_legacy.csv', 'batch1.csv',
                        'batch1_gap.csv'])
    assert result.delivery_order == ['batch2.csv', 'batch2_legacy.csv',
                                     'batch1.csv', 'batch1_gap.csv']
    assert result.rejected_rows == []
    restored_ids = result.table.column('id').to_pylist()
    assert restored_ids[:4] == [10, 11, 12, 20]
    assert restored_ids[4:] == [1, 2, 3, 4, 5]

    rows = {row['id']: row for row in result.table.to_pylist()}

    # exact decimal value beyond float precision, source scale recoverable
    huge = rows[10]['amount']
    assert huge == Decimal('9007199254740993.1234')
    assert int(huge) == 9007199254740993
    assert str(rows[11]['amount']) == '7.5000'
    b1_readout = next(r for r in result.batch_readouts if r.version == V1)
    assert b1_readout.declared_schema.field('amount').type.equals(
        pa.decimal128(18, 2))
    b1_original = b1_readout.table.to_pylist()[0]
    assert str(b1_original['amount']) == '12.50'
    assert result.merged_schema.field('amount').type.equals(pa.decimal128(20, 4))
    assert str(rows[1]['amount']) == '12.5000'

    # text containing delimiter, quote and newline survives via plain csv reader
    assert rows[10]['note'] == 'He said "hi", then\nline two'
    raw_b2 = (handoff_dir / 'batch2.csv').read_text()
    assert '""' in raw_b2  # CSV quote escaping stays visible in the raw file
    with open(handoff_dir / 'batch2.csv', newline='', encoding='utf-8') as handle:
        plain_rows = list(csv.reader(handle))
    assert plain_rows[0] == _v2_schema().names
    # the plain reader only splits the table; business values need the schema/JSON layer
    assert json.loads(plain_rows[1][2]) == 'He said "hi", then\nline two'

    # nested shapes: null element, empty list, null-only list, struct null
    assert rows[1]['payload']['events'][1] is None
    assert rows[3]['payload']['events'] == []
    assert rows[4]['payload']['events'] == [None]
    assert rows[2]['payload'] is None
    assert rows[2]['note'] is None

    # same-named member: absent in v1, explicit null vs actual value in v2
    assert not path_is_declared(_v1_schema(), ['payload', 'events', 'label'])
    assert path_is_declared(_v2_schema(), ['payload', 'events', 'label'])
    label_path = ['payload', 'events', 0, 'label']
    present, value = value_at(rows[10], ['payload'])
    assert present and value['events'][0]['label'] is None
    assert 'label' in value['events'][0]
    assert rows[11]['payload']['events'][0]['label'] == 'VIP'
    # old rows get the member only through the declared compatibility fill
    assert rows[1]['payload']['events'][0]['label'] is None

    # read the old batch on its own: under v1 the member does not exist at all
    v1_readout = read_batch(str(handoff_dir / 'batch1.csv'), registry)
    v1_row1 = next(row for row in v1_readout.table.to_pylist() if row['id'] == 1)
    assert value_at(v1_row1, label_path) == (False, None)
    # ... whereas the merged view reports an explicit compatibility-filled null
    assert value_at(rows[1], label_path) == (True, None)
    # explicit null, actual value and null struct container stay distinguishable
    assert value_at(rows[10], label_path) == (True, None)
    assert value_at(rows[11], label_path) == (True, 'VIP')
    assert value_at(rows[2], ['payload']) == (True, None)
    assert value_at(rows[2], ['payload', 'events']) == (False, None)
    # a null element inside a list keeps its slot and remains null
    assert value_at(rows[1], ['payload', 'events', 1]) == (True, None)

    # legacy-shaped batch 2 file: member physically absent, value filled null
    assert physical_missing_paths(str(b2_old_shape_path), _v2_schema()) == [
        'payload.events[].label']
    legacy_rows = {row['id']: row for row in
                   read_batch(desc_b2_legacy.csv_path, registry).table.to_pylist()}
    assert legacy_rows[20]['payload']['events'][0]['label'] is None
    assert value_at(legacy_rows[20], label_path) == (True, None)


def test_registry_persistence_and_merge_rules(registry, tmp_path):
    path = tmp_path / 'registry.json'
    registry.save(str(path))
    reloaded = SchemaRegistry.load(str(path))
    assert reloaded.get(V1).schema.equals(_v1_schema())
    assert reloaded.get(V2).schema.equals(_v2_schema())
    merged = registry.merged_schema()
    assert merged.field('amount').type.equals(pa.decimal128(20, 4))
    assert merged.field('payload').type.field('events').type.value_type.field(
        'label').type.equals(pa.string())

    # incompatible evolution is refused, not silently merged
    bad = pa.schema([('id', pa.string())])
    with pytest.raises(IncompatibleSchemaError):
        merge_schemas(_v1_schema(), bad)
    non_nullable_add = pa.schema([
        ('id', pa.int64()),
        ('amount', pa.decimal128(18, 2)),
        ('note', pa.string()),
        ('payload', pa.struct([('events', pa.list_(pa.struct([
            pa.field('code', pa.string()),
            pa.field('qty', pa.int32()),
            pa.field('label', pa.string(), nullable=False),
        ])))] )),
    ])
    with pytest.raises(IncompatibleSchemaError):
        merge_schemas(_v1_schema(), non_nullable_add)


def test_missing_old_definition_rejects_only_that_batch(registry, workspace):
    _, handoff_dir = workspace
    write_batch(pa.Table.from_pylist(_batch1_rows(), schema=_v1_schema()),
                str(handoff_dir), 'batch1', V1, registry)
    write_batch(pa.Table.from_pylist(_batch2_rows(), schema=_v2_schema()),
                str(handoff_dir), 'batch2', V2, registry)

    # a receiver whose registry never carried the old definition
    trimmed = SchemaRegistry()
    trimmed.add(V2, _v2_schema())
    result = read_handoff(str(handoff_dir), trimmed)

    rejected_batches = {r.batch_name for r in result.rejected_rows}
    assert rejected_batches == {'batch1'}
    readouts = {r.batch_name: r for r in result.batch_readouts}
    assert readouts['batch1'].verified is False
    assert readouts['batch2'].verified is True
    assert isinstance(readouts['batch1'].rejected_rows[0].reason, str)
    # rows that can still be confirmed under a known version reconcile fine
    assert result.table.column('id').to_pylist() == [10, 11, 12]

    with pytest.raises(UnregisteredVersionError):
        read_batch(str(handoff_dir / 'batch1.csv'), trimmed)


def test_mislabeled_version_fails_signature(registry, workspace):
    _, handoff_dir = workspace
    desc = write_batch(pa.Table.from_pylist(_batch2_rows(), schema=_v2_schema()),
                       str(handoff_dir), 'batch2', V2, registry)
    sidecar_path = Path(desc.sidecar_path)
    sidecar = json.loads(sidecar_path.read_text())
    sidecar['version'] = 'v9-typo'
    sidecar_path.write_text(json.dumps(sidecar))

    readout = try_read_batch(desc.csv_path, registry)
    assert readout.verified is False
    assert readout.table is None
    assert len(readout.rejected_rows) == desc.row_count

    # strict read raises instead; the same failure cannot be guessed under v2
    with pytest.raises(BatchVerificationError):
        read_batch(desc.csv_path, registry)

    # unknown version with a self-consistent signature still cannot verify
    sidecar = json.loads(sidecar_path.read_text())
    sidecar['version'] = 'v3-foreign'
    digest = sidecar['csv_sha256']
    sidecar['signature'] = hashlib.sha256(
        f"{digest}:{sidecar['schema_sha256']}:v3-foreign".encode()).hexdigest()
    sidecar_path.write_text(json.dumps(sidecar))
    with pytest.raises(UnregisteredVersionError):
        read_batch(desc.csv_path, registry)


def test_tampered_csv_and_undeclared_member(registry, workspace):
    _, handoff_dir = workspace
    desc = write_batch(pa.Table.from_pylist(_batch2_rows(), schema=_v2_schema()),
                       str(handoff_dir), 'batch2', V2, registry)
    csv_path = Path(desc.csv_path)
    original = csv_path.read_bytes()
    csv_path.write_bytes(original.replace(b'7.5000', b'7.9999'))

    with pytest.raises(BatchVerificationError):
        read_batch(str(csv_path), registry)

    # row-level guard: an undeclared struct member cannot be guessed in
    record = {'id': 1, 'amount': Decimal('1.00'), 'note': 'n',
              'payload': {'events': [{'code': 'A', 'qty': 1, 'ghost': 'x'}]}}
    with pytest.raises(HandoffError):
        _validate_paths(record, pa.struct(list(_v1_schema())), [])
    valid = {'id': 1, 'amount': Decimal('1.00'), 'note': None,
             'payload': {'events': [None]}}
    _validate_paths(valid, pa.struct(list(_v1_schema())), [])


def test_registry_schema_mismatch_is_detected(tmp_path):
    registry = SchemaRegistry()
    registry.add(V1, _v1_schema())
    other_registry = SchemaRegistry()
    other_registry.add(V1, _v2_schema())  # same version label, different shape

    handoff_dir = tmp_path / 'handoff'
    desc = write_batch(
        pa.Table.from_pylist(_batch1_rows(), schema=_v1_schema()),
        str(handoff_dir), 'batch1', V1, registry)
    with pytest.raises(SchemaMismatchError):
        read_batch(desc.csv_path, other_registry)


def test_flat_export_keeps_original_format():
    df = pd.DataFrame({'one': [-1, 2.5], 'two': ['foo', 'a,b"c']})
    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    assert buffer.getvalue() == 'one,two\n-1.0,foo\n2.5,"a,b""c"\n'


def test_discover_batches(registry, workspace):
    _, handoff_dir = workspace
    assert discover_batches(str(handoff_dir)) == []
    write_batch(pa.Table.from_pylist(_batch1_rows(), schema=_v1_schema()),
                str(handoff_dir), 'batch1', V1, registry)
    (handoff_dir / 'stray.csv').write_text('id\n1\n')
    found = discover_batches(str(handoff_dir))
    assert [path.name for path in found] == ['batch1.csv']
    assert encode_schema(_v1_schema()) != encode_schema(_v2_schema())


def test_unreadable_or_unregistered_batches_never_crash(registry, workspace):
    _, handoff_dir = workspace
    desc = write_batch(pa.Table.from_pylist(_batch1_rows(), schema=_v1_schema()),
                       str(handoff_dir), 'batch1', V1, registry)

    # corrupted sidecar file
    Path(desc.sidecar_path).write_text('{not json')
    result = read_handoff(str(handoff_dir), registry)
    assert result.table.num_rows == 0
    assert result.rejected_rows and result.batch_readouts[0].verified is False

    # empty registry: no definition at all to verify against
    empty = SchemaRegistry()
    result_empty = read_handoff(str(handoff_dir), empty)
    assert result_empty.table.num_rows == 0
    assert result_empty.rejected_rows[0].batch_name == 'batch1'
