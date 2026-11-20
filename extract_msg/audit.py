from __future__ import annotations


__all__ = [
    'StreamEntry',
    'StreamManifest',
    'StreamStatus',
    'buildStreamManifest',
    'buildStreamManifestEntry',
    'build_stream_manifest',
    'getStreamManifest',
    'getStreamManifestEntry',
    'AuditManifest',
    'Manifest',
    'manifestFromPath',
    'manifest_from_path',
    'streamManifest',
    'stream_manifest',
    'StreamInfo',
    'StreamListing',
    'StreamRecord',
    'StreamState',
]


import dataclasses
import enum
import hashlib
import json
import logging

from typing import Any, Dict, Iterable, List, Optional, Tuple, TYPE_CHECKING, Union

from .msg_path import MSGPath


if TYPE_CHECKING:
    from .msg_classes.msg import MSGFile


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


_EMBEDDED_MSG_STORAGE = '__substg1.0_3701000D'
_PROPERTIES_STREAM = '__properties_version1.0'
_NAMEID_STORAGE = '__nameid_version1.0'


class StreamStatus(str, enum.Enum):
    """
    The result of trying to read a stream for a manifest entry.

    The values are distinct on purpose.  In particular, an empty stream is not
    the same thing as a stream that is absent from the OLE directory, and
    neither is the same thing as a stream that is present but cannot be read.
    """
    OK = 'ok'
    ZERO_BYTES = 'zero_bytes'
    MISSING = 'missing'
    READ_ERROR = 'read_error'

    # Readable aliases for callers that prefer shorter names.
    SUCCESS = 'ok'
    READABLE = 'ok'
    EMPTY = 'zero_bytes'
    ZERO = 'zero_bytes'
    ABSENT = 'missing'
    ERROR = 'read_error'
    UNREADABLE = 'read_error'
    READ_FAILURE = 'read_error'
    FAILED = 'read_error'

    def __str__(self) -> str:
        return self.value



