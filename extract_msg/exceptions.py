# -*- coding: utf-8 -*-

"""
extract_msg.exceptions
~~~~~~~~~~~~~~~~~~~~~~
This module contains the set of extract_msg exceptions.
"""

__all__ = [
    'ExMsgBaseException',

    'AmbiguousNamedPropertyError',
    'ConversionError',
    'DataNotFoundError',
    'DeencapMalformedData',
    'DeencapNotEncapsulated',
    'ExecutableNotFound',
    'IncompatibleOptionsError',
    'InvalidFileFormatError',
    'InvalidPropertyIdError',
    'StandardViolationError',
    'TZError',
    'UnknownCodepageError',
    'UnsupportedEncodingError',
    'UnknownTypeError',
    'UnsupportedMSGTypeError',
    'UnrecognizedMSGTypeError',
    'WKError',
]

# Base exception types.

class ExMsgBaseException(Exception):
    """
    The base class for all custom exceptions the module uses.
    """

# I would want this to also be a subclass of NotImplementedError, but Python
# docs say that CPython can make that a bit problematic due to things from the C
# side of the code.
class FeatureNotImplemented(ExMsgBaseException):
    """
    The base class for a feature not yet being implemented in the module.
    """


# More specific exceptions.


class AmbiguousNamedPropertyError(ExMsgBaseException):
    """
    A named property lookup only provided a textual name that resolves to more
    than one named property definition.

    This happens when the same textual name is used by both a numerical named
    property (whose numerical ID happens to render to the same hexadecimal
    string) and a string named property, or by several string named properties
    in different property sets. The ambiguity cannot be resolved without more
    information, such as the property set GUID or the named property type.

    The :attr:`candidates` attribute contains every
    :class:`~extract_msg.properties.named.NamedPropertyBase` definition that
    matched, allowing the caller to present them as choices.
    """

    def __init__(self, name: str, candidates: list):
        self.name = name
        self.candidates = list(candidates)
        names = ', '.join(
            f'{type(item).__name__} {item.identifier[0]!r} in {item.guid}'
            for item in self.candidates
        )
        super().__init__(
            f'The textual name {name!r} is ambiguous; it matches the '
            f'following named property definitions: {names}. Provide the '
            'property set GUID or the named property type to disambiguate.'
        )


class ConversionError(ExMsgBaseException):
    """
    An error occured during type conversion.
    """

class DataNotFoundError(ExMsgBaseException):
    """
    Requested stream type was unavailable.
    """

class DeencapMalformedData(ExMsgBaseException):
    """
    Data to deencapsulate was malformed in some way.
    """

class DeencapNotEncapsulated(ExMsgBaseException):
    """
    Data to deencapsulate did not contain any encapsulated data.
    """

class DependencyError(ExMsgBaseException):
    """
    An optional dependency could not be found or was unable to be used as
    expected.
    """

class ExecutableNotFound(DependencyError):
    """
    Could not find the specified executable.
    """

class IncompatibleOptionsError(ExMsgBaseException):
    """
    Provided options are incompatible with each other.
    """

class InvalidFileFormatError(ExMsgBaseException):
    """
    An Invalid File Format Error occurred.
    """

class InvalidPropertyIdError(ExMsgBaseException):
    """
    The provided property ID was invalid.
    """

class MimetypeFailureError(ExMsgBaseException):
    """
    The mimetype was unable to be properly determined when it was mandatory.
    """

class NotWritableError(ExMsgBaseException):
    """
    Modification was attempted on an instance that is not writable.
    """

class PrefixError(ExMsgBaseException):
    """
    An issue was detected with the provided prefix.

    This should never occur if you have no manually provided a prefix.
    """

class SecurityError(ExMsgBaseException):
    """
    A code path was triggered that would use an insecure feature, but that
    insecure feature was not enabled.
    """

class StandardViolationError(InvalidFileFormatError):
    """
    A critical violation of the MSG standards was detected and could not be
    recovered from.

    Recoverable violations will result in log messages instead.

    Any that could reasonably be skipped, although are likely to still cause
    errors down the line, can be suppressed.
    """

class TooManySectorsError(ExMsgBaseException):
    """
    Ole writer has too much data to write to the file.
    """

class TZError(ExMsgBaseException):
    """
    Specifically not an OSError to avoid being caught by parts of the module.

    This error represents a fatal error in the datetime parsing as it usually
    means your installation of tzlocal or tzdata are broken. If you have
    received this error after using PyInstaller, you must include the resource
    files for tzdata for it to work properly. See TeamMsgExtractor#272 and
    TeamMsgExtractor#169 for information on why you are getting this error.
    """

class UnknownCodepageError(ExMsgBaseException):
    """
    The codepage provided was not one we know of.
    """

class UnsupportedEncodingError(FeatureNotImplemented):
    """
    The codepage provided is known but is not supported.
    """

class UnknownTypeError(ExMsgBaseException):
    """
    The type specified is not one that is recognized.
    """

class UnsupportedMSGTypeError(FeatureNotImplemented):
    """
    An exception that is raised when an MSG class is recognized by not
    supported.
    """

class UnrecognizedMSGTypeError(ExMsgBaseException):
    """
    An exception that is raised when the module cannot determine how to properly
    open a specific class of MSG file.
    """

class WKError(DependencyError):
    """
    An error occured while running wkhtmltopdf.
    """
