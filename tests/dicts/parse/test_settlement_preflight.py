import unittest
from datetime import date, datetime, timezone

from benedict import benedict
from benedict.dicts.parse import ParseDict
from benedict.settlement import (
    SETTLEMENT_UNSET,
    AmbiguousSettlementTime,
    SettlementCalendar,
    SettlementIssueCode,
    SettlementPreflight,
    SettlementStatus,
    preflight_settlement,
)

# Europe/London falls back at 02:00 local time on 2026-10-25.
# The wall clock repeats 01:00-01:59 with offsets +01:00 and 00:00.
FALLBACK = "2026-10-25T01:30:00"

RECORD_OFFSET_ZERO = {
    "id": "rec-1",
    "version": 2,
    "details": [
        {
            "closing_time": FALLBACK,
            "offset": 0,
        }
    ],
}

RECORD_OFFSET_ABSENT = {
    "id": "rec-2",
    "version": 2,
    "details": [
        {
            "closing_time": FALLBACK,
        }
    ],
}

RECORD_BAD_OFFSET = {
    "id": "rec-3",
    "version": 2,
    "closing_time": FALLBACK,
    "details": [
        {
            "closing_time": FALLBACK,
            "offset": "not-a-number",
        }
    ],
}


class settlement_calendar_test_case(unittest.TestCase):
    def test_default_boundary_is_midnight(self) -> None:
        calendar = SettlementCalendar("Europe/London")
        self.assertEqual(calendar.boundary.time, (0, 0, 0, 0))
        self.assertEqual(
            calendar.settlement_date("2026-10-26T00:00:00"), date(2026, 10, 26)
        )
        self.assertEqual(
            calendar.settlement_date("2026-10-25T23:59:59"), date(2026, 10, 25)
        )

    def test_parses_mapping_and_time_string(self) -> None:
        calendar = SettlementCalendar.parse(
            {"time_zone": "Europe/London", "boundary": "06:00"}
        )
        self.assertEqual(calendar.time_zone, "Europe/London")
        self.assertEqual(
            calendar.settlement_date("2026-10-26T05:59:59"), date(2026, 10, 25)
        )
        self.assertEqual(
            calendar.settlement_date("2026-10-26T06:00:00"), date(2026, 10, 26)
        )

    def test_unknown_zone_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SettlementCalendar("Not/AZone").settlement_date("2026-01-01T00:00:00")

    def test_fallback_candidates_are_distinct(self) -> None:
        calendar = SettlementCalendar("Europe/London")
        candidates = calendar.candidates(FALLBACK)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            {candidate.offset_minutes for candidate in candidates}, {60, 0}
        )
        self.assertEqual(
            {candidate.utc_time for candidate in candidates},
            {
                datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
                datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc),
            },
        )

    def test_spring_gap_has_no_candidates(self) -> None:
        calendar = SettlementCalendar("Europe/London")
        self.assertEqual(calendar.candidates("2026-03-29T01:30:00"), [])

    def test_offset_pins_one_fallback_candidate(self) -> None:
        calendar = SettlementCalendar("Europe/London")
        early = calendar.candidates(FALLBACK, offset=60)
        late = calendar.candidates(FALLBACK, offset=0)
        self.assertEqual(len(early), 1)
        self.assertEqual(len(late), 1)
        self.assertEqual(early[0].offset_minutes, 60)
        self.assertEqual(late[0].offset_minutes, 0)
        self.assertEqual(
            early[0].utc_time, datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(
            late[0].utc_time, datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
        )

    def test_explicit_null_offset_has_no_candidates(self) -> None:
        calendar = SettlementCalendar("Europe/London")
        self.assertEqual(calendar.candidates(FALLBACK, offset=None), [])

    def test_offset_zero_minutes_is_valid(self) -> None:
        calendar = SettlementCalendar("Europe/London")
        candidates = calendar.candidates("2026-10-25T01:30:00", offset=0)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].offset_minutes, 0)

    def test_calendar_day_not_machine_timezone(self) -> None:
        calendar = SettlementCalendar("Pacific/Kiritimati")
        # +14:00: 2026-10-25T00:30Z is already 2026-10-25 14:30 locally.
        self.assertEqual(
            calendar.settlement_date("2026-10-25T00:30:00+00:00"),
            date(2026, 10, 25),
        )

    def test_non_ambiguous_boundary_splits_days(self) -> None:
        calendar = SettlementCalendar("Europe/London", boundary="06:00")
        self.assertEqual(
            calendar.settlement_date(FALLBACK, offset=60), date(2026, 10, 24)
        )
        self.assertEqual(
            calendar.settlement_date("2026-10-26T05:30:00"), date(2026, 10, 25)
        )
        self.assertEqual(
            calendar.settlement_date("2026-10-26T06:00:00"), date(2026, 10, 26)
        )

    def test_ambiguous_settlement_date_is_not_silent(self) -> None:
        calendar = SettlementCalendar("Europe/London")
        self.assertIsNone(calendar.settlement_date(FALLBACK))
        with self.assertRaises(AmbiguousSettlementTime):
            calendar.settlement_date(FALLBACK, strict=True)
        self.assertEqual(
            calendar.settlement_date(FALLBACK, offset=60), date(2026, 10, 25)
        )

    def test_fallback_boundary_splits_at_repeated_instant(self) -> None:
        # The boundary occurs twice.  The later occurrence starts the new
        # settlement day, so the earlier 02:30 belongs to the previous day.
        calendar = SettlementCalendar("Europe/London", boundary="01:30")
        early = calendar.settlement_date(FALLBACK, offset=60)
        late = calendar.settlement_date(FALLBACK, offset=0)
        self.assertEqual(early, date(2026, 10, 24))
        self.assertEqual(late, date(2026, 10, 25))


