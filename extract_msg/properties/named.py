from __future__ import annotations


__all__ = [
    'Named',
    'NamedProperties',
    'NamedPropertyBase',
    'NamedPropertyCatalog',
    'NamedPropertyEntry',
    'NumericalNamedProperty',
    'StringNamedProperty',
]


import abc
import copy
import logging
import pprint
import weakref

from typing import (
        Any, cast, Dict, Iterable, Iterator, List, Optional, Tuple,
        TYPE_CHECKING, TypeVar, Union
    )

from .. import constants
from ..enums import ErrorBehavior, NamedPropertyType
from ..exceptions import (
        AmbiguousNamedPropertyError, InvalidPropertyIdError,
        StandardViolationError
    )
from ..msg_path import MSGPath
from ..utils import bytesToGuid, divide, verifyPropertyId
from compressed_rtf.crc32 import crc32


# Allow for nice type checking.
if TYPE_CHECKING:
    from ..msg_classes.msg import MSGFile
    from ..attachments.attachment_base import AttachmentBase

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_T = TypeVar('_T')

# A key that fully identifies a named property definition. It combines the
# textual identifier (the numerical ID as hex or the string name), the property
# set GUID, and the kind of named property. Using all three is what allows a
# numerical property whose ID renders as "000A" and a string property named
# "000A" to coexist without one overwriting the other.
FullNamedKey = Tuple[str, str, NamedPropertyType]


def _splitNamedKey(key) -> Tuple[Optional[str], Optional[str], Optional[NamedPropertyType]]:
    """
    Normalizes a lookup key into name, GUID, and named property type, any of
    which may be ``None``.
    """
    if len(key) == 2:
        name, guid = key
        return name, guid, None
    name, guid, propType = key
    return name, guid, propType



