import io
import json
import os
import tempfile
from unittest import TestCase

import json_stream
from json_stream import (
    Checkpoint,
    EventKind,
    IncompleteRootEvent,
    InvalidRootError,
    RootEvent,
    SourceChangedError,
    UnsupportedSourceError,
    resume_from,
    visit_many_checkpointed,
)


ROOTS = [
    b'{"device":"d1","seq":1,"meta":{"site":"north","level":3}}',
    b'{"device":"d2","seq":2,"meta":{"site":"north","level":4}}',
    b'42\n',
    b'[{"x":1},{"x":2}]',
    b'"hello"\n',
    b'true',
    b'{"device":"d3","seq":7,"meta":{"site":"south","level":1}}',
]

EIGHTH = b'{"device":"d4","seq":8,"meta":{"site":"south","level":2},"payload":[10,20,30]}'


class ResumableTestCase(TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)

    @property
    def directory(self):
        return self._directory.name

    def make_file(self, data, name='capture.json'):
        path = os.path.join(self.directory, name)
        with open(path, 'wb') as handle:
            handle.write(data)
        return path

    def read_file(self, path):
        with open(path, 'rb') as handle:
            return handle.read()


class TestCheckpoint(ResumableTestCase):
    def test_roundtrip_and_aliases(self):
        checkpoint = Checkpoint(confirmed_roots=7, position=202, digest='a' * 64)
        self.assertEqual(checkpoint.root_count, 7)
        self.assertEqual(checkpoint.next_root_index, 7)
        self.assertEqual(checkpoint.last_root_index, 6)
        self.assertEqual(checkpoint.last_root_ordinal, 7)
        self.assertEqual(checkpoint.byte_position, 202)
        self.assertEqual(checkpoint.offset, 202)
        self.assertEqual(Checkpoint.from_json(checkpoint.to_json()), checkpoint)
        self.assertEqual(Checkpoint.from_dict(checkpoint.to_dict()), checkpoint)
        self.assertEqual(checkpoint.to_dict()['position'], 202)

    def test_arbitrary_digest_validation(self):
        with self.assertRaises(ValueError):
            Checkpoint(confirmed_roots=1, position=3, digest='nope')
        with self.assertRaises(ValueError):
            Checkpoint(confirmed_roots=0, position=3, digest='a' * 64)

    def test_save_and_load(self):
        path = os.path.join(self.directory, 'ckpt.json')
        checkpoint = Checkpoint(confirmed_roots=2, position=114, digest='b' * 64)
        checkpoint.save(path)
        self.assertEqual(Checkpoint.load(path), checkpoint)

    def test_verify_detects_same_length_prefix_change(self):
        path = self.make_file(b'{"a":1}{"b":2}')
        checkpoint = Checkpoint(confirmed_roots=1, position=7, digest=self.digest_of(path, 7))
        checkpoint.verify(path)
        with open(path, 'r+b') as handle:
            handle.seek(1)
            handle.write(b'A')
        self.assertFalse(checkpoint.is_valid_for(path))
        with self.assertRaises(SourceChangedError) as caught:
            checkpoint.verify(path)
        self.assertTrue(caught.exception.requires_reconfirmation)
        self.assertEqual(caught.exception.reason, 'prefix-changed')

    def test_verify_source_identity(self):
        path = self.make_file(b'{"a":1}')
        checkpoint = Checkpoint(
            confirmed_roots=1,
            position=7,
            digest=self.digest_of(path, 7),
            source_identity='expected',
        )
        with self.assertRaises(SourceChangedError) as caught:
            checkpoint.verify(path, verify='source', source_identity='other')
        self.assertEqual(caught.exception.reason, 'source-identity-changed')
        checkpoint.verify(path, verify='source', source_identity='expected')

    @staticmethod
    def digest_of(path, count):
        import hashlib

        with open(path, 'rb') as handle:
            return hashlib.sha256(handle.read(count)).hexdigest()


