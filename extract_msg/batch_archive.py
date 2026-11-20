from __future__ import annotations


__all__ = [
    # Enums.
    'ArchiveFailureStage',
    'ArchivePartStatus',

    # Data classes.
    'FailureInfo',
    'MessageArchiveResult',
    'PartRecord',
    'SinkContext',

    # Classes.
    'BatchArchiveReport',
    'BatchArchiver',
    'MemoryArchiveSink',
]


import enum
import hashlib
import json
import logging
import os
import pathlib

from typing import Any, Callable, Dict, List, Optional, Tuple, Union


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class ArchiveFailureStage(enum.Enum):
    """
    The stage of the archiving process at which a part (or a whole message)
    failed.

    * ``OPEN``: The message file itself could not be opened.
    * ``CONSTRUCT``: An attachment object could not be constructed from the
      message data.
    * ``READ``: The payload (body or attachment data) could not be read from
      the message.
    * ``SAVE``: The payload was read, but a reliable save result was not
      obtained from the sink.
    """

    OPEN = 'open'
    CONSTRUCT = 'construct'
    READ = 'read'
    SAVE = 'save'


class ArchivePartStatus(enum.Enum):
    """
    The archiving status of a single part of a message.
    """

    ACCEPTED = 'accepted'
    FAILED = 'failed'


class FailureInfo:
    """
    Details about why a message or one of its parts did not archive
    successfully.
    """

    __slots__ = ('stage', 'error', 'detail')

    def __init__(self, stage: ArchiveFailureStage, error: str, detail: str):
        self.stage = stage
        self.error = error
        self.detail = detail

    def __eq__(self, other):
        if not isinstance(other, FailureInfo):
            return NotImplemented
        return (self.stage, self.error, self.detail) == (other.stage, other.error, other.detail)

    def __repr__(self):
        return f'FailureInfo(stage = {self.stage!r}, error = {self.error!r}, detail = {self.detail!r})'

    def toDict(self) -> Dict[str, str]:
        return {
            'stage': self.stage.value,
            'error': self.error,
            'detail': self.detail,
        }

    @classmethod
    def fromDict(cls, data: Dict[str, str]) -> FailureInfo:
        return cls(ArchiveFailureStage(data['stage']), data['error'], data['detail'])

    @classmethod
    def fromException(cls, stage: ArchiveFailureStage, exception: BaseException) -> FailureInfo:
        return cls(stage, type(exception).__name__, str(exception))


class PartRecord:
    """
    The recorded archiving result for a single part of a message.

    The part is identified by its *occurrence* inside the message (``body``
    for the message body, ``att/0`` for the first attachment, ``att/1/att/0``
    for the first attachment of the second attachment when that attachment is
    an embedded message, and so on). This means two attachments that happen to
    share a filename are still tracked as independent parts.
    """

    __slots__ = ('partId', 'kind', 'status', 'name', 'failure')

    def __init__(self, partId: str, kind: str, status: ArchivePartStatus,
                 name: Optional[str] = None, failure: Optional[FailureInfo] = None):
        self.partId = partId
        self.kind = kind
        self.status = status
        self.name = name
        self.failure = failure

    def __repr__(self):
        return (f'PartRecord(partId = {self.partId!r}, kind = {self.kind!r}, '
                f'status = {self.status!r}, name = {self.name!r}, failure = {self.failure!r})')

    @property
    def accepted(self) -> bool:
        """
        ``True`` if this part has been reliably saved and registered
        downstream.
        """
        return self.status is ArchivePartStatus.ACCEPTED

    def toDict(self) -> Dict[str, Any]:
        return {
            'partId': self.partId,
            'kind': self.kind,
            'status': self.status.value,
            'name': self.name,
            'failure': self.failure.toDict() if self.failure else None,
        }

    @classmethod
    def fromDict(cls, data: Dict[str, Any]) -> PartRecord:
        failure = data.get('failure')
        return cls(
            data['partId'],
            data['kind'],
            ArchivePartStatus(data['status']),
            data.get('name'),
            FailureInfo.fromDict(failure) if failure else None,
        )