@dataclasses.dataclass(frozen = True)
class StreamEntry:
    """
    One stream occurrence in a manifest.

    An entry deliberately contains no stream contents.  ``digest`` is a digest
    of bytes that were successfully read; it is ``None`` when the stream is
    missing or could not be read.
    """
    owner: str
    ownerLevel: int
    source: str
    relativePath: str
    container: str
    status: StreamStatus
    declaredSize: Optional[int]
    readSize: Optional[int]
    digest: Optional[str] = None
    digestAlgorithm: Optional[str] = None
    errorType: Optional[str] = None
    errorMessage: Optional[str] = None
    inScope: bool = True
    referenced: bool = False

    @property
    def path(self) -> str:
        """Alias for :attr:`source`, the full path in the original file."""
        return self.source

    @property
    def streamPath(self) -> str:
        """Alias for :attr:`source`."""
        return self.source

    @property
    def fullPath(self) -> str:
        """Alias for :attr:`source`."""
        return self.source

    @property
    def relative(self) -> str:
        """Alias for :attr:`relativePath`."""
        return self.relativePath

    @property
    def ownerPrefix(self) -> str:
        """Alias for :attr:`owner`."""
        return self.owner

    @property
    def ownerPath(self) -> str:
        """The owner path, using ``'.'`` for the root message."""
        return self.owner or '.'

    @property
    def isRootOwner(self) -> bool:
        """Whether the entry is owned by the root message."""
        return not self.ownerParts or tuple(x.casefold() for x in self.ownerParts) == ('.',)

    @property
    def external(self) -> bool:
        """Whether the entry is outside the manifest's scope."""
        return not self.inScope

    @property
    def sourceParts(self) -> Tuple[str, ...]:
        """The source path as a tuple of OLE path segments."""
        return tuple(MSGPath(self.source).parts)

    @property
    def ownerParts(self) -> Tuple[str, ...]:
        """The owner path as a tuple of OLE path segments."""
        return tuple(MSGPath(self.owner).parts) if self.owner else ()

    @property
    def verifiedLength(self) -> Optional[int]:
        """
        The byte length verified by an actual read, or ``None``.
        """
        return self.readSize if self.readable else None

    @property
    def length(self) -> Optional[int]:
        """Alias for :attr:`byteLength`."""
        return self.byteLength

    @property
    def size(self) -> Optional[int]:
        """Alias for :attr:`byteLength`."""
        return self.byteLength

    @property
    def contentDigest(self) -> Optional[str]:
        """Alias for :attr:`digest`."""
        return self.digest

    @property
    def statusValue(self) -> str:
        """The plain-string value of :attr:`status`."""
        return self.status.value

    @property
    def isReadable(self) -> bool:
        """Alias for :attr:`readable`."""
        return self.readable

    @property
    def isMissing(self) -> bool:
        return self.status is StreamStatus.MISSING

    @property
    def isZeroBytes(self) -> bool:
        return self.status is StreamStatus.ZERO_BYTES

    @property
    def isReadError(self) -> bool:
        return self.status is StreamStatus.READ_ERROR

    @property
    def error(self) -> Optional[str]:
        """Alias for :attr:`errorMessage`."""
        return self.errorMessage

    def __getitem__(self, key: str) -> Any:
        """Allows dictionary-style access to the serialized fields."""
        if key in ('relative',):
            key = 'relativePath'
        elif key in ('path', 'streamPath', 'fullPath'):
            key = 'source'
        elif key in ('length', 'size'):
            key = 'byteLength'
        elif key in ('contentDigest',):
            key = 'digest'
        return self.asDict()[key]

    def get(self, key: str, default = None):
        """Dictionary-style access with a default."""
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):
        """Returns the serialized field names."""
        return self.asDict().keys()

    def items(self):
        """Returns the serialized field pairs."""
        return self.asDict().items()

    def values(self):
        """Returns the serialized field values."""
        return self.asDict().values()

    @property
    def name(self) -> str:
        """The final stream name, without its containing storages."""
        return MSGPath(self.source).parts[-1]

    @property
    def byteLength(self) -> Optional[int]:
        """
        The most specific known byte length.

        This is the number of bytes actually read when the stream was readable,
        otherwise the size recorded in the OLE directory.  It is ``None`` when
        neither is available (for example, for an expected but missing stream).
        """
        return self.readSize if self.readSize is not None else self.declaredSize

    @property
    def readable(self) -> bool:
        """Whether the stream was opened and read without an error."""
        return self.status in (StreamStatus.OK, StreamStatus.ZERO_BYTES)

    def asDict(self) -> Dict[str, Any]:
        """
        Converts the entry to a plain dictionary suitable for JSON output.
        """
        return {
            'owner': self.owner,
            'ownerLevel': self.ownerLevel,
            'source': self.source,
            'relativePath': self.relativePath,
            'container': self.container,
            'status': self.status.value,
            'declaredSize': self.declaredSize,
            'readSize': self.readSize,
            'byteLength': self.byteLength,
            'digest': self.digest,
            'digestAlgorithm': self.digestAlgorithm,
            'errorType': self.errorType,
            'errorMessage': self.errorMessage,
            'inScope': self.inScope,
            'referenced': self.referenced,
            'ownerPath': self.ownerPath,
            'verifiedLength': self.verifiedLength,
            'path': self.source,
            'fullPath': self.source,
            'streamPath': self.source,
            'length': self.byteLength,
        }

    def toDict(self) -> Dict[str, Any]:
        """Alias for :meth:`asDict`."""
        return self.asDict()


