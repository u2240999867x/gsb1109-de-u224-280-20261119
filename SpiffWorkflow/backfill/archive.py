"""Durable process archive used as the coordinator's source of truth.

The archive is intentionally separate from the warehouse and approval service.
Writes here are local and can lag behind external acceptance; callers must not
treat a successful archive write as confirmation that the warehouse accepted
anything.  Task states and credentials are kept as histories so a later
rejection or recovery never erases what an operator originally saw.
"""

from __future__ import annotations

from threading import RLock
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import ArchiveNotFoundError, PreviewChangedError
from .model import (
    ArchiveSnapshot,
    CommitReceipt,
    OutputTarget,
    Preview,
    PreviewStatus,
    StagedResult,
    WarehouseVersion,
)


class ArchiveStore:
    """Local process archive with optimistic concurrency on previews."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._previews: Dict[str, Preview] = {}
        self._staged: Dict[str, List[StagedResult]] = {}
        self._receipts: Dict[str, CommitReceipt] = {}
        self._preview_revisions: Dict[str, int] = {}
        self._task_states: Dict[Tuple[str, str], List[Dict[str, object]]] = {}
        self._credentials: Dict[Tuple[str, str], List[Dict[str, object]]] = {}
        self._local_versions: Dict[str, Dict[str, object]] = {}
        self._accepted_cache: Dict[str, WarehouseVersion] = {}
        self._contradictions: List[str] = []
        self._submission_order: List[str] = []

    # -- previews ---------------------------------------------------------

    def save_preview(self, preview: Preview, expected_revision: Optional[int] = None) -> int:
        with self._lock:
            current = self._preview_revisions.get(preview.preview_id, 0)
            if expected_revision is not None and expected_revision != current:
                raise PreviewChangedError(
                    f"preview {preview.preview_id} changed: expected revision "
                    f"{expected_revision}, found {current}"
                )
            self._previews[preview.preview_id] = preview
            revision = current + 1
            self._preview_revisions[preview.preview_id] = revision
            return revision

    def get_preview(self, preview_id: str) -> Preview:
        with self._lock:
            try:
                return self._previews[preview_id]
            except KeyError:
                raise ArchiveNotFoundError(f"preview {preview_id} is not archived")

    def revision_of(self, preview_id: str) -> int:
        with self._lock:
            return self._preview_revisions.get(preview_id, 0)

    def previews(self, status: Optional[PreviewStatus] = None) -> List[Preview]:
        with self._lock:
            values = list(self._previews.values())
        if status is not None:
            values = [p for p in values if p.status == status]
        return sorted(values, key=lambda p: (p.created_at, p.preview_id))

    def previews_for_archive(self, archive_id: str) -> List[Preview]:
        return [p for p in self.previews() if p.request.archive_id == archive_id]

    def previews_for_table(self, table: str) -> List[Preview]:
        return [p for p in self.previews() if table in p.impact.affected_tables]

    # -- staged results ---------------------------------------------------

    def save_staged(self, result: StagedResult) -> None:
        with self._lock:
            existing = self._staged.setdefault(result.preview_id, [])
            for index, item in enumerate(existing):
                if item.write_id == result.write_id:
                    existing[index] = result
                    return
            existing.append(result)

    def staged(self, preview_id: str) -> List[StagedResult]:
        with self._lock:
            return list(self._staged.get(preview_id, ()))

    def staged_result(self, preview_id: str, write_id: str) -> Optional[StagedResult]:
        with self._lock:
            for item in self._staged.get(preview_id, ()):
                if item.write_id == write_id:
                    return item
        return None

    def all_staged(self) -> List[StagedResult]:
        with self._lock:
            return [item for items in self._staged.values() for item in items]

    def discard_staged(self, preview_id: str) -> List[StagedResult]:
        """Make staged results processable while retaining the preview record."""

        with self._lock:
            return list(self._staged.pop(preview_id, ()))

    # -- receipts ---------------------------------------------------------

    def save_receipt(self, receipt: CommitReceipt) -> None:
        with self._lock:
            previous = self._receipts.get(receipt.preview_id)
            self._receipts[receipt.preview_id] = receipt
            if previous is None:
                self._submission_order.append(receipt.preview_id)

    def receipt(self, preview_id: str) -> Optional[CommitReceipt]:
        with self._lock:
            return self._receipts.get(preview_id)

    def receipts(self) -> List[CommitReceipt]:
        with self._lock:
            return list(self._receipts.values())

    def submission_order(self) -> List[str]:
        with self._lock:
            return list(self._submission_order)

    # -- task state history ----------------------------------------------

    def record_task_state(self, station: str, task_id: str, state: Mapping[str, object]) -> None:
        with self._lock:
            history = self._task_states.setdefault((station, task_id), [])
            record = dict(state)
            record.setdefault("recorded_at", None)
            history.append(record)

    def task_history(self, station: str, task_id: str) -> List[Mapping[str, object]]:
        with self._lock:
            return [dict(item) for item in self._task_states.get((station, task_id), ())]

    def task_state(self, station: str, task_id: str) -> Optional[Mapping[str, object]]:
        history = self.task_history(station, task_id)
        return history[-1] if history else None

    def all_task_states(self) -> Mapping[Tuple[str, str], Mapping[str, object]]:
        with self._lock:
            result = {}
            for key, history in self._task_states.items():
                result[key] = dict(history[-1])
            return result

    # -- credential history ----------------------------------------------

    def record_credential(self, station: str, name: str, claims: Mapping[str, object]) -> None:
        with self._lock:
            history = self._credentials.setdefault((station, name), [])
            history.append(dict(claims))

    def credential_history(self, station: str, name: str) -> List[Mapping[str, object]]:
        with self._lock:
            return [dict(item) for item in self._credentials.get((station, name), ())]

    def credential(self, station: str, name: str) -> Optional[Mapping[str, object]]:
        history = self.credential_history(station, name)
        return history[-1] if history else None

    def all_credentials(self) -> Mapping[Tuple[str, str], Mapping[str, object]]:
        with self._lock:
            return {
                key: dict(history[-1])
                for key, history in self._credentials.items()
                if history
            }

    # -- local version bookkeeping ---------------------------------------

    def record_local_version(
        self,
        target: OutputTarget,
        version: int,
        saved_at: Optional[object] = None,
        source: str = "archive",
    ) -> None:
        """Remember what this archive has actually persisted.

        This is deliberately *not* consulted by the warehouse.  It represents
        the local view a process would reload after a crash, and may lag the
        warehouse.
        """

        with self._lock:
            self._local_versions[target.key()] = {
                "version": version,
                "saved_at": saved_at,
                "source": source,
                "target": target,
            }

    def local_versions(self) -> Mapping[str, int]:
        with self._lock:
            return {key: int(entry["version"]) for key, entry in self._local_versions.items()}

    def local_version(self, target: OutputTarget) -> Optional[int]:
        with self._lock:
            entry = self._local_versions.get(target.key())
            return None if entry is None else int(entry["version"])

    def local_version_entries(self) -> Mapping[str, Mapping[str, object]]:
        with self._lock:
            return {key: dict(entry) for key, entry in self._local_versions.items()}

    def remember_accepted(self, version: WarehouseVersion) -> None:
        with self._lock:
            self._accepted_cache[version.key()] = version

    def cached_accepted(self) -> Mapping[str, WarehouseVersion]:
        with self._lock:
            return dict(self._accepted_cache)

    def note_contradiction(self, message: str) -> None:
        with self._lock:
            self._contradictions.append(message)

    def contradictions(self) -> List[str]:
        with self._lock:
            return list(self._contradictions)

    # -- bulk operations --------------------------------------------------

    def snapshot(self) -> ArchiveSnapshot:
        with self._lock:
            return ArchiveSnapshot(
                previews=tuple(self._previews.values()),
                staged=tuple(item for items in self._staged.values() for item in items),
                receipts=tuple(self._receipts.values()),
                task_states=self.all_task_states(),
                credentials=self.all_credentials(),
            )

    def supersede_open_previews(
        self,
        except_preview_id: Optional[str] = None,
        versions: Sequence[WarehouseVersion] = (),
    ) -> List[Preview]:
        """Retire drafts that a newly accepted revision made obsolete.

        A draft is superseded only when it targets the same output or when its
        declared inputs referenced an older version of that output.  This is
        deliberately narrower than "shares a table": accepting a station's
        evidence must not silently retire an unrelated station's draft.
        Committing previews are left alone: they are in flight and must be
        reconciled rather than silently retargeted.
        """

        accepted_keys = {version.key() for version in versions}
        advanced = {
            version.key(): version.version
            for version in versions
        }
        changed = []
        with self._lock:
            for preview_id, preview in list(self._previews.items()):
                if preview_id == except_preview_id:
                    continue
                if preview.status not in {PreviewStatus.DRAFT, PreviewStatus.CONFIRMED}:
                    continue
                if any(w.target.key() in accepted_keys for w in preview.writes):
                    obsolete = True
                else:
                    obsolete = any(
                        dependency_key in advanced
                        and expected < advanced[dependency_key]
                        for w in preview.writes
                        for dependency_key, expected in w.input_versions.items()
                    )
                if not obsolete:
                    continue
                preview = preview.with_status(PreviewStatus.SUPERSEDED)
                self._previews[preview_id] = preview
                self._preview_revisions[preview_id] = self._preview_revisions.get(preview_id, 0) + 1
                changed.append(preview)
        return changed
