import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path
from unittest import TestCase

import json_stream
from json_stream import streamable_dict, streamable_list
from json_stream.publish import (
    Credential,
    DuplicateKeyError,
    Prepared,
    PublicationError,
    Publisher,
    RequestConflictError,
    StaleBaseError,
    encode_streamable,
    publish,
    query,
)


class PublisherTestCase(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "result.json"
        self.publisher = Publisher(self.path)

    def publish(self, result, *args, **kwargs):
        return self.publisher.publish(result, *args, **kwargs)

    def read(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            return json.load(handle)


class TestEncodingParity(PublisherTestCase):
    def test_streaming_encoder_matches_stdlib_compact(self):
        @streamable_dict
        def value():
            yield "a", 1
            yield "b", streamable_list([1, "x", None, True])
            yield "c", {"nested": [1, 2]}
            yield 7, "int key"

        for options in ({}, {"ensure_ascii": False}):
            with self.subTest(options=options):
                buffer = io.StringIO()
                encode_streamable(value(), buffer, **options)
                expected = json.dumps(
                    {
                        "a": 1,
                        "b": [1, "x", None, True],
                        "c": {"nested": [1, 2]},
                        7: "int key",
                    },
                    **options,
                )
                self.assertEqual(expected, buffer.getvalue())

    def test_streaming_encoder_matches_stdlib_indent(self):
        @streamable_dict
        def value():
            yield "a", 1
            yield "b", [1, {"c": 2}]
            yield "empty", streamable_list([])

        for indent in (2, 4, "\t"):
            with self.subTest(indent=indent):
                buffer = io.StringIO()
                encode_streamable(value(), buffer, indent=indent)
                self.assertEqual(
                    json.dumps(
                        {"a": 1, "b": [1, {"c": 2}], "empty": []},
                        indent=indent,
                    ),
                    buffer.getvalue(),
                )

    def test_streaming_encoder_does_not_materialise(self):
        seen = []

        def items():
            for index in range(3):
                seen.append(index)
                yield index, index

        encode_streamable(streamable_dict(items()), io.StringIO())
        self.assertEqual([0, 1, 2], seen)

    def test_original_wrappers_still_work(self):
        @streamable_dict
        def value():
            yield "a list", streamable_list(range(4))

        self.assertEqual(
            '{"a list": [0, 1, 2, 3]}',
            json.dumps(value()),
        )


class TestDuplicateKeys(PublisherTestCase):
    def setUp(self):
        super().setUp()
        self.publish({"stable": True}, request_id="stable")
        self.before = self.path.read_bytes()
        self.credential = self.publisher.credential()

    def assert_published_untouched(self):
        self.assertEqual(self.before, self.path.read_bytes())
        self.assertEqual(self.credential, self.publisher.credential())
        self.assertEqual(1, self.publisher.query("stable").version)

    def test_int_and_string_key_collision_reports_object(self):
        @streamable_dict
        def outer():
            yield "wrapped", streamable_dict(iter([(7, "int"), ("7", "str")]))

        with self.assertRaises(DuplicateKeyError) as caught:
            self.publish(outer())
        self.assertEqual(("wrapped",), caught.exception.object_path)
        self.assertEqual("7", caught.exception.key)
        self.assertEqual({7, "7"}, set(caught.exception.keys))
        self.assert_published_untouched()

    def test_top_level_collision_reports_root(self):
        @streamable_dict
        def outer():
            yield 7, "int"
            yield "7", "str"

        with self.assertRaises(DuplicateKeyError) as caught:
            self.publish(outer())
        self.assertEqual((), caught.exception.object_path)
        self.assert_published_untouched()

    def test_nested_list_object_collision_reports_index_path(self):
        @streamable_dict
        def collision():
            yield "7", 1
            yield 7, 2

        @streamable_list
        def value():
            yield "safe"
            yield collision()

        with self.assertRaises(DuplicateKeyError) as caught:
            self.publish(value())
        self.assertEqual((1,), caught.exception.object_path)
        self.assert_published_untouched()

    def test_same_keys_in_different_objects_allowed(self):
        @streamable_list
        def value():
            yield streamable_dict(iter([(7, "a")]))
            yield streamable_dict(iter([("7", "b")]))

        receipt = self.publish(value(), request_id="distinct")
        self.assertEqual([{"7": "a"}, {"7": "b"}], self.read())
        self.assertEqual("distinct", receipt.request_id)

    def test_duplicate_key_in_late_tail_leaves_no_partial_file(self):
        def tail():
            for index in range(100):
                yield f"k{index}", index
            yield "dup", 1
            yield "dup", 2

        with self.assertRaises(DuplicateKeyError):
            self.publish(tail())
        self.assert_published_untouched()
        self.assertEqual([], list(self.publisher.pending_dir.iterdir()))
        self.assertEqual(
            [], [p for p in self.dir.iterdir() if "json-tmp" in p.name]
        )

    def test_partial_source_not_reread_after_failure(self):
        reads = []

        def source():
            reads.append("started")
            yield "a", 1
            yield "a", 2

        with self.assertRaises(DuplicateKeyError):
            self.publish(source())
        self.assertEqual(["started"], reads)


class TestPublicationAndCredentials(PublisherTestCase):
    def test_receiver_always_sees_complete_json(self):
        @streamable_list
        def slow():
            for index in range(50):
                yield {"index": index, "text": "x" * 100}
                time.sleep(0.001)

        stop = threading.Event()
        seen = []

        def reader():
            while not stop.is_set():
                try:
                    with open(self.path, "rb") as handle:
                        seen.append(handle.read())
                except FileNotFoundError:
                    pass

        self.publish({"before": True}, request_id="before")
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            time.sleep(0.01)
            self.publish(slow(), request_id="slow")
        finally:
            stop.set()
            thread.join()

        self.assertTrue(seen)
        for raw in seen:
            json.loads(raw)  # every observation is a whole document
        self.assertEqual(50, len(self.read()))

    def test_versions_and_tokens_advance(self):
        first = self.publish({"a": 1}, request_id="one")
        second = self.publish({"a": 2}, request_id="two")
        self.assertEqual(1, first.version)
        self.assertEqual(2, second.version)
        self.assertNotEqual(first.token, second.token)
        self.assertEqual(second.credential, self.publisher.credential())

    def test_identical_bytes_are_a_distinct_publication(self):
        first = self.publish({"same": [1, 2, 3]}, request_id="first")
        first_bytes = self.path.read_bytes()

        second = self.publish({"same": [1, 2, 3]}, request_id="second")
        self.assertEqual(first_bytes, self.path.read_bytes())
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.version + 1, second.version)
        self.assertNotEqual(first.token, second.token)
        self.assertFalse(first.credential.matches(second.credential))

        # The old credential must no longer be accepted even though the
        # bytes on disk are identical.
        with self.assertRaises(StaleBaseError) as caught:
            self.publish({"third": True}, base=first.credential)
        self.assertEqual(second.credential, caught.exception.actual)
        self.assertEqual(first_bytes, self.path.read_bytes())
        self.assertEqual(second.version, self.publisher.credential().version)

    def test_stale_writer_does_not_overwrite_newer_publication(self):
        version_one = self.publish({"v": 1}, request_id="v1")
        version_two = self.publish({"v": 2}, request_id="v2")

        prepared = self.publisher.prepare({"v": 3})
        with self.assertRaises(StaleBaseError) as caught:
            self.publisher.commit(
                prepared,
                request_id="late",
                base=version_one.credential,
            )
        self.assertEqual("stale_base", caught.exception.result.status)
        self.assertEqual(version_two.credential, caught.exception.actual)
        self.assertEqual(2, self.publisher.credential().version)
        self.assertEqual({"v": 2}, self.read())
        self.assertIsNone(self.publisher.query("late"))

        # The encoded result was not discarded and can be committed against
        # the fresh credential without the source being read again.
        receipt = self.publisher.commit(
            prepared,
            request_id="late",
            base=version_two.credential,
        )
        self.assertEqual(3, receipt.version)
        self.assertEqual({"v": 3}, self.read())
        prepared.discard()

    def test_staged_request_retried_after_stale_does_not_replay_source(self):
        first = self.publish({"v": 1}, request_id="v1")
        self.publish({"v": 2}, request_id="v2")
        reads = []

        def source():
            reads.append("read")
            yield "v", 3

        with self.assertRaises(StaleBaseError):
            self.publish(source(), request_id="slow", base=first.credential)
        self.assertEqual(["read"], reads)
        self.assertEqual({"v": 2}, self.read())

        receipt = self.publish(source(), request_id="slow", base=self.publisher.credential())
        self.assertEqual(3, receipt.version)
        self.assertEqual({"v": 3}, self.read())
        self.assertEqual(["read"], reads)
        self.assertEqual(
            [],
            list(self.publisher._stage_dir().iterdir())
            if self.publisher._stage_dir().exists()
            else [],
        )

    def test_paused_writer_is_stale_even_without_explicit_base(self):
        self.publish({"v": 1}, request_id="v1")
        gate = threading.Event()
        started = threading.Event()
        outcome = {}

        @streamable_list
        def slow():
            yield {"partial": True}
            started.set()
            gate.wait(timeout=10)
            yield {"partial": False}

        def late_writer():
            try:
                outcome["receipt"] = self.publish(slow(), request_id="late")
            except StaleBaseError as error:
                outcome["error"] = error

        thread = threading.Thread(target=late_writer)
        thread.start()
        try:
            self.assertTrue(started.wait(timeout=10))
            # The publisher at V2 starts from V1 too and wins.
            newer = self.publish({"v": 2}, request_id="v2")
        finally:
            gate.set()
            thread.join(timeout=10)

        self.assertIn("error", outcome)
        self.assertEqual(newer.credential, outcome["error"].actual)
        self.assertEqual({"v": 2}, self.read())
        self.assertIsNone(self.publisher.query("late"))
        self.assertEqual(2, self.publisher.credential().version)

    def test_explicit_fresh_base_overrides_stale_captured_base(self):
        first = self.publish({"v": 1}, request_id="v1")
        old = self.publisher.prepare({"v": 2})
        self.assertEqual(first.credential, old.base)
        newest = self.publish({"v": 3}, request_id="v3")

        with self.assertRaises(StaleBaseError):
            self.publisher.commit(old, request_id="paused")

        receipt = self.publisher.commit(
            old, request_id="paused", base=newest.credential
        )
        self.assertEqual(3, receipt.version)
        self.assertEqual({"v": 2}, self.read())

    def test_prepare_does_not_change_published_path(self):
        self.publish({"old": True}, request_id="old")
        before = self.path.read_bytes()
        credential = self.publisher.credential()

        prepared = self.publisher.prepare({"new": True})
        self.assertIsInstance(prepared, Prepared)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(credential, self.publisher.credential())
        self.assertIsNone(self.publisher.query("new"))
        prepared.discard()

    def test_prepare_does_not_block_other_publishers(self):
        gate = threading.Event()
        started = threading.Event()
        finished = threading.Event()

        def slow_source():
            started.set()
            gate.wait(timeout=10)
            yield "slow", True

        prepared_holder = {}

        def slow_writer():
            prepared_holder["prepared"] = self.publisher.prepare(slow_source())
            finished.set()

        thread = threading.Thread(target=slow_writer)
        thread.start()
        try:
            self.assertTrue(started.wait(timeout=10))
            receipt = self.publish({"fast": True}, request_id="fast")
            self.assertEqual(1, receipt.version)
        finally:
            gate.set()
            thread.join(timeout=10)

        self.assertTrue(finished.is_set())
        prepared_holder["prepared"].discard()


class TestRequestIdempotency(PublisherTestCase):
    def test_query_returns_published_receipt(self):
        receipt = self.publish({"a": 1}, request_id="req-9")
        found = query(self.path, "req-9")
        self.assertEqual(receipt, found)
        self.assertEqual("published", found.status)

    def test_unknown_request_returns_none(self):
        self.publish({"a": 1}, request_id="known")
        self.assertIsNone(query(self.path, "missing"))

    def test_retry_does_not_consume_source_again(self):
        publish(self.path, {"a": 1}, request_id="once")
        reads = []

        def source():
            reads.append(1)
            yield "a", 2

        receipt = publish(self.path, source(), request_id="once")
        self.assertEqual(1, receipt.version)
        self.assertEqual([], reads)
        self.assertEqual({"a": 1}, self.read())

    def test_retry_with_same_request_is_idempotent_even_with_new_bytes(self):
        self.publish({"a": 1}, request_id="same")
        before = self.path.read_bytes()
        credential = self.publisher.credential()
        receipt = self.publish({"a": 2}, request_id="same")
        self.assertEqual(1, receipt.version)
        self.assertEqual({"a": 1}, self.read())
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(credential, self.publisher.credential())

    def test_two_prepared_payloads_for_one_request_conflict(self):
        self.publish({"a": 1}, request_id="same")
        first = self.publisher.prepare({"a": 2})
        second = self.publisher.prepare({"a": 3})
        try:
            self.assertEqual(
                2, self.publisher.commit(first, request_id="fresh").version
            )
            with self.assertRaises(RequestConflictError):
                self.publisher.commit(second, request_id="fresh")
        finally:
            first.discard()
            second.discard()
        self.assertEqual({"a": 2}, self.read())
        self.assertEqual(2, self.publisher.credential().version)

    def test_request_does_not_cross_publication_paths(self):
        other = self.dir / "other.json"
        publish(self.path, {"a": 1}, request_id="shared")
        publish(other, {"b": 2}, request_id="shared")
        self.assertEqual({"a": 1}, self.read())
        with open(other, encoding="utf-8") as handle:
            self.assertEqual({"b": 2}, json.load(handle))


class TestBaselineVersion(PublisherTestCase):
    def test_preexisting_document_can_be_the_base_for_two_writers(self):
        self.path.write_text('{"release": "V1"}', encoding="utf-8")
        base = self.publisher.credential()
        self.assertEqual(0, base.version)

        # Writer A encodes against V1 but is slow to commit.
        prepared = self.publisher.prepare({"release": "A"})

        # Writer B also starts from V1 and publishes V2.
        version_two = self.publish({"release": "V2"}, request_id="B")
        self.assertEqual(1, version_two.version)
        self.assertEqual({"release": "V2"}, self.read())

        # The late writer is rejected by the baseline credential even though
        # it never saw a store record when it started.
        with self.assertRaises(StaleBaseError) as caught:
            self.publisher.commit(prepared, request_id="A", base=base)
        self.assertEqual(version_two.credential, caught.exception.actual)
        self.assertEqual({"release": "V2"}, self.read())
        self.assertEqual(1, self.publisher.credential().version)
        prepared.discard()

    def test_baseline_is_not_recorded_as_a_request(self):
        self.path.write_text('{"release": "V1"}', encoding="utf-8")
        self.publisher.credential()
        self.assertIsNone(self.publisher.query("anything"))
        self.assertEqual(
            {"release": "V1"},
            json.loads(self.path.read_text(encoding="utf-8")),
        )

    def test_baseline_does_not_hide_identical_republish(self):
        self.path.write_text('{"same": true}', encoding="utf-8")
        base = self.publisher.credential()
        receipt = self.publish({"same": True}, request_id="new")
        self.assertEqual(base.version + 1, receipt.version)
        self.assertFalse(base.matches(receipt.credential))
        with self.assertRaises(StaleBaseError):
            self.publish({"other": True}, base=base)


class TestCrashRecovery(PublisherTestCase):
    def _run_publisher(self, source, **env):
        script = textwrap.dedent(
            f"""
            import json
            from pathlib import Path
            from json_stream import streamable_dict
            from json_stream.publish import Publisher

            @streamable_dict
            def source():
                for key, value in {source!r}.items():
                    yield key, value

            Publisher({str(self.path)!r}).publish(
                source(), request_id="victim", base={{'version': 0, 'token': None}}
            )
            print("PUBLISHER FINISHED")
            """
        )
        environment = dict(os.environ)
        environment.update(env)
        return subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=environment,
            cwd=str(Path(__file__).resolve().parents[2]),
        )

    def test_prepare_after_crash_does_not_consume_source(self):
        crashed = self._run_publisher(
            {"already": True},
            JSON_STREAM_PUBLISH_FAILPOINT="after_replace",
        )
        self.assertEqual(99, crashed.returncode, crashed.stderr)

        reads = []

        def source():
            reads.append(1)
            yield "other", True

        prepared = self.publisher.prepare(source(), request_id="victim")
        self.assertEqual([], reads)
        receipt = self.publisher.commit(prepared, request_id="victim")
        self.assertEqual(1, receipt.version)
        self.assertEqual({"already": True}, self.read())
        self.assertEqual([], reads)

    def test_recovery_after_process_dies_after_rename(self):
        result = self._run_publisher(
            {"published": True, "items": [1, 2, 3]},
            JSON_STREAM_PUBLISH_FAILPOINT="after_replace",
        )
        self.assertEqual(99, result.returncode, result.stderr)
        self.assertNotIn("PUBLISHER FINISHED", result.stdout)

        # The published path already contains the complete new document, but
        # the receipt has not been recorded.  A different process can finish
        # the request with the original request id.
        self.assertEqual({"published": True, "items": [1, 2, 3]}, self.read())
        recovered = query(self.path, "victim")
        self.assertEqual("published", recovered.status)
        self.assertEqual(1, recovered.version)
        self.assertEqual("victim", recovered.request_id)

        # And a replay of the same request must not publish again.
        before = self.path.read_bytes()
        replay = publish(self.path, {"ignored": True}, request_id="victim")
        self.assertEqual(recovered, replay)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(1, self.publisher.credential().version)

    def test_recovery_after_process_dies_during_state_write(self):
        result = self._run_publisher(
            {"published": True},
            JSON_STREAM_PUBLISH_FAILPOINT="after_state_write",
        )
        self.assertEqual(99, result.returncode, result.stderr)
        self.assertEqual({"published": True}, self.read())

        first = query(self.path, "victim")
        self.assertEqual(1, first.version)
        # The pending journal entry has been removed and the state is durable.
        second = query(self.path, "victim")
        self.assertEqual(first, second)
        self.assertEqual([], list(self.publisher.pending_dir.iterdir()))

    def test_recovery_twice_is_stable(self):
        crashed = self._run_publisher(
            {"published": True},
            JSON_STREAM_PUBLISH_FAILPOINT="after_replace",
        )
        self.assertEqual(99, crashed.returncode, crashed.stderr)
        self.assertEqual({"published": True}, self.read())

        # A second process starts recovery, writes durable state, then dies
        # before removing the journal entry.
        second = self._run_publisher(
            {"ignored": True},
            JSON_STREAM_PUBLISH_FAILPOINT="after_recover",
        )
        self.assertEqual(99, second.returncode, second.stderr)

        first = query(self.path, "victim")
        self.assertEqual(1, first.version)
        self.assertEqual([], list(self.publisher.pending_dir.iterdir()))
        self.assertEqual(first, query(self.path, "victim"))
        self.assertEqual(1, self.publisher.credential().version)

    def _write_dead_journal(self, request_id, temp, **overrides):
        from json_stream.publish import _atomic_write_json

        self.publisher._ensure_sidecar()
        stat = temp.stat() if temp.exists() else None
        pending = {
            "schema": 1,
            "request_id": request_id,
            "version": 2,
            "token": "dead-token",
            "sha256": "0" * 64,
            "size": 4,
            "temp_path": str(temp),
            "target_dev": stat.st_dev if stat else 0,
            "target_ino": stat.st_ino if stat else 0,
            "created_at": time.time(),
        }
        pending.update(overrides)
        _atomic_write_json(self.publisher.pending_dir / "dead.json", pending)

    def test_crash_before_rename_keeps_old_publication(self):
        self.publish({"old": True}, request_id="old")
        before = self.path.read_bytes()
        credential = self.publisher.credential()

        # A publisher died before the rename.  It has no staging record, so
        # the encoded file was never reusable and is cleaned up.
        temp = self.dir / ".result.json.json-tmp-dead"
        temp.write_text('{"new": true}')
        self._write_dead_journal("dead", temp)

        self.publisher.recover()
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(credential, self.publisher.credential())
        self.assertIsNone(query(self.path, "dead"))
        self.assertFalse(temp.exists())

    def test_crash_before_rename_keeps_staged_file_reusable(self):
        self.publish({"old": True}, request_id="old")
        before = self.path.read_bytes()
        credential = self.publisher.credential()

        # A staged request encoded its source, wrote its intent record, then
        # died before the rename.  The staged bytes must survive so a retry
        # publishes them without reading the source again.
        temp = self.dir / ".result.json.json-tmp-staged"
        temp.write_text('{"new": true}')
        sha = __import__("hashlib").sha256(temp.read_bytes()).hexdigest()
        stage_path = self.publisher._write_stage(
            "dead", temp, sha, temp.stat().st_size
        )
        self._write_dead_journal("dead", temp, sha256=sha, size=temp.stat().st_size)

        self.publisher.recover()
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(credential, self.publisher.credential())
        self.assertTrue(temp.exists())
        self.assertTrue(stage_path.exists())

        # A later publisher holding a fresh credential can finish the request
        # without the source being replayed.
        reads = []

        def source():
            reads.append(1)
            yield "new", True

        receipt = self.publish(
            source(), request_id="dead", base=self.publisher.credential()
        )
        self.assertEqual(2, receipt.version)
        self.assertEqual({"new": True}, self.read())
        self.assertEqual([], reads)
        self.assertFalse(temp.exists())
        self.assertFalse(stage_path.exists())

    def test_recovery_does_not_rewrite_valid_delivery(self):
        first = self.publish({"a": 1}, request_id="one")
        before_stat = self.path.stat()
        time.sleep(0.01)
        self.publisher.recover()
        self.assertEqual(before_stat.st_ino, self.path.stat().st_ino)
        self.assertEqual(before_stat.st_mtime, self.path.stat().st_mtime)
        self.assertEqual(first, query(self.path, "one"))

    def test_state_file_is_beside_not_inside_json(self):
        self.publish({"a": 1}, request_id="one")
        self.assertTrue(self.publisher.state_path.exists())
        self.assertNotEqual(self.path, self.publisher.state_path)
        self.assertEqual(str(self.path) + ".jsonstream", str(self.publisher.sidecar_dir))
        with open(self.path, encoding="utf-8") as handle:
            json.load(handle)


