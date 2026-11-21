"""Byte-by-byte cut-point regressions for mixed persistent consumption.

The scenario under test is the "header + streamed rows + hand-off" pattern:

    {"id": 1, "lines": [10, 20]} {"id": 2, "lines": [30, 40]}

``load_many(..., persistent=True)`` hands the application the first root. The
application keeps the root's header (``id``), consumes the child array
(``lines``) lazily, and finally resumes the generator so a different piece of
code can process the next root.

The source is driven through a controlled reader whose reads are recorded as
``(offset, length)`` spans. The reader stops after the bytes of the truncated
payload, which is not necessarily a file end as far as the application is
concerned: the cut is a property of the fixture, not of the tokenizer.

Two failures used to look identical from the outside:

``cut_before_root_closed``
    The first root's *child container* is never closed. The failure belongs to
    the first root, and ``id``/``lines`` may already have been handed out.

``cut_inside_next_root``
    The first root is complete, and the *next* root breaks in the middle of its
    own content. The failure belongs to the second root; the first root's
    header must stay re-readable.

Each payload is swept one byte at a time with two independent read paths:

``rust-prefetch``
    The existing Rust tokenizer with its default buffer, which reads far ahead
    of the logical progress.

``python-byte``
    The pure-python tokenizer driven by a reader that only ever returns one
    byte per call.

Every step records what the application received, the physical byte spans, and
whether the persistent header can still be served from memory.
"""
import io
from unittest import TestCase, skipUnless

import json_stream
from json_stream.select_tokenizer import default_tokenizer
from json_stream.tokenizer import tokenize

try:
    from json_stream_rs_tokenizer import RustTokenizer
    HAS_RS_TOKENIZER = True
except ImportError:  # pragma: no cover - exercised only without the extension
    RustTokenizer = None
    HAS_RS_TOKENIZER = False


FIRST_ROOT = b'{"id":1,"lines":[10,20]}'
# Fails inside the child array of the *second* root: "40" and both closers are missing.
SECOND_ROOT_BROKEN = b'{"id":2,"lines":[30,4'

# ``cut_before_root_closed``: only the first root, with its child array unclosed.
UNCLOSED_PAYLOAD = b'{"id":1,"lines":[10,20'
# ``cut_inside_next_root``: a complete first root followed by a broken second one.
NEXT_ROOT_PAYLOAD = FIRST_ROOT + SECOND_ROOT_BROKEN

# Offsets of the logical landmarks in the unclosed fixture, measured from the
# bytes and therefore independent of tokenizer and chunking. ``*_END`` values
# are "one past the last byte", the convention used by slicing; ``*_INDEX``
# values are the byte positions themselves.
ROOT1_YIELDS_AT = UNCLOSED_PAYLOAD.index(b'{') + 1      # 1 (one past `{`)
ID_END = UNCLOSED_PAYLOAD.index(b'1') + 1               # 7 (one past `1`)
CHILD_OPEN = UNCLOSED_PAYLOAD.index(b'[') + 1           # 17 (one past `[`)
CHILD_CLOSE_INDEX = NEXT_ROOT_PAYLOAD.index(b']')       # 22
CHILD_CLOSE_END = CHILD_CLOSE_INDEX + 1                 # 23 (one past `]`)
ROOT_CLOSE_INDEX = NEXT_ROOT_PAYLOAD.index(b'}')        # 23
FIRST_ROOT_END = ROOT_CLOSE_INDEX + 1                   # 24 (one past `}`)
NEXT_ROOT_START = FIRST_ROOT_END                        # 24 (the next `{`)
NEXT_ROOT_CHILD_OPEN = NEXT_ROOT_PAYLOAD.rindex(b'[') + 1  # 41 (one past `[`)


class RecordingSource(io.RawIOBase):
    """A controlled binary reader that records every read as ``(offset, length)``.

    ``chunk`` limits each ``read`` to at most that many bytes, which gives an
    application-side chunking strategy independent of the tokenizer's internal
    buffer. ``fail_at`` raises ``OSError`` as soon as the physical cursor has
    reached that offset, i.e. at the exact source span whose effect we want to
    observe. Zero-length probes (the tokenizer uses ``read(0)`` for encoding
    detection) are recorded but never consume the cut.
    """

    def __init__(self, data, chunk=None, fail_at=None):
        self.data = data
        self.chunk = chunk
        self.fail_at = fail_at
        self.pos = 0
        self.spans = []
        self._failed = False

    def readable(self):
        return True

    def seekable(self):
        return False

    def tell(self):
        return self.pos

    def read(self, n=-1):
        if n == 0:
            self.spans.append((self.pos, 0))
            return b''
        if self.fail_at is not None and self.pos >= self.fail_at and not self._failed:
            self._failed = True
            self.spans.append((self.pos, n, 'error'))
            raise OSError(f'injected read failure at offset {self.pos}')
        if n is None or n < 0:
            n = len(self.data) - self.pos
        if self.chunk is not None:
            n = min(n, self.chunk)
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        self.spans.append((self.pos - len(chunk), len(chunk)))
        return chunk

    def readinto(self, buffer):
        chunk = self.read(len(buffer))
        buffer[:len(chunk)] = chunk
        return len(chunk)

    @property
    def consumed(self):
        # Only successful reads contribute bytes; an injected error records the
        # requested length, not bytes that were actually handed to the tokenizer.
        return sum(
            length
            for span in self.spans
            if len(span) == 2 and span[1]
            for length in (span[1],)
        )

    @property
    def error_fired(self):
        return any(len(span) > 2 and span[2] == 'error' for span in self.spans)


