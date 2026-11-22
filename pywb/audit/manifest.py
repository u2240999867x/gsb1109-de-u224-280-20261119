"""Audit manifests and stable selection identifiers.

A manifest is produced offline from one or more CDX index sources plus the
WARC volumes they point at.  Every index entry is treated as a *claim*: the
manifest only marks a representation as verified after its record has been
read out of a volume and its digests have been recomputed.

Selection identifiers are stable content-bound ids.  They never embed a
volume name or offset, so repacking the archives does not invalidate them.
"""

import json
import os
from collections import OrderedDict

from .digests import sha1_b32
from .evidence import EvidenceMissing, read_evidence


def _read_index_source(source_path):
    """Parse a CDXJ index source into ordered entry dicts."""
    entries = []
    with open(source_path, 'r', encoding='utf-8') as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            urlkey, timestamp, json_part = line.split(' ', 2)
            fields = json.loads(json_part)
            fields['urlkey'] = urlkey
            fields['timestamp'] = timestamp
            fields['index_source'] = os.path.basename(source_path)
            entries.append(fields)
    return entries


def _same_second(timestamp, warc_date):
    """Compare a CDX ``YYYYmmddHHMMSS`` timestamp with a WARC ISO date."""
    if not warc_date:
        return False
    compact = warc_date[:19].replace('-', '').replace(':', '').replace('T', '')
    return compact == timestamp


def make_selection_id(uri, timestamp, representation_digest, ordinal):
    """Deterministic, location-independent selection identifier."""
    material = '\x1f'.join([uri, timestamp, representation_digest,
                            str(ordinal)])
    return 'sel-' + sha1_b32(material.encode('utf-8'))


class Candidate(OrderedDict):
    """One registered location for a representation.

    A candidate is either verified from bytes on disk or carries an
    explicit unreadable reason.  File existence alone never verifies it.
    """

    @property
    def coordinate(self):
        return (self['volume'], self['offset'], self.get('length'))

    def to_json(self):
        return OrderedDict(self)


class ManifestEntry(OrderedDict):
    """One selectable representation (distinct header+body digest)."""

    @property
    def verified(self):
        return bool(self.get('representation_digest'))

    @property
    def selection_id(self):
        return self['selection_id']

    def registered_coordinates(self):
        """Coordinates this entry is allowed to be replayed from."""
        return [cand.coordinate for cand in self['candidates']]


class Manifest(object):
    """The first and second stage review lists."""

    def __init__(self, uri=None):
        self.uri = uri
        self.entries = []

    def verified_entries(self):
        return [entry for entry in self.entries if entry.verified]

    def unverifiable_entries(self):
        return [entry for entry in self.entries if not entry.verified]

    def get(self, selection_id):
        for entry in self.entries:
            if entry['selection_id'] == selection_id:
                return entry
        raise KeyError(selection_id)

    def to_json(self):
        return json.dumps({
            'uri': self.uri,
            'entries': [OrderedDict(entry) for entry in self.entries],
        }, indent=2)

    @classmethod
    def from_json(cls, text):
        data = json.loads(text)
        manifest = cls(uri=data.get('uri'))
        for raw in data['entries']:
            entry = ManifestEntry(raw)
            entry['candidates'] = [Candidate(c) for c in raw['candidates']]
            manifest.entries.append(entry)
        return manifest

    def save(self, path):
        with open(path, 'w', encoding='utf-8') as stream:
            stream.write(self.to_json())

    @classmethod
    def load(cls, path):
        with open(path, 'r', encoding='utf-8') as stream:
            return cls.from_json(stream.read())


