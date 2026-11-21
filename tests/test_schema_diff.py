"""End-to-end tests for the read-only Parquet footer schema diff."""

from __future__ import annotations

import json
import os
import struct
from argparse import ArgumentParser
from pathlib import Path

import pytest

from parquet_tools.commands.diff import build_policy, configure_parser
from parquet_tools.gen_py.parquet.ttypes import FieldRepetitionType, Type
from parquet_tools.schema_diff.engine import Policy, compare_schemas
from parquet_tools.schema_diff.footer import load_footer
from tests.fixtures.schema_footers import (
    element,
    list_three_level,
    list_two_level,
    root,
    write_footer,
)

BYTE_ARRAY = Type.BYTE_ARRAY
INT64 = Type.INT64
OPTIONAL = FieldRepetitionType.OPTIONAL
REQUIRED = FieldRepetitionType.REQUIRED
REPEATED = FieldRepetitionType.REPEATED


def _diff(tmp_path: Path, old_schema, new_schema, policy=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    old_path = write_footer(str(tmp_path / "old.parquet"), old_schema)
    new_path = write_footer(str(tmp_path / "new.parquet"), new_schema)
    old_doc = load_footer(old_path)
    new_doc = load_footer(new_path)
    report = compare_schemas(
        old_doc.tree,
        new_doc.tree,
        policy or Policy(allow_display_order=True),
    )
    return report, old_doc, new_doc


def _by_kind(report, kind):
    return [change for change in report["changes"] if change["kind"] == kind]


def _signature(report):
    return [
        (change["kind"], change["impact"], change["change_level"],
         change["old"]["physical_path"] if change["old"] else None,
         change["new"]["physical_path"] if change["new"] else None,
         change["identity"]["basis"])
        for change in report["changes"]
    ]


# ---------------------------------------------------------------------------
# Identity: same-named top-level and nested fields, stable ID rename, reorder
# ---------------------------------------------------------------------------


def _identity_schemas():
    old = [
        root("schema", 3),
        element("id", physical_type=INT64, repetition=OPTIONAL, field_id=1),
        element("list", physical_type=BYTE_ARRAY, repetition=OPTIONAL,
                converted_type=0, field_id=2),
        element("payload", repetition=OPTIONAL, num_children=2, field_id=3),
        element("list", physical_type=BYTE_ARRAY, repetition=OPTIONAL,
                converted_type=0, field_id=4),
        element("flag", physical_type=Type.BOOLEAN, repetition=OPTIONAL, field_id=5),
    ]
    new = [
        root("schema", 3),
        element("payload", repetition=OPTIONAL, num_children=2, field_id=3),
        element("list", physical_type=BYTE_ARRAY, repetition=OPTIONAL,
                converted_type=0, field_id=4),
        element("flag", physical_type=Type.BOOLEAN, repetition=OPTIONAL, field_id=5),
        element("identifier", physical_type=INT64, repetition=OPTIONAL, field_id=1),
        element("list", physical_type=BYTE_ARRAY, repetition=OPTIONAL,
                converted_type=0, field_id=2),
    ]
    return old, new


def test_rename_with_stable_id_and_reorder_are_allowed(tmp_path):
    old, new = _identity_schemas()
    report, _, _ = _diff(tmp_path, old, new, Policy(True, (1,)))

    assert report["read_only"] is True
    assert report["evidence_scope"] == "footer_only"
    assert report["data_pages_scanned"] is False
    assert report["pending_confirmations"] == []

    renames = _by_kind(report, "field_rename")
    assert len(renames) == 1
    rename = renames[0]
    assert rename["old"]["physical_path"] == "id"
    assert rename["new"]["physical_path"] == "identifier"
    assert rename["identity"]["basis"] == "field_id"
    assert rename["impact"] == "metadata_only"
    assert rename["requires_migration"] is False
    assert rename["compatibility"]["old_to_new"]["status"] == "compatible"
    assert rename["compatibility"]["new_to_old"]["status"] == "compatible"

    orders = _by_kind(report, "column_order_change")
    assert len(orders) == 1
    assert orders[0]["impact"] == "display_only"
    assert orders[0]["details"]["old_order"] == [
        "id", "list", "payload.list", "payload.flag"
    ]
    assert report["summary"]["directions"]["old_to_new"]["status"] == "compatible"
    assert report["summary"]["directions"]["new_to_old"]["status"] == "compatible"


def test_same_names_are_paired_by_field_id_not_by_name(tmp_path):
    old, new = _identity_schemas()
    report, _, _ = _diff(tmp_path, old, new, Policy(True, (1,)))

    pairs = {
        pair["old_path"]: pair for pair in report["identity_pairs"]
    }
    assert "list" in pairs and "payload.list" in pairs
    assert pairs["list"]["new_path"] == "list"
    assert pairs["payload.list"]["new_path"] == "payload.list"
    assert pairs["list"]["identity_basis"] == "field_id"
    assert pairs["payload.list"]["identity_basis"] == "field_id"
    assert pairs["list"]["old_field_id"] != pairs["payload.list"]["old_field_id"]
    # The top-level list and nested list share a name but are distinct fields.
    removed = _by_kind(report, "field_removed")
    added = _by_kind(report, "field_added")
    assert removed == [] and added == []


def change_path(change):
    if change["old"] is None or change["new"] is None:
        return (None, None)
    return (change["old"]["physical_path"], change["new"]["physical_path"])


def test_policy_off_marks_rename_and_order_incompatible(tmp_path):
    old, new = _identity_schemas()
    report, _, _ = _diff(tmp_path, old, new, Policy(False, ()))

    assert _by_kind(report, "field_rename")[0]["impact"] == "incompatible"
    assert _by_kind(report, "column_order_change")[0]["impact"] == "incompatible"
    assert (
        report["summary"]["directions"]["old_to_new"]["status"] == "incompatible"
    )
    assert (
        report["summary"]["directions"]["new_to_old"]["status"] == "incompatible"
    )


def test_allowed_rename_requires_matching_id_on_both_sides(tmp_path):
    old = [
        root("schema", 1),
        element("a", physical_type=INT64, repetition=OPTIONAL, field_id=7),
    ]
    new = [
        root("schema", 1),
        element("b", physical_type=INT64, repetition=OPTIONAL, field_id=8),
    ]
    report, _, _ = _diff(tmp_path, old, new, Policy(True, (7, 8)))
    kinds = {change["kind"] for change in report["changes"]}
    assert "field_rename" not in kinds
    assert "field_removed" in kinds and "field_added" in kinds


# ---------------------------------------------------------------------------
# Legal but different LIST wrappers
# ---------------------------------------------------------------------------


def test_three_level_and_two_level_lists_are_equivalent(tmp_path):
    old = [root("schema", 1)] + list_three_level(
        "tags", "element", element_type=BYTE_ARRAY,
        element_repetition=REQUIRED, list_field_id=10,
    )
    new = [root("schema", 1)] + list_two_level(
        "tags", "element", element_type=BYTE_ARRAY, list_field_id=10,
    )
    report, old_doc, new_doc = _diff(tmp_path, old, new)

    kinds = [change["kind"] for change in report["changes"]]
    assert kinds == ["list_wrapper_change"]
    change = report["changes"][0]
    assert change["impact"] == "display_only"
    assert change["change_level"] == "wrapper"
    assert change["details"]["old_layout"]["encoding"] == "three_level"
    assert change["details"]["new_layout"]["encoding"] == "two_level_primitive"
    assert change["details"]["old_physical_element_path"] == "tags.list.element"
    assert change["details"]["new_physical_element_path"] == "tags.element"
    assert change["details"]["logical_element_path"] == "tags[]"
    assert change["compatibility"]["old_to_new"]["status"] == "compatible"
    assert change["compatibility"]["new_to_old"]["status"] == "compatible"

    assert not _by_kind(report, "field_removed")
    assert not _by_kind(report, "field_added")
    assert not _by_kind(report, "list_element_nullability_change")
    assert not _by_kind(report, "physical_type_change")
    # Logical levels agree even though physical wrappers differ.
    old_leaf = old_doc.tree.leaves[0]
    new_leaf = new_doc.tree.leaves[0]
    assert old_leaf.path_text == new_leaf.path_text == "tags[]"
    assert old_doc.tree.nodes[0].list_element.repetition == "required"
    assert new_doc.tree.nodes[0].list_element.repetition == "required"


def test_wrapper_difference_is_not_element_change_even_with_optional_element(tmp_path):
    old = [root("schema", 1)] + list_three_level(
        "tags", "element", element_type=BYTE_ARRAY,
        element_repetition=OPTIONAL, list_field_id=10,
    )
    new = [root("schema", 1)] + list_three_level(
        "tags", "item", element_type=BYTE_ARRAY,
        element_repetition=OPTIONAL, list_field_id=10,
    )
    report, _, _ = _diff(tmp_path, old, new)
    kinds = [change["kind"] for change in report["changes"]]
    assert "list_element_name_change" in kinds
    rename = _by_kind(report, "list_element_name_change")[0]
    assert rename["impact"] == "display_only"
    assert rename["old"]["physical_path"] == "tags.list.element"
    assert rename["new"]["physical_path"] == "tags.list.item"
    assert rename["details"]["physical_name_only"] is True
    assert not _by_kind(report, "physical_type_change")
    assert not _by_kind(report, "list_element_nullability_change")


# ---------------------------------------------------------------------------
# List element nullability: optional -> required is a tightening
# ---------------------------------------------------------------------------


def test_list_element_optional_to_required_is_one_directional(tmp_path):
    old = [root("schema", 1)] + list_three_level(
        "tags", "element", element_type=BYTE_ARRAY,
        element_repetition=OPTIONAL, list_field_id=10,
    )
    new = [root("schema", 1)] + list_three_level(
        "tags", "element", element_type=BYTE_ARRAY,
        element_repetition=REQUIRED, list_field_id=10,
    )
    report, _, _ = _diff(tmp_path, old, new)

    changes = _by_kind(report, "list_element_nullability_change")
    assert len(changes) == 1
    change = changes[0]
    assert change["change_level"] == "element"
    assert change["impact"] == "incompatible"
    assert change["old"]["physical_path"] == "tags.list.element"
    assert change["new"]["physical_path"] == "tags.list.element"
    assert change["compatibility"]["old_to_new"]["status"] == "incompatible"
    assert change["compatibility"]["new_to_old"]["status"] == "compatible"
    assert change["value_verification"] == "recommended"
    assert report["summary"]["directions"]["old_to_new"]["requires_migration"] is True
    assert report["summary"]["directions"]["new_to_old"]["requires_migration"] is False
    assert report["summary"]["directions"]["new_to_old"]["status"] == "compatible"

    # An identical leaf dtype must not cancel the nullability tightening.
    assert change["old"]["physical_type"] == change["new"]["physical_type"]
    assert change["details"]["old_max_definition_level"] > \
        change["details"]["new_max_definition_level"]


def test_list_itself_optional_to_required_is_separate_from_element(tmp_path):
    old = [root("schema", 1)] + list_three_level(
        "tags", "element", element_type=BYTE_ARRAY,
        element_repetition=REQUIRED, list_field_id=10,
    )
    new = [root("schema", 1)] + list_three_level(
        "tags", "element", element_type=BYTE_ARRAY,
        element_repetition=REQUIRED, list_field_id=10, list_repetition=REQUIRED,
    )
    report, _, _ = _diff(tmp_path, old, new)
    changes = _by_kind(report, "repetition_change")
    assert len(changes) == 1
    change = changes[0]
    assert change["change_level"] == "field"
    assert change["old"]["physical_path"] == "tags"
    assert change["impact"] == "incompatible"
    assert change["compatibility"]["old_to_new"]["status"] == "incompatible"
    assert change["compatibility"]["new_to_old"]["status"] == "compatible"
    assert not _by_kind(report, "list_element_nullability_change")


# ---------------------------------------------------------------------------
# Repetition relocated to a different node
# ---------------------------------------------------------------------------


def test_repetition_relocation_is_not_cancelled_by_leaf_dtype(tmp_path):
    old = [
        root("schema", 1),
        element("outer", repetition=REPEATED, num_children=2, field_id=20),
        element("a", physical_type=INT64, repetition=REQUIRED, field_id=21),
        element("b", physical_type=INT64, repetition=REQUIRED, field_id=22),
    ]
    new = [
        root("schema", 1),
        element("outer", repetition=OPTIONAL, num_children=2, field_id=20),
        element("a", physical_type=INT64, repetition=REPEATED, field_id=21),
        element("b", physical_type=INT64, repetition=REQUIRED, field_id=22),
    ]
    report, _, _ = _diff(tmp_path, old, new)

    relocations = _by_kind(report, "repetition_level_relocated")
    assert len(relocations) == 1
    relocation = relocations[0]
    assert relocation["old"]["physical_path"] == "outer.a"
    assert relocation["new"]["physical_path"] == "outer.a"
    assert relocation["old"]["physical_type"] == relocation["new"]["physical_type"]
    assert relocation["details"]["old_repeated_at"] == [
        {"role": "repeated", "logical_path": "outer", "physical_path": "outer"}
    ]
    assert relocation["details"]["new_repeated_at"] == [
        {"role": "repeated", "logical_path": "outer.a", "physical_path": "outer.a"}
    ]
    assert relocation["impact"] == "migration_required"
    assert relocation["compatibility"]["old_to_new"]["status"] == "incompatible"
    assert relocation["compatibility"]["new_to_old"]["status"] == "incompatible"
    assert report["summary"]["directions"]["old_to_new"]["status"] == "incompatible"


def test_column_insertion_is_not_an_order_change(tmp_path):
    old = [
        root("schema", 2),
        element("a", physical_type=INT64, repetition=OPTIONAL, field_id=1),
        element("c", physical_type=INT64, repetition=OPTIONAL, field_id=3),
    ]
    new = [
        root("schema", 3),
        element("a", physical_type=INT64, repetition=OPTIONAL, field_id=1),
        element("b", physical_type=INT64, repetition=OPTIONAL, field_id=2),
        element("c", physical_type=INT64, repetition=OPTIONAL, field_id=3),
    ]
    report, _, _ = _diff(tmp_path, old, new)
    assert not _by_kind(report, "column_order_change")
    assert len(_by_kind(report, "field_added")) == 1
    assert report["summary"]["directions"]["old_to_new"]["status"] == "compatible"


def test_list_wrapper_with_different_element_name_is_display_only(tmp_path):
    old = [root("schema", 1)] + list_three_level(
        "tags", "element", element_type=BYTE_ARRAY,
        element_repetition=REQUIRED, list_field_id=10,
    )
    new = [root("schema", 1)] + list_two_level(
        "tags", "item", element_type=BYTE_ARRAY, list_field_id=10,
    )
    report, _, _ = _diff(tmp_path, old, new)
    kinds = [change["kind"] for change in report["changes"]]
    assert kinds == ["list_wrapper_change", "list_element_name_change"]
    assert all(
        change["impact"] == "display_only" for change in report["changes"]
    )
    assert report["summary"]["directions"]["old_to_new"]["status"] == "compatible"
    assert report["summary"]["directions"]["new_to_old"]["status"] == "compatible"


def test_two_repeated_levels_relocated_with_same_rep_depth(tmp_path):
    old = [
        root("schema", 1),
        element("g", repetition=REPEATED, num_children=1, field_id=1),
        element("h", repetition=REPEATED, num_children=1, field_id=2),
        element("x", physical_type=INT64, repetition=REQUIRED, field_id=3),
    ]
    new = [
        root("schema", 1),
        element("g", repetition=REPEATED, num_children=1, field_id=1),
        element("h", repetition=OPTIONAL, num_children=1, field_id=2),
        element("x", physical_type=INT64, repetition=REPEATED, field_id=3),
    ]
    report, _, _ = _diff(tmp_path, old, new)
    relocation = _by_kind(report, "repetition_level_relocated")
    assert len(relocation) == 1
    assert relocation[0]["details"]["old_max_repetition_level"] == \
        relocation[0]["details"]["new_max_repetition_level"] == 2
    assert relocation[0]["details"]["old_repeated_at"] != \
        relocation[0]["details"]["new_repeated_at"]
    assert relocation[0]["impact"] == "migration_required"


def test_value_verification_is_scoped_and_explicit(tmp_path):
    old = [
        root("schema", 2),
        element("id", physical_type=INT64, repetition=OPTIONAL, field_id=1),
        element("payload", repetition=OPTIONAL, num_children=1, field_id=3),
        element("inner", physical_type=INT64, repetition=OPTIONAL, field_id=8),
    ]
    new = [
        root("schema", 2),
        element("identifier", physical_type=INT64, repetition=REQUIRED, field_id=1),
        element("payload", repetition=OPTIONAL, num_children=1, field_id=3),
        element("inner", physical_type=INT64, repetition=REQUIRED, field_id=8),
    ]
    report, _, _ = _diff(tmp_path, old, new, Policy(True, (1,)))
    checks = report["value_verification"]
    assert len(checks) == 2
    assert all(check["evidence_scope"] == "footer_only" for check in checks)
    assert all(
        check["value_verification"] == "may_resolve_violation"
        for check in checks
    )
    levels = {check["change_level"] for check in checks}
    assert levels == {"field", "inner_field"}
    assert report["summary"]["value_verification_count"] == 2


# ---------------------------------------------------------------------------
# Incomplete IDs and name collisions must stay pending
# ---------------------------------------------------------------------------


def _collision_schemas():
    old = [
        root("schema", 2),
        element("payload", repetition=OPTIONAL, num_children=2),
        element("value", physical_type=INT64, repetition=OPTIONAL),
        element("value", physical_type=BYTE_ARRAY, repetition=OPTIONAL),
        element("payload", repetition=OPTIONAL, num_children=1),
        element("value", physical_type=Type.BOOLEAN, repetition=OPTIONAL),
    ]
    new = [
        root("schema", 2),
        element("payload", repetition=OPTIONAL, num_children=2),
        element("value", physical_type=INT64, repetition=OPTIONAL),
        element("value", physical_type=BYTE_ARRAY, repetition=OPTIONAL),
        element("payload", repetition=OPTIONAL, num_children=1),
        element("value", physical_type=Type.DOUBLE, repetition=OPTIONAL),
    ]
    return old, new


def test_ambiguous_names_without_ids_stay_pending(tmp_path):
    old, new = _collision_schemas()
    report, _, _ = _diff(tmp_path, old, new)

    assert report["changes"] == []
    assert len(report["pending_confirmations"]) >= 1
    groups = report["pending_confirmations"]
    assert all(group["status"] == "pending_confirmation" for group in groups)
    assert any(
        group["kind"] in ("name_conflict", "sibling_name_conflict")
        for group in groups
    )
    for group in groups:
        assert len(group["old_candidates"]) >= 1
        assert len(group["new_candidates"]) >= 1
    assert report["summary"]["directions"]["old_to_new"]["status"] == "unverifiable"
    assert report["summary"]["directions"]["old_to_new"]["footer_can_decide"] is False


def test_duplicate_field_id_never_selects_a_candidate(tmp_path):
    old = [
        root("schema", 2),
        element("a", physical_type=INT64, repetition=OPTIONAL, field_id=9),
        element("b", physical_type=INT64, repetition=OPTIONAL, field_id=9),
    ]
    new = [
        root("schema", 2),
        element("a", physical_type=INT64, repetition=OPTIONAL, field_id=9),
        element("b", physical_type=INT64, repetition=OPTIONAL, field_id=9),
    ]
    report, _, _ = _diff(tmp_path, old, new)
    assert report["changes"] == []
    duplicate = [
        group for group in report["pending_confirmations"]
        if group["kind"] == "duplicate_field_id"
    ]
    assert len(duplicate) == 1
    assert duplicate[0]["status"] == "pending_confirmation"
    assert {candidate["path"] for candidate in duplicate[0]["old_candidates"]} == {
        "a", "b"
    }
    assert {candidate["path"] for candidate in duplicate[0]["new_candidates"]} == {
        "a", "b"
    }
    assert report["summary"]["directions"]["new_to_old"]["status"] == "unverifiable"


def test_identical_ambiguous_schema_is_not_pending(tmp_path):
    schema = [
        root("schema", 1),
        element("payload", repetition=OPTIONAL, num_children=2),
        element("value", physical_type=INT64, repetition=OPTIONAL),
        element("value", physical_type=BYTE_ARRAY, repetition=OPTIONAL),
    ]
    report, _, _ = _diff(tmp_path, schema, schema)
    assert report["changes"] == []
    assert report["pending_confirmations"] == []
    assert report["summary"]["directions"]["old_to_new"]["status"] == "compatible"


def test_ambiguous_schema_with_real_difference_is_pending(tmp_path):
    old = [
        root("schema", 1),
        element("payload", repetition=OPTIONAL, num_children=2),
        element("value", physical_type=INT64, repetition=OPTIONAL),
        element("value", physical_type=BYTE_ARRAY, repetition=OPTIONAL),
    ]
    new = [
        root("schema", 1),
        element("payload", repetition=OPTIONAL, num_children=2),
        element("value", physical_type=INT64, repetition=OPTIONAL),
        element("value", physical_type=INT64, repetition=OPTIONAL),
    ]
    report, _, _ = _diff(tmp_path, old, new)
    assert report["changes"] == []
    assert len(report["pending_confirmations"]) == 1
    assert report["pending_confirmations"][0]["kind"] == "sibling_name_conflict"
    assert report["summary"]["directions"]["old_to_new"]["status"] == "unverifiable"


# ---------------------------------------------------------------------------
# Determinism and read-only guarantees
# ---------------------------------------------------------------------------


def test_report_is_byte_stable_for_same_inputs_and_policy(tmp_path):
    old, new = _identity_schemas()
    first, _, _ = _diff(tmp_path / "run1", old, new, Policy(True, (1,)))
    second, _, _ = _diff(tmp_path / "run2", old, new, Policy(True, (1,)))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert json.dumps(first, sort_keys=True) == json.dumps(first, sort_keys=True)


def test_footer_loading_only_reads_footer_bytes(tmp_path):
    old, new = _identity_schemas()
    path = write_footer(str(tmp_path / "a.parquet"), old)
    document = load_footer(path)
    # The report carries the exact footer digest, not data-page content.
    assert document.footer_sha256
    assert document.footer_size > 0
    assert document.file_size == 4 + document.footer_size + 8
    with open(path, "rb") as handle:
        prefix = handle.read(4)
    assert prefix == b"PAR1"
    report, old_doc, new_doc = _diff(tmp_path, old, new)
    assert old_doc.footer_sha256 == document.footer_sha256
    assert report["data_pages_scanned"] is False
    assert report["read_only"] is True


def test_read_error_is_reported_without_touching_data(tmp_path):
    from parquet_tools.schema_diff.footer import FooterReadError, try_load_footer

    path = tmp_path / "not.parquet"
    path.write_bytes(b"not parquet")
    document, error = try_load_footer(str(path))
    assert document is None
    assert "PAR1" in error