class PlannedSource(io.RawIOBase):
    """A seekable reader that delivers bytes according to an explicit plan.

    Either ``limit`` (max bytes per ``read``) or ``plan`` (a list of absolute
    end offsets, one per read) controls the physical chunking. ``position`` is
    the raw cursor, which prefetch is allowed to move far ahead of the logical
    consumption point; ``park_cursor`` is what maps it back.
    """

    def __init__(self, data, limit=None, plan=None):
        self.data = data
        self.limit = limit
        self.plan = list(plan) if plan is not None else None
        self.position = 0
        self._index = 0
        self.spans = []

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        if whence == 0:
            self.position = offset
        elif whence == 1:
            self.position += offset
        else:
            self.position = len(self.data) + offset
        return self.position

    def read(self, n=-1):
        if n == 0:
            self.spans.append((self.position, 0))
            return b''
        if n is None or n < 0:
            n = len(self.data) - self.position
        if self.plan is not None:
            if self._index >= len(self.plan):
                self.spans.append((self.position, 0))
                return b''
            n = min(n, max(0, self.plan[self._index] - self.position))
            self._index += 1
        elif self.limit is not None:
            n = min(n, self.limit)
        chunk = self.data[self.position:self.position + n]
        self.position += len(chunk)
        self.spans.append((self.position - len(chunk), len(chunk)))
        return chunk

    def readinto(self, buffer):
        chunk = self.read(len(buffer))
        buffer[:len(chunk)] = chunk
        return len(chunk)

    @property
    def pos(self):
        return self.position


class StepRecorder:
    """Runs application operations and records outcomes without hiding failures.

    Results are either ``('ok', value)``, ``('stop', '')`` or
    ``('error', ExceptionType, message)``. The point of the regression is *which
    logical action failed first*, so failures are values rather than assertions
    inside the recorder.
    """

    def __init__(self, loader):
        self.loader = loader
        self.events = []

    def do(self, name, operation):
        try:
            value = operation()
        except StopIteration:
            self.events.append((name, 'stop'))
            return False, None
        except Exception as exc:  # noqa: BLE001 - recorded deliberately
            self.events.append((name, 'error', type(exc).__name__, str(exc)))
            return False, None
        self.events.append((name, 'ok', value))
        return True, value

    def names(self):
        return [event[0] for event in self.events]

    def outcome(self, name):
        for event in self.events:
            if event[0] == name:
                return event[1]
        raise AssertionError(f'no event named {name!r} in {self.events!r}')

    def error_at(self, name):
        for event in self.events:
            if event[0] == name and event[1] == 'error':
                return event[2], event[3]
        return None


class ReadPath:
    def __init__(self, name, tokenizer, chunk, tokenizer_kwargs):
        self.name = name
        self.tokenizer = tokenizer
        self.chunk = chunk
        self.tokenizer_kwargs = tokenizer_kwargs


# The two independent read paths required by the regression. Both use the
# production entry point (``json_stream.load_many``) and only differ in how bytes
# reach the tokenizer.
READ_PATHS = [
    ReadPath('rust-prefetch', default_tokenizer, None, {}),
    ReadPath('python-byte', tokenize, 1, {}),
]


def consume_child(root, recorder, name='child'):
    return recorder.do(name, lambda: list(root['lines']))


def take_one_from_child_then_advance_outer(root, recorder):
    iterator = iter(root['lines'])
    if not recorder.do('child_first', lambda: next(iterator))[0]:
        return False
    return recorder.do('outer', lambda: root.read_all())[0]


def run_cut_scenario(payload, path, action, *, persistent=True):
    """Drive one cut-point/action/path combination and return the recorder.

    ``action`` is one of ``consume_child``, ``one_then_outer`` or
    ``header_then_next``. The scenario always attempts the "next root" hand-off
    and a final drain, so error attribution across the root boundary is visible.
    """
    source = RecordingSource(payload, chunk=path.chunk)
    generator = json_stream.load_many(
        source,
        persistent=persistent,
        tokenizer=path.tokenizer,
        **path.tokenizer_kwargs,
    )
    recorder = StepRecorder(generator)

    ok, root = recorder.do('root1', lambda: next(generator))
    if not ok:
        return recorder, source

    recorder.do('id1', lambda: root['id'])

    if action == 'consume_child':
        if not consume_child(root, recorder):
            recorder.do('id1_reread', lambda: root['id'])
            recorder.do('next_root', lambda: next(generator))
            return recorder, source
        recorder.do('outer', lambda: root.read_all())
    elif action == 'one_then_outer':
        if not take_one_from_child_then_advance_outer(root, recorder):
            recorder.do('id1_reread', lambda: root['id'])
            recorder.do('next_root', lambda: next(generator))
            return recorder, source
    elif action == 'header_then_next':
        if not recorder.do('header_before_next', lambda: root['id'])[0]:
            return recorder, source
    else:  # pragma: no cover - programming error in the test
        raise ValueError(action)

    # The header of the persistent first root must survive whatever happened to
    # the child/next root; this is what "persistent header" buys the application.
    recorder.do('id1_reread', lambda: root['id'])

    ok, next_root = recorder.do('next_root', lambda: next(generator))
    if ok:
        recorder.do('id2', lambda: next_root['id'])
        recorder.do('next_child', lambda: list(next_root['lines']))
        recorder.do('next_outer', lambda: next_root.read_all())
        recorder.do('id1_reread_after_next', lambda: root['id'])
        recorder.do('exhaust', lambda: next(generator))

    return recorder, source