class settlement_preflight_test_case(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = SettlementCalendar("Europe/London")
        self.preflight = SettlementPreflight(self.calendar)

    def test_offset_zero_is_valid_and_storable(self) -> None:
        result = preflight_settlement(RECORD_OFFSET_ZERO, self.calendar)
        self.assertIs(result.status, SettlementStatus.RESOLVED)
        self.assertTrue(result.can_store)
        self.assertEqual(result.offset.value, 0)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].offset_minutes, 0)
        self.assertEqual(
            result.candidates[0].utc_time,
            datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(result.settlement_date, date(2026, 10, 25))

    def test_absent_offset_lists_two_candidates_and_evidence(self) -> None:
        result = preflight_settlement(RECORD_OFFSET_ABSENT, self.calendar)
        self.assertIs(result.status, SettlementStatus.AMBIGUOUS)
        self.assertFalse(result.can_store)
        self.assertTrue(result.needs_source_review)
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.candidate_settlement_dates, (date(2026, 10, 25),))
        self.assertIsNotNone(result.issue(SettlementIssueCode.MISSING_OFFSET))
        self.assertIsNotNone(result.issue(SettlementIssueCode.AMBIGUOUS_TIME))
        self.assertIn(
            "details[0].offset",
            {issue.path_string for issue in result.issues},
        )
        self.assertEqual(result.offset.path, ("details", 0, "offset"))
        self.assertEqual(result.offset.path, "details[0].offset")
        self.assertEqual(result.settlement_date, None)

    def test_explicit_null_offset_is_unconfirmed(self) -> None:
        result = preflight_settlement(
            {
                "id": "rec-2b",
                "version": 2,
                "details": [{"closing_time": FALLBACK, "offset": None}],
            },
            self.calendar,
        )
        self.assertIs(result.status, SettlementStatus.UNCONFIRMED)
        self.assertFalse(result.can_store)
        self.assertEqual(result.candidates, ())
        self.assertIsNotNone(result.issue(SettlementIssueCode.NULL_FIELD))

    def test_bad_offset_points_to_source_and_ignores_legacy_time(self) -> None:
        result = preflight_settlement(RECORD_BAD_OFFSET, self.calendar)
        self.assertIs(result.status, SettlementStatus.INVALID)
        self.assertFalse(result.can_store)
        issue = result.issue(SettlementIssueCode.INVALID_FIELD)
        self.assertIsNotNone(issue)
        self.assertEqual(issue.path_string, "details[0].offset")
        self.assertEqual(issue.path, ("details", 0, "offset"))
        self.assertEqual(result.offset.value, "not-a-number")
        # The valid outer legacy value must not be used instead.
        self.assertEqual(result.candidates, ())
        self.assertIsNone(result.settlement_date)
        # The detail closing_time is present; only the bad offset is at fault.
        self.assertEqual(
            {item.path_string for item in result.issues},
            {"details[0].offset"},
        )

    def test_version_one_uses_outer_closing_time(self) -> None:
        result = preflight_settlement(
            {"id": "legacy", "version": 1, "closing_time": "2026-10-20T12:00:00"},
            self.calendar,
        )
        self.assertIs(result.status, SettlementStatus.RESOLVED)
        self.assertEqual(result.closing_time.path, ("closing_time",))
        self.assertEqual(result.settlement_date, date(2026, 10, 20))

    def test_version_two_does_not_fall_back_to_outer_time(self) -> None:
        result = preflight_settlement(
            {
                "id": "v2-no-detail-time",
                "version": 2,
                "closing_time": "2026-10-20T12:00:00",
                "details": [{"offset": 120}],
            },
            self.calendar,
        )
        self.assertIs(result.status, SettlementStatus.INVALID)
        self.assertIsNotNone(result.issue(SettlementIssueCode.LEGACY_FIELD_ONLY))

    def test_unknown_version_is_invalid(self) -> None:
        result = preflight_settlement(
            {"id": "v9", "version": 9, "details": [{"closing_time": FALLBACK}]},
            self.calendar,
        )
        self.assertIs(result.status, SettlementStatus.INVALID)
        self.assertIsNotNone(result.issue(SettlementIssueCode.INVALID_VERSION))

    def test_record_id_and_unchanged_input(self) -> None:
        source = dict(RECORD_OFFSET_ZERO)
        result = preflight_settlement(source, self.calendar)
        self.assertEqual(result.record_id, "rec-1")
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(source, RECORD_OFFSET_ZERO)

    def test_recheck_only_corrected_record(self) -> None:
        original = self.preflight.record(RECORD_OFFSET_ABSENT)
        self.assertIs(original.result.status, SettlementStatus.AMBIGUOUS)

        corrected = original.recheck(offset=60)
        self.assertIs(corrected.status, SettlementStatus.RESOLVED)
        self.assertEqual(corrected.settlement_date, date(2026, 10, 25))
        self.assertEqual(corrected.candidates[0].offset_minutes, 60)

        # The original snapshot and the other records are unaffected.
        self.assertIs(original.result.status, SettlementStatus.AMBIGUOUS)
        self.assertEqual(original.record, RECORD_OFFSET_ABSENT)
        self.assertIs(
            preflight_settlement(RECORD_OFFSET_ZERO, self.calendar).status,
            SettlementStatus.RESOLVED,
        )
        self.assertIs(
            preflight_settlement(RECORD_BAD_OFFSET, self.calendar).status,
            SettlementStatus.INVALID,
        )

    def test_recheck_offset_zero_is_not_treated_as_absent(self) -> None:
        original = self.preflight.record(RECORD_OFFSET_ABSENT)
        corrected = original.recheck(offset=0)
        self.assertIs(corrected.status, SettlementStatus.RESOLVED)
        self.assertEqual(corrected.offset.value, 0)
        self.assertEqual(
            corrected.candidates[0].utc_time,
            datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc),
        )

    def test_recheck_changed_settlement_date(self) -> None:
        calendar = SettlementCalendar("Europe/London", boundary="01:30")
        preflight = SettlementPreflight(calendar)
        original = preflight.record(
            {
                "id": "boundary",
                "version": 2,
                "details": [{"closing_time": FALLBACK, "offset": 60}],
            }
        )
        self.assertEqual(original.settlement_date, date(2026, 10, 24))
        corrected = original.recheck(offset=0)
        self.assertEqual(corrected.settlement_date, date(2026, 10, 25))

    def test_supplied_offset_must_match_site_calendar(self) -> None:
        result = preflight_settlement(
            {
                "id": "wrong-offset",
                "version": 2,
                "details": [{"closing_time": "2026-10-20T12:00:00", "offset": 999}],
            },
            self.calendar,
        )
        self.assertIs(result.status, SettlementStatus.INVALID)
        self.assertIsNotNone(result.issue(SettlementIssueCode.INVALID_FIELD))

    def test_recheck_on_record_without_details_keeps_legacy_time(self) -> None:
        calendar = SettlementCalendar("Europe/London")
        record = SettlementPreflight(calendar).record(
            {"id": "no-details", "version": 2, "closing_time": "2026-10-25T01:30:00"}
        )
        corrected = record.recheck(offset=60)
        self.assertIs(corrected.status, SettlementStatus.INVALID)
        # The correction lands in details and does not replace the outer time.
        self.assertEqual(record.record["closing_time"], "2026-10-25T01:30:00")
        self.assertEqual(
            record.changed(offset=60).record["details"],
            [{"offset": 60}],
        )

    def test_missing_detail_time_reports_missing_path(self) -> None:
        result = preflight_settlement(
            {"id": "missing", "version": 2, "details": [{"offset": 0}]},
            self.calendar,
        )
        self.assertIs(result.status, SettlementStatus.INVALID)
        issue = result.issue(SettlementIssueCode.MISSING_FIELD)
        self.assertIsNotNone(issue)
        self.assertEqual(issue.path_string, "details[0].closing_time")


