"""Exceptions.

Exceptions relating to problems with BIDS itself, should carry BIDS in their
name.  All exceptions should subclass from PyBIDSError
"""


class PyBIDSError(Exception):
    """Base class.  Typically for mix-in."""


class ConfigError(ValueError, PyBIDSError):
    """Problems with config file."""


class NoMatchError(ValueError, PyBIDSError):
    """No match found where it is required."""


class BIDSEntityError(AttributeError, PyBIDSError):
    """An unknown entity."""


class TargetError(ValueError, PyBIDSError):
    """An unknown target."""


class BIDSValidationError(ValueError, PyBIDSError):
    """An invalid BIDS dataset."""


class BIDSDerivativesValidationError(BIDSValidationError):
    """An invalid BIDS derivative dataset."""


class BIDSConflictingValuesError(BIDSValidationError):
    """A value conflict (e.g. in filename and side-car .json)"""


class BIDSChildDatasetError(ValueError, PyBIDSError):
    """An invalid BIDS dataset."""


class MirrorError(PyBIDSError):
    """Base class for mirror/archive planning problems."""


class UnregisteredRootError(MirrorError):
    """A source URI does not belong to any registered archive root.

    Raised (or reported) when a string prefix is close to a registered root
    but is not contained by one, so that approximate prefixes cannot be
    silently treated as a known archive.
    """


class PlanValidationError(MirrorError):
    """A saved download plan can no longer be honored.

    This covers stale plans whose pinned content is superseded, whose source
    evidence is inaccessible, or whose target is outside the permitted mirror
    scope.
    """
