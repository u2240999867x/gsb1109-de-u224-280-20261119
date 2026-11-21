"""A small S3-compatible object store for range/version tests.

It is intentionally not a general S3 implementation.  It supports exactly
what tail-only parquet inspection needs and records every request and
response byte range so tests can assert that data pages or whole objects were
never fetched.

Two front ends share the same state:

* :class:`FakeS3Store` is an in-process adapter with the same methods as
  :class:`parquet_tools.parquet.remote.S3Store`.
* :class:`FakeS3Server` exposes the subset over HTTP (path-style S3) for tests
  that want to exercise boto3 and real headers.
"""

import dataclasses
import datetime
import hashlib
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from parquet_tools.parquet.remote import ObjectStat, RangeResponse, StoreUnavailable

_RANGE = re.compile(r'^bytes=(\d+)-(\d+)$')


def _etag(data: bytes) -> str:
    return '"' + hashlib.md5(data).hexdigest() + '"'


@dataclasses.dataclass
class FakeVersion:
    data: bytes
    version_id: str
    etag: str
    last_modified: datetime.datetime
    delete_marker: bool = False


@dataclasses.dataclass
class RecordedRequest:
    method: str
    bucket: str
    key: str
    range: Optional[str] = None
    version_id: Optional[str] = None
    if_match: Optional[str] = None
    status: Optional[int] = None
    content_range: Optional[str] = None
    body_length: Optional[int] = None
    sent_length: Optional[int] = None


@dataclasses.dataclass
class Fault:
    """A response fault for one specific version/range.

    mode:
      ``truncate_once`` - correct Content-Range, short body, once.
      ``truncate``      - correct Content-Range, short body, always.
      ``wrong_body``    - correct Content-Range and length, bytes from another
                          offset (``shift``).
      ``ignore_range``  - 200 with the whole version body.
      ``unavailable``   - 501 Not Implemented.
    """

    version_id: Optional[str] = None
    start: Optional[int] = None
    end: Optional[int] = None
    mode: str = 'truncate_once'
    shift: int = 0
    remaining: int = 1