def first_error_stage(recorder):
    for name, kind, *rest in recorder.events:
        if kind == 'error':
            return name, rest
    return None


class CutPointFixtureCriteriaMixin:
    """Fixture-derived expectations, independent of tokenizer or chunking."""

    def assert_first_failure(self, recorder, expected_stage):
        failure = first_error_stage(recorder)
        self.assertIsNotNone(failure, f'expected a failure at {expected_stage!r}: {recorder.events!r}')
        self.assertEqual(expected_stage, failure[0], recorder.events)

    def assert_reaches_root2_and_fails_in_its_content(self, recorder):
        self.assertEqual('ok', recorder.outcome('id1'), recorder.events)
        self.assertEqual('ok', recorder.outcome('next_root'), recorder.events)
        self.assertEqual('ok', recorder.outcome('id2'), recorder.events)
        self.assertEqual(
            'error',
            recorder.outcome('next_child'),
            f'failure must be attributed to the next root, not the first: {recorder.events!r}',
        )
        self.assertIn('Unterminated list', recorder.error_at('next_child')[1], recorder.events)


class TestCutPointSweep(CutPointFixtureCriteriaMixin, TestCase):
    """Move the cut byte by byte over both payload shapes on both read paths.

    The failure stage is a property of the fixture: it changes exactly when the
    cut crosses a logical landmark (child open/close, root close, next root
    header). Physical read spans are allowed to differ freely between paths.
    """

    def _run(self, payload, path, action):
        recorder, source = run_cut_scenario(payload, path, action)
        return recorder, source

    def test_unclosed_child_never_reports_next_root(self):
        for path in READ_PATHS:
            for cut in range(len(UNCLOSED_PAYLOAD) + 1):
                with self.subTest(path=path.name, cut=cut):
                    recorder, _ = self._run(UNCLOSED_PAYLOAD[:cut], path, 'consume_child')
                    self.assertNotIn(
                        'ok',
                        [recorder.outcome(name) for name in recorder.names() if name == 'next_root'],
                        f'a cut inside the first root must not produce a next root: {recorder.events!r}',
                    )
                    if cut and recorder.outcome('root1') == 'ok':
                        self.assertIsNotNone(
                            first_error_stage(recorder),
                            f'cut {cut} of an unclosed payload must fail: {recorder.events!r}',
                        )

    def test_moving_cut_inside_first_child_changes_failure_stage(self):
        # Criteria are the logical landmarks of ``UNCLOSED_PAYLOAD``:
        #   cut < 1   -> nothing can be yielded yet
        #   cut < 7   -> the root object is yielded, but ``id`` cannot be decoded
        #   >= 7      -> header decodes; the child fails while being drained
        # The sweep must reproduce those transitions exactly, for both read
        # paths, regardless of how many physical reads each path performs.
        for path in READ_PATHS:
            transitions = []
            previous = None
            for cut in range(len(UNCLOSED_PAYLOAD) + 1):
                with self.subTest(path=path.name, cut=cut):
                    recorder, _ = self._run(UNCLOSED_PAYLOAD[:cut], path, 'consume_child')
                    if cut < ROOT1_YIELDS_AT:
                        self.assertEqual('stop', recorder.outcome('root1'), recorder.events)
                        stage = 'stop'
                    elif cut < ID_END:
                        self.assert_first_failure(recorder, 'id1')
                        stage = 'id1'
                    else:
                        self.assert_first_failure(recorder, 'child')
                        stage = 'child'
                    if stage != previous:
                        transitions.append((cut, stage))
                        previous = stage
            self.assertEqual(
                [(0, 'stop'), (ROOT1_YIELDS_AT, 'id1'), (ID_END, 'child')], transitions
            )
    def test_complete_first_root_is_never_blamed_for_broken_next_root(self):
        cases = [
            (FIRST_ROOT, 'root2_stop'),                              # exactly the first root
            (NEXT_ROOT_PAYLOAD[:NEXT_ROOT_START], 'root2_stop'),     # through the first root
            (NEXT_ROOT_PAYLOAD[:NEXT_ROOT_CHILD_OPEN], 'next_child'),  # next root header only
            (NEXT_ROOT_PAYLOAD, 'next_child'),                       # second child never closes
        ]
        for path in READ_PATHS:
            for payload, kind in cases:
                with self.subTest(path=path.name, cut=len(payload)):
                    recorder, _ = self._run(payload, path, 'consume_child')
                    self.assertEqual('ok', recorder.outcome('id1'), recorder.events)
                    if kind == 'root2_stop':
                        self.assertEqual('stop', recorder.outcome('next_root'), recorder.events)
                        self.assertEqual('ok', recorder.outcome('id1_reread'), recorder.events)
                    else:
                        self.assert_reaches_root2_and_fails_in_its_content(recorder)
                        self.assertEqual('ok', recorder.outcome('id1_reread'), recorder.events)

    def test_landmark_cut_that_contains_child_close_root_close_and_next_open(self):
        # Same physical chunk contains child `]`, root `}`, and next root `{`.
        # Splitting it into separate chunks must not move the logical failure.
        combined = NEXT_ROOT_PAYLOAD[:NEXT_ROOT_START + 1]  # through the next root's `{`
        self.assertEqual(b']', combined[CHILD_CLOSE_INDEX:CHILD_CLOSE_END])
        self.assertEqual(b'}', combined[ROOT_CLOSE_INDEX:FIRST_ROOT_END])
        self.assertEqual(b'{', combined[NEXT_ROOT_START:NEXT_ROOT_START + 1])

        for path in READ_PATHS:
            with self.subTest(path=path.name, variant='coalesced'):
                coalesced = ReadPath(path.name + '-coalesced', path.tokenizer, None, path.tokenizer_kwargs)
                recorder, _ = run_cut_scenario(FIRST_ROOT, coalesced, 'consume_child')
                self.assertEqual('stop', recorder.outcome('next_root'), recorder.events)

            for cut in (CHILD_CLOSE_END, FIRST_ROOT_END, NEXT_ROOT_START):
                with self.subTest(path=path.name, cut=cut):
                    recorder, _ = run_cut_scenario(NEXT_ROOT_PAYLOAD[:cut], path, 'consume_child')
                    self.assertEqual('ok', recorder.outcome('id1'), recorder.events)
                    self.assertEqual('ok', recorder.outcome('id1_reread'), recorder.events)