class StreamManifest:
    """
    An ordered collection of :class:`StreamEntry` instances.

    Entries are kept as a sequence rather than a mapping keyed by relative
    name or digest.  Two different occurrences with the same name or contents
    therefore remain two entries.
    """

    def __init__(self, entries: Iterable[StreamEntry], prefix = '',
                 digestAlgorithm: Optional[str] = 'sha256'):
        self.__entries = tuple(entries)
        self.__prefix = str(MSGPath(prefix))
        self.__digestAlgorithm = digestAlgorithm

    def __iter__(self):
        return iter(self.__entries)

    def __len__(self) -> int:
        return len(self.__entries)

    def __bool__(self) -> bool:
        return bool(self.__entries)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return StreamManifest(
                self.__entries[index], self.__prefix, self.__digestAlgorithm
            )
        return self.__entries[index]

    def __repr__(self) -> str:
        return (f'<StreamManifest prefix={self.__prefix!r} '
                f'entries={len(self.__entries)}>')

    def __eq__(self, other) -> bool:
        if isinstance(other, StreamManifest):
            return (self.__entries == other.__entries
                    and self.__prefix == other.__prefix
                    and self.__digestAlgorithm == other.__digestAlgorithm)
        return NotImplemented

    @property
    def entries(self) -> Tuple[StreamEntry, ...]:
        """The manifest entries in deterministic order."""
        return self.__entries

    @property
    def records(self) -> Tuple[StreamEntry, ...]:
        """Alias for :attr:`entries`."""
        return self.__entries

    def items(self):
        """Alias for :attr:`entries`."""
        return iter(self.__entries)

    def rows(self) -> List[Dict[str, Any]]:
        """Alias for :meth:`asList`."""
        return self.asList()

    @property
    def prefix(self) -> str:
        """The message prefix that scoped this manifest."""
        return self.__prefix

    @property
    def digestAlgorithm(self) -> Optional[str]:
        """The digest algorithm used, or ``None`` when digests were disabled."""
        return self.__digestAlgorithm

    @property
    def paths(self) -> Tuple[str, ...]:
        """The source paths of all entries, in manifest order."""
        return tuple(x.source for x in self.__entries)

    @property
    def owners(self) -> Tuple[str, ...]:
        """The distinct owners, in first-occurrence order."""
        result = []
        for entry in self.__entries:
            if entry.owner not in result:
                result.append(entry.owner)
        return tuple(result)

    def __contains__(self, source) -> bool:
        return self.bySource(source) is not None

    def statusCounts(self) -> Dict[str, int]:
        """Returns a mapping of status values to entry counts."""
        counts: Dict[str, int] = {x.value: 0 for x in StreamStatus}
        for entry in self.__entries:
            counts[entry.status.value] += 1
        return counts

    @property
    def stored(self) -> 'StreamManifest':
        """Entries physically stored inside this manifest's scope."""
        return self.inScope()

    @property
    def referenced(self) -> 'StreamManifest':
        """Entries that exist elsewhere in the original file."""
        return self.references()

    @property
    def missing(self) -> 'StreamManifest':
        """Entries with the ``missing`` status."""
        return self.withStatus(StreamStatus.MISSING)

    @property
    def readErrors(self) -> 'StreamManifest':
        """Entries with the ``read_error`` status."""
        return self.withStatus(StreamStatus.READ_ERROR)

    @property
    def empty(self) -> 'StreamManifest':
        """Entries with the ``zero_bytes`` status."""
        return self.withStatus(StreamStatus.ZERO_BYTES)

    def byRelativePath(self, relativePath) -> Optional[StreamEntry]:
        """
        Returns the first entry with the requested owner-relative path.
        """
        key = str(MSGPath(relativePath)).casefold()
        for entry in self.__entries:
            if entry.relativePath.casefold() == key:
                return entry
        return None

    def byOwnerAndRelative(self, owner, relativePath) -> Optional[StreamEntry]:
        """
        Returns the entry with the exact owner and owner-relative path.
        """
        ownerKey = str(MSGPath(owner)).casefold()
        relativeKey = str(MSGPath(relativePath)).casefold()
        for entry in self.__entries:
            if (entry.owner.casefold() == ownerKey
                    and entry.relativePath.casefold() == relativeKey):
                return entry
        return None

    def sourceOf(self, owner, relativePath) -> Optional[str]:
        """
        Returns the full source path for an owner-relative stream, or ``None``.
        """
        entry = self.byOwnerAndRelative(owner, relativePath)
        return None if entry is None else entry.source

    def bySource(self, source) -> Optional[StreamEntry]:
        """
        Returns the entry at the original source path, or ``None``.

        Path comparison is case-insensitive, matching OLE directory names.
        """
        key = str(MSGPath(source)).casefold()
        for entry in self.__entries:
            if entry.source.casefold() == key:
                return entry
        return None

    get = bySource

    def forOwner(self, owner) -> 'StreamManifest':
        """
        Returns a new manifest containing entries whose owner matches.
        """
        key = str(MSGPath(owner)).casefold()
        return StreamManifest(
            (x for x in self.__entries if x.owner.casefold() == key),
            self.__prefix,
            self.__digestAlgorithm,
        )

    def withStatus(self, status: Union[StreamStatus, str]) -> 'StreamManifest':
        """
        Returns a new manifest containing entries with the requested status.
        """
        status = StreamStatus(status)
        return StreamManifest(
            (x for x in self.__entries if x.status is status),
            self.__prefix,
            self.__digestAlgorithm,
        )

    def inScope(self) -> 'StreamManifest':
        """Returns a new manifest containing only in-scope entries."""
        return StreamManifest(
            (x for x in self.__entries if x.inScope),
            self.__prefix,
            self.__digestAlgorithm,
        )

    def references(self) -> 'StreamManifest':
        """Returns a new manifest containing only referenced entries."""
        return StreamManifest(
            (x for x in self.__entries if x.referenced),
            self.__prefix,
            self.__digestAlgorithm,
        )

    def indexOf(self, source) -> Optional[int]:
        """
        Returns the index of the entry at ``source``, or ``None``.
        """
        key = str(MSGPath(source)).casefold()
        for index, entry in enumerate(self.__entries):
            if entry.source.casefold() == key:
                return index
        return None

    def asList(self) -> List[Dict[str, Any]]:
        """Returns the entries as a list of plain dictionaries."""
        return [x.asDict() for x in self.__entries]

    def toDict(self) -> Dict[str, Any]:
        """
        Returns a JSON-compatible representation of the whole manifest.
        """
        return {
            'schema': 'extract_msg.stream_manifest/1',
            'prefix': self.__prefix,
            'digestAlgorithm': self.__digestAlgorithm,
            'entries': self.asList(),
        }

    def toJSON(self, indent: Optional[int] = None, **kwargs) -> str:
        """
        Serializes the manifest to JSON.

        Key sorting is enabled so repeated serializations of the same manifest
        are byte-for-byte stable.
        """
        kwargs.setdefault('ensure_ascii', False)
        kwargs.setdefault('sort_keys', True)
        return json.dumps(self.toDict(), indent = indent, **kwargs)

    def save(self, path, indent: Optional[int] = 2, **kwargs) -> None:
        """
        Writes the manifest as UTF-8 JSON to ``path``.
        """
        with open(path, 'w', encoding = 'utf-8', newline = '\n') as f:
            f.write(self.toJSON(indent = indent, **kwargs))
            f.write('\n')

    writeJSON = save

    def write(self, path, indent: Optional[int] = 2, **kwargs) -> None:
        """Alias for :meth:`save`."""
        self.save(path, indent, **kwargs)


