"""Unit tests for the form model and three-way merge rules."""

import unittest

from SpiffWorkflow.inbound import (
    FormValidationError,
    FormVersion,
    FormVersionError,
    Line,
    changed_line_values,
    merge_by_line_no,
)
from SpiffWorkflow.inbound.model import merge_versions


def form(version, order, values, notes=''):
    lines = [
        Line(line_no=line_no, station=values[line_no][0],
             quantity=values[line_no][1], note=values[line_no][2])
        for line_no in order
    ]
    return FormVersion(version=version, form_id='F', lines=lines, notes=notes)


class FormModelTest(unittest.TestCase):

    def test_line_number_must_be_positive_integer(self):
        with self.assertRaises(FormValidationError):
            Line(line_no=0, station='S', quantity=1)
        with self.assertRaises(FormValidationError):
            Line(line_no=True, station='S', quantity=1)

    def test_quantity_must_be_positive_integer(self):
        with self.assertRaises(FormValidationError):
            Line(line_no=1, station='S', quantity=0)
        with self.assertRaises(FormValidationError):
            Line(line_no=1, station='S', quantity=1.5)

    def test_duplicate_line_numbers_rejected(self):
        with self.assertRaises(FormValidationError):
            FormVersion(version=1, form_id='F', lines=[
                Line(line_no=1, station='S', quantity=1),
                Line(line_no=1, station='S', quantity=2),
            ])

    def test_line_values_exclude_notes_and_order(self):
        a = form(1, [1, 2], {1: ('S1', 5, 'a'), 2: ('S2', 6, 'b')})
        b = form(2, [2, 1], {1: ('S1', 5, 'changed'), 2: ('S2', 6, 'b')})
        self.assertEqual(a.line_values(), b.line_values())

    def test_changed_line_values_are_keyed_by_line_number(self):
        a = form(1, [1, 2], {1: ('S1', 5, ''), 2: ('S2', 6, '')})
        b = form(2, [2, 1], {1: ('S1', 5, ''), 2: ('S3', 6, '')})
        changed = changed_line_values(a, b)
        self.assertEqual(list(changed), [2])
        self.assertEqual(changed[2]['new'], {'station': 'S3', 'quantity': 6})


class MergeTest(unittest.TestCase):

    def setUp(self):
        self.base = form(1, [1, 2], {1: ('S1', 5, 'a'), 2: ('S2', 6, 'b')}, notes='keep')

    def test_merge_by_line_number_uses_candidate_order(self):
        candidate = form(1, [2, 1], {1: ('S1', 5, 'a'), 2: ('S3', 6, 'b')}, notes='')
        merged = merge_by_line_no(self.base, candidate)
        self.assertEqual([line.line_no for line in merged.lines], [2, 1])
        self.assertEqual(merged.line(2).station, 'S3')
        self.assertEqual(merged.notes, '')

    def test_three_way_merges_independent_line_and_metadata_edits(self):
        server = form(2, [2, 1], {1: ('S1', 5, 'a'), 2: ('S2', 6, 'b')}, notes='')
        candidate = form(2, [1, 2], {1: ('S1', 5, 'a'), 2: ('S3', 6, 'b')}, notes='keep')
        merged = merge_versions(server, self.base, candidate)
        self.assertEqual([line.line_no for line in merged.lines], [2, 1], 'server order wins')
        self.assertEqual(merged.notes, '', 'server metadata wins')
        self.assertEqual(merged.line(2).station, 'S3', 'editor line value applies')

    def test_three_way_keeps_editor_metadata_when_editor_changed_it(self):
        server = form(2, [1, 2], {1: ('S1', 5, 'a'), 2: ('S3', 6, 'b')}, notes='keep')
        candidate = form(2, [2, 1], {1: ('S1', 5, 'a'), 2: ('S2', 6, 'b')}, notes='')
        merged = merge_versions(server, self.base, candidate)
        self.assertEqual([line.line_no for line in merged.lines], [2, 1])
        self.assertEqual(merged.notes, '')
        self.assertEqual(merged.line(2).station, 'S3', 'server line value survives')

    def test_three_way_conflicting_values_rejected(self):
        server = form(2, [1, 2], {1: ('S1', 5, 'a'), 2: ('S3', 6, 'b')}, notes='keep')
        candidate = form(2, [1, 2], {1: ('S1', 5, 'a'), 2: ('S4', 6, 'b')}, notes='keep')
        with self.assertRaises(FormVersionError):
            merge_versions(server, self.base, candidate)

    def test_different_line_numbers_rejected(self):
        candidate = form(1, [1], {1: ('S1', 5, 'a')})
        with self.assertRaises(FormVersionError):
            merge_by_line_no(self.base, candidate)


if __name__ == '__main__':
    unittest.main()
