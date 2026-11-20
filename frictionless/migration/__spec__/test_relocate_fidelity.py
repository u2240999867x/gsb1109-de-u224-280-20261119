import json
import os

from frictionless import relocate
from frictionless.migration.__spec__.test_relocate import build_sources, write


def test_relocate_infers_schema_when_source_has_none(tmpdir):
    left, _ = build_sources(str(tmpdir))
    descriptor = os.path.join(left, "datapackage.json")
    with open(descriptor) as file:
        package = json.load(file)
    # Remove the explicit schema of the dictionary
    package["resources"][1].pop("schema")
    write(descriptor, json.dumps(package))

    result = relocate(
        str(tmpdir.join("published")), sources=[descriptor], resources=["orders"]
    )
    assert result.valid
    codes = result.package.get_table_resource("codes")
    assert [field.name for field in codes.schema.fields] == ["code", "label"]
    assert [field.type for field in codes.schema.fields] == ["string", "string"]


def test_relocate_keeps_encoding_of_the_source(tmpdir):
    left, _ = build_sources(str(tmpdir))
    descriptor = os.path.join(left, "datapackage.json")
    with open(os.path.join(left, "codes-data.csv"), "wb") as file:
        file.write("code,label\nL1,café\nL2,left-two\n".encode("latin-1"))

    package = json.load(open(descriptor))
    package["resources"][1]["encoding"] = "latin-1"
    write(descriptor, json.dumps(package))

    result = relocate(
        str(tmpdir.join("published")), sources=[descriptor], resources=["orders"]
    )
    assert result.valid
    codes = result.package.get_table_resource("codes")
    assert codes.encoding in ("latin-1", "iso8859-1")
    assert [row["label"] for row in codes.read_rows()] == ["café", "left-two"]


def test_relocate_preserves_dialect_explicitly(tmpdir):
    left, _ = build_sources(str(tmpdir))
    descriptor = os.path.join(left, "datapackage.json")
    write(os.path.join(left, "orders.csv"), "id;code\n1;L1\n2;L2\n")
    package = json.load(open(descriptor))
    package["resources"][0]["dialect"] = {"delimiter": ";"}
    write(descriptor, json.dumps(package))

    result = relocate(
        str(tmpdir.join("published")), sources=[descriptor], resources=["orders"]
    )
    assert result.valid
    orders = result.package.get_table_resource("orders")
    assert orders.dialect.to_descriptor() == {"csv": {"delimiter": ";"}}
    assert [row["code"] for row in orders.read_rows()] == ["L1", "L2"]
