"""Offline tests for the collection-rename change stream repro.

Run with::

    .venv/bin/python -m pytest repro/change_stream_rename -q

These tests need no MongoDB server and no network sockets beyond the in-process
socketpair used by the scripted responder.
"""

from __future__ import annotations

import unittest

import pytest

from repro.change_stream_rename import model
from repro.change_stream_rename.scenario import run_repro

pytestmark = pytest.mark.default


class TestCollectionRenameRepro(unittest.IsolatedAsyncioTestCase):
    report: object

    async def asyncSetUp(self) -> None:
        self.report = await run_repro()

    async def test_records_invalidate_then_recoveries(self) -> None:
        report = self.report
        assert report.invalidate_event["operationType"] == "invalidate"
        assert report.invalidate_event["ns"] == {"db": model.DB, "coll": model.OLD_COLLECTION}
        assert report.first_transition_resume_parameter == "resumeAfter"
        assert report.first_transition_resume_token == model.INVALIDATE_TOKEN

        resume_after_errors = [
            record
            for record in report.records
            if record.kind == "aggregate"
            and record.resume_parameter == "resumeAfter"
            and record.error_code == 280
        ]
        assert len(resume_after_errors) == 1

    async def test_pause_round_is_identical_except_for_delivered_event(self) -> None:
        report = self.report
        old, new = report.attempts

        assert old.batch_token == model.BATCH_TOKEN_P
        assert new.batch_token == model.BATCH_TOKEN_P
        assert old.cursor_alive_at_pause
        assert new.cursor_alive_at_pause
        assert old.seen_before_pause == []
        assert [doc["operationType"] for doc in new.seen_before_pause] == ["insert"]
        assert new.seen_before_pause[0]["ns"]["coll"] == model.NEW_COLLECTION

        for observation in report.pause_observations.values():
            assert observation["token_matches_empty_batch"]

        assert old.empty_getmore is not None
        assert new.empty_getmore is not None
        assert (
            old.empty_getmore.post_batch_resume_token == new.empty_getmore.post_batch_resume_token
        )
        assert old.empty_getmore.next_batch == []
        assert new.empty_getmore.next_batch == []
        assert old.empty_getmore.filter != new.empty_getmore.filter

    async def test_resume_parameter_depends_on_events_seen_before_empty_batch(self) -> None:
        report = self.report
        assert report.resume_tokens["old-filter"] == model.BATCH_TOKEN_P
        assert report.resume_tokens["new-filter"] == model.BATCH_TOKEN_P
        assert report.resume_parameters["old-filter"] == "startAfter"
        assert report.resume_parameters["new-filter"] == "resumeAfter"

    async def test_old_filter_loses_next_event_while_new_filter_delivers_it(self) -> None:
        report = self.report
        assert report.after_restore_events["old-filter"] is None
        delivered = report.after_restore_events["new-filter"]
        assert delivered is not None
        assert delivered["operationType"] == "insert"
        assert delivered["ns"]["coll"] == model.NEW_COLLECTION
        assert delivered["_id"] == model.SECOND_EVENT_TOKEN


if __name__ == "__main__":
    unittest.main()