def _partsKey(path: MSGPath) -> Tuple[str, ...]:
    return tuple(x.casefold() for x in path.parts)


def _pathString(path: MSGPath) -> str:
    """
    Returns the path as a string, using the same root representation as
    :attr:`MSGFile.prefix` (``'.'``).
    """
    return str(path)


def _relativeParts(path: MSGPath, base: MSGPath) -> Optional[Tuple[str, ...]]:
    """
    Returns the path parts below ``base``, or ``None`` if ``path`` is not
    inside it.  Comparison is case-insensitive, as OLE names are.
    """
    pathKey = _partsKey(path)
    baseKey = _partsKey(base)
    if pathKey[:len(baseKey)] != baseKey:
        return None
    return path.parts[len(baseKey):]


def _relativePath(path: MSGPath, base: MSGPath) -> Optional[MSGPath]:
    parts = _relativeParts(path, base)
    if parts is None:
        return None
    return MSGPath(*parts)


def _ownerFor(path: MSGPath, owners: Dict[Tuple[str, ...], MSGPath],
              scope: MSGPath) -> MSGPath:
    """
    Finds the deepest message storage that owns ``path``.
    """
    pathKey = _partsKey(path)
    best = scope
    bestLength = -1
    for ownerKey, owner in owners.items():
        if pathKey[:len(ownerKey)] == ownerKey and len(ownerKey) > bestLength:
            # A stream cannot be its own owner.
            if len(pathKey) == len(ownerKey):
                continue
            best = owner
            bestLength = len(ownerKey)
    return best