class TestActionSpecificProgress(CutPointFixtureCriteriaMixin, TestCase):
    """The three application actions fail at different logical steps."""

    def test_explicit_child_consumption(self):
        # Failure while draining the child array belongs to the child action.
        recorder, _ = run_cut_scenario(UNCLOSED_PAYLOAD, READ_PATHS[0], 'consume_child')
        self.assert_first_failure(recorder, 'child')

    def test_one_child_item_then_outer_advance(self):
        # The first item succeeds; the outer drain discovers the unclosed child.
        recorder, _ = run_cut_scenario(UNCLOSED_PAYLOAD, READ_PATHS[0], 'one_then_outer')
        self.assertEqual('ok', recorder.outcome('child_first'), recorder.events)
        self.assert_first_failure(recorder, 'outer')

    def test_explicit_header_confirmation_then_next_root(self):
        # No child consumption: the next-root step is what advances into the
        # unclosed child, so the failure is reported there.
        recorder, _ = run_cut_scenario(UNCLOSED_PAYLOAD, READ_PATHS[0], 'header_then_next')
        self.assertEqual('ok', recorder.outcome('header_before_next'), recorder.events)
        self.assert_first_failure(recorder, 'next_root')

    def test_header_confirmation_does_not_consume_child(self):
        for path in READ_PATHS:
            with self.subTest(path=path.name):
                source = RecordingSource(FIRST_ROOT, chunk=path.chunk)
                generator = json_stream.load_many(
                    source, persistent=True, tokenizer=path.tokenizer, **path.tokenizer_kwargs
                )
                root = next(generator)
                self.assertEqual(1, root['id'])
                # Confirming the header again is served from the persistent cache
                # and does not advance into the child, even if the bytes happen
                # to have been prefetched already.
                reads_before = len(source.spans)
                self.assertEqual(1, root['id'])
                self.assertEqual(reads_before, len(source.spans))
                self.assertEqual([10, 20], list(root['lines']))


class TestPersistentHeaderReread(CutPointFixtureCriteriaMixin, TestCase):
    """The persistent header remains readable after child/next failures."""

    def test_header_reread_does_not_touch_source_after_child_failure(self):
        for path in READ_PATHS:
            with self.subTest(path=path.name):
                recorder, source = run_cut_scenario(UNCLOSED_PAYLOAD, path, 'consume_child')
                self.assert_first_failure(recorder, 'child')
                reads_before = len(source.spans)
                before = source.pos
                self.assertEqual('ok', recorder.outcome('id1_reread'), recorder.events)
                self.assertEqual(before, source.pos, 'persistent header reread must not read the source')
                self.assertEqual(reads_before, len(source.spans))

    def test_header_of_first_root_survives_next_root_failure(self):
        recorder, _ = run_cut_scenario(NEXT_ROOT_PAYLOAD, READ_PATHS[0], 'consume_child')
        self.assert_reaches_root2_and_fails_in_its_content(recorder)
        self.assertEqual('ok', recorder.outcome('id1_reread'), recorder.events)
        self.assertEqual('ok', recorder.outcome('id1_reread_after_next'), recorder.events)