class Named:
    """
    Class for handling access to the named properties themselves.

    A named property is fully identified by three pieces of information:

    * its textual identifier (a numerical ID rendered as hexadecimal or the
      literal string name);
    * the GUID of the property set it belongs to;
    * its :class:`~extract_msg.enums.NamedPropertyType` (numerical or string).

    Lookups that only supply the textual identifier, or the identifier and
    property set, are allowed but may be ambiguous. When more than one
    definition matches, :meth:`getDefinition` (and the mapping style access)
    raises :class:`~extract_msg.exceptions.AmbiguousNamedPropertyError` whose
    ``candidates`` attribute lists every match. Use :meth:`findDefinitions` to
    retrieve the candidates without raising, or disambiguate by also supplying
    the property set GUID and/or the named property type.
    """

    __dir = MSGPath('__nameid_version1.0')

    def __init__(self, msg: MSGFile):
        self.__msg = weakref.ref(msg)
        # Get the basic streams. If all are emtpy, then nothing to do.
        guidStream = self.getStream('__substg1.0_00020102')
        entryStream = self.getStream('__substg1.0_00030102')
        self.guidStream = guidStream
        self.entryStream = entryStream
        self.namesStream = self.getStream('__substg1.0_00040102')

        # Every definition, in the order they were read. Nothing is ever
        # overwritten here, even when two definitions share an identifier.
        self.__definitions: List[NamedPropertyBase] = []
        # The full, unambiguous key for every definition.
        self.__fullKeyDict: Dict[FullNamedKey, NamedPropertyBase] = {}
        # Maps the legacy two item key to every matching definition. This is
        # what makes ambiguous lookups detectable instead of silently dropping
        # one of the definitions.
        self.__propertiesDict: Dict[Tuple[str, str], List[NamedPropertyBase]] = {}
        # Maps a value stream ID to a single full definition key.
        self.__streamIDDict: Dict[str, FullNamedKey] = {}

        if guidStream is None:
            if ErrorBehavior.STANDARDS_VIOLATION in msg.errorBehavior:
                logger.warning('Standards Violation: Guid stream missing from named properties.')
                guidStream = b''
            else:
                raise StandardViolationError('Guid stream missing from named properties.')

        if entryStream is None:
            if ErrorBehavior.STANDARDS_VIOLATION in msg.errorBehavior:
                logger.warning('Standards Violation: Entry stream missing from named properties.')
                entryStream = b''
            else:
                raise StandardViolationError('Entry stream missing from named properties.')

        if self.namesStream is None:
            if ErrorBehavior.STANDARDS_VIOLATION in msg.errorBehavior:
                logger.error('Standards Violation: Guid stream missing from named properties. Will not parse named properties.')
                # Return immediately since the entry stream will likely fail.
                return
            else:
                raise StandardViolationError('Guid stream missing from named properties.')

        # Check that we even have any entries. If there are none, nothing to do.
        if entryStream:
            guids = tuple([None, constants.ps.PS_MAPI, constants.ps.PS_PUBLIC_STRINGS] + [bytesToGuid(x) for x in divide(guidStream, 16)])
            entries: List[Dict[str, Any]]= []
            for rawStream in divide(entryStream, 8):
                tmp = constants.st.ST_NP_ENT.unpack(rawStream)
                entry = {
                    'id': tmp[0],
                    'pid': tmp[2],
                    'guid_index': tmp[1] >> 1,
                    'pkind': NamedPropertyType(tmp[1] & 1), # 0 if numerical, 1 if string.
                    'rawStream': rawStream,
                }
                entry['guid'] = guids[entry['guid_index']]
                entries.append(entry)

            for entry in entries:
                if entry['pkind'] == NamedPropertyType.STRING_NAMED:
                    name = None
                    try:
                        name = self.__getName(entry['id'])
                    except ValueError as e:
                        if ErrorBehavior.NAMED_NAME_STREAM in msg.errorBehavior:
                            logger.error(f'Dropping named property because it failed to acquire name from name stream: {e}')
                        else:
                            raise

                    if name:
                        self.__addDefinition(StringNamedProperty(entry, name))
                else:
                    self.__addDefinition(NumericalNamedProperty(entry))

    def __addDefinition(self, property: NamedPropertyBase) -> None:
        """
        Registers a definition, preserving any definitions that share a key.
        """
        self.__definitions.append(property)
        self.__fullKeyDict[self.__fullKey(property)] = property
        self.__propertiesDict.setdefault(property.identifier, []).append(property)
        self.__streamIDDict[property.propertyStreamID] = self.__fullKey(property)

    def __contains__(self, key) -> bool:
        if not hasattr(key, '__len__') or len(key) not in (2, 3):
            return False
        return bool(self.findDefinitions(*_splitNamedKey(key)))

    def __getitem__(self, propertyName) -> NamedPropertyBase:
        # Validate the key.
        if not hasattr(propertyName, '__len__') or len(propertyName) not in (2, 3):
            raise TypeError('Named property key must be a tuple of two strings or a tuple of two strings and a NamedPropertyType.')

        matches = self.findDefinitions(*_splitNamedKey(propertyName))
        if not matches:
            raise KeyError(propertyName)
        if len(matches) > 1:
            raise AmbiguousNamedPropertyError(matches[0].identifier[0], matches)
        return matches[0]

    def __iter__(self) -> Iterator[Tuple[str, str]]:
        return iter(self.__propertiesDict)

    def __len__(self) -> int:
        return len(self.__definitions)

    @staticmethod
    def __fullKey(property: NamedPropertyBase) -> FullNamedKey:
        name, guid = property.identifier
        return (name.upper(), guid.upper(), property.type)

    def __getName(self, offset: int) -> str:
        """
        Parses the offset into the named stream and returns the name found.
        """
        # We used to parse names by handing it as an array, as specified by the
        # documentation, but this new method allows for a little bit more wiggle
        # room in terms of what is accepted by the module.
        if offset & 3 != 0:
            # If the offset is not a multiple of 4, that is an error, but we are
            # reducing it to a warning.
            logger.warning(f'Malformed named properties detected due to bad offset ({offset}). Ignoring.')
        # Check that offset is in string stream.
        if offset > len(self.namesStream):
            raise ValueError('Failed to parse named property: offset was not in string stream.')

        # Get the length, in bytes, of the string.
        length = constants.st.ST_LE_I32.unpack(self.namesStream[offset:offset + 4])[0]
        offset += 4

        # Make sure the string can be read entirely. If it can't, something was
        # corrupt.
        if offset + length > len(self.namesStream):
            raise ValueError(f'Failed to parse named property: length ({length}) of string overflows the string stream. This is probably due to a bad offset.')

        return self.namesStream[offset:offset + length].decode('utf-16-le')

    def exists(self, filename: constants.MSG_PATH) -> bool:
        """
        Checks if stream exists inside the named properties folder.

        :raises ReferenceError: The associated ``MSGFile`` instance has been
            garbage collected.
        """
        if (msg := self.__msg()) is None:
            raise ReferenceError('The MSGFile for this Named instance has been garbage collected.')
        return msg.exists(MSGPath(self.__dir, filename), False)

    def findDefinitions(self, name: Optional[str] = None, guid: Optional[str] = None,
                        propType: Optional[NamedPropertyType] = None) -> List[NamedPropertyBase]:
        """
        Returns every named property definition matching the given criteria.

        Every argument is optional and acts as an additional filter. The
        textual :param name: is matched case-insensitively against both the
        numerical IDs and the string names, so searching for ``"000A"`` returns
        every definition that could be referred to by that text.

        Use this to inspect candidates when a lookup is ambiguous.
        """
        matches: List[NamedPropertyBase] = []
        targetName = name.upper() if isinstance(name, str) else None
        targetGuid = guid.upper() if isinstance(guid, str) else None
        for property in self.__definitions:
            propertyName, propertyGuid = property.identifier
            if targetName is not None and propertyName.upper() != targetName:
                continue
            if targetGuid is not None and propertyGuid.upper() != targetGuid:
                continue
            if propType is not None and property.type != propType:
                continue
            matches.append(property)
        return matches

    def get(self, propertyName, default: _T = None) -> Union[NamedPropertyBase, _T]:
        """
        Tries to get a named property based on its key.

        The key may be a tuple of the name and the property set GUID or a tuple
        that also includes the :class:`~extract_msg.enums.NamedPropertyType`.

        Returns :param default: if not found. Raises
        :class:`~extract_msg.exceptions.AmbiguousNamedPropertyError` if the key
        matches more than one definition.
        """
        try:
            return self[propertyName]
        except KeyError:
            return default

    def getDefinition(self, name: str, guid: Optional[str] = None,
                      propType: Optional[NamedPropertyType] = None) -> NamedPropertyBase:
        """
        Gets a single named property definition.

        :raises KeyError: No definition matches.
        :raises AmbiguousNamedPropertyError: More than one definition matches;
            inspect the exception's ``candidates`` attribute.
        """
        matches = self.findDefinitions(name, guid, propType)
        if not matches:
            raise KeyError((name, guid, propType))
        if len(matches) > 1:
            raise AmbiguousNamedPropertyError(name, matches)
        return matches[0]

    def getDefinitionByStreamID(self, streamID: Union[int, str]) -> Optional[NamedPropertyBase]:
        """
        Gets the named property definition stored in the specified stream.

        :param streamID: A 4 hex character identifier or an integer that can
            convert to one.

        :returns: The definition, if the stream refers to a named property,
            otherwise ``None``.

        :raises InvalidPropertyIdError: The Stream ID is invalid.
        :raises TypeError: The Stream ID is not a valid type.
        """
        key = self.__lookupStreamKey(streamID)
        return None if key is None else self.__fullKeyDict[key]

    def getPropNameByStreamID(self, streamID: Union[int, str]) -> Optional[Tuple[str, str]]:
        """
        Gets the name of a property (as a key for the internal dict) that is stored in the specified stream.

        Useful for determining if a stream/property stream entry is a named
        property.

        :param streamID: A 4 hex character identifier that will be checked. May
            also be an integer that can convert to 4 hex characters.

        :returns: The name, if the stream is a named property, otherwise
            ``None``.

        :raises InvalidPropertyIdError: The Stream ID is invalid.
        :raises TypeError: The Stream ID is not a valid type.
        """
        key = self.__lookupStreamKey(streamID)
        return None if key is None else (key[0], key[1])

    def __lookupStreamKey(self, streamID: Union[int, str]) -> Optional[FullNamedKey]:
        if isinstance(streamID, int):
            if streamID < 0x8000:
                # Definitely doesn't refer to a name property, so just return
                # None.
                return None
            if streamID > 0xFFFF:
                raise InvalidPropertyIdError('Stream ID is out of range.')
            streamID = f'{streamID:04X}'
        elif isinstance(streamID, str):
            verifyPropertyId(streamID)
        else:
            raise TypeError(':param streamID: MUST be an int or str.')

        return self.__streamIDDict.get(streamID.upper())

    def getStream(self, filename: constants.MSG_PATH) -> Optional[bytes]:
        """
        Gets a binary representation of the requested stream.

        This should ALWAYS return a ``bytes`` object if it was found, otherwise
        returns ``None``.

        :raises ReferenceError: The associated ``MSGFile`` instance has been
            garbage collected.
        """
        if (msg := self.__msg()) is None:
            raise ReferenceError('The MSGFile for this Named instance has been garbage collected.')
        return msg.getStream(MSGPath(self.__dir, filename), False)

    def items(self) -> Iterable[Tuple[Tuple[str, str], NamedPropertyBase]]:
        for property in self.__definitions:
            yield (property.identifier, property)

    def keys(self) -> Iterable[Tuple[str, str]]:
        return self.__propertiesDict.keys()

    def pprintKeys(self) -> None:
        """
        Uses the pprint function on a sorted list of keys.
        """
        pprint.pprint(sorted(self.__propertiesDict.keys()))

    def values(self) -> Iterable[NamedPropertyBase]:
        return tuple(self.__definitions)

    @property
    def dir(self):
        """
        Returns the directory inside the MSG file where the named properties are located.
        """
        return self.__dir

    @property
    def msg(self) -> MSGFile:
        """
        Returns the Message instance the attachment belongs to.

        :raises ReferenceError: The associated ``MSGFile`` instance has been
            garbage collected.
        """
        if (msg := self.__msg()) is None:
            raise ReferenceError('The MSGFile for this Named instance has been garbage collected.')
        return msg

    @property
    def namedProperties(self) -> Dict[FullNamedKey, NamedPropertyBase]:
        """
        Returns a copy of the dictionary containing all the named property
        definitions, keyed by their full (name, GUID, kind) key.
        """
        return copy.deepcopy(self.__fullKeyDict)