def _ownerLevel(owner: MSGPath) -> int:
    return sum(1 for x in owner.parts if x.casefold() == _EMBEDDED_MSG_STORAGE.casefold())


def _listPaths(msg: MSGFile, streams: bool, storages: bool,
               scope: MSGPath) -> List[MSGPath]:
    """
    Lists physical paths below ``scope``.
    """
    paths = []
    for rawPath in msg._oleListDir(streams, storages):
        path = MSGPath(*rawPath)
        if _relativeParts(path, scope) is not None and len(path) > len(scope):
            paths.append(path)
    return paths


def _streamPaths(msg: MSGFile, scope: MSGPath) -> List[MSGPath]:
    return _listPaths(msg, True, False, scope)


def _ownerPaths(msg: MSGFile, scope: MSGPath,
                streams: Iterable[MSGPath]) -> Dict[Tuple[str, ...], MSGPath]:
    """
    Builds the mapping of message storage prefixes to their paths.

    The root of the file is always an owner.  A nested
    ``__substg1.0_3701000D`` storage is an embedded message owner only when it
    has the properties stream that identifies it as one.  This check uses
    directory existence, not stream contents, so a stream that cannot be read
    does not prevent ownership of its siblings or of its containing message
    from being determined.
    """
    streamKeys = {_partsKey(x) for x in streams}
    storageKeys = {_partsKey(x) for x in _listPaths(msg, False, True, MSGPath())}
    # The scope itself is an owner.  The root of the file is also an owner,
    # which keeps source attribution stable when the scope is the root.
    owners: Dict[Tuple[str, ...], MSGPath] = {(): MSGPath()}
    if scope:
        owners[_partsKey(scope)] = scope

    def isMessageStorage(storage: MSGPath) -> bool:
        key = _partsKey(storage)
        # The properties stream is the definitive marker for a normal message.
        if _partsKey(storage / _PROPERTIES_STREAM) in streamKeys:
            return True
        # A slightly malformed message may be missing its properties stream.
        # Message-only children such as the message class, named-property
        # storage, or recipient storage still identify it structurally.
        for name in ('__nameid_version1.0', '__substg1.0_001A001F',
                     '__substg1.0_001A001E'):
            if _partsKey(storage / name) in streamKeys or _partsKey(storage / name) in storageKeys:
                return True
        for other in storageKeys:
            if (other[:len(key)] == key and len(other) > len(key)
                    and other[len(key)].startswith('__recip_version1.0_#')):
                return True
        return False

    for storage in _listPaths(msg, False, True, scope):
        if storage.parts[-1].casefold() != _EMBEDDED_MSG_STORAGE.casefold():
            continue
        if isMessageStorage(storage):
            owners[_partsKey(storage)] = storage

    return owners


def _resolveExpected(expected, msg: MSGFile, prefix: bool,
                     scope: MSGPath) -> List[MSGPath]:
    """
    Resolves the expected stream paths for a manifest call.

    With ``prefix = True`` each expected path is relative to the message being
    inspected.  With ``prefix = False`` each expected path is already a full
    path from the root of the original file.
    """
    if expected is None:
        return []
    if isinstance(expected, dict):
        expected = expected.keys()
    elif isinstance(expected, (str, bytes, MSGPath)):
        expected = [expected]

    paths = []
    base = msg.prefix if isinstance(msg.prefix, MSGPath) else MSGPath(msg.prefix)
    for item in expected:
        if hasattr(item, 'source'):
            item = item.source
        if isinstance(item, bytes):
            item = item.decode('utf-8')
        path = MSGPath(item)
        if prefix:
            # Accept a path that already includes this message's prefix, as
            # well as one relative to it.  Both forms are common when callers
            # compare an inner manifest with a whole-file manifest.
            if base and _relativeParts(path, base) is not None and len(path) > len(base):
                resolved = path
            else:
                resolved = base / path
        else:
            resolved = path
        paths.append(resolved)

    return paths


