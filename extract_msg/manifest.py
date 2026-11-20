from __future__ import annotations


"""
A convenience alias module for :mod:`extract_msg.audit`.

The manifest implementation lives in :mod:`extract_msg.audit`; this module
exists so callers can import it under the name they expect without importing
the whole package.
"""


__all__ = [
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
    'Manifest',
    'streamManifest',
    'stream_manifest',
]


from .audit import (
        buildStreamManifest, buildStreamManifestEntry, build_stream_manifest,
        getStreamManifest, Manifest, streamManifest, stream_manifest,
        StreamEntry, StreamInfo, StreamListing, StreamManifest, StreamRecord,
        StreamState, StreamStatus
    )
