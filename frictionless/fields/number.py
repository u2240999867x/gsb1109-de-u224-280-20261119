from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Dict, Optional, Tuple, Union

import attrs

from .. import errors, settings
from ..schema import Field
from ..schema.field import (
    check_enum,
    check_exclusiveMaximum,
    check_exclusiveMinimum,
    check_maximum,
    check_minimum,
    check_required,
)

CHECKS = {
    "required": check_required,
    "minimum": check_minimum,
    "maximum": check_maximum,
    "exclusiveMinimum": check_exclusiveMinimum,
    "exclusiveMaximum": check_exclusiveMaximum,
    "enum": check_enum,
}

# A unit is stored internally as a factor: multiplying a value expressed in
# that unit by the factor gives the base ratio. For example "12,5%" is read as
# 12.5 * 1/100 = 0.125. A canonical symbol (the empty string for the base
# ratio) is kept alongside the factor for writing.
#
# By default only the conventional symbols are accepted. Named units (e.g.
# "percent") are understood when configured explicitely; the known names are
# still used to interpret a configured definition as a divisor and to give it
# a canonical output symbol.
DEFAULT_UNITS: Dict[str, Tuple[Fraction, str]] = {
    "": (Fraction(1), ""),
    "1": (Fraction(1), ""),
    "ratio": (Fraction(1), ""),
    "fraction": (Fraction(1), ""),
    "%": (Fraction(1, 100), "%"),
    "‰": (Fraction(1, 1000), "‰"),
}
KNOWN_UNITS: Dict[str, Tuple[Fraction, str]] = {
    **DEFAULT_UNITS,
    "pct": (Fraction(1, 100), "%"),
    "percent": (Fraction(1, 100), "%"),
    "permil": (Fraction(1, 1000), "‰"),
    "permille": (Fraction(1, 1000), "‰"),
    "per_mille": (Fraction(1, 1000), "‰"),
}
BASE_UNIT_NAMES = ("", "1", "ratio", "fraction")
NUMBER_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")

IUnitSpecs = Dict[str, Tuple[Fraction, str]]


