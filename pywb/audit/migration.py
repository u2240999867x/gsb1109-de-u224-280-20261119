"""Migration ledger for repacked archives.

When archives are repacked a response moves to a new volume and a new
offset, and an old offset may be reused by a completely different response.
Coordinates therefore cannot be trusted after a move.  A relocation is only
recorded when a record read from the *new* volume proves, via its
representation digest, that it is byte-for-byte the same representation
(status line, header block and body) that the selection id was registered
for.

Stage signals make partial migrations explicit:

* ``stable``    - old index and old volume agree;
* ``mixed``     - index swap in progress, an old index may meet a new
                  volume catalogue (or the new volume may be absent);
* ``relocated`` - new index and new volume catalogue are both in place.

A relocation that cannot yet be proven returns a retryable
:class:`~pywb.audit.evidence.EvidenceMissing`; the caller must not fall back
to a default candidate or treat a different response as the selected one.
"""

import json
from collections import OrderedDict

from .evidence import EvidenceMissing, scan_volume

STAGE_STABLE = 'stable'
STAGE_MIXED = 'mixed'
STAGE_RELOCATED = 'relocated'

STAGES = (STAGE_STABLE, STAGE_MIXED, STAGE_RELOCATED)


class VolumeCatalogue(object):
    """Catalogue of every response record actually read from one volume.

    This is the migration-stage signal: it is built by reading the new
    volume, never by trusting index coordinates.
    """

    def __init__(self, volume_path, records):
        self.volume_path = volume_path
        self.records = records
        self._by_offset = {record['offset']: record for record in records}
        self._by_digest = {}
        self._by_record_id = {}
        for record in records:
            self._by_digest.setdefault(
                record['representation_digest'], []).append(record)
            self._by_record_id.setdefault(
                record['warc_record_id'], []).append(record)

    @classmethod
    def scan(cls, volume_path):
        return cls(volume_path, list(scan_volume(volume_path)))

    def at_offset(self, offset):
        return self._by_offset.get(offset)

    def by_representation(self, representation_digest):
        return list(self._by_digest.get(representation_digest, ()))

    def by_record_id(self, warc_record_id):
        return list(self._by_record_id.get(warc_record_id, ()))


class MigrationRecord(OrderedDict):
    """Evidence trail for one selection id across one repack."""

    @property
    def verified(self):
        return bool(self.get('verified'))


class MigrationLedger(object):
    def __init__(self, stage=STAGE_STABLE):
        self.stage = stage
        self.records = OrderedDict()

    def add(self, record):
        self.records[record['selection_id']] = record

    def get(self, selection_id):
        return self.records.get(selection_id)

    def verified_coordinate(self, selection_id):
        """New coordinate only when the relocation was proven."""
        record = self.records.get(selection_id)
        if record and record['verified']:
            return (record['new_volume'], record['new_offset'],
                    record.get('new_length'))
        return None

    def to_json(self):
        return json.dumps(
            {'stage': self.stage,
             'records': list(self.records.values())}, indent=2)

    @classmethod
    def from_json(cls, text):
        data = json.loads(text)
        ledger = cls(stage=data.get('stage', STAGE_STABLE))
        for raw in data['records']:
            ledger.add(MigrationRecord(raw))
        return ledger

    def save(self, path):
        with open(path, 'w', encoding='utf-8') as stream:
            stream.write(self.to_json())

    @classmethod
    def load(cls, path):
        with open(path, 'r', encoding='utf-8') as stream:
            return cls.from_json(stream.read())


def _find_relocated_record(entry, catalogues):
    """Locate the same representation in the new volumes by digest."""
    target_digest = entry['representation_digest']
    for catalogue in catalogues:
        matches = catalogue.by_representation(target_digest)
        for record in matches:
            return catalogue, record
    return None, None


def plan_relocations(manifest, catalogues, stage=STAGE_RELOCATED):
    """Plan (or attempt) every relocation against new-volume catalogues.

    Verified entries whose representation is found in a catalogue get a
    proven relocation.  Entries whose representation cannot be found yet get
    an unverified, retryable record carrying the coordinates that were
    tried.  Manifest entries that were never readable stay unverified.
    """
    if stage not in STAGES:
        raise ValueError('unknown migration stage: {0}'.format(stage))

    ledger = MigrationLedger(stage=stage)

    for entry in manifest.entries:
        selection_id = entry['selection_id']
        old_coordinates = [
            OrderedDict([
                ('volume', candidate['volume']),
                ('offset', candidate['offset']),
                ('length', candidate.get('length')),
            ])
            for candidate in entry['candidates']
        ]

        if not entry['verified']:
            ledger.add(MigrationRecord([
                ('selection_id', selection_id),
                ('stage', stage),
                ('verified', False),
                ('old_coordinates', old_coordinates),
                ('new_volume', None),
                ('new_offset', None),
                ('new_length', None),
                ('representation_digest', entry['representation_digest']),
                ('reason', 'source representation was never readable'),
            ]))
            continue

        catalogue, record = _find_relocated_record(entry, catalogues)
        if record is None:
            tried = [catalogue.volume_path for catalogue in catalogues]
            ledger.add(MigrationRecord([
                ('selection_id', selection_id),
                ('stage', stage),
                ('verified', False),
                ('old_coordinates', old_coordinates),
                ('new_volume', None),
                ('new_offset', None),
                ('new_length', None),
                ('representation_digest', entry['representation_digest']),
                ('reason',
                 'no record of the registered representation in catalogues: '
                 + ', '.join(tried)),
            ]))
            continue

        ledger.add(MigrationRecord([
            ('selection_id', selection_id),
            ('stage', stage),
            ('verified', True),
            ('old_coordinates', old_coordinates),
            ('new_volume', record['volume']),
            ('new_offset', record['offset']),
            ('new_length', record['length']),
            ('representation_digest', record['representation_digest']),
            ('header_digest', record['header_digest']),
            ('payload_digest', record['payload_digest']),
            ('warc_record_id', record['warc_record_id']),
            ('reason', None),
        ]))

    return ledger


def apply_relocations(manifest, ledger):
    """Build the post-repack manifest.

    Selection ids and representation digests are preserved.  A proven
    relocation swaps the candidate coordinates to the new volume; an
    unproven one keeps the old coordinates and their unverified state, so
    the new list still shows every same-second representation separately as
    well as the entries that cannot be checked.
    """
    rebuilt = type(manifest)(uri=manifest.uri)
    for entry in manifest.entries:
        record = ledger.get(entry['selection_id'])
        updated = OrderedDict(entry)
        if record is not None and record['verified']:
            updated['candidates'] = [OrderedDict([
                ('volume', record['new_volume']),
                ('offset', record['new_offset']),
                ('length', record.get('new_length')),
                ('index_source', 'migration-ledger'),
                ('index_order', [0, 0]),
                ('claimed_digest', record['payload_digest']),
                ('claimed_warc_record_id', record['warc_record_id']),
                ('verified', True),
                ('warc_record_id', record['warc_record_id']),
                ('representation_digest', record['representation_digest']),
                ('migrated_from', record['old_coordinates']),
                ('migration_stage', record['stage']),
            ])]
        elif record is not None:
            updated['verified'] = False
            updated['reason'] = record['reason']
        rebuilt.entries.append(type(entry)(updated))
    return rebuilt