class TestVisitManyCheckpoint(ResumableTestCase):
    def test_existing_visit_many_still_works(self):
        stream = io.StringIO('{"a":1}{"b":2}')
        visited = []
        for _ in json_stream.visit_many(stream, lambda value, path: visited.append((value, path))):
            pass
        self.assertEqual(visited, [(1, ('a',)), (2, ('b',))])

    def test_each_root_is_confirmed_at_its_own_end(self):
        data = b''.join(ROOTS)
        path = self.make_file(data)
        events = list(json_stream.visit_many(path, lambda value, path: None))
        self.assertEqual([event.kind for event in events], [EventKind.ROOT] * 7)
        self.assertEqual([event.root_index for event in events], list(range(7)))
        expected_ends = []
        offset = 0
        for root in ROOTS:
            offset += len(root)
            stripped = root.rstrip()
            # container roots self-delimit; scalar roots end at their own
            # syntax, so trailing whitespace belongs to the next root
            if stripped[:1] in (b'{', b'['):
                end = offset
            else:
                end = offset - (len(root) - len(stripped))
            expected_ends.append(end)
        self.assertEqual([event.end for event in events], expected_ends)
        self.assertEqual(events[-1].checkpoint.confirmed_roots, 7)
        self.assertEqual(events[-1].checkpoint.position, len(data))

    def test_fields_report_root_index_and_pending_state(self):
        data = b''.join(ROOTS)
        path = self.make_file(data)
        observed = []

        def visitor(value, p, event):
            # at delivery time the owning root has not been verified yet
            observed.append((event.confirmed, event.pending, event.status))
            fields.append(event)

        fields = []
        list(json_stream.visit_many(path, visitor))

        self.assertTrue(observed)
        self.assertTrue(all(state == (False, True, 'pending') for state in observed))

        by_root = {}
        for event in fields:
            by_root.setdefault(event.root_index, []).append(event)
        self.assertEqual(sorted(by_root), list(range(7)))
        for event in fields:
            self.assertEqual(event.kind, EventKind.FIELD)
            # after iteration the shared status reflects the confirmation
            self.assertTrue(event.confirmed)
            self.assertFalse(event.pending)
            self.assertEqual(event.status, 'confirmed')
        sites = [event.path for event in fields if event.value == 'north']
        self.assertIn(('meta', 'site'), sites)
        self.assertEqual(fields[0].root_ordinal, 1)

    def test_short_fields_are_reported_once_as_incomplete(self):
        data = b''.join(ROOTS) + EIGHTH[:len(EIGHTH) // 2]
        path = self.make_file(data)
        seen, events = [], []
        visit = json_stream.visit_many(path, lambda value, p: seen.append(value))
        for event in visit:
            events.append(event)
        self.assertEqual(events[-1].kind, EventKind.INCOMPLETE)
        incomplete = events[-1]
        self.assertIsInstance(incomplete, IncompleteRootEvent)
        self.assertEqual(incomplete.root_index, 7)
        self.assertEqual(incomplete.position, sum(len(r) for r in ROOTS))
        self.assertEqual(incomplete.browsed, len(data))
        self.assertNotEqual(incomplete.position, incomplete.browsed)
        self.assertTrue(incomplete.pending)
        # values may have been seen, but they do not leak into the credential
        self.assertEqual(visit.checkpoint.confirmed_roots, 7)

    def test_append_resume_renumbers_without_reentering_confirmed_roots(self):
        first = b''.join(ROOTS) + EIGHTH[:len(EIGHTH) // 2]
        path = self.make_file(first)
        first_seen = []
        visit = json_stream.visit_many(path, lambda value, p: first_seen.append(value))
        for _ in visit:
            pass
        credential = visit.checkpoint
        self.assertEqual(credential.confirmed_roots, 7)
        confirmed_before = len(first_seen)

        with open(path, 'ab') as handle:
            handle.write(EIGHTH[len(EIGHTH) // 2:])
            handle.write(b'{"device":"d5","seq":9}')

        second_seen = []
        resumed = json_stream.visit_many(
            path, lambda value, p: second_seen.append(value), checkpoint=credential
        )
        events = list(resumed)
        self.assertEqual([event.root_index for event in events], [7, 8])
        self.assertEqual([event.kind for event in events], [EventKind.ROOT] * 2)
        self.assertEqual(resumed.checkpoint.confirmed_roots, 9)
        self.assertEqual(resumed.checkpoint.position, os.path.getsize(path))
        # confirmed roots are not re-emitted, but new fields are
        self.assertNotIn('north', second_seen)
        self.assertEqual(second_seen[-1], 9)
        self.assertGreater(confirmed_before, 0)

    def test_same_length_replacement_cannot_reuse_credential(self):
        path = self.make_file(b''.join(ROOTS))
        visit = json_stream.visit_many(path, lambda value, p: None)
        for _ in visit:
            pass
        credential = visit.checkpoint
        original = self.read_file(path)

        replacement = bytearray(original)
        replacement[1] = ord('X')
        self.assertEqual(len(replacement), len(original))
        self.make_file(bytes(replacement))

        seen = []
        with self.assertRaises(SourceChangedError) as caught:
            list(
                json_stream.visit_many(
                    path, lambda value, p: seen.append(value), checkpoint=credential
                )
            )
        self.assertTrue(caught.exception.requires_reconfirmation)
        self.assertEqual(seen, [])

    def test_reconfirm_restarts_root_numbering(self):
        path = self.make_file(b''.join(ROOTS))
        visit = json_stream.visit_many(path, lambda value, p: None)
        for _ in visit:
            pass
        credential = visit.checkpoint

        original = bytearray(self.read_file(path))
        original[1] = ord('X')
        self.make_file(bytes(original))

        seen = []
        resumed = json_stream.visit_many(
            path,
            lambda value, p: seen.append(value),
            checkpoint=credential,
            reconfirm=True,
        )
        # the changed prefix is no longer valid JSON; it is reported, not skipped
        with self.assertRaises(InvalidRootError) as caught:
            list(resumed)
        self.assertEqual(caught.exception.root_index, 0)
        self.assertEqual(caught.exception.offset, 0)
        self.assertEqual(seen, [])

    def test_reconfirm_after_replacement_with_valid_new_stream(self):
        path = self.make_file(b'{"old":1}{"old":2}')
        visit = json_stream.visit_many(path, lambda value, p: None)
        for _ in visit:
            pass
        credential = visit.checkpoint
        self.make_file(b'{"new":1}{"new":2}{"new":3}')

        events = list(
            json_stream.visit_many(
                path, lambda value, p: None, checkpoint=credential, reconfirm=True
            )
        )
        self.assertEqual([event.root_index for event in events], [0, 1, 2])

    def test_truncated_source_is_reported_as_changed(self):
        path = self.make_file(b'{"a":1}{"b":2}')
        visit = json_stream.visit_many(path, lambda value, p: None)
        for _ in visit:
            pass
        credential = visit.checkpoint
        self.make_file(b'{"a":1}')
        with self.assertRaises(SourceChangedError) as caught:
            list(json_stream.visit_many(path, lambda value, p: None, checkpoint=credential))
        self.assertEqual(caught.exception.reason, 'truncated')

    def test_invalid_new_bytes_are_not_skipped(self):
        path = self.make_file(b'{"a":1}')
        visit = json_stream.visit_many(path, lambda value, p: None)
        for _ in visit:
            pass
        credential = visit.checkpoint
        with open(path, 'ab') as handle:
            handle.write(b'####')
        with self.assertRaises(InvalidRootError) as caught:
            list(json_stream.visit_many(path, lambda value, p: None, checkpoint=credential))
        self.assertEqual(caught.exception.root_index, 1)
        self.assertEqual(caught.exception.offset, 7)

    def test_invalid_new_bytes_reported_as_event(self):
        path = self.make_file(b'{"a":1}@')
        events = []
        visit = visit_many_checkpointed(path, reporter=events.append)
        with self.assertRaises(InvalidRootError):
            list(visit)
        kinds = [event.kind for event in events if event.kind != EventKind.FIELD]
        self.assertEqual(kinds, [EventKind.ROOT, EventKind.INVALID])
        self.assertEqual(events[-1].offset, 7)
        self.assertTrue(events[-1].pending)

    def test_missing_root_boundary_is_invalid_not_skipped(self):
        path = self.make_file(b'{"a":1}{bad}')
        with self.assertRaises(InvalidRootError) as caught:
            list(json_stream.visit_many(path, lambda value, p: None))
        self.assertEqual(caught.exception.root_index, 1)
        self.assertEqual(caught.exception.offset, 7)

    def test_text_and_iterable_sources_are_rejected(self):
        with self.assertRaises(UnsupportedSourceError):
            visit_many_checkpointed(io.StringIO('{"a":1}'))
        with self.assertRaises(UnsupportedSourceError):
            visit_many_checkpointed([b'{"a":1}'])

    def test_non_seekable_binary_source_is_rejected(self):
        class NoSeek(io.BytesIO):
            def seekable(self):
                return False

        with self.assertRaises(UnsupportedSourceError):
            visit_many_checkpointed(NoSeek(b'{"a":1}'))

    def test_binary_file_object_and_path_agree(self):
        data = b''.join(ROOTS)
        path = self.make_file(data)
        from_path = list(json_stream.visit_many(path, lambda value, p: None))
        with open(path, 'rb') as handle:
            from_file = list(
                json_stream.visit_many(handle, lambda value, p: None, reporter=lambda event: None)
            )
        self.assertEqual([e.end for e in from_path], [e.end for e in from_file])

    def test_save_checkpoint_only_after_valid_root(self):
        path = self.make_file(b''.join(ROOTS) + EIGHTH[:10])
        credential_path = os.path.join(self.directory, 'credential.json')
        visit = json_stream.visit_many(
            path,
            lambda value, p: None,
            save_checkpoint=credential_path,
        )
        for _ in visit:
            pass
        saved = Checkpoint.load(credential_path)
        self.assertEqual(saved.confirmed_roots, 7)
        self.assertEqual(saved.position, sum(len(root) for root in ROOTS))

    def test_resume_from_positional_helper(self):
        path = self.make_file(b''.join(ROOTS))
        visit = json_stream.visit_many(path, lambda value, p: None)
        for _ in visit:
            pass
        credential = visit.checkpoint
        with open(path, 'ab') as handle:
            handle.write(b'{"next":true}')
        seen = []
        events = list(resume_from(path, credential, lambda value, p: seen.append(value)))
        self.assertEqual([event.root_index for event in events], [7])
        self.assertEqual(seen, [True])

    def test_bom_is_not_a_root(self):
        path = self.make_file(b'\xef\xbb\xbf{"a":1}')
        events = list(json_stream.visit_many(path, lambda value, p: None))
        self.assertEqual([event.end for event in events], [10])

    def test_empty_roots(self):
        path = self.make_file(b'{}[]{}')
        events = list(json_stream.visit_many(path, lambda value, p: None))
        self.assertEqual([event.end for event in events], [2, 4, 6])
        self.assertEqual(events[-1].checkpoint.confirmed_roots, 3)

    def test_partial_root_can_be_between_roots(self):
        path = self.make_file(b'{"a":1}{"b":')
        seen = []
        events = list(json_stream.visit_many(path, lambda value, p: seen.append(value)))
        self.assertEqual([e.kind for e in events], [EventKind.ROOT, EventKind.INCOMPLETE])
        self.assertEqual(seen, [1])

    def test_reporter_sees_fields_and_roots(self):
        path = self.make_file(b'{"a":1}')
        events = []
        list(visit_many_checkpointed(path, reporter=events.append))
        self.assertEqual([event.kind for event in events], [EventKind.FIELD, EventKind.ROOT])
        field, root = events
        self.assertTrue(root.confirmed)
        self.assertFalse(root.pending)
        # the field event shares its root's status and is now confirmed too
        self.assertTrue(field.confirmed)
        self.assertEqual(field.status, 'confirmed')

    def test_incomplete_root_field_stays_pending(self):
        path = self.make_file(b'{"a":1}{"b":')
        fields = []
        list(json_stream.visit_many(path, lambda value, p, event: fields.append(event)))
        self.assertEqual([event.root_index for event in fields], [0])
        self.assertTrue(fields[0].confirmed)

        # the partial second root delivered no values here, but a partial
        # value would still be pending and must not advance the credential
        path = self.make_file(b'{"a":1}{"b":2', name='partial.json')
        fields = []
        visit = json_stream.visit_many(
            path, lambda value, p, event: fields.append(event)
        )
        events = list(visit)
        self.assertEqual(events[-1].kind, EventKind.INCOMPLETE)
        partial = [event for event in fields if event.root_index == 1]
        self.assertTrue(partial)
        self.assertTrue(all(not event.confirmed for event in partial))
        self.assertTrue(all(event.pending for event in partial))
        self.assertEqual(visit.checkpoint.confirmed_roots, 1)

    def test_scalar_roots_end_at_their_own_syntax(self):
        path = self.make_file(b'"x" \n 42 {"b":2}')
        events = list(json_stream.visit_many(path, lambda value, p: None))
        # string 0..3, number 6..8, object 9..16 (leading framing whitespace
        # is not part of any confirmed root)
        self.assertEqual([(event.root_index, event.end) for event in events],
                         [(0, 3), (1, 8), (2, 16)])

    def test_whitespace_only_needs_no_confirmation(self):
        path = self.make_file(b'{"a":1}\n\n\t  ')
        events = list(json_stream.visit_many(path, lambda value, p: None))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].checkpoint.position, 7)

    def test_resume_across_scalar_root(self):
        path = self.make_file(b'{"a":1} 42 ')
        visit = json_stream.visit_many(path, lambda value, p: None)
        events = list(visit)
        self.assertEqual([event.end for event in events], [7, 10])
        credential = visit.checkpoint
        with open(path, 'ab') as handle:
            handle.write(b' [1]')
        resumed = list(
            json_stream.visit_many(path, lambda value, p: None, checkpoint=credential)
        )
        self.assertEqual([event.root_index for event in resumed], [2])

    def test_browsed_can_exceed_confirmed_position(self):
        # The legacy reader may fetch the whole buffer while parsing the
        # eighth root.  Only the verified prefix becomes a credential.
        head = b''.join(ROOTS)
        path = self.make_file(head + EIGHTH[:20])
        visit = json_stream.visit_many(path, lambda value, p: None)
        events = []
        for event in visit:
            events.append(event)
            if event.root_index == 7 and event.kind == EventKind.INCOMPLETE:
                break
        incomplete = events[-1]
        self.assertEqual(incomplete.position, len(head))
        self.assertGreater(incomplete.browsed, incomplete.position)
        self.assertLessEqual(incomplete.browsed, os.path.getsize(path))

    def test_python_tokenizer_backend(self):
        from json_stream.tokenizer import tokenize

        path = self.make_file(b'{"a":1} 42\n[1,2]')
        events = list(
            json_stream.visit_many(path, lambda value, p: None, tokenizer=tokenize)
        )
        self.assertEqual([event.root_index for event in events], [0, 1, 2])

    def test_escaped_structural_bytes_do_not_split_roots(self):
        data = b'{"a":"}{[]\\"x","b":[1,2]}{"c":3}'
        path = self.make_file(data)
        events = list(json_stream.visit_many(path, lambda value, p: None))
        self.assertEqual([event.root_index for event in events], [0, 1])

    def test_adjacent_scalar_roots(self):
        path = self.make_file(b'42 43 ')
        events = list(json_stream.visit_many(path, lambda value, p: None))
        self.assertEqual([event.end for event in events], [2, 5])

    def test_trailing_bare_number_is_not_confirmed(self):
        # appended digits would silently extend a number ending at EOF
        path = self.make_file(b'{"a":1} 42')
        events = list(json_stream.visit_many(path, lambda value, p: None))
        self.assertEqual([event.kind for event in events],
                         [EventKind.ROOT, EventKind.INCOMPLETE])
        self.assertEqual(events[-1].root_index, 1)
        self.assertEqual(events[-1].position, 7)

    def test_number_without_delimiter_is_invalid(self):
        path = self.make_file(b'42"x"')
        with self.assertRaises(InvalidRootError):
            list(json_stream.visit_many(path, lambda value, p: None))

    def test_utf8_multi_byte_payload(self):
        data = '{"emoji":"😀","nested":{"k":[{"z":"\\u4e2d"}]}} 7 '.encode('utf-8')
        path = self.make_file(data)
        seen = []
        events = list(json_stream.visit_many(path, lambda value, p: seen.append(value)))
        self.assertEqual([event.root_index for event in events], [0, 1])
        self.assertIn('😀', seen)
