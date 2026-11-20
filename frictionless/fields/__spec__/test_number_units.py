from decimal import Decimal

import pytest

from frictionless import Field, FrictionlessException, Resource, Schema, fields

V2_SCHEMA = "https://datapackage.org/profiles/2.0/tableschema.json"


def create_rate_field(**options):
    descriptor = {
        "name": "rate",
        "type": "number",
        "units": {"%": 100, "‰": 1000},
        "outputUnit": "%",
    }
    for name, value in options.items():
        camel = name.split("_")[0] + "".join(part.title() for part in name.split("_")[1:])
        descriptor[camel] = value
    return Field.from_descriptor(descriptor)


# Equivalent notations


@pytest.mark.parametrize("source", ["12,5%", "125‰", "0,125"])
def test_number_ratio_equivalent_notations(source):
    field = create_rate_field()
    cell, notes = field.read_cell(source)
    assert notes is None
    assert cell == Decimal("0.125")
    assert isinstance(cell, Decimal)


def test_number_ratio_equivalent_notations_are_strictly_equal():
    field = create_rate_field()
    values = [field.read_cell(source)[0] for source in ["12,5%", "125‰", "0,125"]]
    assert values[0] == values[1] == values[2]
    assert len(set(values)) == 1


# Bounds


def test_number_ratio_exclusive_maximum_rejects_the_true_bound():
    field = create_rate_field(constraints={"exclusiveMaximum": 0.125})
    for source in ["12,5%", "125‰", "0,125"]:
        cell, notes = field.read_cell(source)
        assert cell == Decimal("0.125")
        assert notes == {"exclusiveMaximum": 'constraint "exclusiveMaximum" is "0.125"'}


def test_number_ratio_bound_is_not_a_float_round_trip_issue():
    # Representing the bound as a float used to accept a value that is
    # actually greater than the decimal bound.
    field = create_rate_field(constraints={"exclusiveMaximum": 0.125})
    cell, notes = field.read_cell("12,5000000000000001%")
    assert cell == Decimal("0.125000000000000001")
    assert notes is not None
    assert "exclusiveMaximum" in notes


def test_number_ratio_just_below_the_bound_is_valid():
    field = create_rate_field(constraints={"exclusiveMaximum": 0.125})
    for source in ["12,4999999999999999%", "124,999999999999999‰", "0,1249999999999999"]:
        cell, notes = field.read_cell(source)
        assert notes is None
        assert cell < Decimal("0.125")


def test_number_ratio_float_number_still_compares_exactly():
    field = create_rate_field(floatNumber=True, constraints={"exclusiveMaximum": 0.125})
    cell, notes = field.read_cell("12,5%")
    # Returning a float would make a value just below the bound
    # indistinguishable from the bound, so the exact ratio is kept.
    assert cell == Decimal("0.125")
    assert notes is not None
    assert "exclusiveMaximum" in notes


def test_number_ratio_float_number_is_not_rounded_into_the_bound():
    field = create_rate_field(floatNumber=True, constraints={"exclusiveMaximum": 0.125})
    cell, notes = field.read_cell("12,49999999999999999%")
    assert cell == Decimal("0.1249999999999999999")
    assert notes is None
    # And the exact value is what gets written back.
    assert field.write_cell(cell)[0] == "12,49999999999999999%"


def test_number_ratio_exclusive_bound_is_applied_through_a_schema():
    schema = Schema.from_descriptor(
        {
            "$schema": V2_SCHEMA,
            "fields": [
                {
                    "name": "rate",
                    "type": "number",
                    "units": {"%": 100, "‰": 1000},
                    "outputUnit": "%",
                    "constraints": {"exclusiveMaximum": 0.125},
                }
            ],
        }
    )
    resource = Resource(
        data=[["rate"], ["12,5%"], ["0,12499999999999999"]], schema=schema
    )
    rows = resource.read_rows()
    assert rows[0].errors[0].type == "constraint-error"
    assert rows[1].errors == []


# Missing values and unexpected units


def test_number_ratio_missing_value_is_not_unit_stripped():
    field = create_rate_field(missing_values=["", "-", "n/a"])
    assert field.read_cell("-") == (None, None)
    assert field.read_cell("n/a") == (None, None)
    assert field.read_cell("") == (None, None)


def test_number_ratio_missing_value_matching_is_lexical():
    # A missing token that happens to end with a unit symbol must still be
    # recognized as missing, not stripped and parsed as a number.
    field = create_rate_field(missing_values=["NA%"])
    assert field.read_cell("NA%") == (None, None)


@pytest.mark.parametrize("source", ["12,5%%", "12,5 percent", "12,5 ppm", "12,5 %%"])
def test_number_ratio_unexpected_unit_is_rejected(source):
    field = create_rate_field()
    cell, notes = field.read_cell(source)
    assert cell is None
    assert notes == {"type": 'type is "number/default"'}


def test_number_ratio_bare_number_never_bypasses_units():
    # A bare number is read as the base ratio, not as an accepted value for
    # an unexpected unit: "125" is 125, not 125‰.
    field = create_rate_field()
    assert field.read_cell("125")[0] == Decimal(125)


# Write


def test_number_ratio_write_uses_the_configured_output_unit():
    field = create_rate_field(outputUnit="%")
    assert field.write_cell(Decimal("0.125")) == ("12,5%", None)
    assert field.write_cell("0.125") == ("12,5%", None)


def test_number_ratio_write_permille():
    field = create_rate_field(outputUnit="‰")
    assert field.write_cell(Decimal("0.125")) == ("125‰", None)


def test_number_ratio_write_base_ratio():
    field = create_rate_field(outputUnit="")
    assert field.write_cell(Decimal("0.125")) == ("0,125", None)


