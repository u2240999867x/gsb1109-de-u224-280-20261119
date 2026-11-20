from copy import deepcopy

from frictionless import Checklist, Package, Resource, checks

# The same work order batch is validated twice: first with the "locations"
# table missing from the package, then again after the table is supplied.
# Only the rows depending on the added table may change their conclusion.

WORK_ORDERS = {
    "name": "work-orders",
    "data": [
        ["site", "object_id", "object_kind"],
        ["S1", "17", "vehicle"],  # unknown kind
        ["S1", "17", "location"],  # target table is missing in the first round
        ["S1", "77", "equipment"],  # the same number only exists in the other table
        ["S1", "5", "sensor"],  # target table exists but is not readable
        ["S1", "17", "equipment"],  # valid
        ["S1", "18", "equipment"],  # not found in the equipment table
    ],
    "schema": {
        "fields": [
            {"name": "site", "type": "string"},
            {"name": "object_id", "type": "string"},
            {"name": "object_kind", "type": "string"},
        ],
    },
}

EQUIPMENT = {
    "name": "equipment",
    "data": [["site", "eq_id"], ["S1", 17]],
    "schema": {
        "fields": [
            {"name": "site", "type": "string"},
            {"name": "eq_id", "type": "integer"},
        ],
    },
}

# The target table exists in the package but cannot be read
SENSORS = {"name": "sensors", "path": "data/unreadable-sensors-table.csv"}

# Both tables may contain the same number 17 for the same site while using
# different column names and different type declarations
LOCATIONS = {
    "name": "locations",
    "data": [["site", "loc_code"], ["S1", "17"], ["S1", "77"]],
    "schema": {
        "fields": [
            {"name": "site", "type": "string"},
            {"name": "loc_code", "type": "string"},
        ],
    },
}

VARIANTS = {
    "equipment": {"resource": "equipment", "fields": ["site", "eq_id"]},
    "location": {"resource": "locations", "fields": ["site", "loc_code"]},
    "sensor": {"resource": "sensors", "fields": ["site", "sensor_id"]},
}


def make_checklist():
    return Checklist(
        checks=[
            checks.polymorphic_foreign_key(
                kind_field="object_kind",
                fields=["site", "object_id"],
                variants=deepcopy(VARIANTS),
            )
        ]
    )


def make_package(*, with_locations=False):
    resources = [deepcopy(WORK_ORDERS), deepcopy(EQUIPMENT), deepcopy(SENSORS)]
    package = Package({"resources": resources})
    if with_locations:
        package.add_resource(Resource(deepcopy(LOCATIONS)))
    return package


def flatten_task(report, name):
    task = next(task for task in report.tasks if task.name == name)
    return task, [[error.row_number, error.type] for error in task.errors]


# General


def test_polymorphic_foreign_key_missing_target_table():
    package = make_package()
    report = package.validate(make_checklist())
    task, errors = flatten_task(report, "work-orders")
    # Unknown kind, missing table, unreadable table and a key not found in
    # the target table are reported as distinguishable errors
    assert errors == [
        [2, "polymorphic-foreign-key-kind"],
        [3, "polymorphic-foreign-key-resource"],
        [4, "foreign-key"],
        [5, "polymorphic-foreign-key-lookup"],
        [7, "foreign-key"],
    ]
    notes = [error.note for error in task.errors]
    assert 'kind "vehicle" is not declared' in notes[0]
    assert '"locations"' in notes[1] and "not present in the package" in notes[1]
    assert 'not found in the lookup table "equipment"' in notes[2]
    assert '"sensors"' in notes[3] and "could not be read" in notes[3]
    assert 'not found in the lookup table "equipment"' in notes[4]


def test_polymorphic_foreign_key_unreadable_table_does_not_skip_others():
    package = make_package()
    report = package.validate(make_checklist())
    _, errors = flatten_task(report, "work-orders")
    # Rows of the readable equipment kind are still checked (rows 4, 6, 7)
    # although the sensors table of the sensor kind (row 5) is unreadable
    assert [4, "foreign-key"] in errors
    assert [7, "foreign-key"] in errors
    assert [error for error in errors if error[0] == 6] == []
    # The equipment table itself stays valid
    assert flatten_task(report, "equipment")[1] == []


def test_polymorphic_foreign_key_adding_missing_table():
    package = make_package()
    before = flatten_task(package.validate(make_checklist()), "work-orders")[1]
    package.add_resource(Resource(deepcopy(LOCATIONS)))
    after = flatten_task(package.validate(make_checklist()), "work-orders")[1]
    # Only the row depending on the added table changes its conclusion
    assert [3, "polymorphic-foreign-key-resource"] in before
    assert after == [error for error in before if error[0] != 3]
    assert after == [
        [2, "polymorphic-foreign-key-kind"],
        [4, "foreign-key"],
        [5, "polymorphic-foreign-key-lookup"],
        [7, "foreign-key"],
    ]


def test_polymorphic_foreign_key_kind_specific_lookup():
    # The number 77 only exists in the locations table; an equipment row
    # carrying it is invalid because each row is looked up only in the table
    # declared for its own kind
    package = make_package(with_locations=True)
    report = package.validate(make_checklist())
    task, errors = flatten_task(report, "work-orders")
    assert [4, "foreign-key"] in errors
    error = next(error for error in task.errors if error.row_number == 4)
    assert error.reference_name == "equipment"
    assert error.reference_field_names == ["site", "eq_id"]


def test_polymorphic_foreign_key_from_descriptor():
    checklist = Checklist.from_descriptor(
        {
            "checks": [
                {
                    "type": "polymorphic-foreign-key",
                    "kindField": "object_kind",
                    "fields": ["site", "object_id"],
                    "variants": VARIANTS,
                }
            ]
        }
    )
    report = make_package().validate(checklist)
    _, errors = flatten_task(report, "work-orders")
    assert errors == [
        [2, "polymorphic-foreign-key-kind"],
        [3, "polymorphic-foreign-key-resource"],
        [4, "foreign-key"],
        [5, "polymorphic-foreign-key-lookup"],
        [7, "foreign-key"],
    ]


def test_polymorphic_foreign_key_with_static_foreign_keys():
    work_orders = deepcopy(WORK_ORDERS)
    work_orders["schema"]["foreignKeys"] = [
        {"fields": "site", "reference": {"resource": "equipment", "fields": "site"}}
    ]
    work_orders["data"].append(["S9", "17", "equipment"])
    package = Package(
        {"resources": [work_orders, deepcopy(EQUIPMENT), deepcopy(SENSORS)]}
    )
    report = package.validate(make_checklist())
    task, _ = flatten_task(report, "work-orders")
    # The plain static foreign key and the polymorphic one both report
    # row numbers for the same row
    row_errors = [error for error in task.errors if error.row_number == 8]
    assert len(row_errors) == 2
    assert [error.type for error in row_errors] == ["foreign-key", "foreign-key"]
    notes = [error.note for error in row_errors]
    assert any('for "site": values "S9"' in note for note in notes)
    assert any('for "site, object_id": values "S9, 17"' in note for note in notes)
