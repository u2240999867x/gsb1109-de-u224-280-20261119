from __future__ import annotations

import copy
from typing import Optional

import pytest

from frictionless import FrictionlessException, Package

Y_HEADER = "id,label\n"
Y_TOTAL_ROWS = 5000
Y_TRUNCATED_ROWS = 4500


def y_rows(start: int, stop: int) -> str:
    return "".join(
        f"{index},dictionary-entry-{index:06d}\n" for index in range(start, stop)
    )


Y_COMPLETE_MISSING_KEY = Y_HEADER + y_rows(1, Y_TOTAL_ROWS + 1)
Y_REPAIRED = Y_COMPLETE_MISSING_KEY + "99999,dictionary-entry-099999\n"
Y_TRUNCATED_CLEAN = Y_HEADER + y_rows(1, Y_TRUNCATED_ROWS + 1)
Y_TRUNCATED_MID_ROW = Y_TRUNCATED_CLEAN + "5001"


def y_late_broken() -> str:
    marker = b"4500,dictionary-entry-004500\n"
    data = Y_REPAIRED.encode("utf-8")
    index = data.index(marker) + len(marker)
    data = data[:index] + b"4501,bad-\xff\xfe\n" + data[index:]
    return data.decode("latin-1")


X_DATA = "id,label\n1,alpha\n2,beta\n"
ORDERS_X_DATA = "id,x_id,amount\n1,1,10\n2,9,20\n3,2,30\n"
ORDERS_Y_DATA = "id,y_id,amount\n1,1,10\n2,99999,20\n3,5000,30\n"
ORDERS_BOTH_DATA = "id,x_id,y_id,amount\n1,1,1,10\n2,9,1,20\n3,1,99999,30\n4,2,5000,40\n"
DETAIL_DATA = "id,qty\n1,2\n2,oops\n"


def dictionary_schema() -> dict:
    return {
        "fields": [
            {"name": "id", "type": "integer"},
            {"name": "label", "type": "string"},
        ],
        "primaryKey": ["id"],
    }


def foreign_key(field: str) -> dict:
    return {
        "fields": field,
        "reference": {
            "resource": "dict_x" if field == "x_id" else "dict_y",
            "fields": "id",
        },
    }


SCHEMAS = {
    "orders_x": {
        "fields": [
            {"name": "id", "type": "integer"},
            {"name": "x_id", "type": "integer"},
            {"name": "amount", "type": "integer"},
        ],
        "primaryKey": ["id"],
        "foreignKeys": [foreign_key("x_id")],
    },
    "orders_y": {
        "fields": [
            {"name": "id", "type": "integer"},
            {"name": "y_id", "type": "integer"},
            {"name": "amount", "type": "integer"},
        ],
        "primaryKey": ["id"],
        "foreignKeys": [foreign_key("y_id")],
    },
    "orders_both": {
        "fields": [
            {"name": "id", "type": "integer"},
            {"name": "x_id", "type": "integer"},
            {"name": "y_id", "type": "integer"},
            {"name": "amount", "type": "integer"},
        ],
        "primaryKey": ["id"],
        "foreignKeys": [foreign_key("x_id"), foreign_key("y_id")],
    },
    "dict_x": dictionary_schema(),
    "dict_y": dictionary_schema(),
    "detail": {
        "fields": [
            {"name": "id", "type": "integer"},
            {"name": "qty", "type": "integer"},
        ]
    },
}

PATHS = {
    "orders_x": "orders_x.csv",
    "orders_y": "orders_y.csv",
    "orders_both": "orders_both.csv",
    "dict_x": "dict_x.csv",
    "dict_y": "dict_y.csv",
    "detail": "detail.csv",
}

NORMAL_ORDER = [
    "orders_x",
    "orders_y",
    "orders_both",
    "dict_x",
    "dict_y",
    "detail",
]
REVERSED_ORDER = list(reversed(NORMAL_ORDER))


def write_fixture(basepath, y_content: Optional[str]) -> None:
    files = {
        "dict_x.csv": X_DATA,
        "orders_x.csv": ORDERS_X_DATA,
        "orders_y.csv": ORDERS_Y_DATA,
        "orders_both.csv": ORDERS_BOTH_DATA,
        "detail.csv": DETAIL_DATA,
    }
    for name, content in files.items():
        (basepath / name).write_text(content, encoding="utf-8")
    if y_content is not None:
        (basepath / "dict_y.csv").write_text(y_content, encoding="utf-8")


def make_package(basepath, order) -> Package:
    resources = []
    for name in order:
        descriptor = {
            "name": name,
            "path": PATHS[name],
            "schema": copy.deepcopy(SCHEMAS[name]),
        }
        if name == "dict_y":
            descriptor["encoding"] = "utf-8"
        resources.append(descriptor)
    return Package({"resources": resources}, basepath=str(basepath))