def test_number_ratio_write_reports_a_lossy_output_unit():
    field = create_rate_field(outputUnit="ppm")
    cell, notes = field.write_cell(Decimal("0.125"))
    assert cell is None
    assert notes is not None
    assert "output unit" in notes["precision"]


def test_number_ratio_write_reports_lossy_display_precision():
    field = create_rate_field(outputPrecision=2)
    cell, notes = field.write_cell(Decimal("0.12345"))
    assert cell is None
    assert notes is not None
    assert "2 decimal place" in notes["precision"]


def test_number_ratio_write_keeps_an_exactly_representable_value():
    field = create_rate_field(outputPrecision=3)
    assert field.write_cell(Decimal("0.125")) == ("12,5%", None)


def test_number_ratio_write_honours_an_explicit_decimal_char():
    field = create_rate_field(decimalChar=".")
    assert field.write_cell(Decimal("0.125")) == ("12.5%", None)
    assert field.read_cell("12.5%")[0] == Decimal("0.125")


def test_number_ratio_write_honours_an_explicit_group_char():
    field = create_rate_field(decimalChar=",", groupChar=".")
    assert field.write_cell(Decimal("0.125")) == ("12,5%", None)
    assert field.write_cell(Decimal("1.1")) == ("110%", None)


def test_number_ratio_comma_group_char_keeps_a_distinct_decimal_char():
    field = create_rate_field(groupChar=",")
    cell, notes = field.read_cell("1,500%")
    assert notes is None
    assert cell == Decimal("15")
    assert field.write_cell(cell) == ("1,500%", None)
    assert field.write_cell(Decimal("0.125")) == ("12.5%", None)


def test_number_ratio_group_char_round_trip():
    field = create_rate_field(decimalChar=",", groupChar=".")
    cell, notes = field.read_cell("12.345,6%")
    assert notes is None
    assert cell == Decimal("123.456")
    written = field.write_cell(cell)[0]
    assert written == "12.345,6%"
    assert field.read_cell(written) == (cell, None)


# Legacy options


def test_number_ratio_only_decimal_and_group_char_use_the_legacy_path():
    field = Field.from_descriptor(
        {
            "name": "value",
            "type": "number",
            "groupChar": ".",
            "decimalChar": ",",
        }
    )
    assert not field._has_unit_config()
    assert field.read_cell("8.699,8") == (Decimal("8699.8"), None)
    assert field.write_cell(Decimal("8699.8")) == ("8.699,8", None)


# End-to-end


def test_number_ratio_export_and_reopen(tmp_path):
    schema_descriptor = {
        "$schema": V2_SCHEMA,
        "fields": [
            {
                "name": "rate",
                "type": "number",
                "units": {"%": 100, "‰": 1000},
                "outputUnit": "%",
                "missingValues": ["", "n/a"],
                "constraints": {"exclusiveMaximum": 0.125},
            }
        ],
    }
    schema = Schema.from_descriptor(schema_descriptor)
    source = [["rate"], ["12,5%"], ["125‰"], ["0,125"], ["0,1249999999999999"], ["n/a"]]

    resource = Resource(data=source, schema=schema)
    rows = resource.read_rows()
    assert rows[0]["rate"] == Decimal("0.125")
    assert rows[1]["rate"] == Decimal("0.125")
    assert rows[2]["rate"] == Decimal("0.125")
    assert rows[3]["rate"] == Decimal("0.1249999999999999")
    assert rows[4]["rate"] is None

    target = str(tmp_path / "ratio.csv")
    resource.write(target)
    written = open(target).read()
    assert "12,5%" in written
    assert "12,49999999999999%" in written

    reopened = Resource(target, schema=Schema.from_descriptor(schema_descriptor))
    reopened_rows = reopened.read_rows()
    assert reopened_rows[0]["rate"] == Decimal("0.125")
    assert reopened_rows[1]["rate"] == Decimal("0.125")
    assert reopened_rows[2]["rate"] == Decimal("0.125")
    assert reopened_rows[3]["rate"] == Decimal("0.1249999999999999")
    assert reopened_rows[4]["rate"] is None


def test_number_ratio_export_fails_instead_of_losing_precision(tmp_path):
    schema = Schema.from_descriptor(
        {
            "$schema": V2_SCHEMA,
            "fields": [
                {
                    "name": "rate",
                    "type": "number",
                    "units": {"%": 100},
                    "outputUnit": "%",
                    "outputPrecision": 2,
                }
            ],
        }
    )
    resource = Resource(data=[["rate"], ["0.12345"]], schema=schema)
    target = str(tmp_path / "ratio.csv")
    with pytest.raises(FrictionlessException) as excinfo:
        resource.write(target)
    error = excinfo.value.error
    assert error.type == "lossy-value"
    assert error.cell == "0.12345"
    assert error.field_name == "rate"
    assert error.row_number == 2


def test_number_ratio_descriptor_round_trip():
    field = create_rate_field(outputPrecision=3)
    descriptor = field.to_descriptor()
    assert descriptor["units"] == {"%": 100, "‰": 1000}
    assert descriptor["outputUnit"] == "%"
    assert descriptor["outputPrecision"] == 3
    reimported = Field.from_descriptor(descriptor)
    assert reimported.to_descriptor() == descriptor
    assert reimported.read_cell("12,5%")[0] == Decimal("0.125")


def test_number_ratio_named_units_are_understood():
    field = fields.NumberField(
        name="rate",
        units={"percent": 100, "permille": 1000},
        output_unit="%",
    )
    assert field.read_cell("12,5 percent")[0] == Decimal("0.125")
    assert field.read_cell("125 permille")[0] == Decimal("0.125")
    assert field.write_cell(Decimal("0.125")) == ("12,5%", None)
