import json
from copy import deepcopy

from frictionless import CompatibilityAudit, CompatibilityContract, Package


def test_compatibility_contract_loads_both_sides():
    contract = CompatibilityContract.from_descriptor(
        "data/compatibility/equipment-maintenance.json"
    )
    assert contract.old.package
    assert contract.new.package
    assert contract.directions == ["old-write/new-read", "new-write/old-read"]
    assert contract.records_digest()


def test_compatibility_does_not_mutate_descriptors():
    source = "data/compatibility/equipment-maintenance.json"
    with open(source, encoding="utf-8") as file:
        before = json.load(file)
    contract = CompatibilityContract.from_descriptor(source)
    old_descriptor = deepcopy(contract.old.descriptor)
    new_descriptor = deepcopy(contract.new.descriptor)

    audit = CompatibilityAudit(contract=contract)
    report = audit.run()

    assert contract.old.descriptor == old_descriptor
    assert contract.new.descriptor == new_descriptor
    assert report.inputs["descriptorsUnchanged"] is True
    assert report.to_descriptor()["inputs"]["records"]
    with open(source, encoding="utf-8") as file:
        assert json.load(file) == before


def test_compatibility_old_write_new_read_missing_word_changes_key():
    report = CompatibilityAudit.from_descriptor(
        "data/compatibility/equipment-maintenance.json"
    ).run()
    direction = _direction(report, "old-write/new-read")

    changed = _item(direction, "changed", "equipment", "eq-9999-missing-word")
    assert changed["valueChanges"] == [
        {
            "field": "device_code",
            "kind": "value-became-missing",
            "baseline": "9999",
            "observed": None,
        }
    ]
    assert changed["original"]["values"]["device_code"] == "9999"
    assert changed["read"]["device_code"] is None

    linked = _item(direction, "changed", "maintenance", "mt-1-linked-to-missing-word")
    assert linked["relationshipChanges"][0]["baseline"]["state"] == "linked"
    assert linked["relationshipChanges"][0]["observed"]["state"] == "unassociated"


def test_compatibility_boundary_and_out_of_range_values():
    report = CompatibilityAudit.from_descriptor(
        "data/compatibility/equipment-maintenance.json"
    ).run()
    direction = _direction(report, "old-write/new-read")

    boundary = _item(direction, "compatible", "maintenance", "mt-2-new-max-boundary")
    assert boundary["read"]["hours"] == 8

    rejected = _item(
        direction, "rejected", "maintenance", "mt-3-old-max-outside-new-range"
    )
    assert rejected["reason"] == "row-error"
    assert rejected["rowErrors"][0]["type"] == "constraint-error"
    assert rejected["rowErrors"][0]["note"] == 'constraint "maximum" is "8"'
    assert rejected["wire"]["cells"] == ["3", "9998", "12"]
    assert rejected["original"]["values"]["hours"] == "12"


def test_compatibility_new_write_old_read_absent_column_is_evidence():
    report = CompatibilityAudit.from_descriptor(
        "data/compatibility/equipment-maintenance.json"
    ).run()
    direction = _direction(report, "new-write/old-read")

    rejected = _item(
        direction, "rejected", "maintenance", "mt-1-linked-to-missing-word"
    )
    assert rejected["reason"] == "header-error"
    assert rejected["headerErrors"][0]["type"] == "extra-label"
    assert rejected["absentFromRecord"] == ["notes"]
    assert rejected["writeChanges"] == [
        {
            "field": "device_code",
            "kind": "wire-value-rewritten",
            "original": "9999",
            "wire": "",
        },
        {
            "field": "notes",
            "kind": "absent-field-materialized",
            "original": "<absent>",
            "wire": "",
        },
    ]


def test_compatibility_new_write_rewrites_a_valid_key_on_the_wire():
    # This is distinct from the read-side missingValues effect: with `9999`
    # declared missing, writing a still-valid "9999" (e.g. copied from an old
    # reader that kept it) emits the blank token, so even a new->new round
    # trip silently loses the key.
    report = CompatibilityAudit.from_descriptor(
        "data/compatibility/equipment-maintenance.json"
    ).run()
    direction = _direction(report, "new-write/old-read")
    rejected = _item(
        direction, "rejected", "maintenance", "mt-1-linked-to-missing-word"
    )
    assert rejected["writeChanges"][0] == {
        "field": "device_code",
        "kind": "wire-value-rewritten",
        "original": "9999",
        "wire": "",
    }
    assert rejected["wire"]["cells"][1] == ""


def test_compatibility_silent_link_loss_is_not_compatible():
    report = CompatibilityAudit.from_descriptor(
        "data/compatibility/equipment-maintenance-silent.json"
    ).run()
    assert report.status == "changed"
    direction = _direction(report, "old-write/new-read")
    assert direction["status"] == "changed"
    assert direction["summary"]["compatible"] == 1
    assert direction["summary"]["changed"] == 2
    assert direction["summary"]["not-certifiable"] == 1
    # Every row still reads: no header or row error in the silent sample.
    for item in (
        direction["changed"]
        + direction["compatible"]
        + direction["not-certifiable"]
    ):
        assert item["headerErrors"] == []
        assert item["rowErrors"] == []
    # ... but the relation is reported as lost, not certified.
    assert direction["certified"]["certifiable"] is False