class NamedProperties:
    """
    An instance that uses a Named instance and an extract-msg class to read the
    data of named properties.
    """
    def __init__(self, named: Named, streamSource: Union[MSGFile, AttachmentBase]):
        """
        :param named: The Named instance to refer to for named properties
            entries.
        :param streamSource: The source to use for acquiring the data of a named
            property.
        """
        self.__named = named
        self.__streamSource = weakref.ref(streamSource)

    def __getitem__(self, item: Union[Tuple, NamedPropertyBase]):
        """
        Get a named property using the [] operator. Item must be a named
        property instance or a tuple with 2 items (the name and the GUID
        string), or a tuple with a third
        :class:`~extract_msg.enums.NamedPropertyType` item.

        :raises AmbiguousNamedPropertyError: A tuple key matched more than one
            definition.
        :raises ReferenceError: The associated instance for getting actual
            property data has been garbage collected.
        """
        if (source := self.__streamSource()) is None:
            raise ReferenceError('The stream source for the NamedProperties instance has been garbage collected.')
        if isinstance(item, NamedPropertyBase):
            return source._getTypedData(item.propertyStreamID)
        else:
            return source._getTypedData(self.__named[item].propertyStreamID)

    def get(self, item: Union[Tuple, NamedPropertyBase], default: _T = None) -> Union[Any, _T]:
        """
        Get a named property, returning the value of :param default: if not
        found. Item must be a tuple with the name and the GUID string (and
        optionally a :class:`~extract_msg.enums.NamedPropertyType`), or a
        :class:`NamedPropertyBase` instance.

        Ambiguous matches raise
        :class:`~extract_msg.exceptions.AmbiguousNamedPropertyError` rather than
        silently picking a definition.

        :raises ReferenceError: The associated instance for getting actual
            property data has been garbage collected.
        """
        try:
            return self[item]
        except KeyError:
            return default