def error_signatures(report) -> dict:
    result = {}
    for task in report.tasks:
        result[task.name] = [
            (
                error.type,
                getattr(error, "row_number", None),
                tuple(getattr(error, "field_cells", []) or []),
                getattr(error, "reference_name", None),
                getattr(error, "field_name", None),
                getattr(error, "cell", None),
            )
            for error in task.errors
        ]
    return result


def rows_by_resource(report) -> dict:
    return {task.name: task.stats.get("rows") for task in report.tasks}


def foreign_keys_by_resource(package: Package) -> dict:
    return {resource.name: resource.schema.foreign_keys for resource in package.resources}


def probe_dictionary(package: Package, name: str = "dict_y"):
    resource = package.get_resource(name).to_copy()
    opened = False
    rows_read = 0
    error = None
    try:
        resource.open()
        opened = True
        for _ in resource.row_stream:
            rows_read += 1
    except FrictionlessException as exception:
        error = exception.error
    finally:
        resource.close()
    return opened, rows_read, error


@pytest.mark.parametrize(
    "order", [NORMAL_ORDER, REVERSED_ORDER], ids=["normal", "reversed"]
)
def test_package_validate_foreign_key_missing_dictionary_file(tmp_path, order):
    write_fixture(tmp_path, y_content=None)
    package = make_package(tmp_path, order)
    report = package.validate()

    signatures = error_signatures(report)
    rows = rows_by_resource(report)

    assert report.valid is False
    assert rows["dict_x"] == 2
    assert rows["detail"] == 2
    assert rows["orders_x"] == 3
    assert rows["dict_y"] is None
    assert rows["orders_y"] is None
    assert rows["orders_both"] is None

    assert signatures["dict_x"] == []
    assert signatures["orders_x"] == [("foreign-key", 3, ("9",), "dict_x", None, None)]
    assert signatures["detail"] == [("type-error", 3, (), None, "qty", "oops")]
    assert signatures["dict_y"] == [("scheme-error", None, (), None, None, None)]
    assert signatures["orders_y"] == [("scheme-error", None, (), None, None, None)]
    assert signatures["orders_both"] == [("scheme-error", None, (), None, None, None)]

    assert all("dict_y" not in str(signature) for signature in signatures["orders_y"])
    assert [resource.name for resource in package.resources] == order


@pytest.mark.parametrize(
    "order", [NORMAL_ORDER, REVERSED_ORDER], ids=["normal", "reversed"]
)
def test_package_validate_foreign_key_late_dictionary_damage(tmp_path, order):
    write_fixture(tmp_path, y_content=None)
    (tmp_path / "dict_y.csv").write_bytes(y_late_broken().encode("latin-1"))
    package = make_package(tmp_path, order)

    opened, rows_read, error = probe_dictionary(package)
    assert opened is True
    assert error is not None
    assert error.type == "encoding-error"
    assert 0 < rows_read < Y_TOTAL_ROWS + 1

    report = package.validate()
    signatures = error_signatures(report)
    rows = rows_by_resource(report)

    assert report.valid is False
    assert rows["dict_x"] == 2
    assert rows["detail"] == 2
    assert rows["orders_x"] == 3
    assert rows["dict_y"] == rows_read
    assert rows["orders_y"] is None
    assert rows["orders_both"] is None

    assert signatures["dict_x"] == []
    assert signatures["orders_x"] == [("foreign-key", 3, ("9",), "dict_x", None, None)]
    assert signatures["detail"] == [("type-error", 3, (), None, "qty", "oops")]
    assert [signature[0] for signature in signatures["dict_y"]] == ["encoding-error"]
    assert [signature[0] for signature in signatures["orders_y"]] == ["encoding-error"]
    assert [signature[0] for signature in signatures["orders_both"]] == ["encoding-error"]


@pytest.mark.parametrize(
    "order", [NORMAL_ORDER, REVERSED_ORDER], ids=["normal", "reversed"]
)
def test_package_validate_foreign_key_complete_dictionary_missing_key(tmp_path, order):
    write_fixture(tmp_path, y_content=Y_COMPLETE_MISSING_KEY)
    package = make_package(tmp_path, order)
    report = package.validate()

    signatures = error_signatures(report)
    rows = rows_by_resource(report)

    assert report.valid is False
    assert rows["dict_x"] == 2
    assert rows["dict_y"] == Y_TOTAL_ROWS
    assert rows["orders_x"] == 3
    assert rows["orders_y"] == 3
    assert rows["orders_both"] == 4
    assert rows["detail"] == 2

    assert signatures["dict_x"] == []
    assert signatures["dict_y"] == []
    assert signatures["orders_x"] == [("foreign-key", 3, ("9",), "dict_x", None, None)]
    assert signatures["orders_y"] == [
        ("foreign-key", 3, ("99999",), "dict_y", None, None)
    ]
    assert signatures["orders_both"] == [
        ("foreign-key", 3, ("9",), "dict_x", None, None),
        ("foreign-key", 4, ("99999",), "dict_y", None, None),
    ]
    assert signatures["detail"] == [("type-error", 3, (), None, "qty", "oops")]


