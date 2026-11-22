__authors__ = "Johannes Köster, Sven Nahnsen"
__copyright__ = "Copyright 2022, Johannes Köster, Sven Nahnsen"
__email__ = "johannes.koester@uni-due.de"
__license__ = "MIT"

from contextlib import contextmanager
from dataclasses import dataclass
from tempfile import TemporaryDirectory
from pathlib import Path
import json
import os
import shutil
import stat
import time
import uuid
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None

from snakemake.logging import logger
from snakemake.jobs import Job
from snakemake.exceptions import WorkflowError
from snakemake.caching import AbstractOutputFileCache

#: Wait on the existence of the file given by this environment variable
#: after the first output has been placed into a new generation but before
#: the release manifest is committed. Used to simulate an interrupted or
#: slow publisher. The wait happens without holding the exclusive lock, so
#: readers of the previously completed generation are not blocked.
WAIT_ENVVAR = "SNAKEMAKE_CACHE_WAIT_FILE"

#: Number of superseded generations to keep around for readers that have
#: pinned an older release.
KEEP_OLD_GENERATIONS = 2


@dataclass(frozen=True)
class LocalRelease:
    """A pinned, complete generation of all outputs of one cache key."""

    key: str
    release_id: str
    files: tuple
    directory: Path

    def path(self, name: str) -> Path:
        return self.directory / name


