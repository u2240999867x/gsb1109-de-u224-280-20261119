"""
Classes and functions involved with managing properties.
"""

__all__ = [
    'FixedLengthProp',
    'Named',
    'NamedProperties',
    'NamedPropertyBase',
    'NamedPropertyCatalog',
    'NamedPropertyEntry',
    'NumericalNamedProperty',
    'PropBase',
    'PropertiesStore',
    'StringNamedProperty',
    'VariableLengthProp',
]


from .named import (
        Named, NamedProperties, NamedPropertyBase, NamedPropertyCatalog,
        NamedPropertyEntry, NumericalNamedProperty, StringNamedProperty
    )
from .prop import FixedLengthProp, PropBase, VariableLengthProp
from .properties_store import PropertiesStore