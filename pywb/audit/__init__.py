"""Offline archive review tooling.

This package gives every distinct archived representation of a URI a
stable, location-independent selection identifier, backed by read evidence
from the WARC volumes, and keeps those identifiers resolvable after the
archives are repacked.  See :mod:`pywb.audit.manifest`,
:mod:`pywb.audit.migration` and :mod:`pywb.audit.replay`.
"""

from .digests import header_digest, payload_digest, representation_digest
from .evidence import EvidenceMissing, read_evidence, scan_volume
from .manifest import Manifest, build_manifest
from .migration import (MigrationLedger, VolumeCatalogue, apply_relocations,
                        plan_relocations)
from .replay import Replayer, verify_manifest_offline

__all__ = [
    'EvidenceMissing',
    'Manifest',
    'MigrationLedger',
    'Replayer',
    'VolumeCatalogue',
    'apply_relocations',
    'build_manifest',
    'header_digest',
    'payload_digest',
    'plan_relocations',
    'read_evidence',
    'representation_digest',
    'scan_volume',
    'verify_manifest_offline',
]
