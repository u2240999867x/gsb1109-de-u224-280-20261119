"""Regression-style evidence tests for the two-vendor rollup diagnosis.

Run from the repository root:

    .venv/bin/python -m pytest investigation/tests -v

The tests assert observed engine behaviour only; they do not modify SpiffWorkflow
or the BPMN process definitions.
"""

import unittest

from investigation.diagnosis import harness as h
from investigation.diagnosis import scenarios as s


class MidpointEvidenceTest(unittest.TestCase):
    def test_public_data_identical_but_alias_topology_rebuilt(self):
        for case in h.CASES:
            with self.subTest(case=case):
                record = s.midpoint_evidence(case)
                self.assertTrue(record["public_data_identical"])
                self.assertTrue(record["lab_only_live_has_parent_child_alias"])
                self.assertFalse(record["lab_only_restored_has_parent_child_alias"])

    def test_midpoint_rows_match_the_swap_and_b_is_idle(self):
        record = s.midpoint_evidence("no_io")
        rows = record["live"]["public"]["vendor_a:a_park"]["data"]["total"]["rows"]
        self.assertEqual(
            rows,
            [
                {"vendor": "B", "count": 0},
                {"vendor": "A", "count": 11},
            ],
        )
        b_state = record["live"]["public"]["vendor_b:b_entry"]["state"]
        self.assertEqual(b_state, "READY")

    def test_archive_round_trip_keeps_every_public_state_and_value(self):
        live = h.start_to_midpoint("no_io")
        restored = h.archive_and_restore(live)
        self.assertEqual(
            h.public_snapshot(live),
            h.public_snapshot(restored),
        )


class SameOrderCompletionTest(unittest.TestCase):
    def test_live_b_edit_pollutes_a_before_either_returns(self):
        record = s.driven_run("no_io", False, ("a", "b"))
        self.assertEqual(s.a_row_count_before_return(record), 19)

    def test_restored_run_keeps_a_at_eleven_before_return(self):
        record = s.driven_run("no_io", True, ("a", "b"))
        self.assertEqual(s.a_row_count_before_return(record), 11)

    def test_live_and_restored_diverge_under_identical_operation_order(self):
        live = s.driven_run("no_io", False, ("a", "b"))
        restored = s.driven_run("no_io", True, ("a", "b"))
        self.assertEqual(live["final_report"]["counts"], [0, 19])
        self.assertEqual(restored["final_report"]["counts"], [0, 11])
        self.assertNotEqual(
            live["final_report"]["total_rows"],
            restored["final_report"]["total_rows"],
        )


class ReverseOrderCompletionTest(unittest.TestCase):
    def test_same_final_row_hides_different_stage_failures(self):
        live = s.driven_run("no_io", False, ("b", "a"))
        restored = s.driven_run("no_io", True, ("b", "a"))
        self.assertEqual(live["final_report"]["vendors"], ["B"])
        self.assertEqual(restored["final_report"]["vendors"], ["B"])
        self.assertEqual(
            live["final_report"]["total_rows"],
            restored["final_report"]["total_rows"],
        )
        self.assertEqual(s.a_row_count_before_return(live), 19)
        self.assertEqual(s.a_row_count_before_return(restored), 11)


class ReturnBoundaryContrastTest(unittest.TestCase):
    def test_no_io_deepcopies_while_total_output_keeps_row_alias(self):
        evidence = s.return_boundary_evidence()
        self.assertFalse(evidence["no_io"]["rows_object_shared_across_return"])
        self.assertTrue(evidence["io_total"]["rows_object_shared_across_return"])

    def test_total_only_output_preserves_unrelated_parent_variables(self):
        evidence = s.return_boundary_evidence()
        self.assertFalse(evidence["no_io"]["audit_tag_preserved"])
        self.assertTrue(evidence["io_total"]["audit_tag_preserved"])

    def test_output_only_declaration_does_not_remove_entry_side_aliasing(self):
        record = s.midpoint_evidence("io_total")
        self.assertTrue(record["lab_only_live_has_parent_child_alias"])
        live = s.driven_run("io_total", False, ("a", "b"))
        self.assertEqual(s.a_row_count_before_return(live), 19)


class MergeTruncationTest(unittest.TestCase):
    def test_shorter_incoming_list_trims_target_rows(self):
        evidence = s.merge_truncation_evidence()
        self.assertEqual(
            evidence["two_row_target_plus_one_row_incoming"],
            [{"vendor": "B", "count": 0}],
        )
        self.assertEqual(
            evidence["one_row_target_plus_two_row_incoming"],
            [
                {"vendor": "B", "count": 0},
                {"vendor": "A", "count": 11},
            ],
        )


if __name__ == "__main__":
    unittest.main()
