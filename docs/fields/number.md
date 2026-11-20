# Number Field

## Overview

The field contains numbers of any kind including decimals. Read more in [Table Schema Standard](https://specs.frictionlessdata.io/table-schema/#number).

## Example

```python script tabs=Python
from frictionless import Schema, extract, fields

data = [['name'], ['1.1'], ['2.2'], ['3.3']]
rows = extract(data, schema=Schema(fields=[fields.NumberField(name='name')]))
print(rows)
```

## Units

A `NumberField` can describe a ratio expressed with a unit. The `units`
mapping gives the conversion factor of each accepted unit, so all of these
notations read as the same ratio:

```python
from decimal import Decimal

from frictionless import Schema, extract, fields

data = [['rate'], ['12,5%'], ['125‰'], ['0,125']]
schema = Schema(fields=[fields.NumberField(
    name='rate',
    units={'%': 100, '‰': 1000},
    output_unit='%',
)])
rows = extract(data, schema=schema)
assert [row['rate'] for row in rows['memory']] == [Decimal('0.125')] * 3
```

Values are converted to the base ratio with an exact decimal arithmetic
(`Decimal`/`Fraction`), so a value sitting on a bound is rejected by
`exclusiveMaximum` even when the equivalent float would look equal. A unit
must be declared: an unknown suffix (`12,5%%`, `12,5 ppm`) is a type error
and is never accepted as a bare number. Missing values are matched on the
complete lexical token before any unit is stripped.

When no `output_unit` is given, values are written as the base ratio. With an
output unit, writing converts back to that single unit. The agreed convention
uses a comma as the decimal separator; an explicit `decimal_char` takes
precedence. When a value cannot be represented exactly in the configured
output unit or `output_precision`, the write fails with a `lossy-value` error
pointing at the original cell instead of silently rounding it.

Setting only `decimal_char`/`group_char` keeps the historical number
read/write behavior unchanged.

## Reference

```yaml reference
references:
  - frictionless.fields.NumberField
```