class parse_dict_settlement_test_case(unittest.TestCase):
    def test_preflight_settlement_method(self) -> None:
        d = benedict(
            {
                "records": {
                    "rec-2": RECORD_OFFSET_ABSENT,
                    "rec-1": RECORD_OFFSET_ZERO,
                }
            }
        )
        result = d.preflight_settlement(
            "records.rec-2", SettlementCalendar("Europe/London")
        )
        self.assertIs(result.status, SettlementStatus.AMBIGUOUS)
        self.assertEqual(result.record_id, "rec-2")
        self.assertEqual(d["records.rec-2"], RECORD_OFFSET_ABSENT)
        resolved = d.preflight_settlement(
            "records.rec-1", SettlementCalendar("Europe/London")
        )
        self.assertIs(resolved.status, SettlementStatus.RESOLVED)

    def test_settlement_record_method_keeps_source(self) -> None:
        d = benedict({"records": {"rec-2": RECORD_OFFSET_ABSENT}})
        record = d.get_settlement_record(
            "records.rec-2", SettlementCalendar("Europe/London")
        )
        corrected = record.recheck(offset=60)
        self.assertIs(corrected.status, SettlementStatus.RESOLVED)
        self.assertEqual(d["records.rec-2"], RECORD_OFFSET_ABSENT)
        self.assertEqual(record.record, RECORD_OFFSET_ABSENT)

    def test_settlement_date_uses_site_calendar(self) -> None:
        d = benedict({"closing_time": "2026-10-25T00:30:00"})
        calendar = SettlementCalendar("Pacific/Kiritimati")
        self.assertEqual(
            d.get_settlement_date("closing_time", calendar, offset=14 * 60),
            date(2026, 10, 25),
        )

    def test_get_datetime_and_get_int_keep_defaults(self) -> None:
        d = ParseDict({"a": "", "b": None, "c": "bad", "d": "12", "e": 0})
        self.assertEqual(d.get_int("a"), 0)
        self.assertEqual(d.get_int("b"), 0)
        self.assertEqual(d.get_int("c"), 0)
        self.assertEqual(d.get_int("d"), 12)
        self.assertEqual(d.get_int("e", 99), 0)
        self.assertIsNone(d.get_datetime("b"))

    def test_settlement_unset_is_exported(self) -> None:
        self.assertIsNotNone(SETTLEMENT_UNSET)

    def test_result_to_dict_is_json_serializable(self) -> None:
        import json

        result = preflight_settlement(
            RECORD_OFFSET_ZERO, SettlementCalendar("Europe/London")
        )
        payload = json.loads(json.dumps(result.to_dict()))
        self.assertEqual(payload["status"], "resolved")
        self.assertEqual(payload["settlement_date"], "2026-10-25")


if __name__ == "__main__":
    unittest.main()
