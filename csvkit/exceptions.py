#!/usr/bin/env python


class CustomException(Exception):
    """
    A base exception that handles pretty-printing errors for command-line tools.
    """

    def __init__(self, msg):
        self.msg = msg

    def __unicode__(self):
        return self.msg

    def __str__(self):
        return self.msg


class ColumnIdentifierError(CustomException):
    """
    Exception raised when the user supplies an invalid column identifier.
    """
    pass


class InvalidValueForTypeException(CustomException):
    """
    Exception raised when a value can not be normalized to a specified type.
    """

    def __init__(self, index, value, normal_type):
        self.index = index
        self.value = value
        self.normal_type = normal_type
        msg = 'Unable to convert "%s" to type %s (at index %i)' % (value, normal_type, index)
        super().__init__(msg)


class RequiredHeaderError(CustomException):
    """
    Exception raised when an operation requires a CSV file to have a header row.
    """
    pass


class FieldBoundaryError(CustomException):
    """
    Exception raised when a fixed-width schema's byte positions split a
    multibyte character in the input.
    """

    def __init__(self, line_number, field, line, encoding):
        self.line_number = line_number
        self.field = field
        self.line = line
        self.encoding = encoding
        msg = (
            'Column "%s" (start byte %i, length %i) splits a multibyte character on line %i '
            'when positions are interpreted as %s bytes. Offending line: %r'
        ) % (field.name, field.start, field.length, line_number, encoding, line)
        super().__init__(msg)
