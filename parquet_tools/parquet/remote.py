"""Object-store access used by tail-only parquet inspection.

The inspector only needs three operations: HEAD, a byte Range GET and (for
diagnostics) ListObjectVersions.  Keeping them behind this small interface
makes the version/range rules testable without a network and avoids ever
falling back to a whole-object download.
"""

from dataclasses import dataclass
from typing import List, Optional

from .credential import ObjectIdentity


class StoreError(Exception):
    pass


class StoreUnavailable(StoreError):
    """The requested object or object version cannot be read."""


class StoreIntegrity(StoreError):
    """The store's response does not prove it returned the requested bytes."""


@dataclass(frozen=True)
class ObjectStat:
    size: int
    etag: Optional[str]
    version_id: Optional[str]
    last_modified: Optional[str] = None

    def identity(self) -> ObjectIdentity:
        return ObjectIdentity(
            size=self.size,
            etag=self.etag,
            version_id=self.version_id,
            last_modified=self.last_modified,
        )


@dataclass
class RangeResponse:
    status: int
    body: bytes
    content_range: Optional[str] = None
    etag: Optional[str] = None
    version_id: Optional[str] = None
    content_length: Optional[int] = None
    accept_ranges: Optional[str] = None
    delete_marker: bool = False


class S3Store:
    """Minimal boto3-backed S3 client for tail reads."""

    def __init__(self, session, endpoint_url: Optional[str] = None, region_name: Optional[str] = None):
        self.endpoint_url = endpoint_url
        self._client = session.client('s3', endpoint_url=endpoint_url, region_name=region_name)
        self.region_name = region_name or getattr(self._client.meta, 'region_name', None)

    @property
    def endpoint_ref(self):
        from .credential import EndpointRef
        return EndpointRef.for_client(self.endpoint_url, self.region_name)

    def head(self, bucket: str, key: str, version_id: Optional[str] = None) -> ObjectStat:
        params = {'Bucket': bucket, 'Key': key}
        if version_id is not None:
            params['VersionId'] = version_id
        try:
            response = self._client.head_object(**params)
        except Exception as exc:
            raise _translate_client_error(exc) from exc
        return ObjectStat(
            size=int(response['ContentLength']),
            etag=response.get('ETag'),
            version_id=response.get('VersionId'),
            last_modified=_stringify(response.get('LastModified')),
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
        params = {
            'Bucket': bucket,
            'Key': key,
            'Range': f'bytes={start}-{end}',
        }
        if version_id is not None:
            params['VersionId'] = version_id
        if if_match is not None:
            params['IfMatch'] = if_match
        try:
            response = self._client.get_object(**params)
            status = int(response.get('ResponseMetadata', {}).get('HTTPStatusCode', 200))
            body_stream = response['Body']
            body = b''
            try:
                while True:
                    try:
                        chunk = body_stream.read(64 * 1024)
                    except Exception as exc:
                        if _is_incomplete_read(exc):
                            # Keep the prefix already received.  The caller
                            # verifies that its Content-Range covers it.
                            break
                        raise
                    if not chunk:
                        break
                    body += chunk
            finally:
                close = getattr(body_stream, 'close', None)
                if close is not None:
                    close()
            return RangeResponse(
                status=status,
                body=body,
                content_range=response.get('ContentRange'),
                etag=response.get('ETag'),
                version_id=response.get('VersionId'),
                content_length=_to_int(response.get('ContentLength')),
                accept_ranges=response.get('AcceptRanges'),
                delete_marker=bool(response.get('DeleteMarker', False)),
            )
        except Exception as exc:
            raise _translate_client_error(exc) from exc

    def list_versions(self, bucket: str, key: str) -> List[dict]:
        try:
            response = self._client.list_object_versions(Bucket=bucket, Prefix=key)
        except Exception as exc:
            raise _translate_client_error(exc) from exc
        return [
            entry for entry in response.get('Versions', [])
            if entry.get('Key') == key
        ]


def _to_int(value):
    return int(value) if value is not None else None


def _is_incomplete_read(exc: Exception) -> bool:
    try:
        from botocore.exceptions import IncompleteReadError
    except ImportError:  # pragma: no cover
        return False
    if isinstance(exc, IncompleteReadError):
        return True
    # urllib3's protocol error is what botocore surfaces when a Content-Length
    # body is cut short.
    return exc.__class__.__name__ in {'ProtocolError', 'IncompleteRead', 'ChunkedEncodingError'}


def _stringify(value):
    if value is None:
        return None
    return value.isoformat() if hasattr(value, 'isoformat') else str(value)


def _translate_client_error(exc: Exception) -> StoreError:
    try:
        from botocore.exceptions import ClientError, IncompleteReadError
    except ImportError:  # pragma: no cover - boto3 is a runtime dependency
        return StoreError(str(exc))
    if isinstance(exc, IncompleteReadError):
        return StoreIntegrity('response body ended before the requested range was complete')
    if isinstance(exc, ClientError):
        error = exc.response.get('Error', {})
        code = str(error.get('Code', ''))
        status = exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode')
        if code in {
            'NoSuchKey', 'NoSuchVersion', 'NotFound', 'InvalidArgument',
            'InvalidVersion', 'AccessDenied', 'PreconditionFailed',
            'NotImplemented', 'MethodNotAllowed', 'InvalidRange',
        } or status in (400, 403, 404, 412, 416, 501):
            return StoreUnavailable(f'{code or status}: {error.get("Message", str(exc))}')
        return StoreError(str(exc))
    return StoreError(str(exc))