class SinkContext:
    """
    The full source location of a part being registered with the downstream
    sink.

    Combines the batch, the message file, the content version of that file,
    and the occurrence of the part inside the message.
    """

    __slots__ = ('batchId', 'path', 'fingerprint', 'partId', 'kind', 'name')

    def __init__(self, batchId: str, path: str, fingerprint: Optional[str],
                 partId: str, kind: str, name: Optional[str]):
        self.batchId = batchId
        self.path = path
        self.fingerprint = fingerprint
        self.partId = partId
        self.kind = kind
        self.name = name

    def __eq__(self, other):
        if not isinstance(other, SinkContext):
            return NotImplemented
        return all(getattr(self, x) == getattr(other, x) for x in self.__slots__)

    def __repr__(self):
        inner = ', '.join(f'{x} = {getattr(self, x)!r}' for x in self.__slots__)
        return f'SinkContext({inner})'


class MemoryArchiveSink:
    """
    A simple sink that records every registration in memory.

    Mainly useful for tests and as a reference for the sink protocol. The
    registrations are available as a list of ``(SinkContext, payload)`` tuples
    in :attr:`registrations`.
    """

    def __init__(self):
        self.registrations: List[Tuple[SinkContext, Union[str, bytes]]] = []

    def __call__(self, context: SinkContext, payload: Union[str, bytes]) -> None:
        self.registrations.append((context, payload))


class MessageArchiveResult:
    """
    A read-only view of the archiving result for one version of one input
    message file.
    """

    __slots__ = ('path', 'fingerprint', 'parts', 'openError', 'complete')

    def __init__(self, path: str, fingerprint: Optional[str], parts: Tuple[PartRecord, ...],
                 openError: Optional[FailureInfo], complete: bool):
        self.path = path
        self.fingerprint = fingerprint
        self.parts = parts
        self.openError = openError
        self.complete = complete

    def __repr__(self):
        return (f'MessageArchiveResult(path = {self.path!r}, fingerprint = {self.fingerprint!r}, '
                f'parts = {self.parts!r}, openError = {self.openError!r}, complete = {self.complete!r})')

    @property
    def failedParts(self) -> Tuple[PartRecord, ...]:
        """
        The parts of this message that have not been archived successfully.
        """
        return tuple(part for part in self.parts if not part.accepted)


class BatchArchiveReport:
    """
    A read-only view of the results of a batch, in the original input order.
    """

    __slots__ = ('batchId', 'messages')

    def __init__(self, batchId: str, messages: Tuple[MessageArchiveResult, ...]):
        self.batchId = batchId
        self.messages = messages

    def __repr__(self):
        return f'BatchArchiveReport(batchId = {self.batchId!r}, messages = {self.messages!r})'

    @property
    def complete(self) -> bool:
        """
        ``True`` only if every message in the batch was archived completely.
        """
        return all(message.complete for message in self.messages)

    def unopenable(self) -> List[MessageArchiveResult]:
        """
        The messages that could not even be opened.
        """
        return [message for message in self.messages if message.openError is not None]

    def partialFailures(self) -> List[Tuple[MessageArchiveResult, PartRecord]]:
        """
        ``(message, part)`` pairs for every part of an opened message that has
        not been archived successfully.
        """
        return [(message, part)
                for message in self.messages
                for part in message.parts
                if not part.accepted]


class _VersionRecord:
    """
    Internal, mutable state for a single version of a single input file.
    """

    __slots__ = ('fingerprint', 'openError', 'parts', 'finalized')

    def __init__(self, fingerprint: Optional[str]):
        self.fingerprint = fingerprint
        self.openError: Optional[FailureInfo] = None
        self.parts: List[PartRecord] = []
        # Set once a processing pass has run to completion. A version whose
        # processing never finished (e.g. it could not be opened) is never
        # considered complete.
        self.finalized = False

    @property
    def complete(self) -> bool:
        return self.finalized and self.openError is None and all(part.accepted for part in self.parts)

    def findPart(self, partId: str) -> Optional[PartRecord]:
        for part in self.parts:
            if part.partId == partId:
                return part
        return None

    def getOrCreatePart(self, partId: str, kind: str) -> PartRecord:
        part = self.findPart(partId)
        if part is None:
            part = PartRecord(partId, kind, ArchivePartStatus.FAILED)
            self.parts.append(part)
        return part

    def removePart(self, partId: str) -> None:
        self.parts = [part for part in self.parts if part.partId != partId]

    def toResult(self, path: str) -> MessageArchiveResult:
        return MessageArchiveResult(path, self.fingerprint, tuple(self.parts), self.openError, self.complete)

    def toDict(self) -> Dict[str, Any]:
        return {
            'fingerprint': self.fingerprint,
            'finalized': self.finalized,
            'openError': self.openError.toDict() if self.openError else None,
            'parts': [part.toDict() for part in self.parts],
        }

    @classmethod
    def fromDict(cls, data: Dict[str, Any]) -> _VersionRecord:
        record = cls(data['fingerprint'])
        record.finalized = bool(data.get('finalized', False))
        openError = data.get('openError')
        record.openError = FailureInfo.fromDict(openError) if openError else None
        record.parts = [PartRecord.fromDict(part) for part in data.get('parts', [])]
        return record


