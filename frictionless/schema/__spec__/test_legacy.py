import pytest

from frictionless import FrictionlessException, Schema
from frictionless.schema import missing_values as missing_values_module
from frictionless.schema.legacy import LEGACY_LABELS_KEY, LegacyExporter

V1_PROFILE = "https://datapackage.org/profiles/1.0/tableschema.json"
V2_PROFILE = "https://datapackage.org/profiles/2.0/tableschema.json"


def v2_descriptor():
    # The reading column inherits the schema-level labeled sentinels (9999 =
    # not measured, 9998 = downtime, both without a measured value) and is
    # required. The device column overrides the schema missing values with a
    # scope that does not contain 9999, so the device number 9999 is a real,
    # measured value.
    return {
        "$schema": V2_PROFILE,
        "fields": [
            {"name": "reading", "type": "integer", "constraints": {"required": True}},
            {"name": "device", "type": "integer", "missingValues": [""]},
        ],
        "missingValues": [
            {"value": "9999", "label": "NOT_MEASURED"},
            {"value": "9998", "label": "DOWNTIME"},
        ],
    }


def contains_object_entries(node):
    if isinstance(node, dict):
        if set(node) >= {"value", "label"}:
            return True
        return any(contains_object_entries(value) for value in node.values())
    if isinstance(node, list):
        return any(contains_object_entries(item) for item in node)
    return False


# Descriptor export


def test_legacy_export_writes_plain_string_missing_values():
    schema = Schema.from_descriptor(v2_descriptor())
    descriptor = LegacyExporter(schema=schema).to_descriptor()

    assert "$schema" not in descriptor
    assert descriptor["missingValues"] == ["9999", "9998"]
    assert not contains_object_entries(descriptor)
    # the reasons a v1 reader cannot store survive as an ignored side-car
    assert descriptor[LEGACY_LABELS_KEY] == {
        "9999": "NOT_MEASURED",
        "9998": "DOWNTIME",
    }
    # the field-level override is exported as plain strings too
    assert descriptor["fields"][1]["missingValues"] == [""]


def test_legacy_export_is_accepted_by_a_v1_reader():
    schema = Schema.from_descriptor(v2_descriptor())
    descriptor = LegacyExporter(schema=schema).to_descriptor()

    report = Schema.validate_descriptor({**descriptor, "$schema": V1_PROFILE})
    assert report.valid
    assert report.flatten(["note"]) == []


def test_legacy_export_field_scoped_labels_go_to_field_sidecar():
    descriptor = {
        "$schema": V2_PROFILE,
        "fields": [
            {
                "name": "reading",
                "type": "integer",
                "missingValues": [
                    {"value": "9999", "label": "NOT_MEASURED"},
                    {"value": "9998", "label": "DOWNTIME"},
                ],
            }
        ],
    }
    schema = Schema.from_descriptor(descriptor)
    exported = LegacyExporter(schema=schema).to_descriptor()

    field = exported["fields"][0]
    assert field["missingValues"] == ["9999", "9998"]
    assert field[LEGACY_LABELS_KEY] == {
        "9999": "NOT_MEASURED",
        "9998": "DOWNTIME",
    }
    assert LEGACY_LABELS_KEY not in exported
    assert not contains_object_entries(exported)


def test_legacy_export_without_labels_has_no_sidecar():
    descriptor = {
        "fields": [{"name": "name", "type": "string"}],
        "missingValues": ["", "NA"],
    }
    schema = Schema.from_descriptor(descriptor)
    exported = LegacyExporter(schema=schema).to_descriptor()

    assert exported["missingValues"] == ["", "NA"]
    assert LEGACY_LABELS_KEY not in exported


# Data export and the true/missing distinction


def test_legacy_rows_keep_real_values_and_write_back_missing_tokens():
    schema = Schema.from_descriptor(v2_descriptor())
    exporter = LegacyExporter(schema=schema)
    rows = exporter.write_rows([["9999", "9999"], ["9998", "12"], ["42", "7"]])

    # missing inputs are written as their declared token, real values as strings
    assert [row.cells for row in rows] == [
        ["9999", "9999"],
        ["9998", "12"],
        ["42", "7"],
    ]


