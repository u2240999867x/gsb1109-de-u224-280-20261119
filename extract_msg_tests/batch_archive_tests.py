__all__ = [
    'BatchArchiveTests',
]


import json
import struct
import tempfile
import unittest

from pathlib import Path
from unittest import mock

import extract_msg

from extract_msg.attachments.attachment import Attachment
from extract_msg.batch_archive import (
        ArchiveFailureStage, ArchivePartStatus, BatchArchiver,
        MemoryArchiveSink,
    )
from extract_msg.ole_writer import OleWriter


def _fixedProp(propertyId: int, propertyType: int, value: bytes) -> bytes:
    """
    Creates a single 16 byte fixed-length property entry for a properties
    stream. ``value`` must be exactly 8 bytes.
    """
    return struct.pack('<2HI', propertyType, propertyId, 0) + value


def _attachmentPropsStream(method: int) -> bytes:
    """
    Creates the ``__properties_version1.0`` stream for an attachment, setting
    only the attachment method (1 = data, 5 = embedded MSG).
    """
    return b'\x00' * 8 + _fixedProp(0x3705, 0x0003, struct.pack('<I', method) + b'\x00' * 4)


def _messagePropsStream(embedded: bool, attachmentCount: int) -> bytes:
    """
    Creates the ``__properties_version1.0`` stream for a message.
    """
    data = struct.pack('<8x4I', 0, attachmentCount, 0, attachmentCount)
    if not embedded:
        data += b'\x00' * 8
    return data


def _addMessageEntries(writer: OleWriter, prefix: str, spec: dict, embedded: bool) -> None:
    """
    Adds the entries for a message to the writer. ``spec`` is a dict with an
    optional ``body`` (str) and an optional ``attachments`` list. Each
    attachment is a dict of the form::

        {'type': 'data', 'name': str, 'data': bytes}
        {'type': 'msg', 'name': str, 'spec': <message spec dict>}
        {'type': 'broken'}  # attachment with no data, fails to construct
    """
    attachments = spec.get('attachments', [])
    writer.addEntry(prefix + '__properties_version1.0', _messagePropsStream(embedded, len(attachments)))
    writer.addEntry(prefix + '__substg1.0_001A001F', 'IPM.Note'.encode('utf-16-le'))
    if spec.get('body') is not None:
        writer.addEntry(prefix + '__substg1.0_1000001F', spec['body'].encode('utf-16-le'))
    if not embedded:
        # The named property streams must exist, but may be empty.
        writer.addEntry(prefix + '__nameid_version1.0/__substg1.0_00020102', b'')
        writer.addEntry(prefix + '__nameid_version1.0/__substg1.0_00030102', b'')
        writer.addEntry(prefix + '__nameid_version1.0/__substg1.0_00040102', b'')
    for index, attachment in enumerate(attachments):
        attachmentDir = f'{prefix}__attach_version1.0_#{index:08X}/'
        if attachment['type'] == 'data':
            writer.addEntry(attachmentDir + '__properties_version1.0', _attachmentPropsStream(1))
            writer.addEntry(attachmentDir + '__substg1.0_37010102', attachment['data'])
            writer.addEntry(attachmentDir + '__substg1.0_3707001F', attachment['name'].encode('utf-16-le'))
        elif attachment['type'] == 'msg':
            writer.addEntry(attachmentDir + '__properties_version1.0', _attachmentPropsStream(5))
            writer.addEntry(attachmentDir + '__substg1.0_3707001F', attachment['name'].encode('utf-16-le'))
            _addMessageEntries(writer, attachmentDir + '__substg1.0_3701000D/', attachment['spec'], True)
        elif attachment['type'] == 'broken':
            # Claims to be a data attachment but has no data stream, which
            # makes the attachment object fail to construct.
            writer.addEntry(attachmentDir + '__properties_version1.0', _attachmentPropsStream(1))
        else:
            raise ValueError(f'Unknown attachment type: {attachment["type"]!r}')