class TestConcurrentPublishers(PublisherTestCase):
    def test_only_one_writer_wins_with_same_base(self):
        base = self.publish({"initial": True}, request_id="initial").credential
        results = []
        barrier = threading.Barrier(2)

        def writer(name):
            prepared = self.publisher.prepare({"writer": name})
            barrier.wait(timeout=10)
            try:
                results.append(
                    (
                        "ok",
                        self.publisher.commit(
                            prepared, request_id=name, base=base
                        ),
                    )
                )
            except StaleBaseError as error:
                results.append(("stale", error))
            finally:
                prepared.discard()

        threads = [
            threading.Thread(target=writer, args=(name,))
            for name in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(2, len(results))
        statuses = sorted(status for status, _ in results)
        self.assertEqual(["ok", "stale"], statuses)
        winner = [value for status, value in results if status == "ok"][0]
        self.assertEqual(2, winner.version)
        self.assertEqual({"writer": winner.request_id}, self.read())

    def test_many_sequential_requests_are_unique(self):
        seen = set()
        for index in range(20):
            receipt = self.publish(
                {"index": index}, request_id=f"req-{index}"
            )
            self.assertEqual(index + 1, receipt.version)
            self.assertNotIn(receipt.token, seen)
            seen.add(receipt.token)
        self.assertEqual({"index": 19}, self.read())
