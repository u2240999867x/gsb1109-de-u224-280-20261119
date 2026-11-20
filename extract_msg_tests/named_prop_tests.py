__all__ = [
    'NamedPropTests',
]


import io
import unittest

import extract_msg

from .constants import TEST_FILE_DIR
from extract_msg import openMsg
from extract_msg.enums import NamedPropertyType
from extract_msg.exceptions import AmbiguousNamedPropertyError


# The two custom property sets used by the fixture.
GUID_1 = '{11111111-1111-1111-1111-111111111111}'
GUID_2 = '{22222222-2222-2222-2222-222222222222}'

# Property type codes.
PT_I32 = 0x0003
PT_UNICODE = 0x001F
PT_BINARY = 0x0102

# The value stream IDs assigned to the three definitions.
STREAM_NUM = '8000'
STREAM_STR_1 = '8001'
STREAM_STR_2 = '8002'

FIXTURE = TEST_FILE_DIR / 'named-property-catalog.msg'


class NamedPropTests(unittest.TestCase):
    def setUp(self):
        self.msg = openMsg(FIXTURE)
        self.addCleanup(self.msg.close)
        self.child = self.msg.attachments[0].data

    def testDefinitionsDoNotOverwrite(self):
        """
        The numerical "000A" and the two string "000A" properties are all kept
        as separate definitions.
        """
        definitions = list(self.msg.named.values())
        self.assertEqual(len(definitions), 3)

        fullKeys = sorted(
            (definition.identifier[0], definition.guid, definition.type)
            for definition in definitions
        )
        self.assertEqual(fullKeys, [
            ('000A', GUID_1, NamedPropertyType.NUMERICAL_NAMED),
            ('000A', GUID_1, NamedPropertyType.STRING_NAMED),
            ('000A', GUID_2, NamedPropertyType.STRING_NAMED),
        ])

        # The definitions map to distinct value streams, so none shadows another.
        streamIDs = sorted(definition.propertyStreamID for definition in definitions)
        self.assertEqual(streamIDs, [STREAM_NUM, STREAM_STR_1, STREAM_STR_2])

    def testTextualNameReturnsCandidates(self):
        """
        A lookup by the bare text "000A" returns every matching definition.
        """
        candidates = self.msg.named.findDefinitions('000A')
        self.assertEqual(len(candidates), 3)

        catalogCandidates = self.msg.namedCatalog.find('000A')
        self.assertEqual(len(catalogCandidates), 3)

        # Adding the property set narrows the list but does not resolve the
        # numerical/string collision inside the first set.
        self.assertEqual(len(self.msg.named.findDefinitions('000A', GUID_1)), 2)
        self.assertEqual(len(self.msg.named.findDefinitions('000A', GUID_2)), 1)

    def testAmbiguousLookupRaisesWithCandidates(self):
        """
        An ambiguous direct lookup raises instead of silently choosing one
        definition, exposing the candidates.
        """
        with self.assertRaises(AmbiguousNamedPropertyError) as context:
            self.msg.named[('000A', GUID_1)]
        self.assertEqual(len(context.exception.candidates), 2)

        # The data access layer propagates the ambiguity rather than hiding it.
        with self.assertRaises(AmbiguousNamedPropertyError):
            self.msg.namedProperties[('000A', GUID_1)]

        # The catalog behaves the same way.
        with self.assertRaises(AmbiguousNamedPropertyError):
            self.msg.namedCatalog[('000A', GUID_1)]

    def testDisambiguateByKindAndGuid(self):
        """
        Supplying the named property kind (and/or the property set) resolves
        each definition individually.
        """
        numerical = self.msg.named.getDefinition(
            '000A', GUID_1, NamedPropertyType.NUMERICAL_NAMED
        )
        stringOne = self.msg.named.getDefinition(
            '000A', GUID_1, NamedPropertyType.STRING_NAMED
        )
        stringTwo = self.msg.named.getDefinition('000A', GUID_2)

        self.assertEqual(numerical.propertyStreamID, STREAM_NUM)
        self.assertEqual(stringOne.propertyStreamID, STREAM_STR_1)
        self.assertEqual(stringTwo.propertyStreamID, STREAM_STR_2)

        # The full three item keys are also usable through the mapping API.
        self.assertIs(
            self.msg.named[('000A', GUID_1, NamedPropertyType.NUMERICAL_NAMED)],
            numerical,
        )
        self.assertIs(
            self.msg.named[('000A', GUID_1, NamedPropertyType.STRING_NAMED)],
            stringOne,
        )

    def testUnambiguousLookupIsPreserved(self):
        """
        The original two item (name, GUID) lookup still works where the name is
        unambiguous, including the convenience method on MSGFile.
        """
        definition = self.msg.named[('000A', GUID_2)]
        self.assertEqual(definition.type, NamedPropertyType.STRING_NAMED)
        self.assertEqual(self.msg.getNamedProp('000A', GUID_2), 'parent-text-two')

        # A missing definition returns the supplied default.
        self.assertIsNone(self.msg.named.get(('9999', GUID_2)))
        self.assertEqual(self.msg.named.get(('9999', GUID_2), 'fallback'), 'fallback')

    def testStreamIDLookups(self):
        """
        Stream IDs resolve back to the correct definition, including integer
        input.
        """
        self.assertEqual(
            self.msg.named.getPropNameByStreamID(STREAM_NUM),
            ('000A', GUID_1),
        )
        definition = self.msg.named.getDefinitionByStreamID(0x8002)
        self.assertEqual(definition.identifier, ('000A', GUID_2))
        self.assertIsNone(self.msg.named.getPropNameByStreamID('7000'))

    def testParentCatalogReportsValuesAndTypes(self):
        """
        The parent catalog shows all three properties with their own values and
        the real value types.
        """
        catalog = self.msg.namedCatalog
        self.assertFalse(catalog.sharedDefinitions)

        numerical = catalog[('000A', GUID_1, NamedPropertyType.NUMERICAL_NAMED)]
        stringOne = catalog[('000A', GUID_1, NamedPropertyType.STRING_NAMED)]
        stringTwo = catalog[('000A', GUID_2, NamedPropertyType.STRING_NAMED)]

        self.assertTrue(numerical.hasValue)
        self.assertEqual(numerical.actualTypes, (PT_I32,))
        self.assertEqual(numerical.value, 10)

        self.assertTrue(stringOne.hasValue)
        self.assertEqual(stringOne.actualTypes, (PT_UNICODE,))
        self.assertEqual(stringOne.value, 'parent-text-one')

        self.assertTrue(stringTwo.hasValue)
        self.assertEqual(stringTwo.actualTypes, (PT_UNICODE,))
        self.assertEqual(stringTwo.value, 'parent-text-two')

    def testChildCatalogSharesDefinitionsButOwnsReadings(self):
        """
        The forwarded message shares the definitions but reports its own values
        and never borrows the parent's readings.
        """
        catalog = self.child.namedCatalog
        self.assertTrue(catalog.sharedDefinitions)
        self.assertIs(catalog.definitionSource, self.msg)

        # The same numerical definition is present in the child but with a
        # different type (binary instead of int) and a different value.
        numerical = catalog[('000A', GUID_1, NamedPropertyType.NUMERICAL_NAMED)]
        self.assertTrue(numerical.hasValue)
        self.assertEqual(numerical.actualTypes, (PT_BINARY,))
        self.assertEqual(numerical.value, b'\xDE\xAD\xBE\xEF')

        stringOne = catalog[('000A', GUID_1, NamedPropertyType.STRING_NAMED)]
        self.assertTrue(stringOne.hasValue)
        self.assertEqual(stringOne.actualTypes, (PT_UNICODE,))
        self.assertEqual(stringOne.value, 'child-text-one')

        # The third property was not written to the forwarded message, so it can
        # only be reported as having no value there despite the parent's value.
        stringTwo = catalog[('000A', GUID_2, NamedPropertyType.STRING_NAMED)]
        self.assertFalse(stringTwo.hasValue)
        self.assertEqual(stringTwo.actualTypes, ())
        self.assertIsNone(stringTwo.value)

        # The data layer agrees that the value is absent on this message.
        self.assertIsNone(self.child.namedProperties.get(
            ('000A', GUID_2, NamedPropertyType.STRING_NAMED)
        ))
        # Reading the raw value stream returns None rather than borrowing the
        # parent's value.
        self.assertIsNone(self.child._getTypedData(STREAM_STR_2))

    def testReadingsAreNotFilledFromOtherMessage(self):
        """
        Different values (and a missing value) are tolerated for the same shared
        definition without either message overwriting the other.
        """
        parentValue = self.msg.namedProperties[
            ('000A', GUID_1, NamedPropertyType.NUMERICAL_NAMED)
        ]
        childValue = self.child.namedProperties[
            ('000A', GUID_1, NamedPropertyType.NUMERICAL_NAMED)
        ]
        self.assertEqual(parentValue, 10)
        self.assertEqual(childValue, b'\xDE\xAD\xBE\xEF')
        self.assertNotEqual(type(parentValue), type(childValue))

    def testExportedChildReopensWithDefinitionsAndTypes(self):
        """
        Exporting the nested message standalone and reopening it preserves the
        two values it carries, their original types, and the missing property.
        """
        exported = self.child.exportBytes()
        standalone = openMsg(io.BytesIO(exported))
        self.addCleanup(standalone.close)

        # The reopened message owns a copy of the named property definitions.
        self.assertFalse(standalone.namedCatalog.sharedDefinitions)
        self.assertEqual(len(list(standalone.named.values())), 3)

        numerical = standalone.namedCatalog[
            ('000A', GUID_1, NamedPropertyType.NUMERICAL_NAMED)
        ]
        stringOne = standalone.namedCatalog[
            ('000A', GUID_1, NamedPropertyType.STRING_NAMED)
        ]
        stringTwo = standalone.namedCatalog[
            ('000A', GUID_2, NamedPropertyType.STRING_NAMED)
        ]

        self.assertEqual(numerical.actualTypes, (PT_BINARY,))
        self.assertEqual(numerical.value, b'\xDE\xAD\xBE\xEF')
        self.assertEqual(stringOne.actualTypes, (PT_UNICODE,))
        self.assertEqual(stringOne.value, 'child-text-one')
        self.assertFalse(stringTwo.hasValue)
        self.assertIsNone(stringTwo.value)

        # Ambiguity is still detectable after reopening.
        self.assertEqual(len(standalone.named.findDefinitions('000A')), 3)


if __name__ == '__main__':
    unittest.main()