def _validateDigest(digest: Optional[str]) -> Optional[str]:
    if digest is None:
        return None
    if not isinstance(digest, str):
        raise TypeError(':param digest: must be a string or None.')
    try:
        hashlib.new(digest)
    except (TypeError, ValueError) as e:
        raise ValueError(f'Unsupported digest algorithm: {digest}') from e
    return digest


def _readStream(msg: MSGFile, path: MSGPath, inScope: bool) -> Optional[bytes]:
    """
    Reads a stream for the manifest.

    In-scope streams go through ``MSGFile.getStream`` so that the public
    read path, and any instrumentation of it, is used.  A path outside the
    message's scope cannot be found by that method because its existence
    cache is limited to the prefix; read it directly from the shared OLE file
    instead.
    """
    try:
        data = msg.getStream(path, prefix = False)
    except Exception:
        # A path outside the scope can fail in getStream for reasons other
        # than absence.  If it is a real directory entry, the manifest must
        # still report the read result, so try the shared OLE stream directly.
        if inScope:
            raise
        data = None

    if data is not None or inScope:
        return data

    entry = msg._getOleEntry(path, prefix = False)
    with entry.olefile.openstream(str(path)) as stream:
        return stream.read() or b''


def _entryForStream(msg: MSGFile, path: MSGPath, owner: MSGPath,
                    digest: Optional[str], inScope: bool = True) -> StreamEntry:
    relative = _relativePath(path, owner)
    if relative is None:
        relative = MSGPath()
    container = MSGPath(*path.parts[:-1]) if len(path.parts) > 1 else MSGPath()

    declaredSize = None
    lookupError = None
    try:
        declaredSize = msg._getOleEntry(path, prefix = False).size
    except Exception as e:
        lookupError = e

    errorType = None
    errorMessage = None
    digestValue = None
    digestAlgorithm = None
    readSize = None

    try:
        data = _readStream(msg, path, inScope)
    except Exception as e:
        logger.debug('Failed to read stream "%s" for the stream manifest.', path, exc_info = True)
        errorType = type(e).__name__
        errorMessage = str(e)
        status = StreamStatus.READ_ERROR
    else:
        if data is None:
            if lookupError is None:
                lookupError = OSError('The stream was listed but returned no data.')
            errorType = type(lookupError).__name__
            errorMessage = str(lookupError)
            status = StreamStatus.READ_ERROR
        else:
            readSize = len(data)
            status = StreamStatus.ZERO_BYTES if readSize == 0 else StreamStatus.OK
            if digest is not None:
                digestValue = hashlib.new(digest, data).hexdigest()
                digestAlgorithm = digest

    return StreamEntry(
        owner = _pathString(owner),
        ownerLevel = _ownerLevel(owner),
        source = str(path),
        relativePath = _pathString(relative),
        container = _pathString(container),
        status = status,
        declaredSize = declaredSize,
        readSize = readSize,
        digest = digestValue,
        digestAlgorithm = digestAlgorithm,
        errorType = errorType,
        errorMessage = errorMessage,
    )


def _entryForMissing(path: MSGPath, owner: MSGPath) -> StreamEntry:
    relative = _relativePath(path, owner)
    if relative is None:
        relative = MSGPath()
    container = MSGPath(*path.parts[:-1]) if len(path.parts) > 1 else MSGPath()
    return StreamEntry(
        owner = _pathString(owner),
        ownerLevel = _ownerLevel(owner),
        source = str(path),
        relativePath = _pathString(relative),
        container = _pathString(container),
        status = StreamStatus.MISSING,
        declaredSize = None,
        readSize = None,
    )