class TestSourceSpansRecorded(TestCase):
    """Every source read is recorded as a byte span and matches the payload."""

    def test_spans_are_disjoint_and_within_payload(self):
        for path in READ_PATHS:
            with self.subTest(path=path.name):
                recorder, source = run_cut_scenario(NEXT_ROOT_PAYLOAD, path, 'consume_child')
                self.assertEqual('error', recorder.outcome('next_child'), recorder.events)
                nonzero = [(offset, length) for offset, length, *rest in source.spans if length]
                for offset, length in nonzero:
                    self.assertGreaterEqual(offset, 0)
                    self.assertLessEqual(offset + length, len(NEXT_ROOT_PAYLOAD))
                # Reads are sequential: each starts where the previous one ended.
                for (first_offset, first_length), (second_offset, _) in zip(nonzero, nonzero[1:]):
                    self.assertEqual(first_offset + first_length, second_offset)
                self.assertEqual(source.consumed, len(NEXT_ROOT_PAYLOAD))

    def test_prefetch_reads_past_logical_progress_but_python_byte_path_does_not(self):
        # This is the observable difference between the two read paths. It is a
        # property of the *physical* reader, not of the logical root.
        prefetch, prefetch_source = run_cut_scenario(NEXT_ROOT_PAYLOAD, READ_PATHS[0], 'consume_child')
        python_byte, python_byte_source = run_cut_scenario(NEXT_ROOT_PAYLOAD, READ_PATHS[1], 'consume_child')

        self.assertEqual([e[0] for e in prefetch.events], [e[0] for e in python_byte.events])
        self.assertEqual('error', prefetch.outcome('next_child'), prefetch.events)
        self.assertEqual('error', python_byte.outcome('next_child'), python_byte.events)

        # Up to and including the first root's items, the Rust default buffer has
        # already seen the whole payload.
        first_prefetch_span = [span for span in prefetch_source.spans if span[1]]
        self.assertTrue(first_prefetch_span)
        self.assertEqual(len(NEXT_ROOT_PAYLOAD), first_prefetch_span[0][0] + first_prefetch_span[0][1])

        # The byte-at-a-time path stops at the exact cut.
        self.assertEqual(len(NEXT_ROOT_PAYLOAD), python_byte_source.pos)


