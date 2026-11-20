import json
import os

import pytest

from frictionless import FrictionlessException, relocate


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        file.write(text)


def build_sources(root):
    """Two unrelated packages, each with a business table and its own `codes`

    The left table's schema is a standalone descriptor referenced relatively.
    Each business table's foreign key points at the `codes` resource of its
    own package, although both resources are named `codes`.
    """
    left = os.path.join(root, "left")
    right = os.path.join(root, "right")
    write(
        os.path.join(left, "datapackage.json"),
        json.dumps(
            {
                "name": "left-biz",
                "title": "Left Business",
                "resources": [
                    {
                        "name": "orders",
                        "type": "table",
                        "path": "orders.csv",
                        "schema": "schemas/orders.schema.json",
                        "title": "Orders",
                        "x-business": "left-sales",
                    },
                    {
                        "name": "codes",
                        "type": "table",
                        "path": "codes-data.csv",
                        "schema": {
                            "fields": [
                                {"name": "code", "type": "string"},
                                {"name": "label", "type": "string"},
                            ],
                            "primaryKey": ["code"],
                        },
                        "title": "Left Codes",
                        "x-business": "left-dictionary",
                    },
                ],
            }
        ),
    )
    write(
        os.path.join(left, "schemas", "orders.schema.json"),
        json.dumps(
            {
                "fields": [
                    {"name": "id", "type": "integer"},
                    {"name": "code", "type": "string"},
                ],
                "primaryKey": ["id"],
                "foreignKeys": [
                    {
                        "fields": ["code"],
                        "reference": {"resource": "codes", "fields": ["code"]},
                    }
                ],
            }
        ),
    )
    write(os.path.join(left, "orders.csv"), "id,code\n1,L1\n2,L2\n")
    write(os.path.join(left, "codes-data.csv"), "code,label\nL1,left-one\nL2,left-two\n")

    write(
        os.path.join(right, "datapackage.json"),
        json.dumps(
            {
                "name": "right-biz",
                "title": "Right Business",
                "resources": [
                    {
                        "name": "sales",
                        "type": "table",
                        "path": "sales.csv",
                        "schema": {
                            "fields": [
                                {"name": "id", "type": "integer"},
                                {"name": "code", "type": "string"},
                            ],
                            "primaryKey": ["id"],
                            "foreignKeys": [
                                {
                                    "fields": ["code"],
                                    "reference": {
                                        "resource": "codes",
                                        "fields": ["code"],
                                    },
                                }
                            ],
                        },
                        "title": "Sales",
                        "x-business": "right-sales",
                    },
                    {
                        "name": "codes",
                        "type": "table",
                        "path": "codes-data.csv",
                        "schema": {
                            "fields": [
                                {"name": "code", "type": "string"},
                                {"name": "label", "type": "string"},
                            ],
                            "primaryKey": ["code"],
                        },
                        "title": "Right Codes",
                        "x-business": "right-dictionary",
                    },
                ],
            }
        ),
    )
    write(os.path.join(right, "sales.csv"), "id,code\n1,R1\n2,R2\n")
    write(
        os.path.join(right, "codes-data.csv"),
        "code,label\nR1,right-one\nR2,right-two\n",
    )
    return left, right


def build_selection(left, right):
    return {
        os.path.join(left, "datapackage.json"): ["orders"],
        os.path.join(right, "datapackage.json"): ["sales"],
    }


# Allowed bundle


def test_relocate_with_dependencies_keeps_dictionary_identity(tmpdir):
    left, right = build_sources(str(tmpdir))
    selection = build_selection(left, right)
    target = str(tmpdir.join("published"))

    result = relocate(target, sources=list(selection), selection=selection)

    # The reopened package is valid as a whole: each foreign key is checked
    # against the dictionary of its own source package
    assert result.valid
    assert result.package.basepath == target
    names = result.package.resource_names
    assert names == ["left-biz.codes", "orders", "right-biz.codes", "sales"]
    assert result.package.get_table_resource("orders").schema.foreign_keys == [
        {
            "fields": ["code"],
            "reference": {"resource": "left-biz.codes", "fields": ["code"]},
        }
    ]
    assert result.package.get_table_resource("sales").schema.foreign_keys == [
        {
            "fields": ["code"],
            "reference": {"resource": "right-biz.codes", "fields": ["code"]},
        }
    ]
    # Independent identities, correct data paths and business properties
    left_codes = result.package.get_table_resource("left-biz.codes")
    right_codes = result.package.get_table_resource("right-biz.codes")
    assert left_codes.title == "Left Codes"
    assert right_codes.title == "Right Codes"
    assert left_codes.custom["x-business"] == "left-dictionary"
    assert right_codes.custom["x-business"] == "right-dictionary"
    assert [row["label"] for row in left_codes.read_rows()] == ["left-one", "left-two"]
    assert [row["label"] for row in right_codes.read_rows()] == [
        "right-one",
        "right-two",
    ]
    assert left_codes.normpath != right_codes.normpath

    # The relative schema descriptor is kept relative, and its foreign key is
    # rewritten to the target identity
    orders = result.package.get_table_resource("orders")
    descriptor = orders.to_descriptor()
    assert descriptor["schema"] == "schemas/orders.schema.json"
    with open(os.path.join(target, "schemas", "orders.schema.json")) as file:
        schema = json.load(file)
    assert schema["foreignKeys"] == [
        {
            "fields": ["code"],
            "reference": {"resource": "left-biz.codes", "fields": ["code"]},
        }
    ]