class NamedPropertyBase(abc.ABC):
    def __init__(self, entry: Dict[str, Any]):
        self.__entry = entry
        self.__guidIndex = entry['guid_index']
        self.__namedPropertyID = entry['pid']
        self.__guid = entry['guid']
        self.__propertyStreamID = f'{0x8000 + self.__namedPropertyID:04X}'

    @property
    def guid(self) -> str:
        """
        The guid of the property's property set.
        """
        return self.__guid

    @property
    def guidIndex(self) -> int:
        """
        The guid index of the property's property set.
        """
        return self.__guidIndex

    @property
    @abc.abstractmethod
    def identifier(self) -> Tuple[str, str]:
        """
        An identifier that can be used to full identify the property.
        """

    @property
    def namedPropertyID(self) -> int:
        """
        The named property id.
        """
        return self.__namedPropertyID

    @property
    def propertyStreamID(self) -> str:
        """
        An ID usable for grabbing the value stream.
        """
        return self.__propertyStreamID

    @property
    def rawEntry(self) -> Dict[str, Any]:
        return copy.deepcopy(self.__entry)

    @property
    def rawEntryStream(self) -> bytes:
        """
        The raw data used for the entry.
        """
        return self.__entry['rawStream']

    @property
    @abc.abstractmethod
    def type(self) -> NamedPropertyType:
        """
        Returns the type of the named property. This will be a member of the
        NamedPropertyType enum.
        """



