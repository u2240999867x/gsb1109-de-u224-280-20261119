class BackfillError(Exception):
    """Base class for backfill coordination errors."""


class PreviewNotFoundError(BackfillError):
    pass


class PreviewStateError(BackfillError):
    pass


class PreviewChangedError(BackfillError):
    pass


class ApprovalError(BackfillError):
    pass


class GrantNotPermittedError(ApprovalError):
    pass


class UnauthorizedOperationError(BackfillError):
    pass


class CrossStationImpactError(BackfillError):
    pass


class ArchiveError(BackfillError):
    pass


class ArchiveNotFoundError(ArchiveError):
    pass


class ArchiveWriteError(ArchiveError):
    pass


class WarehouseError(BackfillError):
    pass


class UnknownOutputError(WarehouseError):
    pass


class StaleVersionError(WarehouseError):
    pass
