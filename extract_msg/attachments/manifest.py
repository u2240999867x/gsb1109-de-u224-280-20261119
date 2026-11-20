from __future__ import annotations


__all__ = [
    'ATTACHMENT_MANIFEST_FILENAME',
    'ATTACHMENT_MANIFEST_SCHEMA',
    'AttachmentManifest',
    'AttachmentManifestEntry',
]


import datetime
import hashlib
import json
import pathlib

from typing import Any, Dict, Iterable, List, Optional, Tuple, TYPE_CHECKING, Union

from ..enums import AttachmentType, SaveType
from ..msg_path import MSGPath
from ..utils import prepareFilename


if TYPE_CHECKING:
    from ..msg_classes.msg import MSGFile
    from .attachment_base import AttachmentBase
    from .signed_att import SignedAttachment


# The default filename used when a manifest is written next to the saved data.
ATTACHMENT_MANIFEST_FILENAME = 'attachment_manifest.json'

# Schema identifier so consumers can distinguish this document from arbitrary
# JSON files that may also live in an archive.
ATTACHMENT_MANIFEST_SCHEMA = 'extract_msg.attachment_manifest/1'


def _pathString(path) -> str:
    """
    Converts a path-like object into a normalized string without changing its
    meaning for the destination (folder paths stay absolute, ZIP members stay
    relative).
    """
    return str(path).replace('\\', '/')


def _relativePath(path, root) -> Optional[str]:
    """
    Returns the part of :param path: that is below :param root:, if possible.
    """
    if path is None or root is None:
        return None

    pathString = _pathString(path)
    rootString = _pathString(root).rstrip('/')
    if not rootString:
        return pathString
    if pathString.startswith(rootString + '/'):
        return pathString[len(rootString) + 1:]
    if pathString == rootString:
        return ''

    try:
        return str(pathlib.PurePath(pathString).relative_to(pathlib.PurePath(rootString)))
    except (TypeError, ValueError):
        return None


def _contentHash(data: Any) -> Optional[str]:
    """
    Hashes a byte attachment if it is directly available.

    Embedded MSG files are represented by an ``MSGFile`` instead of bytes in
    this API. Hashing their exported bytes here would make manifest generation
    disproportionately expensive, so their content is left unhashed.
    """
    if isinstance(data, bytes):
        return hashlib.sha256(data).hexdigest()
    return None


def _attachmentTypeName(type_: AttachmentType) -> str:
    """
    Produces a stable, human-readable name for an attachment type.
    """
    return type_.name if isinstance(type_, AttachmentType) else str(type_)