def test_legacy_rows_trace_each_missing_reason_per_source_row():
    schema = Schema.from_descriptor(v2_descriptor())
    rows = LegacyExporter(schema=schema).write_rows([["9999", "9999"], ["9998", "12"]])

    assert [cell.to_descriptor() for cell in rows[0].missing_cells] == [
        {
            "fieldName": "reading",
            "fieldNumber": 1,
            "cell": "9999",
            "reason": "NOT_MEASURED",
        }
    ]
    assert [cell.to_descriptor() for cell in rows[1].missing_cells] == [
        {
            "fieldName": "reading",
            "fieldNumber": 1,
            "cell": "9998",
            "reason": "DOWNTIME",
        }
    ]
    # the device number 9999 is a real value: not recorded as missing
    assert rows[0].missing_cells[0].field_name == "reading"


def test_legacy_required_rejections_keep_the_original_reason():
    schema = Schema.from_descriptor(v2_descriptor())
    rows = LegacyExporter(schema=schema).write_rows([["9999", "9999"], ["9998", "12"]])

    assert [rejection.to_descriptor() for rejection in rows[0].rejections] == [
        {
            "rowNumber": 1,
            "fieldName": "reading",
            "fieldNumber": 1,
            "cell": "9999",
            "note": 'constraint "required" is "True"',
            "reason": "NOT_MEASURED",
        }
    ]
    assert rows[1].rejections[0].reason == "DOWNTIME"
    # a non-required missing input is delivered, not rejected
    assert rows[0].rejections[0].field_name == "reading"
    assert len(rows[0].rejections) == 1


# Reopening under v1


def test_reopened_legacy_descriptor_keeps_missing_and_required_semantics():
    schema = Schema.from_descriptor(v2_descriptor())
    exported = LegacyExporter(schema=schema).to_descriptor()
    reopened = Schema.from_descriptor({**exported, "$schema": V1_PROFILE})

    for source in (["9999", "9999"], ["9998", "12"], ["42", "7"]):
        source_cells, source_notes = schema.read_cells(source)
        legacy_cells, legacy_notes = reopened.read_cells(source)
        assert legacy_cells == source_cells
        assert legacy_notes == source_notes

    # the device number 9999 is still a valid integer after reopening
    assert reopened.read_cells(["9999", "9999"])[0] == [None, 9999]


def test_delivered_descriptor_reopens_as_v1_without_a_schema_declaration():
    # the delivered descriptor carries no `$schema`: an absent `$schema` is
    # itself an explicit v1, so a legacy repository reopens it unchanged
    schema = Schema.from_descriptor(v2_descriptor())
    exported = LegacyExporter(schema=schema).to_descriptor()
    assert "$schema" not in exported

    reopened = Schema.from_descriptor(exported)
    assert reopened._datapackage_version == "v1"
    assert reopened.read_cells(["9999", "9999"])[0] == [None, 9999]
    rows = LegacyExporter(schema=reopened).write_rows([["9999", "9999"]])
    assert rows[0].missing_cells[0].reason == "NOT_MEASURED"
    assert rows[0].rejections[0].reason == "NOT_MEASURED"


def test_reopened_legacy_rows_still_resolve_reasons_from_the_sidecar():
    schema = Schema.from_descriptor(v2_descriptor())
    exported = LegacyExporter(schema=schema).to_descriptor()
    reopened = Schema.from_descriptor({**exported, "$schema": V1_PROFILE})

    rows = LegacyExporter(schema=reopened).write_rows([["9999", "9999"], ["9998", "12"]])
    assert rows[0].missing_cells[0].reason == "NOT_MEASURED"
    assert rows[1].missing_cells[0].reason == "DOWNTIME"
    assert rows[0].rejections[0].reason == "NOT_MEASURED"
    assert rows[1].rejections[0].reason == "DOWNTIME"


def test_reopened_field_scoped_sidecar_keeps_reasons():
    descriptor = {
        "$schema": V2_PROFILE,
        "fields": [
            {
                "name": "reading",
                "type": "integer",
                "missingValues": [
                    {"value": "9999", "label": "NOT_MEASURED"},
                    {"value": "9998", "label": "DOWNTIME"},
                ],
            }
        ],
    }
    exported = LegacyExporter(schema=Schema.from_descriptor(descriptor)).to_descriptor()
    reopened = Schema.from_descriptor({**exported, "$schema": V1_PROFILE})

    rows = LegacyExporter(schema=reopened).write_rows([["9999"], ["9998"]])
    assert rows[0].missing_cells[0].reason == "NOT_MEASURED"
    assert rows[1].missing_cells[0].reason == "DOWNTIME"


