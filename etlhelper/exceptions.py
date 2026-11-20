"""
ETLHelper exceptions are derived from the ETLHelperError base class.

"""


class ETLHelperError(Exception):
    """Base class for exceptions in this module"""


class ETLHelperConnectionError(ETLHelperError):
    """Exception raised for bad database connections"""


class ETLHelperQueryError(ETLHelperError):
    """Exception raised for SQL query errors and similar"""


class ETLHelperDbParamsError(ETLHelperError):
    """Exception raised for bad database parameters"""


class ETLHelperExtractError(ETLHelperError):
    """Exception raised when extracting data."""


class ETLHelperInsertError(ETLHelperError):
    """Exception raised when inserting data."""


class ETLHelperAbort(ETLHelperError):
    """Exception raised when `abort` is called."""


class ETLHelperHelperError(ETLHelperError):
    """Exception raised when helper selection fails."""


class ETLHelperBadIdentifierError(ETLHelperError):
    """
    Exception raised when identifier contains invalid characters.  This may
    indicate an attempt at SQL injection.
    """


class ETLHelperBackfillError(ETLHelperError):
    """Exception raised for invalid or failed resumable backfill operations."""


class ETLHelperCommitUnconfirmedError(ETLHelperBackfillError):
    """
    Exception raised when the outcome of a destination commit cannot be
    confirmed.  The block must not be rewritten; the caller has to reconcile
    the destination before retrying.
    """

    def __init__(self, message, run_id=None, unconfirmed_range=None):
        super().__init__(message)
        self.run_id = run_id
        self.unconfirmed_range = unconfirmed_range
