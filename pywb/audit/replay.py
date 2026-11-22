"""Offline replay of a selected representation.

Clicking a selection identifier may only open:

1. a candidate coordinate registered for that identifier in the manifest, or
2. a new coordinate recorded in a migration ledger whose relocation was
   proven by representation-digest equality.

It must never re-query an index, scan all captures of a URL online, or fall
back to a default candidate.  If the bytes found at an allowed coordinate do
not hash to the registered representation digest (for example because an
old offset was reused by a different response after repacking), the lookup
fails with a retryable :class:`~pywb.audit.evidence.EvidenceMissing`.
"""

from collections import OrderedDict

from .evidence import EvidenceMissing, read_evidence


class ReplayResult(OrderedDict):
    """The verified response returned for one selection id."""


class Replayer(object):
    """Resolves selection ids against a manifest, optionally with a ledger.

    The object deliberately holds no index client and no URL-based lookup:
    the only inputs are the manifest and (optionally) a migration ledger.
    """

    def __init__(self, manifest, ledger=None):
        self.manifest = manifest
        self.ledger = ledger

    def _allowed_coordinates(self, entry):
        """All coordinates this identifier is allowed to be read from."""
        coordinates = []
        for candidate in entry['candidates']:
            coordinates.append(OrderedDict([
                ('volume', candidate['volume']),
                ('offset', candidate['offset']),
                ('length', candidate.get('length')),
                ('origin', 'registered'),
            ]))

        if self.ledger is not None:
            record = self.ledger.get(entry['selection_id'])
            new_coordinate = self.ledger.verified_coordinate(
                entry['selection_id']) if record else None
            if new_coordinate is not None:
                volume, offset, length = new_coordinate
                coordinates.append(OrderedDict([
                    ('volume', volume),
                    ('offset', offset),
                    ('length', length),
                    ('origin', 'migration-ledger:' + record['stage']),
                ]))
        return coordinates

    def replay(self, selection_id):
        """Return the representation registered for ``selection_id``.

        Every allowed coordinate is tried in order.  A readable record whose
        representation digest differs from the registered one is refused,
        not returned.  Raises :class:`EvidenceMissing` if no coordinate can
        prove the registered representation.
        """
        try:
            entry = self.manifest.get(selection_id)
        except KeyError:
            raise EvidenceMissing(
                'unknown selection id: {0}'.format(selection_id))

        if not entry['verified']:
            raise EvidenceMissing(
                'selection id {0} has no readable source representation: '
                '{1}'.format(selection_id, entry.get('reason')),
                candidates=entry['candidates'])

        registered_digest = entry['representation_digest']
        attempts = []
        for coordinate in self._allowed_coordinates(entry):
            try:
                evidence = read_evidence(coordinate['volume'],
                                         coordinate['offset'],
                                         coordinate.get('length'))
            except EvidenceMissing as exc:
                attempts.append(OrderedDict([
                    ('coordinate', coordinate),
                    ('outcome', 'unreadable'),
                    ('reason', str(exc)),
                ]))
                continue

            if evidence['representation_digest'] != registered_digest:
                # An old offset reused by another response, or a catalogue
                # entry that cannot prove it is the original representation.
                attempts.append(OrderedDict([
                    ('coordinate', coordinate),
                    ('outcome', 'representation-mismatch'),
                    ('registered_digest', registered_digest),
                    ('actual_digest', evidence['representation_digest']),
                ]))
                continue

            return ReplayResult([
                ('selection_id', selection_id),
                ('uri', entry['uri']),
                ('timestamp', entry['timestamp']),
                ('status', evidence['status']),
                ('headers', evidence['headers']),
                ('body', evidence['body']),
                ('coordinate', coordinate),
                ('representation_digest', evidence['representation_digest']),
                ('payload_digest', evidence['payload_digest']),
                ('header_digest', evidence['header_digest']),
                ('warc_record_id', evidence['warc_record_id']),
                ('attempts', attempts),
            ])

        raise EvidenceMissing(
            'no readable coordinate proves selection id {0}; '
            'retryable, no default candidate used'.format(selection_id),
            candidates=attempts)


def verify_manifest_offline(manifest):
    """Re-check every manifest entry candidate by reading the volumes.

    Returns a JSON-serialisable report; works fully offline and can be run
    entry by entry.  Only coordinates stored in the manifest are opened.
    """
    report = []
    for entry in manifest.entries:
        entry_report = OrderedDict([
            ('selection_id', entry['selection_id']),
            ('uri', entry['uri']),
            ('timestamp', entry['timestamp']),
            ('registered', entry['verified']),
            ('candidate_checks', []),
            ('verified', False),
        ])
        for candidate in entry['candidates']:
            check = OrderedDict([
                ('volume', candidate['volume']),
                ('offset', candidate['offset']),
            ])
            try:
                evidence = read_evidence(candidate['volume'],
                                         candidate['offset'],
                                         candidate.get('length'))
            except EvidenceMissing as exc:
                check['outcome'] = 'unreadable'
                check['reason'] = str(exc)
                entry_report['candidate_checks'].append(check)
                continue

            if entry['verified'] and evidence['representation_digest'] != \
                    entry['representation_digest']:
                check['outcome'] = 'representation-mismatch'
                check['actual_digest'] = evidence['representation_digest']
            else:
                check['outcome'] = 'matches'
                check['representation_digest'] = \
                    evidence['representation_digest']
                entry_report['verified'] = True
            entry_report['candidate_checks'].append(check)

        report.append(entry_report)
    return report
