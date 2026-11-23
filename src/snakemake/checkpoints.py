import contextvars
import hashlib
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Dict, Iterable, Iterator, Optional

from snakemake.exceptions import IncompleteCheckpointException, WorkflowError
from snakemake.io import checkpoint_target
from snakemake.logging import logger

if TYPE_CHECKING:
    from snakemake.rules import Rule


# Checkpoint usages collected during (possibly nested) input expansion calls.
# The value maps checkpoint names to the revision that was observed when the
# consumer was expanded.
_checkpoint_usage: contextvars.ContextVar[Optional[Dict[str, str]]] = (
    contextvars.ContextVar("snakemake_checkpoint_usage", default=None)
)


@contextmanager
def checkpoint_usage() -> Iterator[Dict[str, str]]:
    """Collect checkpoint usages of a single input expansion.

    Snakemake expands inputs synchronously, hence a context variable is enough
    to associate ``checkpoints.<name>.get(...)`` calls with the job that is
    currently being constructed. Nested expansions (e.g. an input function
    calling another helper that accesses a checkpoint) share the same usage
    set.
    """
    current = _checkpoint_usage.get()
    if current is not None:
        # Nested expansion (e.g. a helper called by an input function) shares
        # the usage set of the outer expansion.
        yield current
        return
    usage: Dict[str, str] = {}
    token = _checkpoint_usage.set(usage)
    try:
        yield usage
    finally:
        _checkpoint_usage.reset(token)


def _usage_key(outputs: Iterable[str]) -> str:
    """Stable key for a concrete checkpoint output set."""
    return "\0".join(sorted(str(out) for out in outputs))


def _record_usage(outputs: Iterable[str], revision: str) -> None:
    usage = _checkpoint_usage.get()
    if usage is not None:
        usage[_usage_key(outputs)] = revision


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# Cache of content digests, keyed by the file identity from stat(). Content is
# only re-read when size, mtime or ctime (and inode) changed. This keeps
# revision checks cheap for large checkpoint outputs while still reacting to
# rewrites that preserve the modification time (e.g. cp -p), because ctime is
# not settable from user space.
_digest_cache: Dict[tuple, str] = dict()


def _cached_digest(full: str, st: os.stat_result) -> str:
    key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    try:
        return _digest_cache[key]
    except KeyError:
        digest = _hash_file(full)
        _digest_cache[key] = digest
        return digest


def _iter_output_entries(paths: Iterable[str]) -> Iterator[tuple[str, str, int]]:
    """Yield ``(relative path, content digest, size)`` for all output files.

    Directory outputs are traversed recursively. The relative path is included
    in the revision, so moving a file inside the directory changes the
    revision as well.
    """
    for path in paths:
        path = str(path)
        if os.path.isdir(path) and not os.path.islink(path):
            for root, dirs, files in os.walk(path):
                dirs.sort()
                for filename in sorted(files):
                    full = os.path.join(root, filename)
                    rel = os.path.relpath(full, path)
                    try:
                        st = os.stat(full)
                    except FileNotFoundError:
                        continue
                    yield rel, _cached_digest(full, st), st.st_size
        elif os.path.isfile(path):
            st = os.stat(path)
            yield os.path.basename(path), _cached_digest(path, st), st.st_size


def compute_revision(outputs: Iterable[str]) -> str:
    """Return a content based revision for the given checkpoint outputs.

    The revision changes whenever a file is added, removed, renamed or its
    content changes. It is used to decide whether dynamically created jobs
    have to be re-expanded and whether their previous outputs may be reused.
    """
    h = hashlib.sha256()
    entries = list(_iter_output_entries(outputs))
    for rel, digest, size in entries:
        h.update(rel.encode())
        h.update(b"\0")
        h.update(digest.encode())
        h.update(b"\0")
        h.update(str(size).encode())
        h.update(b"\0")
    return h.hexdigest()


class Checkpoints:
    """A singleton object in a workflow.

    Created_output can be accessed by checkpoint rules in or out modules.
    This never go into snakefile, so no rules name will be set to it.
    """

    def __init__(self):
        self._created_output = set()

    @property
    def created_output(self):
        return self._created_output

    def spawn_new_namespace(self):
        """Make a new namespace for checkpoints in the module."""
        return CheckpointsProxy(self)

    @staticmethod
    def revision(outputs: Iterable[str]) -> str:
        """Return the current content revision of a concrete checkpoint output set."""
        return compute_revision(outputs)


class CheckpointsProxy(Checkpoints):
    """A namespace for checkpoints so that they can be accessed via dot notation.

    It will be created once a module is created,
    and different module will have different checkpoint namespace,
    but share a single created_output set.
    """

    def __init__(self, parent: Checkpoints):
        self.parent = parent

    @property
    def created_output(self):
        return self.parent.created_output

    def register(self, rule: "Rule", fallback_name=None):
        checkpoint = Checkpoint(rule, self)
        if fallback_name:
            setattr(self, fallback_name, checkpoint)
        setattr(self, rule.name, checkpoint)

    @staticmethod
    def revision(outputs: Iterable[str]) -> str:
        return Checkpoints.revision(outputs)


class Checkpoint:
    __slots__ = ["rule", "checkpoints"]

    def __init__(self, rule: "Rule", checkpoints: Checkpoints):
        self.rule = rule
        self.checkpoints = checkpoints

    def get(self, **wildcards):
        missing = self.rule.wildcard_names.difference(wildcards.keys())
        if missing:
            raise WorkflowError(
                "Missing wildcard values for {}".format(", ".join(missing))
            )

        output, _ = self.rule.expand_output(wildcards)
        if self.checkpoints.created_output:
            missing_output = set(output) - set(self.checkpoints.created_output)
            if not missing_output:
                revision = self.checkpoints.revision(output)
                _record_usage(output, revision)
                return CheckpointJob(self.rule, output, revision)
            else:
                logger.debug(
                    f"Missing checkpoint output for {self.rule.name} "
                    f"(wildcards: {wildcards}): {','.join(missing_output)} of {','.join(output)}"
                )

        raise IncompleteCheckpointException(self.rule, checkpoint_target(output[0]))

    def revision(self, **wildcards) -> str:
        """Return the current content revision of this checkpoint."""
        output, _ = self.rule.expand_output(wildcards)
        return self.checkpoints.revision(output)


class CheckpointJob:
    __slots__ = ["rule", "output", "revision"]

    def __init__(self, rule: "Rule", output, revision: str = ""):
        self.output = output
        self.rule = rule
        self.revision = revision