def buildStreamManifest(msg: MSGFile, expected = None, prefix: bool = True,
                        digest: Optional[str] = 'sha256') -> StreamManifest:
    """
    Builds a deterministic inventory of the physical streams in an MSG file.

    The inventory is built from the OLE directory tree, not from logical
    properties resolved by the parser.  This keeps ownership tied to the
    storage in which a stream actually occurs.  In particular, a named
    property definition stored in the root message is reported once with the
    root as its owner; it is not copied into the manifest of each embedded
    message that can refer to it.

    :param msg: The ``MSGFile`` (or subclass) instance to inspect.
    :param expected: Optional iterable of stream paths to include even if they
        are not present.  Such entries have the ``MISSING`` status.  A path
        that is itself a storage, or an ordinary stream that only exists in a
        different message or storage, is also ``MISSING``.  A named-property
        definition that is shared from the root is reported at its root source
        with ``inScope = False`` and ``referenced = True``.
    :param prefix: If ``True`` (default), values in ``expected`` are relative
        to ``msg.prefix``.  If ``False``, they are full paths from the root of
        the original OLE file, exactly like ``prefix = False`` on
        :meth:`MSGFile.getStream`.
    :param digest: A ``hashlib`` algorithm name, or ``None`` to omit digests.
        Digests are calculated only for streams whose bytes were read without
        an error.  An empty stream still has a digest of the empty byte string.
    :returns: A :class:`StreamManifest`.  Entries are sorted by their full
        source path, so repeated calls produce the same order.

    Each stream is read in isolation.  A read failure is recorded on that
    entry and does not stop the remaining streams from being listed.
    """
    digest = _validateDigest(digest)
    scope = msg.prefix if isinstance(msg.prefix, MSGPath) else MSGPath(msg.prefix)

    streams = _streamPaths(msg, scope)
    owners = _ownerPaths(msg, scope, streams)

    rootStreams = _streamPaths(msg, MSGPath())
    rootStreamKeys = {_partsKey(x) for x in rootStreams}
    rootStorageKeys = {_partsKey(x) for x in _listPaths(msg, False, True, MSGPath())}
    rootOwners = _ownerPaths(msg, MSGPath(), rootStreams)
    # Every storage in the file, not just those in scope, so an expected
    # storage can be classified without a per-path lookup.
    allStorageKeys = rootStorageKeys
    allStreamKeys = rootStreamKeys

    entries: List[StreamEntry] = []
    seen = set()
    for path in streams:
        key = _partsKey(path)
        if key in seen:
            continue
        seen.add(key)
        owner = _ownerFor(path, owners, scope)
        entries.append(_entryForStream(msg, path, owner, digest))

    for path in _resolveExpected(expected, msg, prefix, scope):
        key = _partsKey(path)
        if key in seen:
            continue
        seen.add(key)
        owner = _ownerFor(path, owners, scope)
        inScope = (_relativeParts(path, scope) is not None
                   and len(path) > len(scope))
        if key in allStorageKeys and key not in allStreamKeys:
            # A storage was supplied where a stream was expected.
            entries.append(_entryForMissing(path, owner))
        elif inScope:
            # A common named-property definition can live at the root of the
            # original file while an inner message refers to it.  Only that
            # definition storage is resolved to the root occurrence; an
            # ordinary absent stream remains ``missing`` even if a stream of
            # the same name happens to exist elsewhere in the file.
            rootCandidate = MSGPath(*path.parts[len(scope.parts):])
            isDefinition = (rootCandidate.parts
                            and rootCandidate.parts[0].casefold() == _NAMEID_STORAGE.casefold())
            if (isDefinition
                    and _partsKey(path) not in rootStreamKeys
                    and _partsKey(rootCandidate) in rootStreamKeys):
                externalOwner = _ownerFor(rootCandidate, rootOwners, MSGPath())
                entries.append(dataclasses.replace(
                    _entryForStream(msg, rootCandidate, externalOwner, digest, False),
                    inScope = False,
                    referenced = True,
                ))
            else:
                entries.append(_entryForMissing(path, owner))
        elif key in rootStreamKeys:
            # The path exists elsewhere in the original file.  It is reported
            # with its actual owner so it maps to the same source as the full
            # manifest, and is marked as outside this manifest's scope rather
            # than being counted as stored again in this message.
            externalOwner = _ownerFor(path, rootOwners, MSGPath())
            entries.append(dataclasses.replace(
                _entryForStream(msg, path, externalOwner, digest, False),
                inScope = False,
                referenced = True,
            ))
        else:
            entries.append(dataclasses.replace(
                _entryForMissing(path, owner), inScope = False
            ))

    entries.sort(key = lambda x: (_partsKey(MSGPath(x.source)), MSGPath(x.source).parts))
    return StreamManifest(entries, scope, digest)