def test_compatibility_custom_constraint_outside_domain_is_not_certifiable():
    report = CompatibilityAudit.from_descriptor(
        "data/compatibility/equipment-maintenance.json"
    ).run()
    direction = _direction(report, "old-write/new-read")
    item = _item(
        direction, "not-certifiable", "equipment", "eq-9997-outside-domain"
    )
    assert item["uncoveredConstraints"][0]["constraint"] == "externalAssetRegistry"
    assert direction["certified"]["certifiable"] is False
    assert direction["certified"]["uncoveredConstraints"]
    # The custom constraint applies to every equipment record, including the
    # ones whose values otherwise survive.
    preserved = _item(direction, "not-certifiable", "equipment", "eq-1000-original-valid")
    assert preserved["valueChanges"] == []
    assert preserved["uncoveredConstraints"][0]["constraint"] == (
        "externalAssetRegistry"
    )


def test_compatibility_report_replays_with_the_same_inputs():
    source = "data/compatibility/equipment-maintenance.json"
    first = CompatibilityAudit.from_descriptor(source).run().to_descriptor()
    second = CompatibilityAudit.from_descriptor(source).run().to_descriptor()
    assert first["directions"] == second["directions"]
    assert first["inputs"]["records"] == second["inputs"]["records"]
    assert first["inputs"]["descriptorsUnchanged"] is True


def test_compatibility_descriptor_is_not_published():
    # Auditing only reads the descriptors and raw records; the audit report
    # is not a descriptor and must not be importable as one.
    report = CompatibilityAudit.from_descriptor(
        "data/compatibility/equipment-maintenance-silent.json"
    ).run()
    descriptor = report.to_descriptor()
    assert "directions" in descriptor
    assert "inputs" in descriptor
    assert Package.validate_descriptor(descriptor) is not None


def test_compatibility_covered_custom_constraint_is_certified():
    descriptor = {
        "name": "covered",
        "old": {
            "package": {
                "resources": [
                    {"name": "t", "schema": {"fields": [{"name": "a", "type": "string"}]}}
                ]
            }
        },
        "new": {
            "package": {
                "resources": [
                    {"name": "t", "schema": {"fields": [{"name": "a", "type": "string"}]}}
                ]
            }
        },
        "directions": ["old-write/new-read"],
        "records": {"t": [{"id": "r1", "values": {"a": "x"}}]},
        "customConstraints": [
            {
                "table": "t",
                "field": "a",
                "constraint": "registryLookup",
                "custom": True,
                "covered": True,
                "values": ["x"],
            }
        ],
    }
    report = CompatibilityAudit.from_descriptor(descriptor).run()
    assert report.status == "compatible"
    direction = _direction(report, "old-write/new-read")
    assert direction["certified"]["certifiable"] is True
    assert direction["certified"]["uncoveredConstraints"] == []


def test_compatibility_schema_only_contract():
    descriptor = {
        "name": "schema-only",
        "old": {
            "package": {
                "resources": [
                    {"name": "t", "schema": {"fields": [{"name": "a", "type": "string"}]}}
                ]
            }
        },
        "new": {
            "package": {
                "resources": [
                    {"name": "t", "schema": {"fields": [{"name": "a", "type": "string"}]}}
                ]
            }
        },
        "directions": ["old-write/new-read"],
        "records": {"t": [{"id": "r1", "values": {"a": "x"}}]},
    }
    report = CompatibilityAudit.from_descriptor(descriptor).run()
    assert report.status == "compatible"
    direction = _direction(report, "old-write/new-read")
    assert direction["summary"]["compatible"] == 1
    assert direction["descriptorErrors"] == {"write": [], "read": []}
    assert direction["certified"]["certifiable"] is True


def test_compatibility_same_version_directions():
    with open(
        "data/compatibility/equipment-maintenance-silent.json", encoding="utf-8"
    ) as file:
        descriptor = json.load(file)
    descriptor["directions"] = [
        "old-write/new-read",
        "new-write/old-read",
        "old-write/old-read",
        "new-write/new-read",
    ]
    # A custom constraint outside the supplied domain applies to every
    # direction: it would make the same-version baselines not certifiable too.
    descriptor["customConstraints"] = []
    report = CompatibilityAudit.from_descriptor(descriptor).run()

    old_to_old = _direction(report, "old-write/old-read")
    assert old_to_old["status"] == "compatible"
    assert old_to_old["summary"] == {
        "rejected": 0,
        "changed": 0,
        "compatible": 4,
        "not-certifiable": 0,
    }

    # The baseline is always the old descriptor's reading: a new-to-new
    # round trip is self-consistent but still changes the old key semantics
    # for the 9999 record and its relation. The added nullable column is
    # recorded as field evidence, not as a value change.
    new_to_new = _direction(report, "new-write/new-read")
    assert new_to_new["status"] == "changed"
    assert new_to_new["summary"] == {
        "rejected": 0,
        "changed": 2,
        "compatible": 2,
        "not-certifiable": 0,
    }
    added = [
        item
        for item in new_to_new["changed"] + new_to_new["compatible"]
        if item["record"] == "mt-2-original-valid"
    ][0]
    assert added["valueChanges"] == []
    assert added["fieldEvidence"] == [
        {
            "field": "notes",
            "kind": "field-added",
            "baseline": "<absent>",
            "observed": None,
        }
    ]


def test_compatibility_direction_aliases():
    with open(
        "data/compatibility/equipment-maintenance-silent.json", encoding="utf-8"
    ) as file:
        descriptor = json.load(file)
    descriptor["directions"] = ["old->new"]
    contract = CompatibilityContract(descriptor)
    assert contract.directions == ["old-write/new-read"]


def _direction(report, name):
    for direction in report.to_descriptor()["directions"]:
        if direction["direction"] == name:
            return direction
    raise AssertionError(f'direction "{name}" not found')


def _item(direction, outcome, table, record):
    for item in direction[outcome]:
        if item["table"] == table and item["record"] == record:
            return item
    raise AssertionError(f'{outcome} item "{table}.{record}" not found')
