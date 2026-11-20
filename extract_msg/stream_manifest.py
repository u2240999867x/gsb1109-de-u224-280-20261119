from __future__ import annotations


"""
Convenience alias module for :mod:`extract_msg.audit`.
"""


__all__ = [
    'AuditManifest',
    'Manifest',
    'StreamEntry',
    'StreamInfo',
    'StreamListing',
    'StreamManifest',
    'StreamRecord',
    'StreamState',
    'StreamStatus',
    'buildStreamManifest',
    'buildStreamManifestEntry',
    'build_stream_manifest',
    'getStreamManifest',
    'getStreamManifestEntry',
    'manifestFromPath',
    'manifest_from_path',
    'streamManifest',
    'stream_manifest',
]


from .audit import (
        buildStreamManifest, buildStreamManifestEntry, build_stream_manifest,
        getStreamManifest, getStreamManifestEntry, Manifest, manifestFromPath,
        manifest_from_path, streamManifest, stream_manifest, StreamEntry,
        StreamInfo, StreamListing, StreamManifest, StreamRecord, StreamState,
        StreamStatus
    )


AuditManifest = StreamManifest
