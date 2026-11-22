"""Durable evidence for jobs that update existing output files.

The regular persistence layer records only the *current* provenance of a file.
That is not enough for ``update()`` outputs, because an updated file can be
restored to its previous bytes after a failed attempt.  In that situation the
path, its content and its modification time are indistinguishable from the
state before the attempt, even though the attempt really did write something.

This module keeps three deliberately separate kinds of evidence below
``.snakemake/update_attempts``:

``attempts/<attempt>.json``
    One record per job attempt.  It captures the declared outputs, whether
    each output was pre-existing, which bytes were used as the isolated work
    candidate, what the candidate looked like afterwards, and which outputs
    were registered as committed.

``groups/<group>/attempts/<attempt>.json``
    A *consumable* group record.  It is written only after every declared
    output of the job (or group job) has been registered.  A crash between the
    per-output registrations and this record therefore leaves outputs on disk
    without a group commit basis; such outputs must not be reused.

``restores.jsonl``
    Append-only log of backup restore actions.  A restore is what makes an
    updated path readable again after a failure, so it must not be mistaken
    for production by the current round.

A crash-test hook is provided for deterministic tests.  Setting
``SNAKEMAKE_UPDATE_TEST_CRASH`` to a comma separated list of stage names makes
the process kill itself (SIGKILL) at that stage after the journal has been
fsynced.  This never happens unless the variable is set.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import signal
import stat
import time
from pathlib import Path
from typing import Any, Iterable

from snakemake.io import is_flagged
from snakemake.logging import logger

JOURNAL_FORMAT_VERSION = 1
CHUNK_SIZE = 1024 * 1024


def _encode(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def file_stat(path: str | os.PathLike, hash_content: bool = True) -> dict[str, Any]:
    """Return a compact, stable description of a path.

    ``ctime`` and the inode are included on purpose.  They make it possible to
    observe that a job rewrote a file even when the resulting bytes and mtime
    are identical to the previous version.
    """
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return {"exists": False}

    is_dir = stat.S_ISDIR(st.st_mode)
    is_link = stat.S_ISLNK(st.st_mode)
    result: dict[str, Any] = {
        "exists": True,
        "is_dir": is_dir,
        "is_link": is_link,
        "size": st.st_size if not is_dir else None,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
        "inode": st.st_ino,
    }
    if hash_content and not is_dir and not is_link:
        try:
            result["sha256"] = _sha256(path)
        except OSError as err:  # pragma: no cover - raced with deletion
            result["sha256"] = None
            result["hash_error"] = str(err)
    return result


def _stat_differs(first: dict[str, Any] | None, second: dict[str, Any] | None) -> bool:
    """Whether two stat descriptions prove that a write happened."""
    if second is None or not second.get("exists"):
        return False
    if first is None or not first.get("exists"):
        return True
    for key in ("sha256", "size", "mtime_ns", "ctime_ns", "inode"):
        if first.get(key) != second.get(key):
            return True
    return False


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    with open(tmp, "w") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(value, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with open(path, "r") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return


def maybe_crash(stage: str) -> None:
    """Kill this process at a named test barrier (only if requested)."""
    requested = os.environ.get("SNAKEMAKE_UPDATE_TEST_CRASH", "")
    if not requested:
        return
    stages = {item.strip() for item in requested.split(",") if item.strip()}
    if stage not in stages:
        return
    logger.warning(f"update-attempt test barrier reached: {stage}; killing process.")
    # Make sure the journal is durable before the simulated crash.
    os.sync()
    os.kill(os.getpid(), signal.SIGKILL)


class UpdateAttemptStore:
    """Filesystem journal for update attempts, group commits and restores."""

    def __init__(self, path: Path):
        self.root = Path(path) / "update_attempts"
        self._attempts = self.root / "attempts"
        self._groups = self.root / "groups"
        self._snapshots = self.root / "snapshots"
        self._by_output = self.root / "by_output"
        self._restores = self.root / "restores.jsonl"
        for directory in (
            self._attempts,
            self._groups,
            self._snapshots,
            self._by_output,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # identifiers
    # ------------------------------------------------------------------
    @staticmethod
    def attempt_id(run_id: str, group_id: str, attempt: int | str) -> str:
        return f"{run_id}:{group_id}:{attempt}"

    def _attempt_path(self, attempt_id: str) -> Path:
        return self._attempts / f"{_encode(attempt_id)}.json"

    def _group_dir(self, group_id: str) -> Path:
        return self._groups / _encode(group_id)

    def _group_path(self, group_id: str, attempt_id: str) -> Path:
        return self._group_dir(group_id) / "attempts" / f"{_encode(attempt_id)}.json"

    def _snapshot_dir(self, attempt_id: str) -> Path:
        return self._snapshots / _encode(attempt_id)

    def _snapshot_path(self, attempt_id: str, output: str) -> Path:
        return self._snapshot_dir(attempt_id) / _encode(output)

    def work_dir(self, attempt_id: str) -> Path:
        return self.root / "work" / _encode(attempt_id)

    def _work_copy(self, attempt_id: str, kind: str, output: str) -> Path:
        return self.work_dir(attempt_id) / kind / _encode(output)

    def _pre_path(self, attempt_id: str, output: str) -> Path:
        return self._work_copy(attempt_id, "pre", output)

    def _ensure_pre_copy(self, attempt_id: str, output: str, source: str) -> str | None:
        """Persist the bytes as they were before the attempt started."""
        target = self._pre_path(attempt_id, output)
        if target.exists():
            return str(target)
        if not os.path.exists(source):
            return None
        try:
            self._copy_path(source, target)
        except OSError as err:  # pragma: no cover - permissions
            logger.warning(f"Could not persist pre-attempt bytes for {source}: {err}")
            return None
        return str(target)

    @staticmethod
    def _copy_path(source: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.isdir(source) and not os.path.islink(source):
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target, copy_function=shutil.copy2)
        else:
            if target.exists():
                target.unlink()
            shutil.copy2(source, target)

    def save_seed(self, attempt_id: str, path: str, source: str) -> str | None:
        """Persist a copy of the bytes an isolated candidate is seeded from."""
        if not os.path.exists(source):
            return None
        target = self._work_copy(attempt_id, "seed", path)
        try:
            self._copy_path(source, target)
        except OSError as err:  # pragma: no cover - permissions
            logger.warning(f"Could not persist update seed for {source}: {err}")
            return None
        return str(target)

    def save_candidate(self, attempt_id: str, path: str, source: str) -> str | None:
        """Persist the isolated candidate bytes as soon as they are observed."""
        if not os.path.exists(source):
            return None
        target = self._work_copy(attempt_id, "candidate", path)
        try:
            self._copy_path(source, target)
        except OSError as err:  # pragma: no cover - permissions
            logger.warning(f"Could not persist update candidate for {source}: {err}")
            return None
        return str(target)

    # ------------------------------------------------------------------
    # attempt lifecycle
    # ------------------------------------------------------------------
    def read_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        record = _read_json(self._attempt_path(attempt_id))
        if not record or not record.get("attempt_id"):
            return None
        return record

    def begin(
        self,
        *,
        attempt_id: str,
        run_id: str,
        group_id: str,
        job_id: str,
        rule: str,
        attempt: int,
        shadow: bool,
        outputs: Iterable[str],
        update_outputs: Iterable[str],
        previous_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        update_outputs = set(update_outputs)
        entries = {}
        for output in outputs:
            entries[output] = {
                "path": output,
                "is_update": output in update_outputs,
                "pre_existing": False,
                "seed_source": None,
                "seed_stat": None,
                "candidate_stat": None,
                "write_observed": None,
                "produced": False,
                "registered": False,
                "committed": False,
                "restored": False,
                "error": None,
            }
        record = {
            "attempt_id": attempt_id,
            "run_id": run_id,
            "group_id": group_id,
            "job_id": job_id,
            "rule": rule,
            "attempt": attempt,
            "shadow": shadow,
            "started_at": time.time(),
            "ended_at": None,
            "status": "prepared",
            "error": None,
            "outputs": entries,
            "previous_metadata": previous_metadata,
            "journal_format_version": JOURNAL_FORMAT_VERSION,
        }
        existing = self.read_attempt(attempt_id)
        if existing is not None:
            # Several jobs of one group share the attempt.  Do not reset it
            # while the group is still being executed.
            return existing
        _atomic_json(self._attempt_path(attempt_id), record)

        # Keep the pre-attempt bytes for recovery.  A later crash may leave a
        # partially written file at the final path, and there may not yet be
        # any complete group to fall back to.
        for output in update_outputs:
            stat_info = file_stat(output)
            if stat_info.get("exists"):
                pre = self._ensure_pre_copy(attempt_id, output, output)
                if pre is not None:
                    entry = record["outputs"].setdefault(output, {"path": output})
                    entry["pre_copy"] = pre
        return record

    def set_pre_existing(self, attempt_id: str, path: str, stat_info: dict) -> None:
        record = self.read_attempt(attempt_id)
        if record is None:
            return
        entry = record.get("outputs", {}).setdefault(path, {"path": path})
        entry["pre_existing"] = bool(stat_info.get("exists"))
        entry["pre_stat"] = stat_info
        _atomic_json(self._attempt_path(attempt_id), record)
        _append_jsonl(
            self._by_output / f"{_encode(path)}.jsonl",
            {
                "attempt_id": attempt_id,
                "run_id": record.get("run_id"),
                "group_id": record.get("group_id"),
                "job_id": record.get("job_id"),
                "rule": record.get("rule"),
                "attempt": record.get("attempt"),
                "event": "seen_before_attempt",
                "pre_existing": entry["pre_existing"],
                "at": time.time(),
            },
        )

    def set_seed(
        self, attempt_id: str, path: str, source: str, stat_info: dict
    ) -> None:
        record = self.read_attempt(attempt_id)
        if record is None:
            return
        entry = record.get("outputs", {}).setdefault(path, {"path": path})
        entry["seed_source"] = source
        entry["seed_stat"] = stat_info
        _atomic_json(self._attempt_path(attempt_id), record)

    def set_candidate(
        self,
        attempt_id: str,
        path: str,
        stat_info: dict,
        *,
        candidate_location: str | None = None,
    ) -> None:
        record = self.read_attempt(attempt_id)
        if record is None:
            return
        entry = record.get("outputs", {}).setdefault(path, {"path": path})
        entry["candidate_stat"] = stat_info
        if candidate_location is not None:
            entry["candidate_location"] = candidate_location
        entry["write_observed"] = _stat_differs(entry.get("seed_stat"), stat_info)
        entry["produced"] = bool(
            entry.get("is_update") and entry["write_observed"]
        ) or bool(
            not entry.get("is_update") and stat_info.get("exists")
        )
        _atomic_json(self._attempt_path(attempt_id), record)

    def register_output(
        self, attempt_id: str, path: str, stat_info: dict, *, location: str = "final"
    ) -> None:
        record = self.read_attempt(attempt_id)
        if record is None:
            return
        entry = record.get("outputs", {}).setdefault(path, {"path": path})
        if entry.get("candidate_stat") is None:
            # Keep the isolated-candidate stat when there is one; the final-path
            # stat is recorded separately as committed_stat.
            entry["candidate_stat"] = stat_info
        entry["committed_stat"] = stat_info
        entry["commit_location"] = location
        entry["registered"] = True
        entry["committed"] = False
        if entry.get("write_observed") is None:
            entry["write_observed"] = _stat_differs(
                entry.get("seed_stat"), stat_info
            )
        if entry.get("is_update"):
            entry["produced"] = bool(entry["write_observed"])
        else:
            entry["produced"] = bool(stat_info.get("exists"))
        _atomic_json(self._attempt_path(attempt_id), record)
        _append_jsonl(
            self._by_output / f"{_encode(path)}.jsonl",
            {
                "attempt_id": attempt_id,
                "run_id": record.get("run_id"),
                "group_id": record.get("group_id"),
                "job_id": record.get("job_id"),
                "rule": record.get("rule"),
                "attempt": record.get("attempt"),
                "event": "registered",
                "produced": entry["produced"],
                "write_observed": entry.get("write_observed"),
                "sha256": stat_info.get("sha256"),
                "size": stat_info.get("size"),
                "mtime_ns": stat_info.get("mtime_ns"),
                "at": time.time(),
            },
        )

    def finish(self, attempt_id: str, *, success: bool, error: str | None = None) -> None:
        record = self.read_attempt(attempt_id) or {}
        record["ended_at"] = time.time()
        record["status"] = "succeeded" if success else "failed"
        record["error"] = error
        _atomic_json(self._attempt_path(attempt_id), record)

    # ------------------------------------------------------------------
    # group commits
    # ------------------------------------------------------------------
    def write_group(
        self,
        *,
        group_id: str,
        attempt_id: str,
        run_id: str,
        job_id: str,
        rule: str,
        outputs: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Persist the consumable record for a complete group.

        A group is only consumable if every declared output is present and has
        been explicitly registered for this attempt.  ``outputs`` maps
        path -> stat/location information.
        """
        record = self.read_attempt(attempt_id) or {}
        declared = record.get("outputs", {})
        missing = [path for path, info in outputs.items() if not info.get("exists")]
        unregistered = [
            path
            for path in outputs
            if not (declared.get(path) or {}).get("registered")
        ]
        if missing or unregistered or (declared and set(declared) != set(outputs)):
            return None
        record_out = {
            "group_id": group_id,
            "attempt_id": attempt_id,
            "run_id": run_id,
            "job_id": job_id,
            "rule": rule,
            "committed_at": time.time(),
            "complete": True,
            "outputs": outputs,
            "journal_format_version": JOURNAL_FORMAT_VERSION,
        }
        _atomic_json(self._group_path(group_id, attempt_id), record_out)

        for path in outputs:
            entry = declared.get(path)
            if entry is not None:
                entry["committed"] = True
        if record:
            _atomic_json(self._attempt_path(attempt_id), record)

        # Keep a snapshot of the committed bytes.  It is used when a later
        # attempt crashes before committing: the next run must seed its
        # isolated candidate from the last complete result, not from the
        # in-flight partial file.
        snapshot_root = self._snapshot_dir(attempt_id)
        for path in outputs:
            source = outputs[path].get("local_path")
            if not source or not os.path.exists(source):
                continue
            target = self._snapshot_path(attempt_id, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                if os.path.isdir(source) and not os.path.islink(source):
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(source, target, copy_function=shutil.copy2)
                else:
                    shutil.copy2(source, target)
            except OSError as err:  # pragma: no cover - permission issues
                logger.warning(
                    f"Could not snapshot committed update output {source}: {err}"
                )
        return record_out

    def read_group(self, group_id: str, attempt_id: str) -> dict[str, Any] | None:
        return _read_json(self._group_path(group_id, attempt_id))

    def latest_complete_group(self, group_id: str) -> dict[str, Any] | None:
        attempts_dir = self._group_dir(group_id) / "attempts"
        if not attempts_dir.is_dir():
            return None
        records = []
        for item in attempts_dir.iterdir():
            if item.suffix != ".json":
                continue
            record = _read_json(item)
            if record and record.get("complete"):
                records.append(record)
        if not records:
            return None
        return max(records, key=lambda record: record.get("committed_at", 0))

    def group_is_complete(self, group_id: str, attempt_id: str) -> bool:
        record = self.read_group(group_id, attempt_id)
        return bool(record and record.get("complete"))

    def snapshot_for(self, group_id: str, path: str) -> str | None:
        """Return the snapshot path of ``path`` in the latest complete group."""
        record = self.latest_complete_group(group_id)
        if record is None:
            return None
        if path not in record.get("outputs", {}):
            return None
        snapshot = self._snapshot_path(record["attempt_id"], path)
        return str(snapshot) if os.path.exists(snapshot) else None

    def attempt_has_uncommitted_output(self, path: str) -> bool:
        """Whether the newest attempt touching ``path`` lacks a group commit."""
        attempts = []
        for line in _iter_jsonl(self._by_output / f"{_encode(path)}.jsonl"):
            if line.get("event") == "registered":
                attempts.append(line)
        if not attempts:
            return False
        latest = max(attempts, key=lambda item: item.get("at", 0))
        return not self.group_is_complete(
            latest.get("group_id"), latest.get("attempt_id")
        )

    def group_has_uncommitted_attempt(
        self, group_id: str, exclude_attempt_id: str | None = None
    ) -> bool:
        """Whether a *previous* attempt of a unit has no consumable group record.

        This is deliberately not restricted to a single path: if a job declared
        two outputs and crashed after registering the first one, the second
        path may also have been touched in the isolated candidate even though
        nothing was registered for it.  Neither output may seed the next
        attempt from its current bytes.
        """
        records = []
        for item in self._attempts.glob("*.json"):
            record = _read_json(item)
            if not record or record.get("group_id") != group_id:
                continue
            if exclude_attempt_id is not None and (
                record.get("attempt_id") == exclude_attempt_id
            ):
                continue
            records.append(record)
        if not records:
            return False
        latest = max(records, key=lambda record: record.get("started_at", 0))
        return not self.group_is_complete(group_id, latest.get("attempt_id"))

    def recover_interrupted(
        self, valid_outputs: Iterable[str] | None = None
    ) -> dict[str, str]:
        """Restore the last complete group for attempts that never committed.

        Returns a mapping of path -> state, where state is one of:

        ``restored_from_previous_commit``
            The path was put back to the bytes of the last complete group.
        ``no_previous_commit``
            No complete group exists; the path must be treated as missing
            unless a pre-existing original was never touched.
        ``left_untouched``
            Nothing uncommitted was detected for this path.
        """
        valid = set(valid_outputs or [])
        result: dict[str, str] = {}

        by_unit: dict[str, list[dict[str, Any]]] = {}
        for record in self._all_attempts():
            by_unit.setdefault(record.get("group_id"), []).append(record)

        for group_id, records in by_unit.items():
            records.sort(key=lambda record: record.get("started_at", 0))
            latest = records[-1]
            if self.group_is_complete(group_id, latest.get("attempt_id")):
                continue
            if latest.get("status") not in ("prepared", "registered"):
                continue
            records = [latest]
            for record in records:
                attempt_id = record.get("attempt_id")
                if not attempt_id:
                    continue
                if self.group_is_complete(group_id, attempt_id):
                    continue
                if record.get("status") not in ("prepared", "registered"):
                    continue

                committed = self.latest_complete_group(group_id)
                for path, entry in record.get("outputs", {}).items():
                    if valid and path not in valid:
                        continue
                    candidate = entry.get("candidate_stat") or {}
                    touched = entry.get("registered") or candidate.get("exists")
                    if not touched:
                        # The attempt marked this output incomplete when it
                        # started, but never wrote it.  The bytes on disk are
                        # therefore still whatever was there before the
                        # attempt; do not claim the attempt produced them.
                        result[path] = (
                            "left_untouched"
                            if entry.get("pre_existing")
                            else "not_produced"
                        )
                        continue
                    snapshot = (
                        self._snapshot_path(committed["attempt_id"], path)
                        if committed
                        else None
                    )
                    if snapshot is None or not os.path.exists(snapshot):
                        pre_copy = entry.get("pre_copy")
                        if pre_copy and os.path.exists(pre_copy):
                            snapshot = pre_copy
                    if snapshot is not None and os.path.exists(snapshot):
                        target_dir = os.path.dirname(path)
                        if target_dir:
                            os.makedirs(target_dir, exist_ok=True)
                        before = file_stat(path)
                        try:
                            self._copy_path(str(snapshot), Path(path))
                        except OSError as err:
                            logger.warning(
                                f"Could not restore committed update output {path}: {err}"
                            )
                            result[path] = "restore_failed"
                            continue
                        _append_jsonl(
                            self._restores,
                            {
                                "attempt_id": attempt_id,
                                "path": path,
                                "backup": str(snapshot),
                                "restored": True,
                                "before_stat": before,
                                "after_stat": file_stat(path),
                                "reason": "interrupted_attempt_recovery",
                                "at": time.time(),
                            },
                        )
                        result[path] = "restored_from_previous_commit"
                    elif entry.get("pre_existing"):
                        result[path] = "left_untouched"
                    else:
                        result[path] = "no_previous_commit"
                record["status"] = "abandoned"
                record["recovered_at"] = time.time()
                record["recovered_outputs"] = {
                    path: state
                    for path, state in result.items()
                    if path in record.get("outputs", {})
                }
                _atomic_json(self._attempt_path(attempt_id), record)
        return result

    def has_uncommitted_attempt(self) -> bool:
        for record in self._all_attempts():
            if record.get("status") in ("prepared", "registered"):
                return True
        return False

    # ------------------------------------------------------------------
    # restores
    # ------------------------------------------------------------------
    def restore_pre_attempt(self, attempt_id: str | None, path: str) -> str | None:
        """Put back the bytes that existed before the attempt touched ``path``.

        This is the fallback used when the ordinary backup is unavailable, for
        example because the job ran in a killed subprocess.  It keeps the path
        usable without erasing the attempt's write evidence.
        """
        if attempt_id is None:
            return None
        record = self.read_attempt(attempt_id)
        if record is None:
            return None
        entry = record.get("outputs", {}).get(path)
        if not entry:
            return None
        source = entry.get("pre_copy")
        if not source or not os.path.exists(source):
            return None
        if not entry.get("pre_existing"):
            # The path did not exist before the attempt; there is nothing to
            # restore, and the caller will clean it up.
            return None
        target_dir = os.path.dirname(path)
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)
        try:
            self._copy_path(source, Path(path))
        except OSError as err:  # pragma: no cover - permissions
            logger.warning(f"Could not restore pre-attempt bytes of {path}: {err}")
            return None
        return source

    def note_restore(self, attempt_id: str | None, path: str, restored: bool) -> None:
        """Record that a path was rolled back, without erasing what it did.

        ``registered``, ``produced`` and ``write_observed`` are intentionally
        preserved: they are the proof that the attempt really wrote and
        registered the output, which is exactly what must remain visible after
        the bytes have been rolled back.  Only the group-level ``committed``
        flag is cleared, because a restore means no complete group exists for
        this attempt.
        """
        if attempt_id is None:
            return
        record = self.read_attempt(attempt_id)
        if record is None:
            return
        entry = record.get("outputs", {}).get(path)
        if entry is None:
            return
        entry["restored"] = restored
        entry["committed"] = False
        _atomic_json(self._attempt_path(attempt_id), record)

    def attempt_for(
        self, run_id: str | None, unit_key: str | None
    ) -> dict[str, Any] | None:
        """Newest attempt of a unit, optionally restricted to a run."""
        records = []
        for record in self._all_attempts():
            if unit_key is not None and record.get("group_id") != unit_key:
                continue
            if run_id is not None and record.get("run_id") != run_id:
                continue
            records.append(record)
        if not records:
            return None
        return max(records, key=lambda record: record.get("started_at", 0))

    def recorded_restore(
        self,
        attempt_id: str | None,
        path: str,
        run_id: str | None = None,
        unit_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the last restore that is still the current state of ``path``.

        A restore is only considered still in effect if the file has not been
        touched since; otherwise a later attempt that wrote new bytes must be
        allowed to restore its own backup.  The ``unit_key`` fallback is needed
        because error postprocessing can run on a freshly constructed Job (or a
        spawned subprocess) that has not started an attempt of its own.
        """
        current = file_stat(path)
        for record in reversed(self._all_restores()):
            if not record.get("restored") or record.get("path") != path:
                continue
            after = record.get("after_stat") or {}
            unchanged = bool(after.get("exists")) and all(
                after.get(key) == current.get(key)
                for key in ("sha256", "size", "mtime_ns", "ctime_ns", "inode")
            )
            if not unchanged:
                continue
            if attempt_id is not None and record.get("attempt_id") == attempt_id:
                return record
            if unit_key is not None and record.get("unit_key") == unit_key:
                return record
        return None

    def record_restore(
        self,
        *,
        attempt_id: str | None,
        path: str,
        backup: str,
        restored: bool,
        before_stat: dict | None,
        after_stat: dict | None,
        error: str | None = None,
        run_id: str | None = None,
        unit_key: str | None = None,
        reason: str = "job_failure",
    ) -> None:
        _append_jsonl(
            self._restores,
            {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "unit_key": unit_key,
                "path": path,
                "backup": backup,
                "restored": restored,
                "before_stat": before_stat,
                "after_stat": after_stat,
                "error": error,
                "reason": reason,
                "at": time.time(),
            },
        )

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------
    def _all_attempts(self) -> list[dict[str, Any]]:
        records = []
        for item in self._attempts.glob("*.json"):
            record = _read_json(item)
            if record and record.get("attempt_id"):
                records.append(record)
        records.sort(key=lambda record: record.get("started_at", 0))
        return records

    def _all_groups(self) -> list[dict[str, Any]]:
        records = []
        for item in self._groups.glob("*/attempts/*.json"):
            record = _read_json(item)
            if record and record.get("complete"):
                records.append(record)
        records.sort(key=lambda record: record.get("committed_at", 0))
        return records

    def _all_restores(self) -> list[dict[str, Any]]:
        records = list(_iter_jsonl(self._restores))
        records.sort(key=lambda record: record.get("at", 0))
        return records

    def report(self, paths: Iterable[str] | None = None) -> dict[str, Any]:
        """Return the three evidence views, kept separate on purpose.

        ``consumable`` answers "which complete group, if any, backs the bytes
        currently readable at this path".  It never claims that the latest
        round produced the path: a restore or a pre-existing file can make a
        path readable without any production in the current round.

        ``attempts`` and ``restores`` are the per-file audit trail.
        """
        groups = self._all_groups()
        attempts = self._all_attempts()
        restores = self._all_restores()

        known_paths = set(paths or [])
        if not known_paths:
            for record in groups:
                known_paths.update(record.get("outputs", {}))
            for record in attempts:
                known_paths.update(record.get("outputs", {}))
            known_paths.update(record.get("path") for record in restores)
            known_paths.discard(None)

        latest_group_for_path: dict[str, dict[str, Any]] = {}
        for record in groups:
            for path in record.get("outputs", {}):
                latest_group_for_path[path] = record

        latest_attempt_for_path: dict[str, dict[str, Any]] = {}
        for record in attempts:
            for path in record.get("outputs", {}):
                previous = latest_attempt_for_path.get(path)
                if previous is None or record.get("started_at", 0) >= previous.get(
                    "started_at", 0
                ):
                    latest_attempt_for_path[path] = record

        restores_for_path: dict[str, dict[str, Any]] = {}
        for record in restores:
            restores_for_path[record.get("path")] = record

        consumable: dict[str, Any] = {}
        for path in sorted(known_paths):
            group = latest_group_for_path.get(path)
            attempt = latest_attempt_for_path.get(path)
            restore = restores_for_path.get(path)
            current = file_stat(path)
            entry: dict[str, Any] = {
                "path": path,
                "readable": bool(current.get("exists")),
                "current": current,
                "produced_by_latest_attempt": False,
                "latest_attempt": None,
                "last_restore": None,
            }
            if attempt is not None:
                attempt_output = attempt.get("outputs", {}).get(path, {})
                entry["latest_attempt"] = {
                    "attempt_id": attempt.get("attempt_id"),
                    "run_id": attempt.get("run_id"),
                    "status": attempt.get("status"),
                    "started_at": attempt.get("started_at"),
                    "produced": bool(attempt_output.get("produced")),
                    "write_observed": attempt_output.get("write_observed"),
                    "registered": bool(attempt_output.get("registered")),
                    "pre_existing": bool(attempt_output.get("pre_existing")),
                    "group_committed": self.group_is_complete(
                        attempt.get("group_id"), attempt.get("attempt_id")
                    ),
                }
                # A path can be readable, and even hold the right bytes,
                # without having been produced by this round: it may have been
                # present before the attempt, or moved back by a restore.  Only
                # a complete group commit from the latest attempt proves
                # production in this round.
                entry["produced_by_latest_attempt"] = bool(
                    attempt_output.get("produced")
                ) and bool(entry["latest_attempt"]["group_committed"])
            if restore is not None:
                entry["last_restore"] = {
                    "at": restore.get("at"),
                    "attempt_id": restore.get("attempt_id"),
                    "restored": restore.get("restored"),
                    "reason": restore.get("reason", "job_failure"),
                }

            if group is None:
                entry["status"] = (
                    "preexisting_unverified"
                    if current.get("exists")
                    else "never_committed"
                )
                entry["basis"] = "none"
                entry["consumable"] = False
            else:
                committed = group.get("outputs", {}).get(path, {})
                entry.update(
                    {
                        "group_id": group.get("group_id"),
                        "attempt_id": group.get("attempt_id"),
                        "run_id": group.get("run_id"),
                        "committed_at": group.get("committed_at"),
                        "complete": True,
                        "committed": committed,
                    }
                )
                if not current.get("exists"):
                    entry["status"] = "missing"
                    entry["basis"] = "previous_group_commit"
                    entry["consumable"] = False
                else:
                    matches = (
                        committed.get("sha256") == current.get("sha256")
                        if committed.get("sha256") and current.get("sha256")
                        else (
                            committed.get("size") == current.get("size")
                            and committed.get("mtime_ns") == current.get("mtime_ns")
                        )
                    )
                    entry["matches_committed"] = matches
                    entry["status"] = "consumable" if matches else "stale"
                    entry["consumable"] = matches
                    if entry["produced_by_latest_attempt"]:
                        entry["basis"] = "latest_group_commit"
                    elif restore is not None:
                        entry["basis"] = "restored_previous_group_commit"
                    else:
                        entry["basis"] = "previous_group_commit"

            consumable[path] = entry

        attempt_entries = []
        for record in attempts:
            outputs = record.get("outputs", {})
            if paths and not (set(outputs) & set(paths)):
                continue
            attempt_entries.append(record)

        restore_entries = [
            record
            for record in restores
            if not paths or record.get("path") in set(paths)
        ]

        return {
            "consumable": consumable,
            "attempts": attempt_entries,
            "restores": restore_entries,
        }