class OutputFileCache(AbstractOutputFileCache):
    """
    A cache for output files that uses a provenance hash value that
    describes all steps, parameters, and software needed to generate
    each output file.

    All outputs of a job are published together as one immutable
    *generation*. A generation becomes visible to readers only after every
    output file has been placed and a release manifest has been committed
    atomically. Hence a reader either observes the previous complete
    generation or the new complete generation, never a mix. A generation
    that is only half written is never referenced by a manifest and
    therefore stays a permanent cache miss until a complete publish
    succeeds.
    """

    def __init__(self):
        super().__init__()
        self.path = Path(self.cache_location)
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "generations").mkdir(exist_ok=True)
        (self.path / "releases").mkdir(exist_ok=True)
        (self.path / "staging").mkdir(exist_ok=True)
        (self.path / "locks").mkdir(exist_ok=True)
        # make readable/writeable for all
        self.file_permissions = (
            stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IRGRP
            | stat.S_IWGRP
            | stat.S_IROTH
            | stat.S_IWOTH
        )
        # directories need to have exec permission as well (for opening)
        self.dir_permissions = (
            self.file_permissions | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )

    def check_writeable(self, cachefile):
        if not (os.access(cachefile.parent, os.W_OK) or os.access(cachefile, os.W_OK)):
            self.raise_write_error(cachefile)

    def check_readable(self, cachefile):
        if not os.access(cachefile, os.R_OK):
            self.raise_read_error(cachefile)

    @contextmanager
    def _key_lock(self, key, shared=False):
        """Short-lived per-key lock.

        Readers use a shared lock and writers an exclusive one. The lock is
        only held while metadata is swapped, never while output data is
        copied or while a publisher waits, so an in-flight publisher cannot
        block readers of an already complete generation for long. On
        platforms without advisory file locks the operation degrades to a
        no-op context manager.
        """
        if fcntl is None:  # pragma: no cover - Windows fallback
            yield
            return
        lockfile = self.path / "locks" / f"{key}.lock"
        with open(lockfile, "a+") as fh:
            fcntl.flock(
                fh.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            )
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _entry_name(ext):
        """File name of an output inside a generation directory.

        Outputs without a suffix (an empty extension) still need a concrete
        file name.
        """
        name = ext.lstrip(os.sep)
        return name or "_data"

    async def _get_entries(self, job: Job):
        provenance_hash = await self.provenance_hash_map.get_provenance_hash(job)
        return provenance_hash, [
            (Path(outputfile), ext)
            for outputfile, ext in self.get_outputfiles(job)
        ]

    def _manifest_path(self, key):
        return self.path / "releases" / f"{key}.json"

    def _generation_dir(self, key, release_id):
        return self.path / "generations" / key / release_id

    def _pin_path(self, key):
        # Pins live in the invoking workflow's working directory, so each
        # independent working directory keeps its own view of the cache.
        return Path.cwd() / ".snakemake" / "cache_pins" / key

    def _read_pin(self, key):
        pin = self._pin_path(key)
        if not pin.is_file():
            return None
        try:
            return pin.read_text().strip()
        except OSError:
            return None

    def _write_pin(self, key, release_id):
        pin = self._pin_path(key)
        pin.parent.mkdir(parents=True, exist_ok=True)
        tmp_pin = pin.with_name(f".{pin.name}.tmp.{uuid.uuid4().hex[:6]}")
        tmp_pin.write_text(release_id)
        os.replace(tmp_pin, pin)

    def _pinned_release(self, key, pinned_id):
        if not pinned_id:
            return None
        directory = self._generation_dir(key, pinned_id)
        manifest = directory / "RELEASE"
        if not manifest.is_file():
            return None
        try:
            with open(manifest) as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None
        if not all((directory / name).exists() for name in data.get("files", ())):
            return None
        return LocalRelease(
            key=key,
            release_id=pinned_id,
            files=tuple(data["files"]),
            directory=directory,
        )

    def _read_manifest(self, key):
        manifest = self._manifest_path(key)
        if not manifest.is_file():
            return None
        try:
            with open(manifest) as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None
        release_id = data.get("release")
        files = tuple(data.get("files", ()))
        if not release_id or not files:
            return None
        directory = self._generation_dir(key, release_id)
        if not directory.is_dir():
            return None
        if not all((directory / name).exists() for name in files):
            return None
        return LocalRelease(
            key=key,
            release_id=release_id,
            files=files,
            directory=directory,
        )

    def _new_release_id(self):
        return "{}-{}".format(
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"),
            uuid.uuid4().hex[:8],
        )

    def _outputs_are_release_symlinks(self, release, entries):
        """True if every job output is already a symlink into ``release``.

        This is the signature of the double publish performed by
        run-directive jobs (subprocess publishes first and replaces the
        outputs with symlinks). Regular recomputations produce real files
        and must not take this shortcut.
        """
        release_root = os.path.realpath(release.directory)
        for outputfile, ext in entries:
            if not outputfile.is_symlink():
                return False
            target = os.path.realpath(outputfile)
            if target != os.path.realpath(
                release.directory / self._entry_name(ext)
            ):
                return False
            if os.path.commonpath((target, release_root)) != release_root:
                return False
        return True

    def _wait_if_configured(self):
        wait_file = os.environ.get(WAIT_ENVVAR)
        if wait_file and not os.path.exists(wait_file):
            logger.info(
                f"Cache publisher pausing (waiting for {wait_file}) before "
                "committing the release manifest."
            )
            while not os.path.exists(wait_file):
                time.sleep(0.05)

    async def store(self, job: Job):
        """
        Store generated job output in the cache as one atomic generation.

        Publishing is idempotent only against the double publish of
        run-directive jobs: there the spawned subprocess has already
        published a generation and replaced the job outputs with symlinks
        into it, so the parent's store call observes outputs that already
        resolve inside the current release and must not create a duplicate
        generation. A genuine (re-)computation produces regular output files
        and therefore always publishes a new generation -- even when the
        regenerated bytes are identical, as happens when a release is
        re-issued after a cache-side repair.
        """
        if not os.access(self.path, os.W_OK):
            raise WorkflowError(
                "Cannot access cache location {}. Please ensure that "
                "it is present and writeable.".format(self.path)
            )

        key, entries = await self._get_entries(job)

        with self._key_lock(key, shared=True):
            existing = self._read_manifest(key)
        if existing is not None and self._outputs_are_release_symlinks(
            existing, entries
        ):
            for outputfile, ext in entries:
                target = existing.directory / self._entry_name(ext)
                # The subprocess has already linked the output; only repair
                # the link if it is missing or pointing elsewhere.
                if not outputfile.is_symlink() or os.path.realpath(
                    outputfile
                ) != os.path.realpath(target):
                    if outputfile.is_symlink() or outputfile.exists():
                        outputfile.unlink()
                    self.symlink(target, outputfile, utime=False)
            self._write_pin(key, existing.release_id)
            logger.info(
                f"Reused identical cache generation {existing.release_id} "
                f"for key {key}; no new generation published."
            )
            return

        release_id = self._new_release_id()

        # Stage every output in a private temp directory on the same
        # filesystem, so no partial state is ever visible in the cache.
        with TemporaryDirectory(dir=self.path / "staging") as tmpdirname:
            staging = Path(tmpdirname)
            staged = []
            for outputfile, ext in entries:
                if not os.path.exists(outputfile):
                    raise WorkflowError(
                        f"Cannot move output file {outputfile} to cache. It does not exist "
                        "(maybe it was not created by the job?)."
                    )
                flat_cachefile = self.path / f"{key}{ext}"
                self.check_writeable(flat_cachefile)
                logger.info(f"Moving output file {outputfile} to cache.")

                name = self._entry_name(ext)
                tmp = staging / name
                # First move is performed into a tempdir (it might involve a copy if not on the same FS).
                # This is important, such that network filesystem latency
                # does not lead to concurrent writes to the same file.
                # We can use the plain copy method of shutil, because we do not care about the metadata.
                shutil.move(outputfile, tmp, copy_function=shutil.copy)
                self.set_permissions(tmp)
                staged.append((name, tmp))

            # Publish the data files into a unique, not-yet-referenced
            # generation directory. Unique names make this safe without a
            # lock and without disturbing the currently released generation.
            generation_dir = self._generation_dir(key, release_id)
            generation_dir.parent.mkdir(parents=True, exist_ok=True)
            os.mkdir(generation_dir)
            for index, (name, tmp) in enumerate(staged):
                shutil.move(str(tmp), generation_dir / name)
                if index == 0:
                    # Hook for simulating an interrupted/slow publisher.
                    # No lock is held here, so readers keep being served
                    # from the previous complete generation.
                    self._wait_if_configured()

            self._commit_release(key, release_id, generation_dir, entries)

        for outputfile, ext in entries:
            self.symlink(
                generation_dir / self._entry_name(ext), outputfile, utime=False
            )
        self._write_pin(key, release_id)
        logger.info(
            f"Published cache generation {release_id} for key {key} "
            f"({len(entries)} output(s))."
        )
        self._prune_old_generations(
            key,
            LocalRelease(
                key=key,
                release_id=release_id,
                files=tuple(self._entry_name(ext) for _, ext in entries),
                directory=generation_dir,
            ),
        )

    def _commit_release(self, key, release_id, generation_dir, entries):
        exts = tuple(self._entry_name(ext) for _, ext in entries)
        release_info = {
            "key": key,
            "release": release_id,
            "files": exts,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._key_lock(key):
            # Re-validate completeness immediately before committing.
            for name in release_info["files"]:
                if not (generation_dir / name).exists():
                    raise WorkflowError(
                        f"Refusing to release incomplete cache generation "
                        f"{release_id} for key {key}: missing {name}."
                    )

            with open(generation_dir / "RELEASE", "w") as fh:
                json.dump(release_info, fh, indent=2)
                fh.write("\n")
            self.set_permissions(generation_dir / "RELEASE")

            manifest = self._manifest_path(key)
            tmp_manifest = manifest.with_name(
                f".{manifest.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}"
            )
            with open(tmp_manifest, "w") as fh:
                json.dump(release_info, fh, indent=2)
                fh.write("\n")
            os.replace(tmp_manifest, manifest)

            # Maintain the historical flat cache layout
            # (<provenance hash><ext>) as symlinks into the generation.
            for outputfile, ext in entries:
                flat_cachefile = self.path / f"{key}{ext}"
                target = generation_dir / self._entry_name(ext)
                tmp_link = flat_cachefile.with_name(
                    f".{flat_cachefile.name}.tmp.{uuid.uuid4().hex[:6]}"
                )
                os.symlink(
                    os.path.relpath(target, flat_cachefile.parent), tmp_link
                )
                os.replace(tmp_link, flat_cachefile)

    def _prune_old_generations(self, key, current_release):
        key_dir = self.path / "generations" / key
        if not key_dir.is_dir():
            return
        # Only complete (released) generations are pruned. A directory
        # without a RELEASE manifest might belong to a publisher that is
        # still in flight, so deleting it here could corrupt a concurrent
        # publish. Such an orphan is simply never referenced by a manifest
        # and therefore stays a permanent cache miss.
        complete = [
            entry
            for entry in key_dir.iterdir()
            if entry.is_dir()
            and entry.name != current_release.release_id
            and (entry / "RELEASE").is_file()
        ]
        complete.sort(key=lambda entry: entry.name, reverse=True)
        for stale in complete[KEEP_OLD_GENERATIONS:]:
            shutil.rmtree(stale, ignore_errors=True)

    async def snapshot(self, job: Job):
        """
        Return the released, complete generation visible from the current
        working directory, or raise a cache miss.

        If the current working directory has previously pinned a generation
        for this key, that exact generation is returned even if a newer one
        has since been published. Otherwise the latest committed release is
        pinned for this working directory. The returned release stays valid
        for subsequent accesses even if a newer generation is published in
        between.
        """
        key, entries = await self._get_entries(job)
        expected = tuple(self._entry_name(ext) for _, ext in entries)
        with self._key_lock(key, shared=True):
            pinned_id = self._read_pin(key)
            release = (
                self._pinned_release(key, pinned_id)
                if pinned_id is not None
                else None
            )
            if release is None:
                release = self._read_manifest(key)
            if release is None or not set(expected).issubset(set(release.files)):
                self.raise_cache_miss_exception(job)
            for name in expected:
                self.check_readable(release.directory / name)
            if pinned_id != release.release_id:
                self._write_pin(key, release.release_id)
            return release

    async def fetch(self, job: Job):
        """
        Retrieve the complete cached generation and symlink every output
        to the place where the job expects it. All outputs are guaranteed
        to come from the same generation.
        """
        release = await self.snapshot(job)
        key, entries = await self._get_entries(job)
        for outputfile, ext in entries:
            cachefile = release.directory / self._entry_name(ext)
            logger.debug(
                "Output file {} exists as {} in the cache (release {}).".format(
                    outputfile, cachefile, release.release_id
                )
            )
            if cachefile.is_dir():
                # For directories, create a new one and symlink each entry.
                # Then, the .snakemake_timestamp of the new dir is touched
                # by the executor.
                outputfile.mkdir(parents=True, exist_ok=True)
                for f in cachefile.iterdir():
                    self.symlink(f, outputfile / f.name)
            else:
                # On a pure cache hit no job code runs, so the output's
                # parent directory has to be created here.
                outputfile.parent.mkdir(parents=True, exist_ok=True)
                self.symlink(cachefile, outputfile)
        logger.info(
            f"Fetched cache release {release.release_id} for key {key} "
            f"({len(entries)} output(s) from one generation)."
        )

    async def exists(self, job: Job):
        """
        Return True only if a released generation containing every expected
        output is visible from the current working directory. Half-written
        generations without a committed manifest are treated as cache misses.
        """
        key, entries = await self._get_entries(job)
        expected = tuple(self._entry_name(ext) for _, ext in entries)
        with self._key_lock(key, shared=True):
            pinned_id = self._read_pin(key)
            release = (
                self._pinned_release(key, pinned_id)
                if pinned_id is not None
                else None
            )
            if release is None:
                release = self._read_manifest(key)
            if release is None:
                return False
            if not set(expected).issubset(set(release.files)):
                return False
            for name in expected:
                cachefile = release.directory / name
                if not cachefile.exists():
                    return False
                logger.debug(
                    "Output file {} exists as {} in the cache (release {}).".format(
                        name, cachefile, release.release_id
                    )
                )
                self.check_readable(cachefile)
            return True

    def symlink(self, path, outputfile, utime=True):
        if os.utime in os.supports_follow_symlinks or not utime:
            logger.info(f"Symlinking output file {outputfile} from cache.")
            os.symlink(path, outputfile)
            if utime:
                os.utime(outputfile, follow_symlinks=False)
        else:
            logger.info(
                "Copying output file {} from cache (OS does not support updating the modification date of symlinks).".format(
                    outputfile
                )
            )
            shutil.copyfile(path, outputfile)

    def set_permissions(self, entry):
        # make readable/writeable for all
        if entry.is_dir():
            # recursively apply permissions for the contained files
            for root, dirs, files in os.walk(entry):
                root = Path(root)
                for d in dirs:
                    os.chmod(root / d, self.dir_permissions)
                for f in files:
                    os.chmod(root / f, self.file_permissions)
            os.chmod(entry, self.dir_permissions)
        else:
            os.chmod(entry, self.file_permissions)