class TestInjectedReadErrors(CutPointFixtureCriteriaMixin, TestCase):
    """A source ``OSError`` at a logical span must be attributed to that span.

    With prefetch the *physical* read happens earlier than the logical progress,
    so the injection fires at a span chosen by the read path. The expected
    logical action is still the one that consumes the offset, which is why the
    criteria below are expressed in offsets and action names rather than in the
    number of reads.
    """

    def _run_failing(self, payload, path, action, fail_at):
        source = RecordingSource(payload, chunk=path.chunk, fail_at=fail_at)
        generator = json_stream.load_many(
            source, persistent=True, tokenizer=path.tokenizer, **path.tokenizer_kwargs
        )
        recorder = StepRecorder(generator)
        ok, root = recorder.do('root1', lambda: next(generator))
        if not ok:
            return recorder, source
        recorder.do('id1', lambda: root['id'])
        if action == 'consume_child':
            consume_child(root, recorder)
            # The child ends at CHILD_CLOSE_END, so an error injected after that
            # offset is no longer "inside the first child". Draining the outer
            # root is what consumes those remaining bytes.
            recorder.do('outer', lambda: root.read_all())
        elif action == 'one_then_outer':
            take_one_from_child_then_advance_outer(root, recorder)
        elif action == 'header_then_next':
            recorder.do('header_before_next', lambda: root['id'])
        recorder.do('next_root', lambda: next(generator))
        return recorder, source

    def test_error_inside_first_child_is_not_reported_as_next_root(self):
        for path in READ_PATHS:
            # Offsets strictly inside the first child array: after `[`, and up to
            # and including its closing `]`. An error here is discovered while
            # the application consumes the child (or, for a one-item read, while
            # the outer root finishes it).
            for fail_at in range(CHILD_OPEN + 1, CHILD_CLOSE_END + 1):
                with self.subTest(path=path.name, fail_at=fail_at):
                    recorder, source = self._run_failing(
                        NEXT_ROOT_PAYLOAD, path, 'one_then_outer', fail_at
                    )
                    if not source.error_fired:
                        # Prefetch already delivered these bytes before the action,
                        # so there is no source read left to fail here. The
                        # payload must nevertheless have been fully consumed and
                        # parsed without error: this is the physical/logical
                        # split, not a swallowed failure.
                        self.assertEqual(len(NEXT_ROOT_PAYLOAD), source.consumed)
                        self.assertIsNone(first_error_stage(recorder), recorder.events)
                        self.assertEqual('ok', recorder.outcome('next_root'), recorder.events)
                        continue
                    failure = first_error_stage(recorder)
                    self.assertIsNotNone(failure, recorder.events)
                    self.assertEqual('OSError', failure[1][0])
                    self.assertIn(failure[0], ('child_first', 'outer'), recorder.events)
                    self.assertNotEqual('next_root', failure[0], recorder.events)

    def test_error_at_first_root_close_is_attributed_to_outer_drain(self):
        # The root's closing `}` is the byte at offset FIRST_ROOT_END - 1. A
        # failure raised when that byte is read still belongs to the first root,
        # not to the hand-off that starts at NEXT_ROOT_START.
        for path in READ_PATHS:
            for action in ('consume_child', 'one_then_outer'):
                with self.subTest(path=path.name, action=action):
                    recorder, source = self._run_failing(
                        NEXT_ROOT_PAYLOAD, path, action, FIRST_ROOT_END - 1
                    )
                    if not source.error_fired:
                        continue
                    failure = first_error_stage(recorder)
                    self.assertIsNotNone(failure, recorder.events)
                    self.assertEqual('OSError', failure[1][0])
                    self.assertEqual('outer', failure[0], recorder.events)

    def test_error_inside_next_root_is_reported_at_next_root_after_header(self):
        for path in READ_PATHS:
            # From the next root's `{` onwards: the hand-off is the action that
            # consumes these bytes, so the failure must surface there.
            for fail_at in range(NEXT_ROOT_START, len(NEXT_ROOT_PAYLOAD) + 1):
                with self.subTest(path=path.name, fail_at=fail_at):
                    recorder, source = self._run_failing(
                        NEXT_ROOT_PAYLOAD, path, 'header_then_next', fail_at
                    )
                    self.assertEqual('ok', recorder.outcome('header_before_next'), recorder.events)
                    if not source.error_fired:
                        self.assertIsNone(first_error_stage(recorder), recorder.events)
                        continue
                    failure = first_error_stage(recorder)
                    self.assertIsNotNone(failure, recorder.events)
                    self.assertEqual('OSError', failure[1][0])
                    self.assertEqual('next_root', failure[0], recorder.events)

    def test_prefetch_fires_io_error_at_physical_read_but_not_logically_early(self):
        # With the default buffer the whole payload is read in one go, so an
        # injected failure at the *end* of the payload fires during the very
        # first physical read. The first root is nevertheless parsed from the
        # bytes already delivered, and the header is served from the persistent
        # cache without touching the source again.
        prefetch = ReadPath('rust-prefetch', default_tokenizer, None, {})
        source = RecordingSource(NEXT_ROOT_PAYLOAD, chunk=prefetch.chunk, fail_at=len(NEXT_ROOT_PAYLOAD) + 1)
        generator = json_stream.load_many(source, persistent=True, tokenizer=prefetch.tokenizer)
        root = next(generator)
        self.assertEqual(1, root['id'])
        self.assertEqual([10, 20], list(root['lines']))
        reads_before = len(source.spans)
        self.assertEqual(1, root['id'])
        self.assertEqual(reads_before, len(source.spans))

    def test_prefetch_moves_the_physical_read_before_the_logical_consumer(self):
        # Contrast the two read paths at the same injected *logical* offset.
        # Prefetch has already paid for the bytes before the application asks,
        # so the injection never fires; the byte path reaches them only when the
        # action consumes them, and the OSError is attributed to that action.
        prefetch = ReadPath('rust-prefetch', default_tokenizer, None, {})
        byte_path = READ_PATHS[1]

        prefetch_recorder, prefetch_source = self._run_failing(
            NEXT_ROOT_PAYLOAD, prefetch, 'consume_child', CHILD_OPEN + 5
        )
        byte_recorder, byte_source = self._run_failing(
            NEXT_ROOT_PAYLOAD, byte_path, 'consume_child', CHILD_OPEN + 5
        )

        self.assertTrue(prefetch_source.consumed > len(NEXT_ROOT_PAYLOAD) - 1)
        self.assertFalse(prefetch_source.error_fired)
        self.assertIsNone(first_error_stage(prefetch_recorder), prefetch_recorder.events)

        self.assertTrue(byte_source.error_fired)
        failure = first_error_stage(byte_recorder)
        self.assertIsNotNone(failure, byte_recorder.events)
        self.assertEqual('child', failure[0], byte_recorder.events)
        self.assertEqual('OSError', failure[1][0], byte_recorder.events)


