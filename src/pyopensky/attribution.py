"""Bitemporal attribution of OpenSky positions to registrations.

Two independent time axes are kept apart:

* **valid time** -- half-open ``[valid_from, valid_to)`` intervals matched
  against the OpenSky ``lastposupdate`` timestamp of each position. The
  interval is left-closed and right-open, so a handover at 12:00 keeps the
  11:59:59 sample on the previous registration and moves the 12:00 sample
  to the new one. The collection timestamp (``record_time``) is only kept
  for traceability and never participates in matching.
* **publication time** -- ``published_at`` states when a registry row
  actually became known. Filtering rows by a report cutoff (``as_of``)
  reruns a historical report against exactly the evidence available on
  that day. Rows published later can never alter an old report.

Corrections withdraw a previously published entry of the *same* source
through the ``supersedes`` link. A second issuing channel is therefore
left untouched when one channel corrects its own evidence. Every output
row exposes both the evidence actually adopted and the evidence that was
replaced, so later changes to historical attributions stay auditable.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Union

import pandas as pd

from .time import to_datetime

ATTRIBUTED = "attributed"
CONFLICT = "conflict"
UNREGISTERED = "unregistered"
INVALID_TIME = "invalid_time"

STATUSES = (ATTRIBUTED, CONFLICT, UNREGISTERED, INVALID_TIME)

ENTRY_COLUMNS = (
    "entry_id",
    "icao24",
    "registration",
    "source",
    "valid_from",
    "valid_to",
    "published_at",
    "supersedes",
)

PathLike = Union[str, Path]
timelike = Union[str, float, pd.Timestamp]


@dataclass(frozen=True)
class RegistryEntry:
    """One registration evidence row published by an issuing source."""

    entry_id: str
    icao24: str
    registration: str
    source: str
    valid_from: timelike
    published_at: timelike
    valid_to: timelike | None = None
    supersedes: str | None = None


def _utc(ts: object) -> pd.Timestamp:
    if ts is None or pd.isna(ts):
        return pd.NaT
    return to_datetime(ts)


def entries_to_frame(entries: Iterable[RegistryEntry]) -> pd.DataFrame:
    """Convert registry entries to the normalized frame used by the engine."""
    rows = [
        {
            "entry_id": entry.entry_id,
            "icao24": entry.icao24.lower(),
            "registration": entry.registration,
            "source": entry.source,
            "valid_from": _utc(entry.valid_from),
            "valid_to": _utc(entry.valid_to),
            "published_at": _utc(entry.published_at),
            "supersedes": entry.supersedes,
        }
        for entry in entries
    ]
    frame = pd.DataFrame(rows, columns=list(ENTRY_COLUMNS))
    return _validate(frame)


def load_registry(path: PathLike) -> list[RegistryEntry]:
    """Load a registry CSV (UTF-8, offline, no network access needed)."""
    frame = pd.read_csv(path, dtype={"supersedes": "string"})
    frame["icao24"] = frame["icao24"].str.lower()
    for column in ("valid_from", "valid_to", "published_at"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    entries = [
        RegistryEntry(
            entry_id=row.entry_id,
            icao24=row.icao24,
            registration=row.registration,
            source=row.source,
            valid_from=row.valid_from,
            valid_to=None if pd.isna(row.valid_to) else row.valid_to,
            published_at=row.published_at,
            supersedes=None
            if pd.isna(row.supersedes)
            else str(row.supersedes),
        )
        for row in frame.itertuples(index=False)
    ]
    return entries


def load_observations(path: PathLike) -> pd.DataFrame:
    """Load an OpenSky-like position CSV with ``lastposupdate`` timestamps."""
    frame = pd.read_csv(path)
    frame["icao24"] = frame["icao24"].str.lower()
    for column in ("lastposupdate", "record_time"):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], utc=True)
    if "obs_id" not in frame.columns:
        frame["obs_id"] = [f"obs-{index}" for index in range(len(frame))]
    return frame


def _validate(frame: pd.DataFrame) -> pd.DataFrame:
    if frame["entry_id"].duplicated().any():
        duplicated = frame["entry_id"].duplicated()
        duplicate = frame.loc[duplicated, "entry_id"].iloc[0]
        raise ValueError(f"Duplicate registry entry_id: {duplicate}")
    if frame["valid_from"].isna().any() or frame["published_at"].isna().any():
        raise ValueError("valid_from and published_at are mandatory")
    bounded = frame["valid_to"].notna()
    if bounded.any():
        invalid = (
            frame.loc[bounded, "valid_to"]
            <= frame.loc[bounded, "valid_from"]
        )
        if invalid.any():
            raise ValueError(
                "valid_to must be strictly greater than valid_from"
            )
    by_id = frame.set_index("entry_id")
    for row in frame.itertuples(index=False):
        if row.supersedes is None or pd.isna(row.supersedes):
            continue
        if row.supersedes not in by_id.index:
            raise ValueError(
                f"Entry {row.entry_id} supersedes unknown entry "
                f"{row.supersedes}"
            )
        target = by_id.loc[row.supersedes]
        if target["icao24"] != row.icao24 or target["source"] != row.source:
            raise ValueError(
                f"Entry {row.entry_id} may only supersede an entry of the "
                f"same icao24 and source, found {row.supersedes}"
            )
    return frame.sort_values("entry_id").reset_index(drop=True)


class AttributionEngine:
    """Attribute positions to registrations for one publication cutoff.

    The engine is immutable: ``as_of`` returns a new engine restricted to
    rows published by the cutoff. Results do not depend on the ordering of
    the input rows.
    """

    def __init__(self, entries: pd.DataFrame | Iterable[RegistryEntry]):
        if isinstance(entries, pd.DataFrame):
            self._entries = _validate(entries.copy())
        else:
            self._entries = entries_to_frame(entries)
        self._superseded: set[str] = set(
            self._entries.loc[
                self._entries["supersedes"].notna(), "supersedes"
            ]
        )
        self._superseder: dict[str, str] = dict(
            zip(
                self._entries.loc[
                    self._entries["supersedes"].notna(), "supersedes"
                ],
                self._entries.loc[
                    self._entries["supersedes"].notna(), "entry_id"
                ],
            )
        )

    def as_of(self, cutoff: timelike) -> "AttributionEngine":
        """Return an engine seeing only rows published by ``cutoff``."""
        moment = to_datetime(cutoff)
        visible = self._entries.loc[
            self._entries["published_at"] <= moment
        ].copy()
        return AttributionEngine(visible)

    @property
    def entry_ids(self) -> tuple[str, ...]:
        return tuple(self._entries["entry_id"])

    def _covering(self, icao24: str, moment: pd.Timestamp) -> pd.DataFrame:
        entries = self._entries
        mask = (
            (entries["icao24"] == icao24)
            & (entries["valid_from"] <= moment)
            & (entries["valid_to"].isna() | (entries["valid_to"] > moment))
        )
        return entries.loc[mask].sort_values("entry_id")

    @staticmethod
    def _evidence_label(row: Any) -> str:
        return f"{row.entry_id}@{row.source}:{row.registration}"

    def attribute(self, observations: pd.DataFrame) -> pd.DataFrame:
        """Attribute every position, one row per observation.

        Statuses are ``attributed`` (one registration, possibly several
        agreeing evidences), ``conflict`` (several registrations),
        ``unregistered`` (no effective entry at the position time) and
        ``invalid_time`` (missing ``lastposupdate``).
        """
        records: list[dict[str, object]] = []
        for row in observations.itertuples(index=False):
            record: dict[str, object] = {
                "obs_id": getattr(row, "obs_id", None),
                "icao24": row.icao24,
                "position_time": getattr(row, "lastposupdate", pd.NaT),
                "record_time": getattr(row, "record_time", pd.NaT),
                "lat": getattr(row, "lat", pd.NA),
                "lon": getattr(row, "lon", pd.NA),
            }
            position_time = record["position_time"]
            if position_time is None or pd.isna(position_time):
                record.update(
                    status=INVALID_TIME,
                    registrations=(),
                    adopted=None,
                    evidence_ids=(),
                    evidence=(),
                    superseded_evidence=(),
                )
                records.append(record)
                continue

            covering = self._covering(row.icao24, position_time)
            replaced_mask = covering["entry_id"].isin(self._superseded)
            alive = covering.loc[~replaced_mask]
            replaced = covering.loc[replaced_mask]
            registrations = tuple(sorted(alive["registration"].unique()))

            if not registrations:
                status = UNREGISTERED
                adopted = None
            elif len(registrations) == 1:
                status = ATTRIBUTED
                adopted = registrations[0]
            else:
                status = CONFLICT
                adopted = None

            record.update(
                status=status,
                registrations=registrations,
                adopted=adopted,
                evidence_ids=tuple(alive["entry_id"]),
                evidence=tuple(
                    self._evidence_label(alive_row)
                    for alive_row in alive.itertuples(index=False)
                ),
                superseded_evidence=tuple(
                    self._replacement_label(replaced_row)
                    for replaced_row in replaced.itertuples(index=False)
                ),
            )
            records.append(record)

        return pd.DataFrame(
            records,
            columns=[
                "obs_id",
                "icao24",
                "position_time",
                "record_time",
                "lat",
                "lon",
                "status",
                "registrations",
                "adopted",
                "evidence_ids",
                "evidence",
                "superseded_evidence",
            ],
        )

    def _replacement_label(self, row: Any) -> str:
        return (
            f"{row.entry_id}@{row.source}:{row.registration} "
            f"replaced_by {self._superseder[row.entry_id]}"
        )


def diff_reports(earlier: pd.DataFrame, later: pd.DataFrame) -> pd.DataFrame:
    """Compare two report runs and return the attributions that changed."""
    columns = ["obs_id", "status", "adopted", "evidence_ids"]
    merged = earlier[columns].merge(
        later[columns],
        on="obs_id",
        suffixes=("_earlier", "_later"),
        how="outer",
    )

    def added(row: pd.Series) -> tuple[str, ...]:
        earlier = row.evidence_ids_earlier
        later = row.evidence_ids_later
        if not isinstance(earlier, tuple) or not isinstance(later, tuple):
            return later if isinstance(later, tuple) else ()
        return tuple(
            entry_id for entry_id in later if entry_id not in earlier
        )

    def removed(row: pd.Series) -> tuple[str, ...]:
        earlier = row.evidence_ids_earlier
        later = row.evidence_ids_later
        if not isinstance(earlier, tuple) or not isinstance(later, tuple):
            return earlier if isinstance(earlier, tuple) else ()
        return tuple(
            entry_id for entry_id in earlier if entry_id not in later
        )

    merged["evidence_added"] = merged.apply(added, axis=1)
    merged["evidence_removed"] = merged.apply(removed, axis=1)
    status_changed = merged["status_earlier"] != merged["status_later"]
    adopted_changed = ~merged["adopted_earlier"].fillna("").eq(
        merged["adopted_later"].fillna("")
    )
    changed = merged.loc[
        status_changed | adopted_changed
    ].reset_index(drop=True)
    return changed


def run_report(
    registry_path: PathLike,
    observations_path: PathLike,
    cutoff: timelike,
) -> pd.DataFrame:
    """Convenience helper: load both CSVs and rerun a report at ``cutoff``."""
    engine = AttributionEngine(load_registry(registry_path))
    observations = load_observations(observations_path)
    return engine.as_of(cutoff).attribute(observations)