def test_relocate_records_mapping_and_does_not_change_sources(tmpdir):
    left, right = build_sources(str(tmpdir))
    selection = build_selection(left, right)
    before = {}
    for base in (left, right):
        for root, _, files in os.walk(base):
            for name in files:
                path = os.path.join(root, name)
                with open(path, "rb") as file:
                    before[path] = file.read()

    result = relocate(
        str(tmpdir.join("published")), sources=list(selection), selection=selection
    )

    after = {}
    for base in (left, right):
        for root, _, files in os.walk(base):
            for name in files:
                path = os.path.join(root, name)
                with open(path, "rb") as file:
                    after[path] = file.read()
    assert before == after

    manifest = result.manifest
    assert [source["package"] for source in manifest["sources"]] == [
        "left-biz",
        "right-biz",
    ]
    mappings = {
        (item["resource"], item["target"]): item for item in manifest["resources"]
    }
    assert set(mappings) == {
        ("orders", "orders"),
        ("sales", "sales"),
        ("codes", "left-biz.codes"),
        ("codes", "right-biz.codes"),
    }
    orders = mappings[("orders", "orders")]
    assert orders["role"] == "selected"
    assert orders["identityRule"] == "preserved"
    assert orders["schema"] == "relative"
    assert orders["schemaSource"] == os.path.join(left, "schemas", "orders.schema.json")
    assert orders["schemaTarget"] == "schemas/orders.schema.json"
    assert orders["sourcePath"] == os.path.join(left, "orders.csv")
    assert orders["targetPath"] == "data/orders.csv"
    assert orders["properties"] == {"title": "Orders", "x-business": "left-sales"}
    assert orders["foreignKeys"] == [
        {
            "fields": ["code"],
            "reference": {"resource": "left-biz.codes", "fields": ["code"]},
        }
    ]
    left_codes = mappings[("codes", "left-biz.codes")]
    assert left_codes["role"] == "dependency"
    assert left_codes["identityRule"] == "package-qualified"
    assert left_codes["properties"]["title"] == "Left Codes"

    relations = {
        (item["resource"], item["reference"]["target"]): item
        for item in manifest["relations"]
    }
    assert relations[("orders", "left-biz.codes")]["resolvedInTarget"] is True
    assert relations[("sales", "right-biz.codes")]["resolvedInTarget"] is True
    assert manifest["validation"]["valid"] is True
    assert {
        item["name"]: item["valid"] for item in manifest["validation"]["resources"]
    } == {
        "left-biz.codes": True,
        "orders": True,
        "right-biz.codes": True,
        "sales": True,
    }


def test_relocate_explicit_target_names(tmpdir):
    left, right = build_sources(str(tmpdir))
    selection = build_selection(left, right)
    names = {
        f"{os.path.join(left, 'datapackage.json')}::codes": "left.dictionary",
        f"{os.path.join(right, 'datapackage.json')}::codes": "right.dictionary",
    }
    result = relocate(
        str(tmpdir.join("published")),
        sources=list(selection),
        selection=selection,
        names=names,
    )
    assert result.valid
    assert result.package.resource_names == [
        "left.dictionary",
        "orders",
        "right.dictionary",
        "sales",
    ]
    by_name = {item["target"]: item for item in result.manifest["resources"]}
    assert by_name["left.dictionary"]["identityRule"] == "explicit"


def test_relocate_single_source(tmpdir):
    left, _ = build_sources(str(tmpdir))
    result = relocate(
        str(tmpdir.join("published")),
        sources=[os.path.join(left, "datapackage.json")],
        resources=["orders"],
    )
    assert result.valid
    assert result.package.resource_names == ["codes", "orders"]


# Refusals


