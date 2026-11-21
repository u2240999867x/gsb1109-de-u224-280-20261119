"""Resumable, complete S3 prefix listings.

The regular S3 wildcard reader only needs a list of keys.  Archival callers
also need to know whether that list is complete, where to continue after a
transient failure, and whether the service returned contradictory information
for the same object while pages were being fetched.

This module keeps the pagination state in a small serialisable value object.
It deliberately does not claim that a completed listing is a point-in-time
snapshot of the bucket: the only thing a terminal page proves is that the
service has no more pages for that pagination run.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class S3ManifestStatus(str, Enum):
    """The state a caller has to act on.

    ``COMPLETE``
        A terminal page was received and no contradiction was observed.
    ``PARTIAL``
        Only the pages confirmed so far are usable.  If a continuation token
        is present the caller can resume; otherwise the listing can be
        restarted.
    ``CONFLICT``
        Pagination may or may not have reached a terminal page, but at least
        one object key was observed with different metadata.  There is no
        single version of that object that can be confirmed from this run.
    ``STOPPED``
        The credential was used against another endpoint/target, or the
        service repeated a continuation token.  No completion is reported.
    """

    COMPLETE = 'complete'
    PARTIAL = 'partial'
    CONFLICT = 'conflict'
    STOPPED = 'stopped'

    # Readable aliases for callers that describe the same states differently.
    INCOMPLETE = 'partial'
    CONFLICTING = 'conflict'
    BLOCKED = 'stopped'


# Stable, machine-readable stop reasons.
STOP_ENDPOINT_MISMATCH = 'endpoint_mismatch'
STOP_BUCKET_MISMATCH = 'bucket_mismatch'
STOP_PREFIX_MISMATCH = 'prefix_mismatch'
STOP_CONTINUATION_CYCLE = 'continuation_cycle'
STOP_MISSING_CONTINUATION_TOKEN = 'missing_continuation_token'


@dataclass(frozen=True)
class S3ObjectObservation:
    """One observation of one object in a ``ListObjectsV2`` page."""

    key: str
    etag: Optional[str] = None
    size: Optional[int] = None
    last_modified: Optional[str] = None
    storage_class: Optional[str] = None
    version_id: Optional[str] = None
    checksum_algorithm: Tuple[str, ...] = ()
    checksum_type: Optional[str] = None

    @classmethod
    def from_s3_item(cls, item: Mapping[str, Any]) -> 'S3ObjectObservation':
        last_modified = _normalise_last_modified(item.get('LastModified'))

        checksum = item.get('ChecksumAlgorithm') or ()
        if isinstance(checksum, str):
            checksum = (checksum,)
        else:
            checksum = tuple(sorted(str(value) for value in checksum))

        size = item.get('Size')
        if size is not None:
            try:
                size = int(size)
            except (TypeError, ValueError):
                size = None

        return cls(
            key=str(item['Key']),
            etag=_optional_string(item.get('ETag', item.get('Etag'))),
            size=size,
            last_modified=last_modified,
            storage_class=_optional_string(item.get('StorageClass')),
            version_id=_optional_string(item.get('VersionId')),
            checksum_algorithm=checksum,
            checksum_type=_optional_string(item.get('ChecksumType')),
        )

    def __getitem__(self, name: str) -> Any:
        # Small mapping-style convenience for schedulers that serialise
        # entries without importing the dataclass.
        return getattr(self, name)

    def get(self, name: str, default: Any = None) -> Any:
        return getattr(self, name, default)

    @property
    def content_signature(self) -> Tuple[Any, ...]:
        """Everything except the key that identifies this observation.

        A difference in any of these fields means the service made two
        different statements about the same key during one pagination run.
        """
        return (
            self.etag,
            self.size,
            self.last_modified,
            self.storage_class,
            self.version_id,
            self.checksum_algorithm,
            self.checksum_type,
        )

    @property
    def metadata(self) -> Dict[str, Any]:
        return {
            'etag': self.etag,
            'size': self.size,
            'last_modified': self.last_modified,
            'storage_class': self.storage_class,
            'version_id': self.version_id,
            'checksum_algorithm': list(self.checksum_algorithm),
            'checksum_type': self.checksum_type,
        }

    def sort_key(self) -> Tuple[str, ...]:
        # The sort key never depends on the order in which pages arrived.
        return (self.key, ) + tuple(
            _sortable(value) for value in self.content_signature
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'key': self.key,
            'etag': self.etag,
            'size': self.size,
            'last_modified': self.last_modified,
            'storage_class': self.storage_class,
            'version_id': self.version_id,
            'checksum_algorithm': list(self.checksum_algorithm),
            'checksum_type': self.checksum_type,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'S3ObjectObservation':
        return cls(
            key=str(data.get('key', data.get('Key'))),
            etag=data.get('etag', data.get('ETag', data.get('Etag'))),
            size=data.get('size', data.get('Size')),
            last_modified=data.get('last_modified', data.get('LastModified')),
            storage_class=data.get('storage_class', data.get('StorageClass')),
            version_id=data.get('version_id', data.get('VersionId')),
            checksum_algorithm=tuple(data.get('checksum_algorithm') or ()),
            checksum_type=data.get('checksum_type', data.get('ChecksumType')),
        )


@dataclass(frozen=True)
class S3ManifestConflict:
    """All observations that disagree about one object key."""

    key: str
    observations: Tuple[S3ObjectObservation, ...]
    version_confirmed: bool = False
    reason: str = 'object_metadata_conflict'
    message: str = (
        'Conflicting observations for this key were returned during one '
        'listing run. The service did not identify a single version of the '
        'listing, so no version is confirmed and no observation was chosen '
        'by arrival order. Retry against a non-conflicting copy to obtain a '
        'complete listing.'
    )

    @property
    def winner(self) -> None:
        """Always ``None``: a conflict has no arrival-order winner."""
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'key': self.key,
            'reason': self.reason,
            'version_confirmed': self.version_confirmed,
            'message': self.message,
            'observations': [observation.to_dict() for observation in self.observations],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'S3ManifestConflict':
        return cls(
            key=str(data['key']),
            observations=tuple(
                sorted(
                    (S3ObjectObservation.from_dict(item) for item in data.get('observations', ())),
                    key=lambda observation: observation.sort_key(),
                )
            ),
            version_confirmed=bool(data.get('version_confirmed', False)),
            reason=str(data.get('reason', 'object_metadata_conflict')),
            message=str(data.get('message', cls.message)),
        )


@dataclass(frozen=True)
class S3Manifest:
    """A resumable listing result and the credential for continuing it.

    ``observations`` contains every distinct observation seen so far.
    ``objects`` contains only objects whose key has a single confirmed
    observation.  Conflicting keys are exposed through ``conflicts`` and are
    intentionally absent from ``objects``.
    """

    bucket: str
    prefix: str
    endpoint_url: Optional[str]
    status: S3ManifestStatus = S3ManifestStatus.PARTIAL
    observations: Tuple[S3ObjectObservation, ...] = ()
    conflicts: Tuple[S3ManifestConflict, ...] = ()
    continuation_token: Optional[str] = None
    pages_confirmed: int = 0
    pagination_complete: bool = False
    used_tokens: Tuple[str, ...] = ()
    error: Optional[str] = None
    stop_reason: Optional[str] = None
    snapshot_consistent: bool = False
    completeness_basis: str = 'terminal_page_received'
    manifest_version: int = 1

    @property
    def objects(self) -> Tuple[S3ObjectObservation, ...]:
        """Observations belonging to keys that are not in conflict."""
        conflict_keys = {conflict.key for conflict in self.conflicts}
        return tuple(
            observation for observation in self.observations
            if observation.key not in conflict_keys
        )

    @property
    def entries(self) -> Tuple[S3ObjectObservation, ...]:
        """Alias for every distinct observation received so far."""
        return self.observations

    @property
    def object_list(self) -> Tuple[S3ObjectObservation, ...]:
        """Same as ``objects``; explicit for callers that build file lists."""
        return self.objects

    @property
    def keys(self) -> Tuple[str, ...]:
        """Unique keys in stable order, including keys currently in conflict."""
        return tuple(sorted({observation.key for observation in self.observations}))

    @property
    def confirmed_keys(self) -> Tuple[str, ...]:
        """Keys with exactly one content observation."""
        return tuple(observation.key for observation in self.objects)

    @property
    def conflicting_keys(self) -> Tuple[str, ...]:
        return tuple(conflict.key for conflict in self.conflicts)

    @property
    def unresolved(self) -> Tuple[S3ManifestConflict, ...]:
        return self.conflicts

    @property
    def is_snapshot(self) -> bool:
        return self.snapshot_consistent

    @property
    def is_complete(self) -> bool:
        return self.status is S3ManifestStatus.COMPLETE

    @property
    def complete(self) -> bool:
        return self.is_complete

    @property
    def conflict(self) -> bool:
        return bool(self.conflicts)

    @property
    def page_count(self) -> int:
        return self.pages_confirmed

    @property
    def next_token(self) -> Optional[str]:
        return self.continuation_token

    @property
    def endpoint(self) -> Optional[str]:
        return self.endpoint_url

    @property
    def all_observations(self) -> Tuple[S3ObjectObservation, ...]:
        return self.observations

    def __len__(self) -> int:
        return len(self.keys)

    def __iter__(self):
        return iter(self.keys)

    def __contains__(self, key: object) -> bool:
        return key in self.keys

    @property
    def observation_count(self) -> int:
        return len(self.observations)

    @property
    def resume_token(self) -> Optional[str]:
        """Alias for ``continuation_token`` used by simple schedulers."""
        return self.continuation_token

    @property
    def token(self) -> Optional[str]:
        return self.continuation_token

    @property
    def checkpoint(self) -> Optional[str]:
        return self.continuation_token

    @property
    def state(self) -> str:
        return self.status.value

    @property
    def has_more(self) -> bool:
        """Whether another page can still be requested with this credential."""
        if self.pagination_complete or self.stop_reason is not None:
            return False
        return self.continuation_token is not None or self.pages_confirmed == 0

    @property
    def version_confirmed(self) -> bool:
        """Every object has one confirmed version (completion + no conflict).

        This does not claim the listing is a point-in-time snapshot; see
        ``snapshot_consistent`` for that separate question.
        """
        return self.is_complete and not self.conflicts

    @property
    def listing_complete(self) -> bool:
        return self.is_complete

    @property
    def objects_confirmed(self) -> int:
        return len(self.objects)

    @property
    def confirmed_page_count(self) -> int:
        return self.pages_confirmed

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    @property
    def can_resume(self) -> bool:
        """Whether this value can make progress without starting over."""
        if self.pagination_complete or self.stop_reason is not None:
            return False
        return self.continuation_token is not None or self.pages_confirmed == 0

    def object_for_key(self, key: str) -> Optional[S3ObjectObservation]:
        """Return the one confirmed observation for ``key``, if unambiguous."""
        if any(conflict.key == key for conflict in self.conflicts):
            return None
        matches = [observation for observation in self.observations if observation.key == key]
        return matches[0] if len(matches) == 1 else None

    def to_dict(self) -> Dict[str, Any]:
        entries = [observation.to_dict() for observation in self.observations]
        return {
            'manifest_version': self.manifest_version,
            'bucket': self.bucket,
            'prefix': self.prefix,
            'endpoint_url': self.endpoint_url,
            'status': self.status.value,
            'observations': entries,
            'entries': entries,
            'conflicts': [conflict.to_dict() for conflict in self.conflicts],
            'observation_count': self.observation_count,
            'confirmed_object_count': self.objects_confirmed,
            'continuation_token': self.continuation_token,
            'token': self.continuation_token,
            'next_token': self.continuation_token,
            'pages_confirmed': self.pages_confirmed,
            'page_count': self.pages_confirmed,
            'pagination_complete': self.pagination_complete,
            'complete': self.is_complete,
            'has_more': self.has_more,
            'keys': list(self.keys),
            'confirmed_keys': list(self.confirmed_keys),
            'conflicting_keys': list(self.conflicting_keys),
            'version_confirmed': self.version_confirmed,
            'used_tokens': list(self.used_tokens),
            'error': self.error,
            'stop_reason': self.stop_reason,
            'snapshot_consistent': self.snapshot_consistent,
            'snapshot_claim': (
                'point_in_time' if self.snapshot_consistent else 'none'
            ),
            'completeness_basis': self.completeness_basis,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'S3Manifest':
        raw_status = data.get('status')
        if isinstance(raw_status, S3ManifestStatus):
            raw_status = raw_status.value
        pagination_value = data.get('pagination_complete')
        pagination_complete = (
            raw_status == S3ManifestStatus.COMPLETE.value
            if pagination_value is None
            else bool(pagination_value)
        )
        observations = tuple(
            sorted(
                (
                    S3ObjectObservation.from_dict(item)
                    for item in (
                        data.get('observations')
                        or data.get('entries')
                        or ()
                    )
                ),
                key=lambda observation: observation.sort_key(),
            )
        )
        conflicts = tuple(
            sorted(
                (S3ManifestConflict.from_dict(item) for item in data.get('conflicts', ())),
                key=lambda conflict: conflict.key,
            )
        )
        token_value = (
            data.get('continuation_token')
            or data.get('token')
            or data.get('next_token')
        )
        if raw_status is None:
            if conflicts:
                status = S3ManifestStatus.CONFLICT
            elif pagination_complete:
                status = S3ManifestStatus.COMPLETE
            else:
                status = S3ManifestStatus.PARTIAL
        else:
            status = S3ManifestStatus(raw_status)
        return cls(
            bucket=str(data['bucket']),
            prefix=str(data.get('prefix', '')),
            endpoint_url=data.get('endpoint_url', data.get('endpoint')),
            status=status,
            observations=observations,
            conflicts=conflicts,
            continuation_token=_clean_token(token_value),
            pages_confirmed=int(
                data.get('pages_confirmed', data.get('page_count', 0))
            ),
            pagination_complete=pagination_complete,
            used_tokens=tuple(str(token) for token in (data.get('used_tokens') or ())),
            error=data.get('error'),
            stop_reason=data.get('stop_reason'),
            snapshot_consistent=bool(data.get('snapshot_consistent', False)),
            completeness_basis=str(data.get('completeness_basis', 'terminal_page_received')),
            manifest_version=int(data.get('manifest_version', 1)),
        )

    def __getitem__(self, name: str) -> Any:
        return getattr(self, name)

    def get(self, name: str, default: Any = None) -> Any:
        return getattr(self, name, default)

    def format_text(self) -> str:
        """A compact, human-readable summary for scheduler logs."""
        lines = [
            f'status: {self.status.value}',
            f'target: s3://{self.bucket}/{self.prefix}',
            f'endpoint: {self.endpoint_url}',
            f'confirmed_pages: {self.pages_confirmed}',
            f'continuation_token: {self.continuation_token}',
            f'complete: {self.is_complete}',
            f'snapshot_claim: {self.snapshot_consistent}',
        ]
        for observation in self.objects:
            lines.append(f'object: {observation.key}')
        for conflict in self.conflicts:
            lines.append(f'conflict: {conflict.key}')
            for observation in conflict.observations:
                lines.append(
                    '  observed: '
                    f'etag={observation.etag} size={observation.size} '
                    f'last_modified={observation.last_modified}'
                )
            lines.append(f'  {conflict.message}')
        if self.error:
            lines.append(f'error: {self.error}')
        if self.stop_reason:
            lines.append(f'stop_reason: {self.stop_reason}')
        return '\n'.join(lines)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, value: str) -> 'S3Manifest':
        return cls.from_dict(json.loads(value))

    def save(self, path: Union[str, os.PathLike]) -> None:
        """Persist this checkpoint so a scheduler can resume later."""
        Path(path).write_text(self.to_json(), encoding='utf-8')

    @classmethod
    def load(cls, path: Union[str, os.PathLike]) -> 'S3Manifest':
        return cls.from_json(Path(path).read_text(encoding='utf-8'))

    @classmethod
    def from_checkpoint(cls, checkpoint: Union['S3Manifest', str, Mapping[str, Any]]) -> 'S3Manifest':
        if isinstance(checkpoint, cls):
            return checkpoint
        if isinstance(checkpoint, str):
            return cls.from_json(checkpoint)
        return cls.from_dict(checkpoint)


class S3ManifestError(Exception):
    """Base error for callers that only accept a complete unambiguous list."""

    def __init__(self, manifest: S3Manifest):
        self.manifest = manifest
        super().__init__(_manifest_error_message(manifest))


def _manifest_error_message(manifest: S3Manifest) -> str:
    target = f's3://{manifest.bucket}/{manifest.prefix}'
    if manifest.status is S3ManifestStatus.CONFLICT:
        keys = ', '.join(manifest.conflicting_keys) or '(unknown)'
        return (
            f'Listing of {target} has unresolved object conflicts: {keys}. '
            'No version of the listing is confirmed; conflicting observations '
            'were retained and none was chosen by arrival order.'
        )
    if manifest.status is S3ManifestStatus.STOPPED:
        reason = manifest.stop_reason or 'stopped'
        detail = f': {manifest.error}' if manifest.error else ''
        return f'Listing of {target} stopped ({reason}){detail}'
    detail = f': {manifest.error}' if manifest.error else ''
    return (
        f'Listing of {target} is incomplete after '
        f'{manifest.pages_confirmed} confirmed page(s){detail}'
    )


class S3ManifestIncompleteError(S3ManifestError):
    pass


class S3ManifestConflictError(S3ManifestError):
    pass


class S3ManifestStoppedError(S3ManifestError):
    pass


def raise_for_manifest(manifest: S3Manifest) -> None:
    """Raise a specific error unless ``manifest`` is complete."""
    if manifest.is_complete:
        return
    if manifest.status is S3ManifestStatus.CONFLICT:
        raise S3ManifestConflictError(manifest)
    if manifest.status is S3ManifestStatus.STOPPED:
        raise S3ManifestStoppedError(manifest)
    raise S3ManifestIncompleteError(manifest)


def save_manifest_checkpoint(
    manifest: S3Manifest,
    path: Union[str, os.PathLike],
) -> None:
    manifest.save(path)


def load_manifest_checkpoint(path: Union[str, os.PathLike]) -> S3Manifest:
    return S3Manifest.load(path)


def _as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1', 'yes')
    return bool(value)


def _clean_token(value: Any) -> Optional[str]:
    if value is None:
        return None
    token = str(value)
    return token if token else None


def _optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return str(value)


def _normalise_last_modified(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        try:
            moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _sortable(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, (tuple, list)):
        return '|'.join(_sortable(item) for item in value)
    return f'{type(value).__name__}:{value}'


def _endpoint_identity(endpoint_url: Optional[str]) -> Optional[str]:
    """Canonicalise an endpoint enough to compare credentials.

    Scheme and host are case-insensitive, and a trailing slash does not make
    a new endpoint.  Different hosts, ports or paths remain different.
    """
    if endpoint_url is None:
        return None
    parsed = urlparse(str(endpoint_url).strip())
    if not parsed.scheme and not parsed.netloc:
        return str(endpoint_url).strip().rstrip('/') or None
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip('/')
    query = f'?{parsed.query}' if parsed.query else ''
    return f'{scheme}://{netloc}{path}{query}'


def _observation_map(observations: Iterable[S3ObjectObservation]) -> Dict[str, List[S3ObjectObservation]]:
    by_key: Dict[str, List[S3ObjectObservation]] = {}
    for observation in observations:
        by_key.setdefault(observation.key, []).append(observation)
    return by_key


_CONFLICT_FIELDS = (
    'etag',
    'size',
    'last_modified',
    'storage_class',
    'version_id',
    'checksum_algorithm',
    'checksum_type',
)


def _field_value(observation: S3ObjectObservation, name: str) -> Any:
    value = getattr(observation, name)
    if value == () or value == [] or value == '':
        return None
    return value


def _merge_group(
    key: str,
    group: Sequence[S3ObjectObservation],
) -> Tuple[Optional[S3ObjectObservation], Optional[S3ManifestConflict]]:
    """Merge compatible observations, or report a contradiction.

    Missing optional values are not contradictions.  A value that appears in
    one observation and is absent from another simply makes the merged
    observation less complete, never conflicting.  Two different non-empty
    values for the same field are contradictory.
    """
    merged: Dict[str, Any] = {}
    contradictory = False
    for name in _CONFLICT_FIELDS:
        values = {
            _field_value(observation, name)
            for observation in group
        }
        values.discard(None)
        if len(values) > 1:
            contradictory = True
        merged[name] = next(iter(values)) if values else None

    if contradictory:
        unique = sorted(set(group), key=lambda observation: observation.sort_key())
        return None, S3ManifestConflict(key=key, observations=tuple(unique))

    merged.setdefault('checksum_algorithm', ())
    merged['checksum_algorithm'] = tuple(merged['checksum_algorithm'] or ())
    return S3ObjectObservation(key=key, **merged), None


def _merge_observations(
    existing: Iterable[S3ObjectObservation],
    incoming: Iterable[S3ObjectObservation],
) -> Tuple[Tuple[S3ObjectObservation, ...], Tuple[S3ManifestConflict, ...]]:
    # Group every observation for a key, regardless of which page delivered
    # it.  Exactly identical observations collapse; compatible observations
    # with different amounts of optional metadata merge into one.
    by_key = _observation_map(tuple(existing) + tuple(incoming))

    observations: List[S3ObjectObservation] = []
    conflicts: List[S3ManifestConflict] = []
    for key in sorted(by_key):
        observation, conflict = _merge_group(key, by_key[key])
        if conflict is not None:
            conflicts.append(conflict)
            # Keep every distinct contradictory observation; the conflict
            # itself also lists them explicitly.
            observations.extend(conflict.observations)
            continue
        observations.append(observation)

    observations.sort(key=lambda observation: observation.sort_key())
    return tuple(observations), tuple(conflicts)


def _status(
    *,
    pagination_complete: bool,
    conflicts: Sequence[S3ManifestConflict],
    stop_reason: Optional[str],
) -> S3ManifestStatus:
    if stop_reason is not None:
        return S3ManifestStatus.STOPPED
    if conflicts:
        return S3ManifestStatus.CONFLICT
    if pagination_complete:
        return S3ManifestStatus.COMPLETE
    return S3ManifestStatus.PARTIAL


class S3ManifestLister:
    """Fetch ``ListObjectsV2`` pages and retain resumable state.

    ``fetch_all`` follows continuation tokens until a terminal page, but
    stops at the first service error and returns the last confirmed state.
    Call ``resume`` with that state (after serialising it if necessary) to
    continue from the failed page.
    """

    def __init__(
        self,
        aws_session: Any,
        bucket: str,
        prefix: str,
        endpoint_url: Optional[str] = None,
        *,
        page_size: Optional[int] = None,
        client: Any = None,
    ):
        self.aws_session = aws_session
        self.bucket = bucket
        self.prefix = prefix
        # Keep the caller's endpoint spelling for the actual client call;
        # compare credentials using a canonical identity.
        self.endpoint_url = endpoint_url
        self._endpoint_identity = _endpoint_identity(endpoint_url)
        self.page_size = page_size
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self.endpoint_url is None:
            return self.aws_session.client('s3')
        return self.aws_session.client('s3', endpoint_url=self.endpoint_url)

    def initial_state(self) -> S3Manifest:
        return S3Manifest(
            bucket=self.bucket,
            prefix=self.prefix,
            endpoint_url=self.endpoint_url,
            status=S3ManifestStatus.PARTIAL,
        )

    def advance(self, state: Optional[S3Manifest] = None) -> S3Manifest:
        """Fetch the next page from ``state``.

        A failed or invalid page is returned without advancing
        ``pages_confirmed`` or the continuation token.
        """
        if state is None:
            state = self.initial_state()

        binding_error = self._binding_error(state)
        if binding_error is not None:
            reason, message = binding_error
            return replace(
                state,
                status=S3ManifestStatus.STOPPED,
                stop_reason=reason,
                error=message,
            )

        if state.stop_reason is not None or state.pagination_complete:
            return state

        request: Dict[str, Any] = {
            'Bucket': self.bucket,
            'Prefix': self.prefix,
        }
        if state.continuation_token is not None:
            request['ContinuationToken'] = state.continuation_token
        if self.page_size is not None:
            request['MaxKeys'] = self.page_size

        try:
            response = self._get_client().list_objects_v2(**request)
        except Exception as exc:  # noqa: BLE001 - service errors are state
            return replace(
                state,
                status=_status(
                    pagination_complete=False,
                    conflicts=state.conflicts,
                    stop_reason=None,
                ),
                error=f'{type(exc).__name__}: {exc}',
            )

        contents = response.get('Contents') or ()
        incoming = [
            S3ObjectObservation.from_s3_item(item)
            for item in contents
        ]
        observations, conflicts = _merge_observations(state.observations, incoming)

        next_token = _clean_token(response.get('NextContinuationToken'))
        is_truncated_value = _as_bool(response.get('IsTruncated'))
        is_truncated = (
            next_token is not None
            if is_truncated_value is None
            else is_truncated_value
        )
        current_token = (
            None if state.continuation_token is None
            else str(state.continuation_token)
        )
        used_tokens = tuple(str(token) for token in state.used_tokens)

        if not is_truncated:
            if next_token is not None:
                # A terminal page normally has no next token.  If the service
                # supplies one, the terminal marker is authoritative and the
                # dangling token is ignored rather than followed.
                logger.debug(
                    'Ignoring NextContinuationToken %r on a terminal page',
                    next_token,
                )
            return S3Manifest(
                bucket=state.bucket,
                prefix=state.prefix,
                endpoint_url=state.endpoint_url,
                status=_status(
                    pagination_complete=True,
                    conflicts=conflicts,
                    stop_reason=None,
                ),
                observations=observations,
                conflicts=conflicts,
                continuation_token=None,
                pages_confirmed=state.pages_confirmed + 1,
                pagination_complete=True,
                used_tokens=tuple(str(token) for token in state.used_tokens),
                error=None,
                stop_reason=None,
                snapshot_consistent=state.snapshot_consistent,
            )

        if not next_token:
            # The response is malformed: it claims more pages but does not
            # provide a way to ask for them.  The page is not confirmed, so
            # neither the page count nor the observations advance.
            return replace(
                state,
                status=S3ManifestStatus.STOPPED,
                error='S3 returned IsTruncated=true without NextContinuationToken',
                stop_reason=STOP_MISSING_CONTINUATION_TOKEN,
            )

        # A token may not be reused.  Check before committing the page so a
        # looping service cannot make the confirmed state look more complete
        # than it is.
        if next_token == current_token or next_token in used_tokens:
            # Without a new token this page cannot become part of a
            # confirmed sequence, so it is not merged.
            return replace(
                state,
                status=S3ManifestStatus.STOPPED,
                stop_reason=STOP_CONTINUATION_CYCLE,
                error=(
                    'S3 repeated continuation token '
                    f'{next_token!r}; stopped instead of treating the listing '
                    'as complete'
                ),
            )

        return S3Manifest(
            bucket=state.bucket,
            prefix=state.prefix,
            endpoint_url=state.endpoint_url,
            status=_status(
                pagination_complete=False,
                conflicts=conflicts,
                stop_reason=None,
            ),
            observations=observations,
            conflicts=conflicts,
            continuation_token=next_token,
            pages_confirmed=state.pages_confirmed + 1,
            pagination_complete=False,
            used_tokens=used_tokens + (next_token,),
            error=None,
            stop_reason=None,
            snapshot_consistent=state.snapshot_consistent,
        )

    def resume(self, state: S3Manifest) -> S3Manifest:
        """Continue ``state`` until it completes, stops, or fails again."""
        while state.stop_reason is None and not state.pagination_complete:
            previous = state
            state = self.advance(state)
            if state.error is not None:
                # A transient failure leaves the same token and page count;
                # return it so the caller can persist and retry explicitly.
                return state
            if state.stop_reason is not None or state.pagination_complete:
                return state
            if state.pages_confirmed == previous.pages_confirmed:
                # Defensive guard against a client that returns a page
                # without either a token or a terminal marker.
                return state
        return state

    def fetch_all(self) -> S3Manifest:
        """Fetch a listing from the beginning, stopping at the first failure."""
        return self.resume(self.initial_state())

    def start(self) -> S3Manifest:
        """Alias for ``fetch_all`` for scheduler-style callers."""
        return self.fetch_all()

    def continue_from(self, state: S3Manifest) -> S3Manifest:
        """Alias for ``resume`` with a name schedulers tend to use."""
        return self.resume(state)

    def list(self, checkpoint: Optional[S3Manifest] = None) -> S3Manifest:
        """Alias for ``resume(checkpoint)`` or ``fetch_all()``."""
        return self.fetch_all() if checkpoint is None else self.resume(checkpoint)

    def iter_pages(self, state: Optional[S3Manifest] = None):
        """Yield a manifest after each page, including a failed page.

        The yielded value is always safe to persist.  A caller that sees a
        ``PARTIAL`` state with an ``error`` can retry with ``resume``; a
        caller that sees ``COMPLETE``, ``CONFLICT`` or ``STOPPED`` is done.
        """
        if state is None:
            state = self.initial_state()
        yield state
        while state.stop_reason is None and not state.pagination_complete:
            previous = state
            state = self.advance(state)
            yield state
            if state.error is not None:
                return
            if state.stop_reason is not None or state.pagination_complete:
                return
            if state.pages_confirmed == previous.pages_confirmed:
                return

    def _binding_error(self, state: S3Manifest) -> Optional[Tuple[str, str]]:
        if _endpoint_identity(state.endpoint_url) != self._endpoint_identity:
            return (
                STOP_ENDPOINT_MISMATCH,
                'Listing credential belongs to endpoint '
                f'{state.endpoint_url!r}, not {self.endpoint_url!r}; '
                'no request was sent.',
            )
        if state.bucket != self.bucket:
            return (
                STOP_BUCKET_MISMATCH,
                f'Listing credential belongs to bucket {state.bucket!r}, '
                f'not {self.bucket!r}; no request was sent.',
            )
        if state.prefix != self.prefix:
            return (
                STOP_PREFIX_MISMATCH,
                f'Listing credential belongs to prefix {state.prefix!r}, '
                f'not {self.prefix!r}; no request was sent.',
            )
        return None


def resolve_s3_wildcard_manifest(
    aws_session: Any,
    bucket: str,
    prefix: str,
    endpoint_url: Optional[str] = None,
    *,
    page_size: Optional[int] = None,
    client: Any = None,
) -> S3Manifest:
    return list_s3_prefix(
        aws_session=aws_session,
        bucket=bucket,
        prefix=prefix,
        endpoint_url=endpoint_url,
        page_size=page_size,
        client=client,
    )


def list_s3_prefix(
    aws_session: Any,
    bucket: str,
    prefix: str,
    endpoint_url: Optional[str] = None,
    *,
    page_size: Optional[int] = None,
    client: Any = None,
) -> S3Manifest:
    """Convenience wrapper for a complete first attempt at a prefix."""
    return S3ManifestLister(
        aws_session=aws_session,
        bucket=bucket,
        prefix=prefix,
        endpoint_url=endpoint_url,
        page_size=page_size,
        client=client,
    ).fetch_all()


# Backwards-friendly aliases for callers that use "listing" terminology.
S3ListingStatus = S3ManifestStatus
S3PrefixListing = S3Manifest
S3PrefixLister = S3ManifestLister
ManifestStatus = S3ManifestStatus
ManifestEntry = S3ObjectObservation
ObjectObservation = S3ObjectObservation
ManifestConflict = S3ManifestConflict
S3WildcardManifest = S3Manifest
S3WildcardLister = S3ManifestLister
ResumableListing = S3Manifest
IncompleteListingError = S3ManifestIncompleteError
ConflictingListingError = S3ManifestConflictError
ListingStoppedError = S3ManifestStoppedError