class AttachmentManifestEntry:
    """
    One source attachment occurrence in the message tree.

    An occurrence is identified by its location in the MSG file, not by the
    bytes of the attachment. Two attachments with identical content therefore
    remain two entries.
    """

    def __init__(
        self,
        occurrenceId: str,
        sourcePath: str,
        parentOccurrenceId: Optional[str],
        embeddedDepth: int,
        index: int,
        attachment: Union['AttachmentBase', 'SignedAttachment', None] = None,
    ):
        self.__occurrenceId = occurrenceId
        self.__sourcePath = sourcePath
        self.__parentOccurrenceId = parentOccurrenceId
        self.__embeddedDepth = embeddedDepth
        self.__index = index
        self.__saves: Dict[str, Dict[str, Any]] = {}

        name = None
        attachmentType = None
        contentSha256 = None
        if attachment is not None:
            try:
                name = attachment.name
            except Exception:
                name = None
            try:
                attachmentType = _attachmentTypeName(attachment.type)
            except Exception:
                attachmentType = None
            try:
                contentSha256 = _contentHash(attachment.data)
            except Exception:
                contentSha256 = None

        self.__name = name
        self.__attachmentType = attachmentType
        self.__contentSha256 = contentSha256

    @property
    def occurrenceId(self) -> str:
        """
        The stable identifier for this source occurrence.
        """
        return self.__occurrenceId

    @property
    def parentOccurrenceId(self) -> Optional[str]:
        """
        The occurrence that contains this attachment, if it is nested.
        """
        return self.__parentOccurrenceId

    @property
    def embeddedDepth(self) -> int:
        """
        How many embedded messages must be traversed to reach this attachment.
        """
        return self.__embeddedDepth

    @property
    def sourcePath(self) -> str:
        """
        The internal MSG path of the attachment storage.
        """
        return self.__sourcePath

    @property
    def saves(self) -> Tuple[Dict[str, Any], ...]:
        """
        The save records for this occurrence, one for each destination.
        """
        return tuple(self.__saves.values())

    @property
    def sanitizedName(self) -> Optional[str]:
        """
        The attachment name after the characters invalid in a filename are
        removed.

        This is useful when two distinct display names only differ by such
        characters.
        """
        if self.__name is None:
            return None
        try:
            return prepareFilename(str(self.__name))
        except Exception:
            return None

    def _setSave(self, record: Dict[str, Any]) -> None:
        self.__saves[record['destinationId']] = record

    def _hasSave(self, destinationId: str) -> bool:
        return destinationId in self.__saves

    def _discardSave(self, destinationId: str) -> None:
        self.__saves.pop(destinationId, None)

    def _getSave(self, destinationId: str) -> Optional[Dict[str, Any]]:
        return self.__saves.get(destinationId)

    def _toDict(self) -> Dict[str, Any]:
        return {
            'occurrenceId': self.__occurrenceId,
            'source': {
                'attachmentPath': self.__sourcePath,
                'parentOccurrenceId': self.__parentOccurrenceId,
                'embeddedDepth': self.__embeddedDepth,
                'index': self.__index,
                'name': self.__name,
                'sanitizedName': self.sanitizedName,
                'attachmentType': self.__attachmentType,
                'contentSha256': self.__contentSha256,
            },
            'saved': list(self.__saves.values()),
        }