class StringNamedProperty(NamedPropertyBase):
    def __init__(self, entry: Dict, name: str):
        super().__init__(entry)
        self.__name = name

        # Finally got this to be correct after asking about it on a Microsoft
        # forum. Apparently it uses the same CRC-32 as the Compressed RTF
        # standard does, so we can just use the function defined in the
        # compressed-rtf Python module.
        #
        # First thing to note is that the name should only ever be lowered if it
        # is part of the PS_INTERNET_HEADERS property set **AND** it is
        # generated by certain versions of Outlook. As such, a little bit of
        # additional code will need to run to determine exactly what the stream
        # ID should be if it's in that property set.
        if self.guid == constants.ps.PS_INTERNET_HEADERS:
            # To be sure if it needs to be lower the most effective method would
            # be to just get the Stream ID and then check if the entry is in
            # there. If it isn't, then check the regular case and see. If it is
            # not in either... well, we don't use it for anything so it will
            # just be a warning, and the Stream ID will be set to 0.
            #
            # TODO: Unfortunately, doing this will need to be put off until a
            # different version, preferably after Python 2 support is removed,
            # as this will require restructuring a lot of internal code. For now
            # we just assume that it is lowercase.
            self.__streamID = 0x1000 + (crc32(name.lower().encode('utf-16-le')) ^ (self.guidIndex << 1 | 1)) % 0x1F

        else:
            # No special logic here to determine what to do.
            self.__streamID = 0x1000 + (crc32(name.encode('utf-16-le')) ^ (self.guidIndex << 1 | 1)) % 0x1F

    @property
    def identifier(self) -> Tuple[str, str]:
        return (self.name, self.guid)

    @property
    def name(self) -> str:
        """
        The name of the property.
        """
        return self.__name

    @property
    def streamID(self) -> int:
        """
        Returns the streamID of the named property. This may not be accurate.
        """
        return self.__streamID

    @property
    def type(self) -> NamedPropertyType:
        """
        Returns the type of the named property. This will be a member of the
        NamedPropertyType enum.
        """
        return NamedPropertyType.STRING_NAMED



class NumericalNamedProperty(NamedPropertyBase):
    def __init__(self, entry: Dict):
        super().__init__(entry)
        self.__propertyID = f'{entry["id"]:04X}'
        self.__streamID = 0x1000 + (entry['id'] ^ (self.guidIndex << 1)) % 0x1F

    @property
    def identifier(self) -> Tuple[str, str]:
        return (self.propertyID, self.guid)

    @property
    def propertyID(self) -> str:
        """
        The actualy property id of the named property.
        """
        return self.__propertyID

    @property
    def streamID(self) -> int:
        """
        Returns the streamID of the named property. This may not be accurate.
        """
        return self.__streamID

    @property
    def type(self) -> NamedPropertyType:
        """
        Returns the type of the named property. This will be a member of the
        NamedPropertyType enum.
        """
        return NamedPropertyType.NUMERICAL_NAMED