# Configuration conflicts


def test_extra_legacy_token_that_swallows_a_real_value_is_a_conflict():
    schema = Schema.from_descriptor(v2_descriptor())
    exporter = LegacyExporter(
        schema=schema, field_missing_values={"device": ["", "9999"]}
    )

    with pytest.raises(FrictionlessException) as excinfo:
        exporter.to_descriptor()
    notes = [error.note for error in excinfo.value.reasons]
    assert notes == [
        'field "device": legacy missing value "9999" is not a missing value '
        "in the source descriptor and would turn a valid value into missing"
    ]
    # no deliverable data either
    with pytest.raises(FrictionlessException):
        exporter.write_row(["9999", "9999"], row_number=1)


def test_dropped_legacy_token_is_a_conflict():
    schema = Schema.from_descriptor(v2_descriptor())
    # dropping 9998 from the schema-level legacy words would resurrect downtime
    exporter = LegacyExporter(schema=schema, missing_values=["9999"])

    with pytest.raises(FrictionlessException) as excinfo:
        exporter.to_descriptor()
    notes = [error.note for error in excinfo.value.reasons]
    assert notes == [
        'schema: missing value "9998" would be dropped from the legacy '
        "descriptor and turn a documented missing value into a real one"
    ]


def test_lossless_override_is_accepted():
    schema = Schema.from_descriptor(v2_descriptor())
    # same token set, different order: the recognized cells do not change
    exporter = LegacyExporter(schema=schema, missing_values=["9998", "9999"])
    descriptor = exporter.to_descriptor()

    assert descriptor["missingValues"] == ["9998", "9999"]
    assert descriptor[LEGACY_LABELS_KEY] == {
        "9999": "NOT_MEASURED",
        "9998": "DOWNTIME",
    }


def test_overriding_a_field_that_inherits_schema_words_is_rejected():
    schema = Schema.from_descriptor(v2_descriptor())
    # "reading" declares no field-level missingValues: it inherits the schema
    exporter = LegacyExporter(schema=schema, field_missing_values={"reading": ["9999"]})

    with pytest.raises(FrictionlessException) as excinfo:
        exporter.to_descriptor()
    assert [error.note for error in excinfo.value.reasons] == [
        'field "reading": it inherits the schema "missingValues" and cannot '
        "be overridden separately"
    ]


def test_conflicting_configuration_delivers_no_descriptor():
    schema = Schema.from_descriptor(v2_descriptor())
    exporter = LegacyExporter(
        schema=schema, field_missing_values={"device": ["", "9999"]}
    )
    with pytest.raises(FrictionlessException):
        exporter.to_descriptor()
    # resolving the conflict afterwards (without the bad override) works
    clean = LegacyExporter(schema=schema)
    assert clean.to_descriptor()["fields"][1]["missingValues"] == [""]


# Pure helpers


def test_legacy_sidecar_keeps_only_labeled_tokens_in_order():
    assert missing_values_module.legacy_sidecar(
        ["9999", "9998", ""], {"9999": "NOT_MEASURED", "9998": "DOWNTIME"}
    ) == {"9999": "NOT_MEASURED", "9998": "DOWNTIME"}
    assert missing_values_module.legacy_sidecar(["", "NA"], {}) == {}


def test_legacy_conflict_notes_detects_extra_and_dropped_tokens():
    notes = missing_values_module.legacy_conflict_notes(
        "schema", ["9999", "9998"], ["9999", "7"]
    )
    assert notes == [
        'schema: legacy missing value "7" is not a missing value in the source '
        "descriptor and would turn a valid value into missing",
        'schema: missing value "9998" would be dropped from the legacy descriptor '
        "and turn a documented missing value into a real one",
    ]


def test_legacy_conflict_notes_silent_for_the_same_token_set():
    assert (
        missing_values_module.legacy_conflict_notes(
            "schema", ["9999", "9998"], ["9998", "9999"]
        )
        == []
    )