class AttachmentManifest:
    """
    A record of every attachment occurrence in a message and where each
    occurrence was written for each save destination.

    A manifest is created from a message before saving. Pass the same instance
    to multiple ``MessageBase.save`` calls to keep one document covering a
    folder export and a ZIP export of the same source message. The occurrence
    identifiers and hashes do not change between destinations; only the save
    records differ.

    Existing save behavior is unchanged when a manifest is not requested.
    """

    def __init__(self, msg: Optional['MSGFile'] = None):
        self.__entries: Dict[str, AttachmentManifestEntry] = {}
        self.__entriesByObject: Dict[int, str] = {}
        self.__containers: set[str] = set()
        self.__destinations: List[Dict[str, Any]] = []
        self.__message = msg
        if msg is not None:
            self.__loadMessage(msg)

    def loadMessage(self, msg: 'MSGFile') -> None:
        """
        Populates the manifest from a message if it has no occurrences yet.

        This allows a caller to construct an empty manifest and pass it to the
        first save call, which then binds it to that source message.
        """
        if self.__entries:
            return
        self.__message = msg
        self.__loadMessage(msg)

    def _ensureLoaded(self, msg: 'MSGFile') -> None:
        if not self.__entries:
            self.loadMessage(msg)

    @property
    def message(self) -> Optional['MSGFile']:
        """
        The source message this manifest is bound to, if any.
        """
        return self.__message

    @property
    def entries(self) -> Tuple[AttachmentManifestEntry, ...]:
        """
        All file attachment occurrences, in message-tree order.

        Embedded messages are containers rather than saved attachment files and
        are excluded from this list. Their path is still available through the
        ``sourceTrail`` and ``parentOccurrenceId`` of the occurrences they
        contain.
        """
        return tuple(
            entry for entry in self.__entries.values()
            if entry.occurrenceId not in self.__containers
        )

    @property
    def allEntries(self) -> Tuple[AttachmentManifestEntry, ...]:
        """
        Every attachment object, including embedded-message containers.
        """
        return tuple(self.__entries.values())

    @property
    def containers(self) -> Tuple[AttachmentManifestEntry, ...]:
        """
        The embedded-message containers in the message tree.
        """
        return tuple(
            entry for entry in self.__entries.values()
            if entry.occurrenceId in self.__containers
        )

    @property
    def destinations(self) -> Tuple[Dict[str, Any], ...]:
        """
        The destinations that have been registered with this manifest.
        """
        return tuple(self.__destinations)

    def __messageInfo(self) -> Dict[str, Any]:
        if self.__message is None:
            return {}
        info: Dict[str, Any] = {}
        for key, attr in (
            ('subject', 'subject'),
            ('classType', 'classType'),
            ('filename', 'filename'),
        ):
            try:
                info[key] = getattr(self.__message, attr)
            except Exception:
                info[key] = None
        return info

    def __loadMessage(self, msg: 'MSGFile') -> None:
        self.__loadAttachments(msg, None, 0)

    def __loadAttachments(
        self,
        msg: 'MSGFile',
        parentOccurrenceId: Optional[str],
        embeddedDepth: int,
    ) -> None:
        try:
            attachments = msg.attachments
        except Exception:
            return

        for index, attachment in enumerate(attachments, 1):
            sourcePath = self.__sourcePathForAttachment(
                attachment, parentOccurrenceId, index
            )
            occurrenceId = sourcePath
            if occurrenceId in self.__entries:
                # Extremely defensive: a malformed message could advertise
                # the same storage twice. Keep the first occurrence and make
                # the duplicate addressable rather than silently merging it.
                occurrenceId = f'{sourcePath}#{index}'
                if occurrenceId in self.__entries:
                    continue

            entry = AttachmentManifestEntry(
                occurrenceId,
                sourcePath,
                parentOccurrenceId,
                embeddedDepth,
                index,
                attachment,
            )
            self.__entries[occurrenceId] = entry
            self.__entriesByObject[id(attachment)] = occurrenceId

            data = None
            try:
                data = attachment.data
            except Exception:
                pass

            if data is not None and hasattr(data, 'attachments') and hasattr(data, 'prefix'):
                self.__containers.add(occurrenceId)
                self.__loadAttachments(data, occurrenceId, embeddedDepth + 1)

    def __sourcePathForAttachment(
        self,
        attachment,
        parentOccurrenceId: Optional[str],
        index: int,
    ) -> str:
        try:
            attachmentDir = getattr(attachment, 'dir', None)
            if attachmentDir is not None:
                msg = getattr(attachment, 'msg', None)
                prefix = getattr(msg, 'prefix', None) if msg is not None else None
                path = MSGPath(prefix) / attachmentDir if prefix else MSGPath(attachmentDir)
                if path:
                    return _pathString(path)
        except Exception:
            pass

        base = parentOccurrenceId or 'message-root'
        return f'{base}/attachment-#{index:08d}'

    def entryFor(self, attachment) -> Optional[AttachmentManifestEntry]:
        """
        Returns the manifest entry for an attachment instance, if it is part
        of this manifest.

        Identity is bound to the attachment's storage path in the MSG file, so
        this also works for a freshly opened instance of the same source
        message.
        """
        return self._findEntry(attachment)

    def getOccurrence(self, occurrenceId: str) -> Optional[AttachmentManifestEntry]:
        """
        Returns an occurrence by its identifier.
        """
        return self.__entries.get(occurrenceId)

    def _findEntry(self, attachment) -> Optional[AttachmentManifestEntry]:
        occurrenceId = self.__entriesByObject.get(id(attachment))
        if occurrenceId is not None and occurrenceId in self.__entries:
            return self.__entries[occurrenceId]

        # The caller may have supplied an attachment from a different
        # ``MSGFile`` instance for the same source. Fall back to the storage
        # path, which is the actual occurrence identity.
        try:
            attachmentDir = getattr(attachment, 'dir', None)
            if attachmentDir is not None:
                msg = getattr(attachment, 'msg', None)
                prefix = getattr(msg, 'prefix', None) if msg is not None else None
                path = MSGPath(prefix) / attachmentDir if prefix else MSGPath(attachmentDir)
                return self.__entries.get(_pathString(path))
        except Exception:
            pass

        # Signed attachments use the tree path rather than a ``dir`` property.
        try:
            treePath = getattr(attachment, 'treePath', None)
            if treePath:
                for entry in self.__entries.values():
                    if self.__entriesByObject.get(id(attachment)) == entry.occurrenceId:
                        return entry
        except Exception:
            pass

        return None

    def beginDestination(
        self,
        kind: str,
        root,
        archive: Optional[str] = None,
    ) -> str:
        """
        Registers a save destination and returns its identifier.

        :param kind: ``'folder'`` or ``'zip'``.
        :param root: The folder, or the path inside the ZIP, that contains the
            saved attachment files.
        :param archive: The ZIP file path, if the destination is a ZIP.
        """
        destinationId = f'destination-{len(self.__destinations) + 1}'
        self.__destinations.append({
            'destinationId': destinationId,
            'kind': kind,
            'root': _pathString(root) if root is not None else None,
            'archive': archive,
        })
        return destinationId

    def __entryPath(self, entry: AttachmentManifestEntry) -> str:
        """
        Returns a readable path through the embedded message hierarchy.
        """
        parts = []
        current: Optional[AttachmentManifestEntry] = entry
        while current is not None:
            parts.append(current.sourcePath)
            parentId = current.parentOccurrenceId
            current = self.__entries.get(parentId) if parentId else None
        return ' -> '.join(reversed(parts))

    def recordSave(
        self,
        attachment,
        destinationId: str,
        result,
        extractEmbedded: bool = False,
    ) -> None:
        """
        Records the result of saving one attachment to one destination.

        :param attachment: The source attachment instance that was saved.
        :param destinationId: The identifier returned by
            :meth:`beginDestination`.
        :param result: The value returned by the attachment's save method.
        :param extractEmbedded: Whether embedded messages were saved as
            standalone ``.msg`` files.
        """
        entry = self._findEntry(attachment)
        if entry is None:
            return

        destination = next(
            (x for x in self.__destinations if x['destinationId'] == destinationId),
            None,
        )
        if destination is None:
            return

        saveType, value = result
        status = {
            SaveType.NONE: 'none',
            SaveType.FILE: 'file',
            SaveType.FILES: 'files',
            SaveType.FOLDER: 'folder',
            SaveType.FOLDERS: 'folders',
            SaveType.CUSTOM: 'custom',
        }.get(saveType, str(saveType))

        paths: List[str] = []
        if isinstance(value, (list, tuple)):
            paths = [_pathString(x) for x in value if x is not None]
        elif value is not None:
            paths = [_pathString(value)]

        expanded: Optional[bool] = None
        if self.__isEmbedded(attachment):
            expanded = bool(saveType is SaveType.FOLDER)

        record: Dict[str, Any] = {
            'destinationId': destinationId,
            'destinationKind': destination['kind'],
            'status': status,
            'paths': paths,
            'path': paths[0] if len(paths) == 1 else None,
            'relativePaths': [
                _relativePath(x, destination['root']) for x in paths
            ],
            'relativePath': (
                _relativePath(paths[0], destination['root'])
                if len(paths) == 1 else None
            ),
            'filenames': [pathlib.PurePath(x).name for x in paths],
            'filename': (
                pathlib.PurePath(paths[0]).name if len(paths) == 1 else None
            ),
            'expanded': expanded,
            'reason': None,
            'sourceTrail': self.__entryPath(entry),
        }

        if status == 'none':
            if self.__isEmbedded(attachment) and not extractEmbedded:
                record['reason'] = (
                    'Embedded message was skipped; nested attachments '
                    'were not expanded.'
                )
            else:
                record['reason'] = 'No data was saved for this attachment.'

        entry._setSave(record)

        if self.__isEmbedded(attachment) and (
            (status == 'file' and extractEmbedded) or status == 'none'
        ):
            reason = (
                'Embedded message was saved as a .msg file; its attachments '
                'were not expanded in this destination.'
                if status == 'file'
                else record['reason']
            )
            self.__markDescendantsNotExpanded(destinationId, entry.occurrenceId, reason)

    def recordTraversed(
        self,
        attachment,
        destinationId: str,
        reason: str,
    ) -> None:
        """
        Records an embedded-message container that was descended into rather
        than written as a file of its own.
        """
        self.__recordContainerNotWritten(
            attachment, destinationId, 'expanded-as-container', reason
        )

    def recordSkipped(
        self,
        attachment,
        destinationId: str,
        reason: str,
    ) -> None:
        """
        Records an attachment that was intentionally not saved.
        """
        self.__recordContainerNotWritten(attachment, destinationId, 'skipped', reason)

    def __recordContainerNotWritten(
        self,
        attachment,
        destinationId: str,
        status: str,
        reason: str,
    ) -> None:
        entry = self._findEntry(attachment)
        if entry is None:
            return
        destination = next(
            (x for x in self.__destinations if x['destinationId'] == destinationId),
            None,
        )
        if destination is None:
            return
        isContainer = entry.occurrenceId in self.__containers
        if isContainer:
            recordStatus = status
        else:
            recordStatus = 'skipped'
        entry._setSave({
            'destinationId': destinationId,
            'destinationKind': destination['kind'],
            'status': recordStatus,
            'paths': [],
            'path': None,
            'relativePaths': [],
            'relativePath': None,
            'filenames': [],
            'filename': None,
            'expanded': (
                True if status == 'expanded-as-container'
                else False if isContainer else None
            ),
            'reason': reason,
            'sourceTrail': self.__entryPath(entry),
        })
        if isContainer and status != 'expanded-as-container':
            self.__markDescendantsNotExpanded(destinationId, entry.occurrenceId, reason)

    def markAllSkipped(self, destinationId: str, reason: str) -> None:
        """
        Records that none of the attachments were saved to a destination.
        """
        kind = next(
            (x['kind'] for x in self.__destinations
             if x['destinationId'] == destinationId),
            None,
        )
        for entry in self.__entries.values():
            if entry._hasSave(destinationId):
                continue
            isContainer = entry.occurrenceId in self.__containers
            entry._setSave({
                'destinationId': destinationId,
                'destinationKind': kind,
                'status': 'not-expanded' if isContainer else 'skipped',
                'paths': [],
                'path': None,
                'relativePaths': [],
                'relativePath': None,
                'filenames': [],
                'filename': None,
                'expanded': False if isContainer else None,
                'reason': reason,
                'sourceTrail': self.__entryPath(entry),
            })

    def __isEmbeddedEntry(self, entry: AttachmentManifestEntry) -> bool:
        return entry.occurrenceId in self.__containers

    def __isEmbedded(self, attachment) -> bool:
        try:
            if attachment.type in (AttachmentType.MSG, AttachmentType.SIGNED_EMBEDDED):
                return True
        except Exception:
            pass
        try:
            data = attachment.data
            return data is not None and hasattr(data, 'attachments') and hasattr(data, 'prefix')
        except Exception:
            return False

    def __markDescendantsNotExpanded(
        self,
        destinationId: str,
        ancestorOccurrenceId: str,
        reason: str,
    ) -> None:
        destinationKind = next(
            (x['kind'] for x in self.__destinations
             if x['destinationId'] == destinationId),
            None,
        )
        for entry in self.__entries.values():
            if not self.__isDescendant(entry, ancestorOccurrenceId):
                continue
            if entry._hasSave(destinationId):
                continue
            entry._setSave({
                'destinationId': destinationId,
                'destinationKind': destinationKind,
                'status': 'not-expanded',
                'paths': [],
                'path': None,
                'relativePaths': [],
                'relativePath': None,
                'filenames': [],
                'filename': None,
                'expanded': False,
                'reason': reason,
                'sourceTrail': self.__entryPath(entry),
            })

    def __isDescendant(
        self,
        entry: AttachmentManifestEntry,
        ancestorOccurrenceId: str,
    ) -> bool:
        parentId = entry.parentOccurrenceId
        while parentId:
            if parentId == ancestorOccurrenceId:
                return True
            parent = self.__entries.get(parentId)
            parentId = parent.parentOccurrenceId if parent is not None else None
        return False

    def finalizeDestination(self, destinationId: str) -> None:
        """
        Completes the records for a destination.

        Any occurrence that was not reached during saving gets an explicit
        status. In particular, nested attachments that were not expanded are
        never assigned a fabricated path.
        """
        destination = next(
            (x for x in self.__destinations if x['destinationId'] == destinationId),
            None,
        )
        if destination is None:
            return

        for entry in self.__entries.values():
            if entry._hasSave(destinationId):
                continue

            # If an ancestor was explicitly not expanded, the same reason
            # applies to this occurrence.
            parentId = entry.parentOccurrenceId
            inheritedReason = None
            while parentId:
                parent = self.__entries.get(parentId)
                if parent is None:
                    break
                parentSave = parent._getSave(destinationId)
                if parentSave is not None and parentSave['status'] in (
                    'not-expanded', 'skipped', 'none'
                ):
                    inheritedReason = parentSave['reason']
                    break
                parentId = parent.parentOccurrenceId

            entry._setSave({
                'destinationId': destinationId,
                'destinationKind': destination['kind'],
                'status': 'not-expanded' if inheritedReason else 'not-saved',
                'paths': [],
                'path': None,
                'relativePaths': [],
                'relativePath': None,
                'filenames': [],
                'filename': None,
                'expanded': False if self.__isEmbeddedEntry(entry) else None,
                'reason': (
                    inheritedReason
                    or 'No save result was reported for this occurrence '
                       'in this destination.'
                ),
                'sourceTrail': self.__entryPath(entry),
            })

    def discardDestination(self, destinationId: str) -> None:
        """
        Removes all records and the destination itself after a failed save.

        This keeps the manifest from claiming files were written when the
        destination operation aborted partway through.
        """
        for entry in self.__entries.values():
            entry._discardSave(destinationId)
        self.__destinations[:] = [
            x for x in self.__destinations
            if x['destinationId'] != destinationId
        ]

    def toDict(self) -> Dict[str, Any]:
        """
        Returns the manifest as a JSON-serializable dictionary.
        """
        attachments = []
        containers = []
        for entry in self.__entries.values():
            data = entry._toDict()
            if entry.occurrenceId in self.__containers:
                containers.append(data)
            else:
                attachments.append(data)

        return {
            'schema': ATTACHMENT_MANIFEST_SCHEMA,
            'generatedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'message': self.__messageInfo(),
            'destinations': list(self.__destinations),
            'attachments': attachments,
            'embeddedMessages': containers,
        }

    def dumps(self, indent: int = 2) -> str:
        """
        Serializes the manifest to JSON text.
        """
        return json.dumps(self.toDict(), ensure_ascii = False, indent = indent)

    def save(self, path) -> None:
        """
        Writes the manifest as JSON to the given path or file-like object.
        """
        data = self.dumps()
        if hasattr(path, 'write'):
            path.write(data)
        else:
            pathlib.Path(path).write_text(data, encoding = 'utf-8')

    def writeTo(self, openFunc, path, mode: str = 'w') -> None:
        """
        Writes the manifest through a caller-supplied open function.

        This is used by save operations that target a ZIP file, where a normal
        filesystem write would place the manifest outside the archive.
        """
        with openFunc(str(path), mode) as f:
            f.write(self.dumps().encode('utf-8'))

    def findSave(self, occurrenceId: str, destinationId: str) -> Optional[Dict[str, Any]]:
        """
        Returns the save record for an occurrence and destination, if present.
        """
        entry = self.__entries.get(occurrenceId)
        return entry._getSave(destinationId) if entry is not None else None

    def findOccurrenceByPath(self, attachmentPath: str) -> Optional[AttachmentManifestEntry]:
        """
        Finds an occurrence by its internal MSG storage path.
        """
        return self.__entries.get(attachmentPath)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterable[AttachmentManifestEntry]:
        return iter(self.entries)