def _writeMsg(path: Path, spec: dict) -> None:
    writer = OleWriter()
    _addMessageEntries(writer, '', spec, False)
    writer.write(str(path))


def _msg2Spec(body: str = 'Second message body.', attachmentData: bytes = b'outer data',
              innerData: bytes = b'inner data') -> dict:
    """
    The spec for the second message of the batch: a readable body, a normal
    attachment, and an embedded (forwarded) message that contains an
    attachment with the *same* filename.
    """
    return {
        'body': body,
        'attachments': [
            {'type': 'data', 'name': 'shared.txt', 'data': attachmentData},
            {
                'type': 'msg',
                'name': 'forwarded.msg',
                'spec': {
                    'body': 'Forwarded message body.',
                    'attachments': [
                        {'type': 'data', 'name': 'shared.txt', 'data': innerData},
                    ],
                },
            },
        ],
    }


class BatchArchiveTests(unittest.TestCase):
    def setUp(self):
        self.__tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.__tmp.cleanup)
        self.root = Path(self.__tmp.name)
        self.stateDir = self.root / 'state'
        self.inputDir = self.root / 'inputs'
        self.inputDir.mkdir()

        # Message 1: completely unopenable.
        self.path1 = self.inputDir / 'broken.msg'
        with open(self.path1, 'wb') as f:
            f.write(b'this is not an OLE file at all')

        # Message 2: body and a normal attachment are archivable, but the
        # embedded forwarded message has an attachment with the same name.
        self.path2 = self.inputDir / 'second.msg'
        _writeMsg(self.path2, _msg2Spec())

        # Message 3: fully archivable.
        self.path3 = self.inputDir / 'third.msg'
        _writeMsg(self.path3, {
            'body': 'Third message body.',
            'attachments': [
                {'type': 'data', 'name': 'third.txt', 'data': b'third data'},
            ],
        })

        self.paths = [self.path1, self.path2, self.path3]

    def _makeArchiver(self, sink = None) -> BatchArchiver:
        return BatchArchiver(self.stateDir, sink = sink)

    @staticmethod
    def _flakyInnerData():
        """
        A patch that makes reading the data of an attachment inside an
        embedded message raise, simulating a transient read failure.
        """
        realData = Attachment.data.fget

        def flakyData(self):
            if '3701000D' in str(self.msg.prefix):
                raise IOError('transient read failure')
            return realData(self)

        return mock.patch.object(Attachment, 'data', property(flakyData))

    def _submitFirstRun(self, archiver):
        with self._flakyInnerData():
            return archiver.submit('batch-1', self.paths)

    def testPartialFailureThenResume(self):
        sink = MemoryArchiveSink()
        archiver = self._makeArchiver(sink)

        # First run: message 1 does not open, the inner attachment of message
        # 2 fails to read, everything else is archived.
        report = self._submitFirstRun(archiver)

        self.assertFalse(report.complete)

        # The unopenable message is queryable on its own.
        unopenable = report.unopenable()
        self.assertEqual(len(unopenable), 1)
        self.assertEqual(unopenable[0].path, str(self.path1.absolute()))
        self.assertEqual(unopenable[0].openError.stage, ArchiveFailureStage.OPEN)

        # Message 2 is partially archived: body, normal attachment and the
        # forwarded body are accepted, the inner attachment is not.
        msg2 = report.messages[1]
        self.assertFalse(msg2.complete)
        self.assertIsNone(msg2.openError)
        self.assertEqual([p.partId for p in msg2.parts],
                         ['body', 'att/0', 'att/1/body', 'att/1/att/0'])
        self.assertEqual([p.status for p in msg2.parts],
                         [ArchivePartStatus.ACCEPTED, ArchivePartStatus.ACCEPTED,
                          ArchivePartStatus.ACCEPTED, ArchivePartStatus.FAILED])
        innerFailure = msg2.parts[3]
        self.assertEqual(innerFailure.failure.stage, ArchiveFailureStage.READ)
        # The same-named inner attachment is tracked by occurrence, not name.
        self.assertEqual(innerFailure.name, 'shared.txt')
        self.assertEqual(msg2.parts[1].name, 'shared.txt')

        # Message 3 is fully archived.
        msg3 = report.messages[2]
        self.assertTrue(msg3.complete)

        # Partial failures are queryable separately from unopenable messages.
        partials = report.partialFailures()
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0][1].partId, 'att/1/att/0')

        # Exactly the accepted parts were registered downstream.
        registered = {(c.path, c.partId) for c, _ in sink.registrations}
        path2, path3 = str(self.path2.absolute()), str(self.path3.absolute())
        self.assertEqual(registered, {
            (path2, 'body'), (path2, 'att/0'), (path2, 'att/1/body'),
            (path3, 'body'), (path3, 'att/0'),
        })

        # Resume the same batch: the inner attachment is readable again, the
        # first message still does not open.
        report = archiver.submit('batch-1', self.paths)

        self.assertFalse(report.complete)
        self.assertEqual(len(report.unopenable()), 1)
        self.assertEqual(report.partialFailures(), [])

        msg2 = report.messages[1]
        self.assertTrue(msg2.complete)
        # The inner attachment was filled in at its original occurrence.
        self.assertEqual([p.partId for p in msg2.parts],
                         ['body', 'att/0', 'att/1/body', 'att/1/att/0'])
        self.assertEqual(msg2.parts[3].status, ArchivePartStatus.ACCEPTED)

        # Only the previously failed inner attachment was registered again;
        # the accepted parts and message 3 were not re-emitted.
        newRegistrations = sink.registrations[5:]
        self.assertEqual(len(newRegistrations), 1)
        context, payload = newRegistrations[0]
        self.assertEqual((context.path, context.partId), (path2, 'att/1/att/0'))
        self.assertEqual(payload, b'inner data')

    def testReplacedFileProducesIndependentVersion(self):
        sink = MemoryArchiveSink()
        archiver = self._makeArchiver(sink)
        self._submitFirstRun(archiver)
        archiver.submit('batch-1', self.paths)

        # Sanity: everything except message 1 is now complete.
        self.assertTrue(archiver.report('batch-1').messages[1].complete)
        oldFingerprint = archiver.report('batch-1').messages[1].fingerprint
        registrationCount = len(sink.registrations)

        # Replace message 2 at the same path with changed content.
        _writeMsg(self.path2, _msg2Spec(body = 'Replacement body.', attachmentData = b'new outer'))
        report = archiver.submit('batch-1', self.paths)

        msg2 = report.messages[1]
        self.assertTrue(msg2.complete)
        self.assertNotEqual(msg2.fingerprint, oldFingerprint)

        # The new version produced its own, independent results: every part
        # of the new content was registered, nothing was inherited from the
        # old version.
        newRegistrations = sink.registrations[registrationCount:]
        path2 = str(self.path2.absolute())
        self.assertEqual({(c.path, c.partId) for c, _ in newRegistrations},
                         {(path2, 'body'), (path2, 'att/0'),
                          (path2, 'att/1/body'), (path2, 'att/1/att/0')})
        payloads = {c.partId: p for c, p in newRegistrations}
        self.assertEqual(payloads['body'], 'Replacement body.')
        self.assertEqual(payloads['att/0'], b'new outer')

        # Message 3 was not re-emitted while the replacement was processed.
        path3 = str(self.path3.absolute())
        self.assertFalse(any(c.path == path3 for c, _ in newRegistrations))

        # Both versions remain independently queryable.
        history = archiver.versionHistory('batch-1', self.path2)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].fingerprint, oldFingerprint)
        self.assertEqual(history[1].fingerprint, msg2.fingerprint)
        self.assertTrue(all(version.complete for version in history))

    def testSaveFailureIsRecordedAndRetried(self):
        # A sink that refuses to reliably save the normal attachment of
        # message 2 on the first run.
        state = {'refuse': True}

        def sink(context, payload):
            if state['refuse'] and context.partId == 'att/0' and context.path == str(self.path2.absolute()):
                return False

        archiver = self._makeArchiver(sink)
        report = self._submitFirstRun(archiver)

        msg2 = report.messages[1]
        byId = {p.partId: p for p in msg2.parts}
        # The read succeeded, but without a reliable save result the part is
        # not accepted and not skipped.
        self.assertEqual(byId['att/0'].status, ArchivePartStatus.FAILED)
        self.assertEqual(byId['att/0'].failure.stage, ArchiveFailureStage.SAVE)
        self.assertFalse(msg2.complete)

        state['refuse'] = False
        report = archiver.submit('batch-1', self.paths)
        msg2 = report.messages[1]
        byId = {p.partId: p for p in msg2.parts}
        self.assertEqual(byId['att/0'].status, ArchivePartStatus.ACCEPTED)
        self.assertTrue(msg2.complete)

    def testSinkExceptionIsRecordedAsSaveFailure(self):
        def sink(context, payload):
            if context.partId == 'att/0' and context.path == str(self.path3.absolute()):
                raise OSError('disk full')

        archiver = self._makeArchiver(sink)
        report = self._submitFirstRun(archiver)

        msg3 = report.messages[2]
        byId = {p.partId: p for p in msg3.parts}
        self.assertEqual(byId['att/0'].status, ArchivePartStatus.FAILED)
        self.assertEqual(byId['att/0'].failure.stage, ArchiveFailureStage.SAVE)
        self.assertEqual(byId['att/0'].failure.error, 'OSError')
        self.assertIn('disk full', byId['att/0'].failure.detail)
        # The body of the same message was still archived.
        self.assertEqual(byId['body'].status, ArchivePartStatus.ACCEPTED)
        self.assertFalse(msg3.complete)

    def testConstructFailureIsRecorded(self):
        path = self.inputDir / 'construct.msg'
        _writeMsg(path, {
            'body': 'Message with a broken attachment.',
            'attachments': [{'type': 'broken'}],
        })

        sink = MemoryArchiveSink()
        archiver = self._makeArchiver(sink)
        report = archiver.submit('batch-construct', [path])

        message = report.messages[0]
        self.assertFalse(message.complete)
        self.assertIsNone(message.openError)
        byId = {p.partId: p for p in message.parts}
        self.assertEqual(byId['body'].status, ArchivePartStatus.ACCEPTED)
        self.assertEqual(byId['att/0'].status, ArchivePartStatus.FAILED)
        self.assertEqual(byId['att/0'].failure.stage, ArchiveFailureStage.CONSTRUCT)
        self.assertEqual(report.partialFailures()[0][1].partId, 'att/0')

    def testStateSurvivesNewArchiverInstance(self):
        sink = MemoryArchiveSink()
        self._submitFirstRun(self._makeArchiver(sink))

        # A fresh archiver over the same state directory resumes exactly
        # where the previous one stopped.
        newSink = MemoryArchiveSink()
        archiver = self._makeArchiver(newSink)
        report = archiver.submit('batch-1', self.paths)

        self.assertTrue(report.messages[1].complete)
        # Only the previously failed inner attachment was registered.
        self.assertEqual([(c.partId) for c, _ in newSink.registrations], ['att/1/att/0'])

        # The persisted state file is valid JSON with the expected shape.
        with open(self.stateDir / 'batch-1.json', 'r', encoding = 'utf-8') as f:
            state = json.load(f)
        self.assertEqual(state['batchId'], 'batch-1')
        self.assertEqual(len(state['inputs']), 3)

    def testReportWithoutProcessing(self):
        archiver = self._makeArchiver()
        self._submitFirstRun(archiver)

        report = self._makeArchiver().report('batch-1')
        self.assertEqual(len(report.messages), 3)
        self.assertEqual(len(report.unopenable()), 1)
        self.assertEqual(len(report.partialFailures()), 1)

        with self.assertRaises(KeyError):
            archiver.report('no-such-batch')