@attrs.define(kw_only=True, repr=False)
class NumberField(Field):
    type = "number"
    builtin = True
    supported_constraints = [
        "required",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "enum",
    ]

    bare_number: bool = settings.DEFAULT_BARE_NUMBER
    """
    It specifies that the value is a bare number. If true, the pattern to remove non digit
    character does not get applied and vice versa. The default value is True.
    """

    float_number: bool = settings.DEFAULT_FLOAT_NUMBER
    """
    It specifies that the value is a float number.
    """

    decimal_char: str = settings.DEFAULT_DECIMAL_CHAR
    """
    It specifies the char to be used as decimal character. The default
    value is ".". It values can be: ".", "@" etc.
    """

    group_char: str = settings.DEFAULT_GROUP_CHAR
    """
    It specifies the char to be used as group character. The default value
    is "". It can take values such as: ",", "#" etc.
    """

    units: Dict[str, Any] = attrs.field(factory=dict)
    """
    Accepted units as a mapping from a unit suffix to its conversion factor.
    A value is normalised to the base ratio by multiplying it by the factor.
    An object definition can use `factor`, `multiplier`, `scale`, `ratio`, or
    `divisor`; `symbol`/`suffix` can name the lexical suffix. For the standard
    ratio symbols a factor greater than one is understood as a divisor, so
    both `{"%": 100}` and `{"%": 0.01}` mean one hundredth.
    """

    unit_map: Dict[str, Any] = attrs.field(factory=dict)
    """
    Alias for `units` (descriptor property `unitMap`).
    """

    ratio_units: Dict[str, Any] = attrs.field(factory=dict)
    """
    Alias for `units` (descriptor property `ratioUnits`).
    """

    unit: Optional[Union[str, Dict[str, Any]]] = None
    """
    A single unit suffix (also used as the output unit unless `output_unit`
    is set) or an additional unit mapping.
    """

    output_unit: Optional[str] = None
    """
    The only unit used when writing a value. The base ratio is written without
    a suffix; other values must name an accepted unit (e.g. "%" or "‰").
    """

    output_precision: Optional[int] = None
    """
    Maximum number of fractional digits allowed when writing. A value that
    requires more digits is reported as lossy instead of being rounded.
    """

    decimal_places: Optional[int] = None
    """
    Alias for `output_precision` (descriptor property `decimalPlaces`).
    """

    output_decimal_places: Optional[int] = None
    """
    Alias for `output_precision` (descriptor property `outputDecimalPlaces`).
    """

    precision: Optional[int] = None
    """
    Alias for `output_precision` (descriptor property `precision`).
    """

    # Units

    def _has_unit_config(self) -> bool:
        """Whether the unit-aware read/write path is enabled"""
        return bool(
            self.units
            or self.unit_map
            or self.ratio_units
            or isinstance(self.unit, dict)
            or isinstance(self.unit, str)
            or self.output_unit is not None
            or self._precision() is not None
        )

    def _precision(self) -> Optional[int]:
        for value in (
            self.output_precision,
            self.decimal_places,
            self.output_decimal_places,
            self.precision,
        ):
            if value is not None:
                return value
        return None

    def _unit_config(self) -> Dict[str, Any]:
        config: Dict[str, Any] = {}
        config.update(self.unit_map or {})
        config.update(self.ratio_units or {})
        config.update(self.units or {})
        if isinstance(self.unit, dict):
            config.update(self.unit)
        return config

    def _unit_specs(self) -> IUnitSpecs:
        specs = dict(DEFAULT_UNITS)
        for name, definition in self._unit_config().items():
            specs[str(name)] = self._parse_unit_definition(str(name), definition)
        if isinstance(self.unit, str) and self.unit:
            specs[self.unit] = self._parse_unit_definition(self.unit, None)
        return specs

    def _cached_unit_specs(self) -> IUnitSpecs:
        # The specs are built once per field configuration and reused by
        # every cell reader/writer of the field.
        cache = self.__dict__.get("_unit_specs_cache")
        key = (
            repr(sorted((self._unit_config() or {}).items(), key=repr)),
            self.unit if isinstance(self.unit, str) else None,
            repr(self.unit) if isinstance(self.unit, dict) else None,
        )
        if cache is None or cache[0] != key:
            cache = (key, self._unit_specs())
            self.__dict__["_unit_specs_cache"] = cache
        return cache[1]

    @staticmethod
    def _parse_unit_definition(name: str, definition: Any) -> Tuple[Fraction, str]:
        known = KNOWN_UNITS.get(name)
        symbol = known[1] if known else name
        divisor = None
        value = definition
        if isinstance(definition, dict):
            symbol = str(
                definition.get("symbol")
                or definition.get("suffix")
                or definition.get("unit")
                or symbol
            )
            divisor = definition.get("divisor")
            value = next(
                (
                    definition.get(key)
                    for key in ("factor", "multiplier", "scale", "ratio")
                    if key in definition
                ),
                None,
            )
        if divisor is not None:
            factor = Fraction(1) / NumberField._to_fraction(divisor)
        elif value is None:
            factor = known[0] if known else Fraction(1)
        else:
            factor = NumberField._to_fraction(value)
            # Both `{"%": 100}` (a divisor) and `{"%": 0.01}` (a multiplier)
            # are natural notations for the standard ratio symbols.
            if known and factor > 1:
                factor = Fraction(1) / factor
        if factor == 0:
            raise ValueError("a unit factor cannot be zero")
        return factor, symbol

    @staticmethod
    def _to_fraction(value: Any) -> Fraction:
        if isinstance(value, Fraction):
            return value
        if isinstance(value, bool):
            raise ValueError("a boolean is not a number")
        if isinstance(value, int):
            return Fraction(value)
        if isinstance(value, Decimal):
            return Fraction(value)
        if isinstance(value, float):
            return Fraction(str(value))
        if isinstance(value, str):
            text = value.strip()
            if "/" in text:
                return Fraction(text)
            return Fraction(Decimal(text))
        raise ValueError(f"unsupported number: {value!r}")

    @staticmethod
    def _fraction_to_decimal(value: Fraction) -> Optional[Decimal]:
        """Exact decimal representation, or None if it does not terminate"""
        numerator = value.numerator
        denominator = value.denominator
        twos = 0
        fives = 0
        while denominator % 2 == 0:
            twos += 1
            denominator //= 2
        while denominator % 5 == 0:
            fives += 1
            denominator //= 5
        if denominator != 1:
            return None
        scale = max(twos, fives)
        numerator *= 2 ** (scale - twos) * 5 ** (scale - fives)
        sign = "-" if numerator < 0 else ""
        digits = str(abs(numerator))
        if scale:
            digits = digits.rjust(scale + 1, "0")
            digits = f"{digits[:-scale]}.{digits[-scale:]}"
        return Decimal(f"{sign}{digits}")

    # Read

    def create_value_reader(self):
        if not self._has_unit_config():
            return self._create_legacy_value_reader()

        specs = self._cached_unit_specs()

        def value_reader(cell: Any):
            if isinstance(cell, str):
                return self._read_ratio(cell, specs)
            if cell is True or cell is False or cell is None:
                return None
            try:
                return self._fraction_to_decimal(self._to_fraction(cell))
            except Exception:
                return None

        return value_reader

    def _create_legacy_value_reader(self):
        # Create pattern
        pattern = None
        if not self.bare_number:
            pattern = re.compile(r"((^[^-\d]*)|(\D*$))")

        # Create processor
        processor = None
        properties = ["group_char", "decimal_char", "bare_number"]
        if set(properties).intersection(self.list_defined()):

            def processor_function(cell: Any):
                if pattern:
                    cell = pattern.sub("", cell)
                cell = cell.replace(self.group_char, "")
                if self.decimal_char != "." and "." in cell:
                    return None
                cell = cell.replace(self.decimal_char, ".")
                return cell

            processor = processor_function

        # Create reader
        def value_reader(cell: Any):
            Primary = Decimal
            Secondary = float
            if self.float_number:
                Primary = float
                Secondary = Decimal
            if isinstance(cell, str):
                cell = cell.strip()

                # Process the cell
                if processor:
                    cell = processor(cell)  # type: ignore
                    if cell is None:
                        return None

                # Cast the cell
                try:
                    return Primary(cell)  # type: ignore
                except Exception:
                    return None

            elif isinstance(cell, Primary):
                return cell
            elif cell is True or cell is False:
                return None
            elif isinstance(cell, int):
                return cell
            elif isinstance(cell, Secondary):
                return Primary(str(cell) if Primary is Decimal else cell)
            return None

        return value_reader

    def _read_ratio(self, cell: str, specs: IUnitSpecs) -> Optional[Decimal]:
        text = cell.strip()

        # Find the longest accepted unit suffix. Missing values are matched
        # lexically before this reader is called, so unit stripping never
        # applies to them.
        unit: Optional[str] = None
        for suffix in sorted((name for name in specs if name), key=len, reverse=True):
            if text.endswith(suffix):
                unit = suffix
                break
        number = text if unit is None else text[: -len(unit)].strip()
        if not number:
            return None

        number = self._normalize_number_text(number)
        if number is None:
            return None
        try:
            value = Fraction(Decimal(number))
        except (InvalidOperation, ValueError):
            return None
        factor = specs[unit][0] if unit is not None else Fraction(1)
        return self._fraction_to_decimal(value * factor)

    def _normalize_number_text(self, text: str) -> Optional[str]:
        text = text.strip()
        if not text:
            return None
        if self.decimal_char and self.decimal_char == self.group_char:
            return None
        if self.group_char:
            text = text.replace(self.group_char, "")
        if self.decimal_char and self.decimal_char != ".":
            if "." in text:
                return None
            text = text.replace(self.decimal_char, ".")
        elif not self.group_char and "." not in text and text.count(",") == 1:
            # The agreed convention uses a comma as the decimal separator
            # whenever the field is declared with units.
            text = text.replace(",", ".")
        if not NUMBER_PATTERN.match(text):
            return None
        return text

    # Write

    def create_value_writer(self):
        if not self._has_unit_config():
            return self._create_legacy_value_writer()

        def value_writer(cell: Any):
            text, _ = self._write_ratio(cell)
            return text

        return value_writer

    def _create_legacy_value_writer(self):
        # Create writer
        def value_writer(cell: Any):
            if self.has_defined("group_char"):
                cell = f"{cell:,}".replace(",", "g")
            else:
                cell = str(cell)
            if self.has_defined("decimal_char"):
                cell = cell.replace(".", self.decimal_char)
            if self.has_defined("group_char"):
                cell = cell.replace("g", self.group_char)
            return cell

        return value_writer

    def _write_ratio(self, cell: Any) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
        try:
            ratio = (
                self._read_ratio(cell, self._cached_unit_specs())
                if isinstance(cell, str)
                else self._fraction_to_decimal(self._to_fraction(cell))
            )
        except Exception:
            ratio = None
        if ratio is None:
            return None, {"type": f'type is "{self.type}/{self.format}"'}

        specs = self._cached_unit_specs()
        spec = self._output_spec(specs)
        if spec is None:
            return None, {
                "precision": (
                    "value cannot be expressed losslessly "
                    f'in the output unit "{self.output_unit}"'
                )
            }
        factor, symbol = spec

        try:
            value = Fraction(ratio) / factor
        except Exception:
            value = None
        decimal = self._fraction_to_decimal(value) if value is not None else None
        if decimal is None:
            return None, {
                "precision": "value cannot be expressed losslessly in the output unit"
            }

        precision = self._precision()
        if precision is not None:
            # Compare the written value with the source value at the allowed
            # precision: trailing zeros in the source do not make a loss.
            written = decimal.quantize(Decimal(1).scaleb(-precision))
            if written != decimal:
                return None, {
                    "precision": (
                        "value cannot be expressed losslessly with "
                        f"{precision} decimal place(s)"
                    )
                }

        text = self._format_decimal(decimal)
        if text is None:
            return None, {
                "precision": "value cannot be expressed with the display convention"
            }
        if symbol:
            text += symbol
        return text, None

    def _output_spec(self, specs: IUnitSpecs) -> Optional[Tuple[Fraction, str]]:
        requested = self.output_unit
        if requested is None and isinstance(self.unit, str) and self.unit:
            requested = self.unit
        if requested is None:
            return Fraction(1), ""
        name = requested.strip()
        if name in specs:
            return specs[name]
        if name in BASE_UNIT_NAMES:
            return Fraction(1), ""
        return None

    def _output_decimal_char(self) -> str:
        # The agreed convention uses a comma as the decimal separator for a
        # unit-aware field. An explicit `decimalChar` still takes precedence;
        # if a comma is used for grouping, the decimal separator must differ.
        if self.has_defined("decimal_char"):
            return self.decimal_char
        if self.group_char == ",":
            return "."
        return ","

    def _format_decimal(self, value: Decimal) -> Optional[str]:
        decimal_char = self._output_decimal_char()
        if not decimal_char:
            return None
        text = format(value.normalize(), "f")
        if text in ("-0", "-0.0"):
            text = "0"
        sign = ""
        if text.startswith("-"):
            sign, text = "-", text[1:]
        integer, dot, fraction = text.partition(".")
        if self.group_char:
            if self.group_char == decimal_char:
                return None
            integer = f"{int(integer):,}".replace(",", self.group_char)
        return sign + integer + (decimal_char + fraction if dot else "")

    # Cell reader/writer

    def create_cell_reader(self):
        if not self._has_unit_config():
            return super().create_cell_reader()

        value_reader = self.create_value_reader()
        missing_values = self.missing_values
        if not self.has_defined("missing_values") and self.schema:
            missing_values = self.schema.missing_values

        # Constraints are expressed in the base ratio and are compared
        # exactly, even when the field returns floats.
        checks = {}
        for name in self.supported_constraints:
            constraint = self.constraints.get(name)
            if constraint is not None:
                if (
                    name in ("exclusiveMinimum", "exclusiveMaximum")
                    and self._datapackage_version == "v1"
                ):
                    continue
                if name in [
                    "minimum",
                    "maximum",
                    "exclusiveMinimum",
                    "exclusiveMaximum",
                ]:
                    constraint = value_reader(constraint)
                if name == "enum":
                    constraint = list(map(value_reader, constraint))  # type: ignore
                checks[name] = (constraint, CHECKS[name])

        def cell_reader(cell: Any):
            notes: Optional[Dict[str, str]] = None
            # Missing values are matched against the complete lexical token,
            # before any unit is stripped.
            if str(cell) in missing_values:
                cell = None
            if cell is not None:
                cell = value_reader(cell)
                if cell is None:
                    notes = notes or {}
                    notes["type"] = f'type is "{self.type}/{self.format}"'
            if not notes and checks:
                for name, (constraint, check) in checks.items():
                    if not check(constraint, cell):
                        notes = notes or {}
                        constraint = self.constraints[name]
                        notes[name] = f'constraint "{name}" is "{constraint}"'
            # A float cannot represent every decimal exactly: converting
            # would make a value just below a bound indistinguishable from
            # the bound itself. Exact decimals are therefore kept even when
            # `floatNumber` is set; the constraints above are exact anyway.
            return cell, notes

        return cell_reader

    def create_cell_writer(self):
        if not self._has_unit_config():
            return super().create_cell_writer()

        try:
            missing_value = self.missing_values[0]
            if not self.has_defined("missing_values") and self.schema:
                missing_value = self.schema.missing_values[0]
        except IndexError:
            missing_value = settings.DEFAULT_MISSING_VALUES[0]

        def cell_writer(cell: Any, *, ignore_missing: bool = False):
            if cell is None:
                cell = cell if ignore_missing else missing_value
                return cell, None
            return self._write_ratio(cell)

        return cell_writer

    # Metadata

    metadata_profile_patch = {
        "properties": {
            "bareNumber": {"type": "boolean"},
            "floatNumber": {"type": "boolean"},
            "decimalChar": {"type": "string"},
            "groupChar": {"type": "string"},
            "units": {"type": "object"},
            "unitMap": {"type": "object"},
            "ratioUnits": {"type": "object"},
            "unit": {"type": ["string", "object"]},
            "outputUnit": {"type": "string"},
            "outputPrecision": {"type": "integer"},
            "decimalPlaces": {"type": "integer"},
            "outputDecimalPlaces": {"type": "integer"},
            "precision": {"type": "integer"},
        }
    }

    @classmethod
    def metadata_validate(cls, descriptor, *, context=None):  # type: ignore
        yield from super().metadata_validate(descriptor, context=context)

        # Units
        unit_properties = ("units", "unitMap", "ratioUnits")
        for name in unit_properties:
            units = descriptor.get(name)
            if units is None:
                continue
            if not isinstance(units, dict):
                yield errors.FieldError(note=f'"{name}" must be an object')
                continue
            for unit, definition in units.items():
                if not isinstance(definition, (int, float, str, dict)):
                    note = f'unit "{unit}" must be a number or an object'
                    yield errors.FieldError(note=note)
                    continue
                try:
                    cls._parse_unit_definition(str(unit), definition)
                except Exception:
                    note = f'unit "{unit}" does not define a valid factor'
                    yield errors.FieldError(note=note)

        unit = descriptor.get("unit")
        if unit is not None and not isinstance(unit, (str, dict)):
            yield errors.FieldError(note='"unit" must be a string or an object')

        # Precision
        for name in (
            "outputPrecision",
            "decimalPlaces",
            "outputDecimalPlaces",
            "precision",
        ):
            value = descriptor.get(name)
            if value is not None and (not isinstance(value, int) or value < 0):
                yield errors.FieldError(note=f'"{name}" must be a non-negative integer')