class TestTransientRevisitContrast(CutPointFixtureCriteriaMixin, TestCase):
    """A legal revisit of an item already skipped is a *transient* contrast case.

    It must not be confused with a parse/read failure: a transient list yields
    an item once, and asking for it again is a ``TransientAccessException`` (or
    ``IndexError`` if the position is still current but was already produced),
    never an I/O or syntax error. The persistent list, by contrast, serves the
    same item from its cache with no additional source reads.
    """

    PAYLOAD = b'{"id":1,"lines":[10,20,30],"tail":"x"}'

    def test_transient_child_item_is_yielded_once(self):
        data = io.BytesIO(self.PAYLOAD)
        root = json_stream.load(data, persistent=False, tokenizer=default_tokenizer)
        lines = root['lines']
        self.assertEqual(10, lines[0])
        with self.assertRaises(IndexError):
            _ = lines[0]  # index 0 is still current, but the item was already produced

    def test_revisit_after_passing_is_transient_access_not_source_error(self):
        from json_stream.base import TransientAccessException

        data = io.BytesIO(self.PAYLOAD)
        root = json_stream.load(data, persistent=False, tokenizer=default_tokenizer)
        lines = root['lines']
        self.assertEqual(10, lines[0])
        self.assertEqual(20, lines[1])
        with self.assertRaises(TransientAccessException):
            _ = lines[0]

    def test_persistent_child_revisit_reads_nothing_from_source(self):
        source = RecordingSource(self.PAYLOAD)
        root = json_stream.load(source, persistent=True, tokenizer=default_tokenizer)
        lines = root['lines']
        self.assertEqual(10, lines[0])
        self.assertEqual(20, lines[1])
        reads_before = len(source.spans)
        self.assertEqual(10, lines[0])  # legal persistent revisit
        self.assertEqual(reads_before, len(source.spans))
        self.assertEqual(1, root['id'])
        self.assertEqual(reads_before, len(source.spans))

    def test_revisit_over_the_same_spans_is_not_a_source_error(self):
        # Same payload, same byte spans, two modes. In transient mode the
        # revisit is a *positional* error raised by the library; in persistent
        # mode it is served from cache. Neither produces an ``OSError`` nor a
        # parser ``ValueError``, which is what distinguishes this contrast case
        # from the truncation and I/O-error scenarios above.
        from json_stream.base import TransientAccessException

        transient_source = RecordingSource(self.PAYLOAD)
        transient_root = json_stream.load(transient_source, persistent=False)
        transient_lines = transient_root['lines']
        self.assertEqual(10, transient_lines[0])
        self.assertEqual(20, transient_lines[1])
        transient_spans = list(transient_source.spans)
        with self.assertRaises((TransientAccessException, IndexError)) as caught:
            _ = transient_lines[0]
        self.assertNotIsInstance(caught.exception, (OSError, ValueError))

        persistent_source = RecordingSource(self.PAYLOAD)
        persistent_root = json_stream.load(persistent_source, persistent=True)
        persistent_lines = persistent_root['lines']
        self.assertEqual(10, persistent_lines[0])
        self.assertEqual(20, persistent_lines[1])
        self.assertEqual(10, persistent_lines[0])

        # Both modes consumed the same logical prefix; the persistent revisit
        # added no reads while the transient revisit failed without I/O.
        self.assertEqual(
            sum(length for _, length, *rest in transient_spans if length),
            persistent_source.consumed,
        )


@skipUnless(HAS_RS_TOKENIZER, 'park_cursor requires the Rust tokenizer')
class TestRelocationAcrossChunking(TestCase):
    """``park_cursor`` restores the logical root/level regardless of chunking.

    The physical position of the source after prefetch depends on the chunk
    sizes; ``park_cursor`` must map it back to the end of the logical position
    the application actually consumed. For an unclosed child this is the level
    that is still open, which is exactly what makes the failure attributable.
    """

    # (plan, description): ``limit`` is a fixed max bytes per read, ``plan`` is
    # an explicit list of chunk ends.
    CHUNKINGS = [
        ({'limit': 1}, 'one byte at a time'),
        ({'limit': 7}, 'small chunks'),
        ({'plan': [22, 23, 24, len(NEXT_ROOT_PAYLOAD)]}, 'landmark-aligned'),
        ({'plan': [24, len(NEXT_ROOT_PAYLOAD)]}, 'split after first root'),
        ({'plan': [22, 25, len(NEXT_ROOT_PAYLOAD)]}, 'coalesced landmark window'),
    ]

    def _parked(self, chunking, action):
        source = PlannedSource(NEXT_ROOT_PAYLOAD, **chunking)
        generator = json_stream.load_many(
            source,
            persistent=True,
            tokenizer=RustTokenizer,
            correct_cursor=True,
        )
        root = next(generator)
        self.assertEqual(1, root['id'])
        iterator = iter(root['lines'])
        if action == 'one_item':
            self.assertEqual(10, next(iterator))
            expected = 20  # end of the first scalar
        elif action == 'two_items':
            self.assertEqual([10, 20], list(iterator))
            expected = CHILD_CLOSE_END  # just after the child's `]`
        elif action == 'child_and_root':
            self.assertEqual([10, 20], list(iterator))
            root.read_all()
            expected = FIRST_ROOT_END  # just after the root's `}`
        else:  # pragma: no cover
            raise ValueError(action)
        physical_before = source.pos
        root.tokenizer.park_cursor()
        return expected, physical_before, source.pos

    def test_logical_position_is_stable_while_physical_position_varies(self):
        for action in ('one_item', 'two_items', 'child_and_root'):
            parked_positions = set()
            physical_positions = set()
            for chunking, description in self.CHUNKINGS:
                with self.subTest(action=action, chunking=description):
                    expected, before, parked = self._parked(chunking, action)
                    self.assertEqual(expected, parked, description)
                    parked_positions.add(parked)
                    physical_positions.add(before)
            self.assertEqual(1, len(parked_positions), parked_positions)
            # At least one chunking must have prefetched beyond the logical
            # position, otherwise this test would not be exercising the split.
            self.assertTrue(
                any(position > min(parked_positions) for position in physical_positions),
                physical_positions,
            )

    def test_park_at_unclosed_level_matches_fixture_end(self):
        for chunking, description in self.CHUNKINGS:
            with self.subTest(chunking=description):
                source = PlannedSource(UNCLOSED_PAYLOAD, **chunking)
                generator = json_stream.load_many(
                    source,
                    persistent=True,
                    tokenizer=RustTokenizer,
                    correct_cursor=True,
                )
                root = next(generator)
                self.assertEqual(1, root['id'])
                with self.assertRaisesRegex(ValueError, 'Unterminated list'):
                    list(root['lines'])
                root.tokenizer.park_cursor()
                # The unclosed child level starts at CHILD_OPEN and the fixture
                # ends there; relocation must land on the end of the bytes
                # actually present, not on a prefetched boundary.
                self.assertEqual(len(UNCLOSED_PAYLOAD), source.pos)
                self.assertGreaterEqual(CHILD_OPEN, ID_END)