@pytest.mark.parametrize("broken", ["missing", "late"], ids=["missing", "late"])
@pytest.mark.parametrize(
    "limit", [{"limit_rows": 1}, {"limit_errors": 1}], ids=["rows", "errors"]
)
@pytest.mark.parametrize(
    "order", [NORMAL_ORDER, REVERSED_ORDER], ids=["normal", "reversed"]
)
def test_package_validate_foreign_key_limit_repair_same_package(
    tmp_path, order, limit, broken
):
    y_content = None if broken == "missing" else y_late_broken()
    write_fixture(tmp_path, y_content=y_content)
    package = make_package(tmp_path, order)
    declared_foreign_keys = foreign_keys_by_resource(package)

    limited = package.validate(**limit)
    assert [resource.name for resource in package.resources] == order
    assert foreign_keys_by_resource(package) == declared_foreign_keys
    assert any(
        warning.startswith("reached row limit")
        or warning.startswith("reached error limit")
        for task in limited.tasks
        for warning in task.warnings
    )
    # A row limit can stop before a late dictionary defect is reached, and
    # that report is only marked partial through warnings. The repair below
    # must still be able to replace this partial observation.
    if broken == "late" and "limit_rows" in limit:
        assert limited.valid is True
        assert all(task.errors == [] for task in limited.tasks)
    else:
        assert limited.valid is False

    (tmp_path / "dict_y.csv").write_text(Y_REPAIRED, encoding="utf-8")
    repaired = package.validate()

    assert [resource.name for resource in package.resources] == order
    assert foreign_keys_by_resource(package) == declared_foreign_keys
    assert all(resource.package is package for resource in package.resources)

    fresh = make_package(tmp_path, order)
    fresh_report = fresh.validate()

    assert error_signatures(repaired) == error_signatures(fresh_report)
    assert rows_by_resource(repaired) == rows_by_resource(fresh_report)
    assert foreign_keys_by_resource(fresh) == declared_foreign_keys

    signatures = error_signatures(repaired)
    rows = rows_by_resource(repaired)
    assert rows["dict_x"] == 2
    assert rows["dict_y"] == Y_TOTAL_ROWS + 1
    assert rows["detail"] == 2
    assert rows["orders_x"] == 3
    assert rows["orders_y"] == 3
    assert rows["orders_both"] == 4
    assert signatures["dict_x"] == []
    assert signatures["dict_y"] == []
    assert signatures["orders_x"] == [("foreign-key", 3, ("9",), "dict_x", None, None)]
    assert signatures["orders_y"] == []
    assert signatures["orders_both"] == [("foreign-key", 3, ("9",), "dict_x", None, None)]
    assert signatures["detail"] == [("type-error", 3, (), None, "qty", "oops")]

    assert error_signatures(package.validate()) == error_signatures(repaired)


@pytest.mark.parametrize(
    "y_content",
    [Y_TRUNCATED_CLEAN, Y_TRUNCATED_MID_ROW],
    ids=["line-boundary", "mid-row"],
)
def test_package_validate_foreign_key_truncated_dictionary_is_indistinguishable(
    tmp_path, y_content
):
    write_fixture(tmp_path, y_content=y_content)
    package = make_package(tmp_path, NORMAL_ORDER)

    opened, rows_read, error = probe_dictionary(package)
    assert opened is True
    assert error is None
    assert rows_read < Y_TOTAL_ROWS

    truncated = package.validate()
    truncated_signatures = error_signatures(truncated)

    (tmp_path / "dict_y.csv").write_text(Y_COMPLETE_MISSING_KEY, encoding="utf-8")
    complete = make_package(tmp_path, NORMAL_ORDER).validate()
    complete_signatures = error_signatures(complete)

    truncated_fk = [
        signature
        for signature in truncated_signatures["orders_y"]
        if signature[2] == ("99999",)
    ]
    complete_fk = [
        signature
        for signature in complete_signatures["orders_y"]
        if signature[2] == ("99999",)
    ]
    assert truncated_fk == complete_fk
    assert truncated_fk == [("foreign-key", 3, ("99999",), "dict_y", None, None)]

    assert rows_by_resource(truncated)["orders_x"] == 3
    assert truncated_signatures["orders_x"] == [
        ("foreign-key", 3, ("9",), "dict_x", None, None)
    ]
    assert truncated_signatures["detail"] == [("type-error", 3, (), None, "qty", "oops")]

    if y_content is Y_TRUNCATED_CLEAN:
        assert truncated_signatures["dict_y"] == []
        assert rows_by_resource(truncated)["dict_y"] == Y_TRUNCATED_ROWS
    else:
        assert [signature[0] for signature in truncated_signatures["dict_y"]] == [
            "missing-cell"
        ]
        assert rows_by_resource(truncated)["dict_y"] == Y_TRUNCATED_ROWS + 1