def _verify_candidate(index_entry, volume_resolver):
    """Attempt to read the coordinate claimed by one index entry."""
    filename = index_entry['filename']
    offset = int(index_entry['offset'])
    length = index_entry.get('length')
    volume_path = volume_resolver(filename)
    candidate = Candidate([
        ('volume', volume_path),
        ('offset', offset),
        ('length', int(length) if length not in (None, '-') else None),
        ('index_source', index_entry['index_source']),
        ('index_order', index_entry['index_order']),
        ('claimed_digest', index_entry.get('digest')),
        ('claimed_warc_record_id', index_entry.get('warc_record_id')),
    ])
    try:
        evidence = read_evidence(volume_path, offset, length)
    except EvidenceMissing as exc:
        candidate['verified'] = False
        candidate['reason'] = str(exc)
        return candidate, None

    mismatches = []
    actual_payload_digest = evidence['payload_digest']
    claimed = index_entry.get('digest')
    if claimed and claimed != '-' and claimed != actual_payload_digest:
        # The volume is readable and carries its own payload digest; a wrong
        # CDX digest field is a contradicted index claim, not missing evidence.
        mismatches.append(
            'cdx digest {0} contradicted by bytes read {1}'.format(
                claimed, actual_payload_digest))

    claimed_rid = index_entry.get('warc_record_id')
    if claimed_rid and claimed_rid != evidence['warc_record_id']:
        mismatches.append(
            'cdx WARC-Record-ID {0} contradicted by record header {1}'.format(
                claimed_rid, evidence['warc_record_id']))

    if evidence['target_uri'] != index_entry['url']:
        candidate['verified'] = False
        candidate['reason'] = 'target URI mismatch on read record'
        return candidate, None

    if not _same_second(index_entry['timestamp'], evidence['warc_date']):
        candidate['verified'] = False
        candidate['reason'] = 'WARC date is not the indexed UTC second'
        return candidate, None

    candidate['verified'] = True
    candidate['warc_record_id'] = evidence['warc_record_id']
    candidate['representation_digest'] = evidence['representation_digest']
    if mismatches:
        candidate['index_claim_mismatch'] = mismatches
    return candidate, evidence


def build_manifest(index_paths, volume_resolver, uri=None):
    """Build a manifest from CDXJ sources and locally readable volumes.

    Each index entry becomes a candidate coordinate.  Candidates are read
    one by one, then grouped into one manifest entry per distinct
    representation digest, so same-second different representations stay
    separate while byte-identical independent copies share an identifier.
    """
    index_entries = []
    for order, source_path in enumerate(index_paths):
        for source_order, entry in enumerate(_read_index_source(source_path)):
            entry['index_order'] = (order, source_order)
            index_entries.append(entry)

    if uri is None and index_entries:
        uri = index_entries[0]['url']

    groups = OrderedDict()
    for index_entry in index_entries:
        groups.setdefault(
            (index_entry['url'], index_entry['timestamp']), []
        ).append(index_entry)

    manifest = Manifest(uri=uri)

    for (entry_uri, timestamp), claims in groups.items():
        # representation digest -> list of verified candidates/evidence
        representations = OrderedDict()
        failed_candidates = []
        for claim in claims:
            candidate, evidence = _verify_candidate(claim, volume_resolver)
            if not candidate['verified']:
                failed_candidates.append((candidate, claim))
                continue
            representations.setdefault(
                evidence['representation_digest'], []
            ).append((candidate, evidence, claim))

        # Stable ordering: selection ids must not depend on which index
        # source was listed first.  Content digests are location independent,
        # so sorting by them keeps ids identical after an index-source swap.
        ordered_reps = sorted(representations.items(),
                              key=lambda item: item[0])
        for ordinal, (rep_digest, matches) in enumerate(ordered_reps):
            first_evidence = matches[0][1]
            candidates = [match[0] for match in matches]
            selection_id = make_selection_id(entry_uri, timestamp,
                                             rep_digest, ordinal)
            entry = ManifestEntry([
                ('selection_id', selection_id),
                ('uri', entry_uri),
                ('timestamp', timestamp),
                ('status', first_evidence['status']),
                ('mime', _content_type(first_evidence['headers'])),
                ('representation_digest', rep_digest),
                ('header_digest', first_evidence['header_digest']),
                ('payload_digest', first_evidence['payload_digest']),
                ('candidates', candidates),
                ('verified', True),
                ('reason', None),
            ])
            manifest.entries.append(entry)

        for ordinal, (candidate, claim) in enumerate(failed_candidates,
                                                      start=len(representations)):
            selection_id = make_selection_id(
                entry_uri, timestamp,
                claim.get('digest') or 'unreadable', ordinal)
            entry = ManifestEntry([
                ('selection_id', selection_id),
                ('uri', entry_uri),
                ('timestamp', timestamp),
                ('status', claim.get('status')),
                ('mime', claim.get('mime')),
                ('representation_digest', None),
                ('header_digest', None),
                ('payload_digest', None),
                ('candidates', [candidate]),
                ('verified', False),
                ('reason', candidate['reason']),
            ])
            manifest.entries.append(entry)

    return manifest


def _content_type(headers):
    for name, value in headers:
        if name.lower() == 'content-type':
            return value
    return None
