"""Archivable, mergeable station-ingest progress built on xarray.

The package tracks per-collector submissions, partial revocations and sealing
receipts, persists progress to NetCDF and merges independently collected
progress files without ever reviving superseded or revoked observations.
"""

from station_ingest.core import (
    OBS_MISSING,
    OBS_VALUE,
    STATE_CONFLICT,
    STATE_MISSING,
    STATE_UNCOVERED,
    STATE_VALUE,
    CellKey,
    ConflictingRecordError,
    DuplicateRecordError,
    GridView,
    IngestProgress,
    PrunedRecordError,
    Revocation,
    SealedTargetError,
    SealingReceipt,
    Submission,
    consensus_grid,
    merge_progresses,
    obs_missing,
    obs_value,
)
from station_ingest.netcdfio import load_progress, save_progress

__all__ = [
    "OBS_MISSING",
    "OBS_VALUE",
    "STATE_CONFLICT",
    "STATE_MISSING",
    "STATE_UNCOVERED",
    "STATE_VALUE",
    "CellKey",
    "ConflictingRecordError",
    "DuplicateRecordError",
    "GridView",
    "IngestProgress",
    "PrunedRecordError",
    "Revocation",
    "SealedTargetError",
    "SealingReceipt",
    "Submission",
    "consensus_grid",
    "load_progress",
    "merge_progresses",
    "obs_missing",
    "obs_value",
    "save_progress",
]