def buildStreamManifestEntry(msg: MSGFile, path, prefix: bool = True,
                             digest: Optional[str] = 'sha256') -> StreamEntry:
    """
    Builds a single manifest entry for ``path``.

    ``path`` is interpreted exactly as it is by :meth:`MSGFile.getStream`, so
    ``prefix = True`` means relative to ``msg.prefix`` and ``prefix = False``
    means a full path from the root of the original file.  A missing stream
    still produces an entry, with ``StreamStatus.MISSING``.
    """
    manifest = buildStreamManifest(msg, (path,), prefix, digest)
    scope = msg.prefix if isinstance(msg.prefix, MSGPath) else MSGPath(msg.prefix)
    resolved = _resolveExpected((path,), msg, prefix, scope)[0]
    for entry in manifest:
        if entry.source.casefold() == str(resolved).casefold():
            return entry
    raise KeyError(f'Could not build a manifest entry for "{resolved}".')


def getStreamManifestEntry(msg: MSGFile, path, prefix: bool = True,
                           digest: Optional[str] = 'sha256') -> StreamEntry:
    """Alias for :func:`buildStreamManifestEntry`."""
    return buildStreamManifestEntry(msg, path, prefix, digest)


def streamManifest(msg: MSGFile, expected = None, prefix: bool = True,
                   digest: Optional[str] = 'sha256') -> StreamManifest:
    """Module-level alias for :func:`buildStreamManifest`."""
    return buildStreamManifest(msg, expected, prefix, digest)


def getStreamManifest(msg: MSGFile, expected = None, prefix: bool = True,
                      digest: Optional[str] = 'sha256') -> StreamManifest:
    """Alias for :func:`buildStreamManifest`."""
    return buildStreamManifest(msg, expected, prefix, digest)


def build_stream_manifest(msg: MSGFile, expected = None, prefix: bool = True,
                          digest: Optional[str] = 'sha256') -> StreamManifest:
    """Snake-case alias for :func:`buildStreamManifest`."""
    return buildStreamManifest(msg, expected, prefix, digest)


def stream_manifest(msg: MSGFile, expected = None, prefix: bool = True,
                    digest: Optional[str] = 'sha256') -> StreamManifest:
    """Snake-case alias for :func:`buildStreamManifest`."""
    return buildStreamManifest(msg, expected, prefix, digest)



def manifestFromPath(path, expected = None, prefix: bool = True,
                     digest: Optional[str] = 'sha256', **kwargs) -> StreamManifest:
    """
    Opens ``path`` as an MSG file, builds its stream manifest, and closes it.

    All keyword arguments are passed to :func:`extract_msg.openMsg`.
    """
    from .open_msg import openMsg

    with openMsg(path, **kwargs) as msg:
        return buildStreamManifest(msg, expected, prefix, digest)


def manifest_from_path(path, expected = None, prefix: bool = True,
                       digest: Optional[str] = 'sha256', **kwargs) -> StreamManifest:
    """Alias for :func:`manifestFromPath`."""
    return manifestFromPath(path, expected, prefix, digest, **kwargs)



AuditManifest = StreamManifest
Manifest = StreamManifest
StreamRecord = StreamEntry
StreamInfo = StreamEntry
StreamListing = StreamManifest
StreamState = StreamStatus
