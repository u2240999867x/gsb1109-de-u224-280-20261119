"""An in-process, scripted S3 ListObjectsV2 client for pagination tests.

No socket is opened.  Every continuation token has its own queue of scripted
responses, so a test can fail a page, restore the service, and replay the
previous page without depending on network timing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class FakeObject:
    key: str
    etag: str = '"etag"'
    size: int = 1
    last_modified: datetime = datetime(2026, 11, 22, tzinfo=timezone.utc)
    storage_class: str = 'STANDARD'


@dataclass(frozen=True)
class FakePage:
    objects: Tuple[FakeObject, ...] = ()
    next_token: Optional[str] = None
    is_truncated: bool = False
    key_count: Optional[int] = None

    def as_response(self) -> Dict[str, object]:
        response: Dict[str, object] = {
            'IsTruncated': self.is_truncated,
            'KeyCount': len(self.objects) if self.key_count is None else self.key_count,
            'Contents': [
                {
                    'Key': obj.key,
                    'ETag': obj.etag,
                    'Size': obj.size,
                    'LastModified': obj.last_modified,
                    'StorageClass': obj.storage_class,
                }
                for obj in self.objects
            ],
        }
        if self.next_token is not None:
            response['NextContinuationToken'] = self.next_token
        return response


class FakeS3Error(Exception):
    def __init__(self, code: str = 'InternalError', message: str = 'temporary failure'):
        self.code = code
        self.message = message
        super().__init__(f'{code}: {message}')


@dataclass(frozen=True)
class FakeFailure:
    code: str = 'InternalError'
    message: str = 'temporary failure'


@dataclass
class FakeS3Client:
    """A ``list_objects_v2``-compatible scripted client."""

    responses: Dict[Optional[str], List[object]] = field(default_factory=dict)
    requests: List[Dict[str, object]] = field(default_factory=list)

    def add(self, token: Optional[str], response: object) -> 'FakeS3Client':
        self.responses.setdefault(token, []).append(response)
        return self

    @property
    def tokens(self) -> List[Optional[str]]:
        return [request.get('ContinuationToken') for request in self.requests]

    def list_objects_v2(self, **kwargs):
        self.requests.append(dict(kwargs))
        token = kwargs.get('ContinuationToken')
        queue = self.responses.get(token)
        if not queue:
            raise FakeS3Error('NoSuchScriptedPage', f'token={token!r}')
        response = queue.pop(0)
        if isinstance(response, FakeFailure):
            raise FakeS3Error(response.code, response.message)
        if isinstance(response, FakePage):
            return response.as_response()
        return response


class FakeS3Service:
    """A session-like object serving scripted pages per endpoint."""

    def __init__(self):
        self.clients: Dict[str, FakeS3Client] = {}

    def client(self, service_name: str, endpoint_url: Optional[str] = None, **kwargs) -> FakeS3Client:
        if service_name != 's3':
            raise AssertionError(f'unexpected service {service_name!r}')
        endpoint = endpoint_url.rstrip('/') if endpoint_url else ''
        return self.clients.setdefault(endpoint, FakeS3Client())

    def for_endpoint(self, endpoint_url: str) -> FakeS3Client:
        return self.client('s3', endpoint_url=endpoint_url)