class TestRootCompletionCriteria(CutPointFixtureCriteriaMixin, TestCase):
    """A root is complete only when its own content was fully consumed.

    The distinction the fixture must preserve: a complete first root may follow
    a broken next root (``streaming is False`` for root 1 while root 2 fails),
    and a broken first root must *never* report itself complete merely because
    the source ended.
    """

    def test_complete_first_root_reports_complete_before_next_root_fails(self):
        for path in READ_PATHS:
            with self.subTest(path=path.name):
                recorder, _ = run_cut_scenario(NEXT_ROOT_PAYLOAD, path, 'consume_child')
                self.assertEqual('ok', recorder.outcome('id1'), recorder.events)
                self.assert_reaches_root2_and_fails_in_its_content(recorder)

                source = RecordingSource(NEXT_ROOT_PAYLOAD, chunk=path.chunk)
                generator = json_stream.load_many(
                    source, persistent=True, tokenizer=path.tokenizer, **path.tokenizer_kwargs
                )
                root1 = next(generator)
                self.assertEqual([10, 20], list(root1['lines']))
                root1.read_all()
                self.assertFalse(root1.streaming, 'first root must be complete')
                root2 = next(generator)
                self.assertEqual(2, root2['id'])
                with self.assertRaisesRegex(ValueError, 'Unterminated list'):
                    root2.read_all()
                self.assertFalse(root1.streaming)
                self.assertTrue(root2.streaming, 'broken next root must not look complete')

    def test_broken_first_root_never_reports_complete(self):
        for path in READ_PATHS:
            with self.subTest(path=path.name):
                source = RecordingSource(UNCLOSED_PAYLOAD, chunk=path.chunk)
                generator = json_stream.load_many(
                    source, persistent=True, tokenizer=path.tokenizer, **path.tokenizer_kwargs
                )
                root = next(generator)
                self.assertEqual(1, root['id'])
                with self.assertRaisesRegex(ValueError, 'Unterminated list'):
                    list(root['lines'])
                self.assertTrue(root.streaming, 'unclosed child must not mark the root complete')
                with self.assertRaisesRegex(ValueError, 'Unterminated list'):
                    len(root)


class TestFailureClassifierSensitivity(TestCase):
    """Meta-tests that the harness can tell the three confusions apart.

    These do not parse anything new; they check that the *test criteria* reject
    the three wrong stories the regression exists to catch.
    """

    def test_detects_error_misattributed_to_next_root(self):
        recorder, _ = run_cut_scenario(UNCLOSED_PAYLOAD, READ_PATHS[0], 'one_then_outer')
        failure = first_error_stage(recorder)
        self.assertEqual('outer', failure[0])
        self.assertNotEqual('next_root', failure[0])
        # A classifier that blamed the hand-off would be wrong here.
        self.assertNotIn('id2', recorder.names())

        wrong_story = 'next_root'
        self.assertNotEqual(wrong_story, failure[0], 'a broken first root must not blame root 2')

    def test_detects_implicit_drain_swallowing_an_error(self):
        # ``load_many`` drains the previous root when the generator resumes.
        # That implicit drain must not turn the child's failure into a silent
        # ``StopIteration``/no-op on the way to the next root.
        for path in READ_PATHS:
            with self.subTest(path=path.name):
                recorder, _ = run_cut_scenario(UNCLOSED_PAYLOAD, path, 'header_then_next')
                self.assertIsNotNone(first_error_stage(recorder), recorder.events)
                self.assertNotEqual('stop', recorder.outcome('next_root'), recorder.events)
                self.assertNotEqual('ok', recorder.outcome('next_root'), recorder.events)
                # The error came out of the hand-off *as an error*, not as a
                # normal end-of-stream after an implicit drain.
                self.assertEqual('error', recorder.outcome('next_root'), recorder.events)

    def test_detects_prefetch_mistaken_for_business_consumption(self):
        # ``id`` is a header. Reading it must not be counted as consuming the
        # child array, even when the bytes of the child were already prefetched.
        for path in READ_PATHS:
            with self.subTest(path=path.name):
                recorder, source = run_cut_scenario(NEXT_ROOT_PAYLOAD, path, 'header_then_next')
                self.assertEqual('ok', recorder.outcome('header_before_next'), recorder.events)
                # The child was never handed to the application at this point.
                self.assertNotIn('child', recorder.names())
                self.assertNotIn('child_first', recorder.names())

                # On the prefetch path the child's bytes are already in the
                # tokenizer, so a "bytes read == business consumed" classifier
                # would wrongly claim the child was consumed. Make that
                # difference explicit and assert it is not what happened.
                if path.name == 'rust-prefetch':
                    self.assertEqual(len(NEXT_ROOT_PAYLOAD), source.consumed)
                    self.assertNotIn('child', recorder.names())