class NamedPropertyEntry:
    """
    A single named property definition as seen from a specific message instance.

    The entry pairs the shared :class:`NamedPropertyBase` definition with
    information about the value carried by the message the owning catalog was
    created for. This is what lets a review distinguish a property that the
    message defines but leaves empty from one the message actually carries a
    value for, and see the real type of each present value.

    Values are read exclusively from the bound message: an entry never borrows
    a value from a parent, child, or forwarded copy of the message.
    """

    def __init__(self, catalog: 'NamedPropertyCatalog', definition: NamedPropertyBase):
        self.__catalog = weakref.ref(catalog)
        self.__definition = definition
        self.__actualTypes: Optional[Tuple[int, ...]] = None
        self.__hasValue: Optional[bool] = None

    def __repr__(self) -> str:
        return (f'NamedPropertyEntry(name={self.name!r}, guid={self.guid!r}, '
                f'kind={self.propertyType.name}, hasValue={self.hasValue}, '
                f'actualTypes={self.actualTypes})')

    def __detect(self) -> None:
        if self.__hasValue is not None:
            return
        catalog = self.__catalog()
        if catalog is None:
            raise ReferenceError('The NamedPropertyCatalog for this entry has been garbage collected.')
        msg = catalog.msg
        streamID = self.__definition.propertyStreamID
        types: List[int] = []

        # Fixed length properties (and any recorded in the property store).
        for prop in msg.props.getProperties(streamID):
            if prop.type not in types:
                types.append(prop.type)

        # Variable length value streams, including multiple value streams whose
        # base name is "__substg1.0_<streamID><type>". Only streams that are
        # direct children of this message are considered; streams belonging to
        # nested (embedded) messages must not leak into the parent's catalog.
        streamPrefix = f'__substg1.0_{streamID}'
        directDepth = msg.prefixLen + 1
        for path in msg.listDir():
            if len(path.parts) != directDepth:
                continue
            leaf = path[-1]
            if not leaf.startswith(streamPrefix):
                continue
            suffix = leaf[len(streamPrefix):len(streamPrefix) + 4]
            # Multiple value streams add a "-XXXXXXXX" index after the type.
            if len(suffix) == 4:
                try:
                    typeCode = int(suffix, 16)
                except ValueError:
                    continue
                if typeCode not in types:
                    types.append(typeCode)

        self.__actualTypes = tuple(sorted(types))
        self.__hasValue = bool(types)

    @property
    def actualTypes(self) -> Tuple[int, ...]:
        """
        The property type codes (e.g. ``0x0003`` or ``0x001F``) of the values
        this message carries for the property.

        An empty tuple means the message does not itself carry a value.
        """
        self.__detect()
        return cast(Tuple[int, ...], self.__actualTypes)

    @property
    def definition(self) -> NamedPropertyBase:
        """
        The shared named property definition.
        """
        return self.__definition

    @property
    def guid(self) -> str:
        """
        The property set GUID of the definition.
        """
        return self.__definition.guid

    @property
    def hasValue(self) -> bool:
        """
        Whether the bound message carries its own value for the property.

        A missing value is reported as ``False``; the value is never filled in
        from another message instance.
        """
        self.__detect()
        return cast(bool, self.__hasValue)

    @property
    def name(self) -> str:
        """
        The textual identifier of the definition (numerical ID as hex or the
        string name).
        """
        return self.__definition.identifier[0]

    @property
    def namedPropertyType(self) -> NamedPropertyType:
        """
        The kind of named property (numerical or string).
        """
        return self.__definition.type

    @property
    def propertyType(self) -> NamedPropertyType:
        """
        Alias of :attr:`namedPropertyType`.
        """
        return self.__definition.type

    @property
    def propertyStreamID(self) -> str:
        """
        The value stream ID the definition maps to.
        """
        return self.__definition.propertyStreamID

    @property
    def source(self) -> MSGFile:
        """
        The message that owns the named property definitions.

        Nested messages normally share the parent message's named property
        storage, so this is typically the root message; a message reopened from
        an exported file owns its own copy of the definitions.
        """
        catalog = self.__catalog()
        if catalog is None:
            raise ReferenceError('The NamedPropertyCatalog for this entry has been garbage collected.')
        return catalog.definitionSource

    @property
    def value(self) -> Any:
        """
        The value the bound message itself carries, or ``None`` if it does not
        carry one.

        Note that ``None`` is also a valid value for some property types, so use
        :attr:`hasValue` to tell the difference between "present but null" and
        "not carried by this message".
        """
        if not self.hasValue:
            return None
        catalog = self.__catalog()
        if catalog is None:
            raise ReferenceError('The NamedPropertyCatalog for this entry has been garbage collected.')
        return catalog.msg.namedProperties[self.__definition]