class FakeS3:
    """In-process S3 state plus a boto3-compatible adapter."""

    def __init__(self, versioned: bool = True, endpoint: str = 'fake://local'):
        self.versioned = versioned
        self.endpoint = endpoint
        self.objects: Dict[Tuple[str, str], List[FakeVersion]] = {}
        self.requests: List[RecordedRequest] = []
        self.range_supported = True
        self.version_range_supported = True
        self.conditional_supported = True
        self.faults: List[Fault] = []
        self._counter = 0
        self._lock = threading.Lock()

    @property
    def endpoint_ref(self):
        from parquet_tools.parquet.credential import EndpointRef
        return EndpointRef.for_client(self.endpoint, None)

    def client(self, store_type=None):
        """Return a drop-in adapter with ``S3Store``'s constructor signature."""
        return _StoreAdapter(self)

    # -- setup -------------------------------------------------------------
    def put(self, bucket: str, key: str, data: bytes, version_id: Optional[str] = None) -> FakeVersion:
        with self._lock:
            self._counter += 1
            version_id = version_id or f'v{self._counter:04d}'
            version = FakeVersion(
                data=data,
                version_id=version_id,
                etag=_etag(data),
                last_modified=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=self._counter),
            )
            self.objects.setdefault((bucket, key), []).append(version)
            return version

    def replace(self, bucket: str, key: str, data: bytes) -> FakeVersion:
        return self.put(bucket, key, data)

    def add_fault(self, **kwargs) -> Fault:
        fault = Fault(**kwargs)
        self.faults.append(fault)
        return fault

    def clear_requests(self) -> None:
        self.requests.clear()

    def get_requests(self, method: str = 'GET') -> List[RecordedRequest]:
        return [r for r in self.requests if r.method == method]

    def read_intervals(self, method: str = 'GET') -> List[Tuple[int, int]]:
        """Exclusive ``[start, end)`` intervals of successful range reads."""
        intervals = []
        for request in self.requests:
            if request.method != method or not request.range:
                continue
            match = _RANGE.match(request.range)
            if match:
                intervals.append((int(match.group(1)), int(match.group(2)) + 1))
        return intervals

    def total_bytes_sent(self, method: str = 'GET') -> int:
        return sum(r.sent_length or 0 for r in self.requests if r.method == method)

    def versions(self, bucket: str, key: str) -> List[FakeVersion]:
        return list(self.objects.get((bucket, key), []))

    def current(self, bucket: str, key: str) -> FakeVersion:
        versions = self.objects.get((bucket, key)) or []
        for version in reversed(versions):
            if not version.delete_marker:
                return version
        raise StoreUnavailable('NoSuchKey')

    def by_version(self, bucket: str, key: str, version_id: str) -> FakeVersion:
        for version in self.objects.get((bucket, key), []):
            if version.version_id == version_id:
                return version
        raise StoreUnavailable('NoSuchVersion')

    # -- request handling --------------------------------------------------
    def _record(self, request: RecordedRequest) -> None:
        self.requests.append(request)

    def _take_fault(self, version_id: Optional[str], start: int, end: int) -> Optional[Fault]:
        for fault in self.faults:
            if fault.mode == 'truncate_once' and fault.remaining <= 0:
                continue
            if fault.version_id is not None and fault.version_id != version_id:
                continue
            if fault.start is not None and fault.start != start:
                continue
            if fault.end is not None and fault.end != end:
                continue
            if fault.mode == 'truncate_once':
                fault.remaining -= 1
            return fault
        return None

    def head(self, bucket: str, key: str, version_id: Optional[str] = None) -> ObjectStat:
        request = RecordedRequest('HEAD', bucket, key, version_id=version_id)
        self._record(request)
        try:
            version = self.by_version(bucket, key, version_id) if version_id else self.current(bucket, key)
        except StoreUnavailable:
            request.status = 404
            raise
        request.status = 200
        return ObjectStat(
            size=len(version.data),
            etag=version.etag,
            version_id=version.version_id if self.versioned else None,
            last_modified=version.last_modified.isoformat(),
        )

    def get_range(
        self,
        bucket: str,
        key: str,
        start: int,
        end: int,
        version_id: Optional[str] = None,
        if_match: Optional[str] = None,
    ) -> RangeResponse:
        request = RecordedRequest(
            'GET', bucket, key,
            range=f'bytes={start}-{end}',
            version_id=version_id,
            if_match=if_match,
        )
        self._record(request)
        try:
            version = self.by_version(bucket, key, version_id) if version_id else self.current(bucket, key)
        except StoreUnavailable:
            request.status = 404
            raise

        if version_id and not self.version_range_supported:
            request.status = 501
            raise StoreUnavailable('NotImplemented: ranges for historic versions are not supported')

        if if_match is not None:
            if not self.conditional_supported:
                request.status = 501
                raise StoreUnavailable('NotImplemented: conditional reads are not supported')
            if if_match != version.etag:
                request.status = 412
                raise StoreUnavailable('PreconditionFailed: If-Match does not match')

        if not self.range_supported:
            request.status = 200
            request.body_length = len(version.data)
            request.sent_length = len(version.data)
            return RangeResponse(
                status=200,
                body=version.data,
                content_range=None,
                etag=version.etag,
                version_id=version.version_id if self.versioned else None,
                content_length=len(version.data),
                accept_ranges='none',
            )

        fault = self._take_fault(version.version_id, start, end)
        if fault is not None and fault.mode == 'unavailable':
            request.status = 501
            raise StoreUnavailable('NotImplemented: requested range is not available')

        size = len(version.data)
        if start < 0 or end >= size or start > end:
            request.status = 416
            raise StoreUnavailable('InvalidRange')

        if fault is not None and fault.mode == 'ignore_range':
            request.status = 200
            request.body_length = len(version.data)
            request.sent_length = len(version.data)
            return RangeResponse(
                status=200,
                body=version.data,
                content_range=None,
                etag=version.etag,
                version_id=version.version_id if self.versioned else None,
                content_length=len(version.data),
                accept_ranges='none',
            )

        if fault is not None and fault.mode == 'wrong_body':
            shift = fault.shift
            source_start = start + shift
            if source_start < 0:
                source_start = 0
            length = end - start + 1
            if source_start + length > size:
                source_start = size - length
            body = version.data[source_start:source_start + length]
        else:
            body = version.data[start:end + 1]

        truncated = fault is not None and fault.mode.startswith('truncate')
        content_range = f'bytes {start}-{end}/{size}'
        if truncated:
            body = body[:max(0, len(body) - max(1, len(body) // 4))]

        request.status = 206
        request.content_range = content_range
        request.body_length = end - start + 1
        request.sent_length = len(body)
        return RangeResponse(
            status=206,
            body=body,
            content_range=content_range,
            etag=version.etag,
            version_id=version.version_id if self.versioned else None,
            content_length=len(body),
            accept_ranges='bytes',
        )

    def list_versions(self, bucket: str, key: str) -> List[dict]:
        self._record(RecordedRequest('LIST_VERSIONS', bucket, key))
        result = []
        versions = self.objects.get((bucket, key), [])
        latest = versions[-1].version_id if versions else None
        for version in versions:
            result.append({
                'Key': key,
                'VersionId': version.version_id,
                'ETag': version.etag,
                'Size': len(version.data),
                'IsLatest': version.version_id == latest,
                'LastModified': version.last_modified,
            })
        return result


def _parse_range(value: Optional[str]) -> Optional[Tuple[int, int]]:
    if not value:
        return None
    match = _RANGE.match(value.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


class FakeS3Server:
    """Path-style S3 HTTP front end for :class:`FakeS3`."""

    def __init__(self, state: Optional[FakeS3] = None, host: str = '127.0.0.1', port: int = 0):
        self.state = state or FakeS3()
        self._server = ThreadingHTTPServer((host, port), self._handler_class())
        self._thread: Optional[threading.Thread] = None
        self.host = host
        self.port = self._server.server_address[1]

    @property
    def endpoint_url(self) -> str:
        return f'http://{self.host}:{self.port}'

    def _handler_class(self):
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, fmt, *args):
                pass

            def _target(self):
                parsed = urlparse(self.path)
                parts = parsed.path.lstrip('/').split('/', 1)
                bucket = parts[0]
                key = parts[1] if len(parts) > 1 else ''
                query = parse_qs(parsed.query)
                return bucket, key, query

            def _send(self, status: int, body: bytes = b'', headers: Optional[dict] = None):
                self.send_response(status)
                headers = headers or {}
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _error(self, status: int, code: str):
                body = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    f'<Error><Code>{code}</Code><Message>{code}</Message></Error>'
                ).encode()
                self._send(status, body, {'Content-Type': 'application/xml'})

            def do_HEAD(self):
                bucket, key, query = self._target()
                version_id = (query.get('versionId') or [None])[0]
                try:
                    stat = state.head(bucket, key, version_id)
                except StoreUnavailable:
                    self._error(404, 'NoSuchKey')
                    return
                headers = {'Content-Length': str(stat.size), 'ETag': stat.etag or ''}
                if stat.version_id:
                    headers['x-amz-version-id'] = stat.version_id
                self.send_response(200)
                for k, v in headers.items():
                    if k != 'Content-Length':
                        self.send_header(k, v)
                self.send_header('Content-Length', str(stat.size))
                self.end_headers()

            def do_GET(self):
                bucket, key, query = self._target()
                if 'versions' in query or self.path.split('?', 1)[-1].startswith('versions'):
                    self._list_versions(bucket, key)
                    return
                version_id = (query.get('versionId') or [None])[0]
                if_match = self.headers.get('If-Match')
                request_range = _parse_range(self.headers.get('Range'))
                if request_range is None:
                    self._error(400, 'InvalidRequest')
                    return
                start, end = request_range
                try:
                    response = state.get_range(
                        bucket, key, start, end,
                        version_id=version_id, if_match=if_match,
                    )
                except StoreUnavailable as exc:
                    message = str(exc)
                    if message.startswith('PreconditionFailed'):
                        self._error(412, 'PreconditionFailed')
                    elif message.startswith('NotImplemented'):
                        self._error(501, 'NotImplemented')
                    elif message.startswith('InvalidRange'):
                        self._error(416, 'InvalidRange')
                    else:
                        self._error(404, 'NoSuchKey')
                    return
                headers = {
                    'ETag': response.etag or '',
                    'Accept-Ranges': response.accept_ranges or 'bytes',
                }
                if response.version_id:
                    headers['x-amz-version-id'] = response.version_id
                if response.content_range:
                    headers['Content-Range'] = response.content_range
                self.send_response(response.status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(response.body)))
                self.end_headers()
                if response.body:
                    self.wfile.write(response.body)

            def _list_versions(self, bucket: str, key: str):
                versions = state.list_versions(bucket, key)
                items = ''.join(
                    '<Version>'
                    f'<Key>{v["Key"]}</Key>'
                    f'<VersionId>{v["VersionId"]}</VersionId>'
                    f'<ETag>{v["ETag"]}</ETag>'
                    f'<Size>{v["Size"]}</Size>'
                    f'<IsLatest>{"true" if v["IsLatest"] else "false"}</IsLatest>'
                    f'<LastModified>{v["LastModified"].strftime("%Y-%m-%dT%H:%M:%S.000Z")}</LastModified>'
                    '</Version>'
                    for v in versions
                )
                body = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<ListVersionsResult>'
                    f'<Name>{bucket}</Name>'
                    f'<Prefix>{key}</Prefix>'
                    '<IsTruncated>false</IsTruncated>'
                    f'{items}'
                    '</ListVersionsResult>'
                ).encode()
                self._send(200, body, {'Content-Type': 'application/xml'})

        return Handler

    def start(self) -> 'FakeS3Server':
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> 'FakeS3Server':
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


class _StoreAdapter:
    """Mimics ``parquet_tools.parquet.remote.S3Store`` over a FakeS3 state."""

    def __init__(self, state: FakeS3):
        from parquet_tools.parquet.credential import EndpointRef

        self.state = state
        self.endpoint_url = state.endpoint
        self.region_name = None
        self.endpoint_ref = EndpointRef.for_client(state.endpoint, None)

    def head(self, bucket, key, version_id=None):
        return self.state.head(bucket, key, version_id)

    def get_range(self, bucket, key, start, end, version_id=None, if_match=None):
        return self.state.get_range(bucket, key, start, end, version_id, if_match)

    def list_versions(self, bucket, key):
        return self.state.list_versions(bucket, key)