class _BatchState:
    """
    Internal, mutable state for a whole batch.
    """

    __slots__ = ('batchId', 'inputs', 'messages')

    def __init__(self, batchId: str):
        self.batchId = batchId
        self.inputs: List[str] = []
        # Maps the absolute input path to the list of known versions of the
        # file at that path, oldest first.
        self.messages: Dict[str, List[_VersionRecord]] = {}

    def toDict(self) -> Dict[str, Any]:
        return {
            'batchId': self.batchId,
            'inputs': self.inputs,
            'messages': {
                path: {'versions': [version.toDict() for version in versions]}
                for path, versions in self.messages.items()
            },
        }

    @classmethod
    def fromDict(cls, data: Dict[str, Any]) -> _BatchState:
        state = cls(data['batchId'])
        state.inputs = list(data.get('inputs', []))
        for path, entry in data.get('messages', {}).items():
            state.messages[path] = [_VersionRecord.fromDict(version) for version in entry.get('versions', [])]
        return state


class BatchArchiver:
    """
    Archives batches of MSG files with per-part, resumable results.

    Every message in a batch is processed independently: one message (or one
    part of a message) failing never aborts the rest of the batch, and parts
    of a message that can be archived are kept even if other parts of the same
    message fail. Progress is persisted to :param stateDir: after every part,
    so resubmitting the same batch only retries the parts that have not yet
    been accepted.

    Messages are identified by their content, not just their path: if the
    file at a previously seen path changes, it is treated as a new, fully
    independent version and none of the old version's accepted state is
    carried over.

    :param stateDir: Directory in which batch state files are stored.
    :param sink: Callable receiving ``(SinkContext, payload)`` for every part
        that is ready to be registered downstream. The payload is a ``str``
        for bodies and ``bytes`` for attachments. Raising an exception, or
        returning exactly ``False``, marks the part as failed at the save
        stage; it will be retried on the next submission. Defaults to a
        :class:`MemoryArchiveSink`.
    :param opener: Callable used to open a message file. Defaults to
        :func:`extract_msg.openMsg`.
    :param openKwargs: Extra keyword arguments for the default opener. These
        are merged over ``{'delayAttachments': True}``; attachments are
        delayed by default so that a single broken attachment cannot make the
        whole message unopenable.
    """

    def __init__(self, stateDir, sink: Optional[Callable[[SinkContext, Union[str, bytes]], Any]] = None,
                 opener: Optional[Callable[[str], Any]] = None,
                 openKwargs: Optional[Dict[str, Any]] = None):
        self.__stateDir = pathlib.Path(stateDir)
        self.__stateDir.mkdir(parents = True, exist_ok = True)
        self.__sink = sink if sink is not None else MemoryArchiveSink()
        self.__opener = opener
        self.__openKwargs = {'delayAttachments': True}
        if openKwargs:
            self.__openKwargs.update(openKwargs)

    @property
    def sink(self):
        """
        The sink that parts are registered with.
        """
        return self.__sink

    def submit(self, batchId: str, paths) -> BatchArchiveReport:
        """
        Submits (or resumes) a batch of MSG files.

        Paths that have been seen before in this batch are only reprocessed
        where needed: parts that were already accepted are not registered
        again, while failed parts and unopenable messages are retried. New
        paths are appended to the batch.

        :returns: A report of the current state of the batch.
        """
        state = self.__loadState(batchId)
        for path in paths:
            absPath = self.__normalizePath(path)
            if absPath not in state.inputs:
                state.inputs.append(absPath)
        self.__persist(state)

        for absPath in state.inputs:
            self.__processPath(state, absPath)

        return self.__makeReport(state)

    def report(self, batchId: str) -> BatchArchiveReport:
        """
        Returns the current state of a previously submitted batch without
        processing anything.

        :raises KeyError: The batch is unknown.
        """
        return self.__makeReport(self.__loadExistingState(batchId))

    def versionHistory(self, batchId: str, path) -> List[MessageArchiveResult]:
        """
        Returns the results for every known version of the file at
        :param path:, oldest first. A file that was replaced at the same path
        produces one entry per distinct version.

        :raises KeyError: The batch is unknown.
        """
        state = self.__loadExistingState(batchId)
        absPath = self.__normalizePath(path)
        return [version.toResult(absPath) for version in state.messages.get(absPath, [])]

    def __normalizePath(self, path) -> str:
        return os.path.abspath(os.fspath(path))

    def __stateFile(self, batchId: str) -> pathlib.Path:
        if not batchId or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.' for c in batchId):
            raise ValueError(f'Invalid batch id: {batchId!r}')
        return self.__stateDir / f'{batchId}.json'

    def __loadState(self, batchId: str) -> _BatchState:
        stateFile = self.__stateFile(batchId)
        if not stateFile.exists():
            return _BatchState(batchId)
        with open(stateFile, 'r', encoding = 'utf-8') as f:
            return _BatchState.fromDict(json.load(f))

    def __loadExistingState(self, batchId: str) -> _BatchState:
        if not self.__stateFile(batchId).exists():
            raise KeyError(f'Unknown batch: {batchId!r}')
        return self.__loadState(batchId)

    def __persist(self, state: _BatchState) -> None:
        stateFile = self.__stateFile(state.batchId)
        tmpFile = stateFile.with_suffix('.json.tmp')
        with open(tmpFile, 'w', encoding = 'utf-8') as f:
            json.dump(state.toDict(), f, indent = 2)
        os.replace(tmpFile, stateFile)

    def __open(self, path: str):
        if self.__opener is not None:
            return self.__opener(path)
        from .open_msg import openMsg
        return openMsg(path, **self.__openKwargs)

    def __makeReport(self, state: _BatchState) -> BatchArchiveReport:
        messages = []
        for absPath in state.inputs:
            versions = state.messages.get(absPath)
            if versions:
                messages.append(versions[-1].toResult(absPath))
            else:
                # Input was registered but never processed (should not happen,
                # but keep the report complete and honest if it does).
                messages.append(MessageArchiveResult(absPath, None, tuple(), None, False))
        return BatchArchiveReport(state.batchId, tuple(messages))

    def __processPath(self, state: _BatchState, absPath: str) -> None:
        """
        Brings the current version of a single input file as far towards
        complete as possible, persisting after every transition. Never raises
        for message-level problems; one bad file must not break the batch.
        """
        versions = state.messages.setdefault(absPath, [])

        # Identify the current content of the file.
        fingerprint = None
        readError: Optional[BaseException] = None
        try:
            with open(absPath, 'rb') as f:
                fingerprint = hashlib.sha256(f.read()).hexdigest()
        except Exception as e:
            readError = e

        record = None
        for version in versions:
            if version.fingerprint == fingerprint:
                record = version
                break
        if record is None:
            # Content never seen before at this path: a brand new, fully
            # independent version. No state is carried over from older
            # versions.
            record = _VersionRecord(fingerprint)
            versions.append(record)

        if fingerprint is None:
            record.openError = FailureInfo.fromException(ArchiveFailureStage.OPEN, readError)
            self.__persist(state)
            return

        if record.complete:
            # Everything for this version has already been accepted; nothing
            # may be registered again.
            return

        try:
            msg = self.__open(absPath)
        except Exception as e:
            record.openError = FailureInfo.fromException(ArchiveFailureStage.OPEN, e)
            self.__persist(state)
            return

        record.openError = None
        self.__persist(state)

        try:
            try:
                self.__processMessageParts(state, record, absPath, msg, '')
            except Exception as e:
                # Defensive: per-part processing is designed to catch its own
                # errors, so this should be unreachable. If it does happen,
                # keep the message incomplete and queryable instead of
                # breaking the batch.
                logger.exception(f'Unexpected error while archiving "{absPath}".')
                self.__recordFailure(state, record, absPath, '__message__', 'message',
                                     ArchiveFailureStage.CONSTRUCT, e)
                return
            record.finalized = True
            self.__persist(state)
        finally:
            try:
                msg.close()
            except Exception:
                logger.exception(f'Error while closing "{absPath}".')

    def __processMessageParts(self, state: _BatchState, record: _VersionRecord,
                              absPath: str, msg, prefix: str) -> None:
        """
        Processes the body and attachments (recursively, for embedded
        messages) of a single message instance, skipping parts that have
        already been accepted.
        """
        # The body. Only message-like classes have one.
        if hasattr(type(msg), 'body'):
            self.__archiveLeaf(state, record, absPath, prefix + 'body', 'body',
                               lambda: msg.body, None)

        # Enumerate the attachment directories ourselves so that one broken
        # attachment cannot take the rest of the message down with it.
        try:
            attachmentDirs = []
            for dir_ in msg.listDir(False, True, False):
                if dir_[0].startswith('__attach') and dir_[0] not in attachmentDirs:
                    attachmentDirs.append(dir_[0])
        except Exception as e:
            self.__recordFailure(state, record, absPath, prefix + 'att/?', 'attachment',
                                 ArchiveFailureStage.CONSTRUCT, e)
            return

        for index, attachmentDir in enumerate(attachmentDirs):
            partId = f'{prefix}att/{index}'
            existing = record.findPart(partId)
            if existing is not None and existing.accepted:
                continue

            try:
                attachment = msg.initAttachmentFunc(msg, attachmentDir)
            except Exception as e:
                self.__recordFailure(state, record, absPath, partId, 'attachment',
                                     ArchiveFailureStage.CONSTRUCT, e)
                continue

            if self.__isEmbeddedMessage(attachment):
                # Embedded messages are containers, not leaf parts. Any stale
                # leaf record (e.g. from a previous construction failure) is
                # superseded by the records of the embedded message's own
                # parts.
                record.removePart(partId)
                try:
                    embeddedMsg = attachment.data
                except Exception as e:
                    self.__recordFailure(state, record, absPath, partId, 'attachment',
                                         ArchiveFailureStage.CONSTRUCT, e)
                    continue
                self.__processMessageParts(state, record, absPath, embeddedMsg, partId + '/')
            else:
                self.__archiveLeaf(state, record, absPath, partId, 'attachment',
                                   lambda: attachment.data, self.__attachmentName(attachment))

    @staticmethod
    def __isEmbeddedMessage(attachment) -> bool:
        from .enums import AttachmentType
        try:
            return attachment.type is AttachmentType.MSG
        except Exception:
            return False

    @staticmethod
    def __attachmentName(attachment) -> Optional[str]:
        try:
            return attachment.name
        except Exception:
            return None

    def __archiveLeaf(self, state: _BatchState, record: _VersionRecord, absPath: str,
                      partId: str, kind: str, readFn, name: Optional[str]) -> None:
        """
        Reads a single leaf part and, if that succeeds, registers it with the
        sink. The part is only marked accepted once the sink has returned a
        reliable result.
        """
        part = record.findPart(partId)
        if part is not None and part.accepted:
            return

        try:
            payload = readFn()
        except Exception as e:
            self.__recordFailure(state, record, absPath, partId, kind,
                                 ArchiveFailureStage.READ, e, name)
            return

        if payload is None:
            self.__recordFailure(state, record, absPath, partId, kind,
                                 ArchiveFailureStage.READ,
                                 ValueError(f'No data available for part {partId}.'), name)
            return

        context = SinkContext(state.batchId, absPath, record.fingerprint, partId, kind, name)
        try:
            result = self.__sink(context, payload)
            if result is False:
                raise RuntimeError('Sink did not return a reliable save result.')
        except Exception as e:
            self.__recordFailure(state, record, absPath, partId, kind,
                                 ArchiveFailureStage.SAVE, e, name)
            return

        part = record.getOrCreatePart(partId, kind)
        part.status = ArchivePartStatus.ACCEPTED
        part.name = name
        part.failure = None
        self.__persist(state)

    def __recordFailure(self, state: _BatchState, record: _VersionRecord, absPath: str,
                        partId: str, kind: str, stage: ArchiveFailureStage,
                        exception: BaseException, name: Optional[str] = None) -> None:
        part = record.getOrCreatePart(partId, kind)
        part.status = ArchivePartStatus.FAILED
        if name is not None:
            part.name = name
        part.failure = FailureInfo.fromException(stage, exception)
        self.__persist(state)