@pytest.mark.parametrize("exclude_dependency", [True, False])
def test_relocate_missing_dependency_is_refused(tmpdir, exclude_dependency):
    left, right = build_sources(str(tmpdir))
    selection = build_selection(left, right)
    target = str(tmpdir.join("published"))
    options = {}
    if exclude_dependency:
        options["exclude"] = [f"{os.path.join(left, 'datapackage.json')}::codes"]
    else:
        options["include_dependencies"] = False

    with pytest.raises(FrictionlessException) as excinfo:
        relocate(target, sources=list(selection), selection=selection, **options)

    error = excinfo.value.error
    assert error.type == "relocation-error"
    # The relation is named: the selected orders table cannot complete its
    # foreign key to its own package's codes
    assert '"orders"' in error.note
    assert '"codes"' in error.note
    assert "foreign key" in error.note
    # No half-published package is left behind
    assert not os.path.exists(target)


def test_relocate_excluded_dictionary_is_not_masked_by_other_package(tmpdir):
    left, right = build_sources(str(tmpdir))
    selection = build_selection(left, right)
    target = str(tmpdir.join("published"))

    # The right `sales` table still needs the right dictionary, although the
    # left dictionary is present in the target under a qualified name
    with pytest.raises(FrictionlessException) as excinfo:
        relocate(
            target,
            sources=list(selection),
            selection=selection,
            exclude=[f"{os.path.join(right, 'datapackage.json')}::codes"],
        )
    error = excinfo.value.error
    assert '"sales"' in error.note
    assert '"codes"' in error.note
    assert "foreign key" in error.note
    assert not os.path.exists(target)


def test_relocate_invalid_table_from_source_is_not_published(tmpdir):
    left, right = build_sources(str(tmpdir))
    # A row now violates the foreign key in its own source package
    write(os.path.join(right, "sales.csv"), "id,code\n1,UNKNOWN\n")
    selection = build_selection(left, right)
    target = str(tmpdir.join("published"))
    with pytest.raises(FrictionlessException) as excinfo:
        relocate(target, sources=list(selection), selection=selection)
    assert excinfo.value.error.type == "relocation-error"
    assert "foreign-key" in excinfo.value.error.note
    assert not os.path.exists(target)


def test_relocate_rejects_duplicate_explicit_names(tmpdir):
    left, right = build_sources(str(tmpdir))
    selection = build_selection(left, right)
    names = {
        f"{os.path.join(left, 'datapackage.json')}::codes": "dictionary",
        f"{os.path.join(right, 'datapackage.json')}::codes": "dictionary",
    }
    with pytest.raises(FrictionlessException) as excinfo:
        relocate(
            str(tmpdir.join("published")),
            sources=list(selection),
            selection=selection,
            names=names,
        )
    assert "requested more than once" in excinfo.value.error.note


def test_relocate_rejects_non_empty_target(tmpdir):
    left, right = build_sources(str(tmpdir))
    selection = build_selection(left, right)
    target = tmpdir.join("published")
    write(str(target.join("keep.txt")), "keep")
    with pytest.raises(FrictionlessException) as excinfo:
        relocate(str(target), sources=list(selection), selection=selection)
    assert "not empty" in excinfo.value.error.note
    assert target.join("keep.txt").check()


# Shared material


def build_shared_sources(root):
    shared = os.path.join(root, "codes.csv")
    write(shared, "code,label\nC1,common-one\nC2,common-two\n")
    descriptors = {}
    for side, table in [("left", "orders"), ("right", "sales")]:
        directory = os.path.join(root, side)
        write(
            os.path.join(directory, "datapackage.json"),
            json.dumps(
                {
                    "name": f"{side}-shared",
                    "resources": [
                        {
                            "name": table,
                            "type": "table",
                            "path": f"{table}.csv",
                            "schema": {
                                "fields": [
                                    {"name": "id", "type": "integer"},
                                    {"name": "code", "type": "string"},
                                ],
                                "primaryKey": ["id"],
                                "foreignKeys": [
                                    {
                                        "fields": ["code"],
                                        "reference": {
                                            "resource": "codes",
                                            "fields": ["code"],
                                        },
                                    }
                                ],
                            },
                            "title": table.title(),
                            "x-business": side,
                        },
                        {
                            "name": "codes",
                            "type": "table",
                            "path": "codes.csv",
                            "schema": {
                                "fields": [
                                    {"name": "code", "type": "string"},
                                    {"name": "label", "type": "string"},
                                ],
                                "primaryKey": ["code"],
                            },
                            "title": f"{side.title()} Codes",
                            "x-business": f"{side}-dictionary",
                        },
                    ],
                }
            ),
        )
        write(
            os.path.join(directory, f"{table}.csv"),
            "id,code\n1,C1\n2,C2\n",
        )
        os.symlink(shared, os.path.join(directory, "codes.csv"))
        descriptors[side] = os.path.join(directory, "datapackage.json")
    return descriptors