class NamedPropertyCatalog:
    """
    A reviewable catalog of the named property definitions as they apply to a
    single message instance.

    The catalog lists every definition from the message's shared
    :class:`Named` storage while reporting, per definition, the real type and
    presence of the value in that specific message. Parent and forwarded
    messages that share definitions therefore produce separate catalogs, and a
    property absent from one message is reported as having no value there
    rather than borrowing the other message's reading.
    """

    def __init__(self, msg: MSGFile):
        self.__msg = weakref.ref(msg)
        self.__entries: Optional[List[NamedPropertyEntry]] = None
        self.__fullKey: Dict[FullNamedKey, NamedPropertyEntry] = {}

    def __contains__(self, key) -> bool:
        if not hasattr(key, '__len__') or len(key) not in (2, 3):
            return False
        return bool(self.find(*_splitNamedKey(key)))

    def __getitem__(self, key) -> NamedPropertyEntry:
        if not hasattr(key, '__len__') or len(key) not in (2, 3):
            raise TypeError('Named property key must be a tuple of two strings or a tuple of two strings and a NamedPropertyType.')
        matches = self.find(*_splitNamedKey(key))
        if not matches:
            raise KeyError(key)
        if len(matches) > 1:
            raise AmbiguousNamedPropertyError(matches[0].name, [entry.definition for entry in matches])
        return matches[0]

    def __iter__(self) -> Iterator[NamedPropertyEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, key, default: _T = None) -> Union[NamedPropertyEntry, _T]:
        """
        Returns the matching entry or :param default: if nothing matches.

        Raises :class:`~extract_msg.exceptions.AmbiguousNamedPropertyError` if
        the key matches more than one definition.
        """
        try:
            return self[key]
        except KeyError:
            return default

    def find(self, name: Optional[str] = None, guid: Optional[str] = None,
             propType: Optional[NamedPropertyType] = None) -> List[NamedPropertyEntry]:
        """
        Returns the catalog entries matching the given criteria.

        Every argument is optional and acts as an additional filter. Use this
        to inspect candidates when a textual name is ambiguous.
        """
        definitions = self.msg.named.findDefinitions(name, guid, propType)
        return [self.__entryFor(definition) for definition in definitions]

    def __entryFor(self, definition: NamedPropertyBase) -> NamedPropertyEntry:
        key = (definition.identifier[0].upper(),
               definition.guid.upper(),
               definition.type)
        try:
            return self.__fullKey[key]
        except KeyError:
            entry = NamedPropertyEntry(self, definition)
            self.__fullKey[key] = entry
            return entry

    @property
    def definitionSource(self) -> MSGFile:
        """
        The message that owns the underlying named property definitions.
        """
        return self.msg.named.msg

    @property
    def entries(self) -> List[NamedPropertyEntry]:
        """
        All named property entries, one per definition, in definition order.
        """
        if self.__entries is None:
            self.__entries = [self.__entryFor(definition)
                              for definition in self.msg.named.values()]
        return self.__entries

    @property
    def msg(self) -> MSGFile:
        """
        The message instance the catalog is bound to.

        :raises ReferenceError: The instance has been garbage collected.
        """
        if (msg := self.__msg()) is None:
            raise ReferenceError('The MSGFile for this NamedPropertyCatalog has been garbage collected.')
        return msg

    @property
    def sharedDefinitions(self) -> bool:
        """
        Whether the definitions are owned by a different (parent) message.
        """
        return self.definitionSource is not self.msg