def test_relocate_shared_material_reused_with_distinct_descriptors(tmpdir):
    descriptor = build_shared_sources(str(tmpdir))
    selection = {
        descriptor["left"]: ["orders"],
        descriptor["right"]: ["sales"],
    }
    result = relocate(
        str(tmpdir.join("published")), sources=list(selection), selection=selection
    )

    assert result.valid
    mappings = {item["target"]: item for item in result.manifest["resources"]}
    assert set(mappings) == {"orders", "sales", "left-shared.codes", "right-shared.codes"}
    left_codes = mappings["left-shared.codes"]
    right_codes = mappings["right-shared.codes"]
    assert left_codes["material"] == "copied"
    assert right_codes["material"] == "reused"
    assert left_codes["targetPath"] == right_codes["targetPath"]
    assert left_codes["sourcePath"] != right_codes["sourcePath"]
    assert left_codes["dataSha256"] == right_codes["dataSha256"]
    assert left_codes["properties"]["title"] == "Left Codes"
    assert right_codes["properties"]["title"] == "Right Codes"
    assert left_codes["properties"]["x-business"] == "left-dictionary"
    assert right_codes["properties"]["x-business"] == "right-dictionary"

    data_files = os.listdir(os.path.join(result.root, "data"))
    assert sorted(data_files) == [
        "left-shared.codes.csv",
        "orders.csv",
        "sales.csv",
    ]


def test_relocate_preserves_dialect_while_sharing_material(tmpdir):
    left, right = build_sources(str(tmpdir))
    path = os.path.join(left, "codes-data.csv")
    write(path, "code;label\nL1;left-one\nL2;left-two\n")
    write(os.path.join(left, "orders.csv"), "id;code\n1;L1\n2;L2\n")
    descriptor = os.path.join(left, "datapackage.json")
    with open(descriptor) as file:
        package = json.load(file)
    for resource in package["resources"]:
        resource["dialect"] = {"delimiter": ";"}
    write(descriptor, json.dumps(package))

    result = relocate(
        str(tmpdir.join("published")), sources=[descriptor], resources=["orders"]
    )
    assert result.valid
    assert result.package.get_table_resource("orders").dialect.to_descriptor() == {
        "csv": {"delimiter": ";"}
    }


def test_relocate_does_not_borrow_a_same_named_dictionary(tmpdir):
    left, right = build_sources(str(tmpdir))

    # The tables reference the right dictionary only; the left one is not
    # selected. If the implementation resolved `codes` by name alone, the
    # left dictionary would be published and used by both tables.
    result = relocate(
        str(tmpdir.join("published")),
        sources=[os.path.join(right, "datapackage.json")],
        resources=["sales"],
    )
    assert result.valid
    assert result.package.resource_names == ["codes", "sales"]
    sales = result.package.get_table_resource("sales")
    assert sales.schema.foreign_keys[0]["reference"]["resource"] == "codes"
    assert [
        row["label"] for row in result.package.get_table_resource("codes").read_rows()
    ] == [
        "right-one",
        "right-two",
    ]

    # Selecting the left table instead publishes the left dictionary, with
    # the left data, under the same plain name
    other = relocate(
        str(tmpdir.join("other")),
        sources=[os.path.join(left, "datapackage.json")],
        resources=["orders"],
    )
    assert other.valid
    assert [
        row["label"] for row in other.package.get_table_resource("codes").read_rows()
    ] == ["left-one", "left-two"]


def test_relocate_validates_each_table_against_its_own_dictionary(tmpdir):
    left, right = build_sources(str(tmpdir))
    # Break only the left dictionary: the right table must stay valid, which
    # proves each business table is checked against its own package's codes.
    write(os.path.join(left, "codes-data.csv"), "code,label\nL1,left-one\n")
    selection = {
        os.path.join(left, "datapackage.json"): ["orders"],
        os.path.join(right, "datapackage.json"): ["sales"],
    }
    target = str(tmpdir.join("published"))

    with pytest.raises(FrictionlessException) as excinfo:
        relocate(target, sources=list(selection), selection=selection)
    note = excinfo.value.error.note
    assert "orders" in note
    assert "sales" not in note

    # The same mismatch is visible without publishing: with validation off we
    # can inspect both tables' reports, and only the left one fails
    result = relocate(
        str(tmpdir.join("inspected")),
        sources=list(selection),
        selection=selection,
        validate=False,
    )
    orders = result.package.get_table_resource("orders").validate()
    sales = result.package.get_table_resource("sales").validate()
    assert not orders.valid
    assert orders.flatten(["type"]) == [["foreign-key"]]
    assert sales.valid
