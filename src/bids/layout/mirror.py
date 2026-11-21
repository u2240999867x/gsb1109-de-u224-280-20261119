"""Read-only mirror resolution and download planning for indexed remote BIDS data.

This module lets an offline node reason about *where* an indexed remote file
may be obtained and *which exact content* it is allowed to fetch, without ever
issuing a network request.

The model is deliberately content-addressed and immutable:

* Two (or more) remote archives ("roots") expose the same relative paths. A
  :class:`RemoteArchiveRegistry` records every root that may be consulted.
* A :class:`ContentManifest` records, for every indexed relative path, a
  trustworthy content digest and, where available, a version condition that can
  be shipped to a remote node.
* A :class:`MirrorMap` describes how an archive is mounted locally and the
  relative-path scope the node is permitted to write. Mounts can be relocated
  and replaced independently of any saved plan.
* A :class:`DownloadPlan` is an immutable, content-pinned instruction. It is
  created from first-round evidence and can never silently switch to a new
  version published at the same source URI. Re-checking an old plan only ever
  relocates it to a currently permitted location that still holds the pinned
  content; it never claims a newer local file satisfies the plan.

Everything here works against a saved :class:`~bids.layout.BIDSLayout` and a
local (``fsspec``/``upath``) filesystem, so file queries, source URIs and
file-to-file relationships remain exactly as indexed.
"""

import dataclasses
import hashlib
import json
from enum import Enum

from upath import UPath as Path

from ..exceptions import PlanValidationError, UnregisteredRootError

__all__ = [
    'FileState',
    'RemoteArchiveRegistry',
    'ContentManifest',
    'ContentRecord',
    'MirrorMount',
    'MirrorMap',
    'DownloadPlan',
    'PlanItem',
    'PlanReport',
    'MirrorPlanner',
]

# Status of a planned (or queried) relative path.
PRESENT = 'present'  # exact pinned/required content is locally available
MISSING = 'missing'  # permitted location exists but the file is not fetched
INACCESSIBLE = 'inaccessible'  # source/root cannot be read; cannot confirm content
VERSION_UNKNOWN = 'version_unknown'  # present but digest cannot be established
SUPERSEDED = 'superseded'  # only a different (newer) content is available
OUT_OF_SCOPE = 'out_of_scope'  # current mapping does not permit this relative path
UNREGISTERED = 'unregistered'  # URI belongs to no registered archive root


class FileState(str, Enum):
    """Lifecycle states that can be queried for planned files."""

    PRESENT = PRESENT
    MISSING = MISSING
    INACCESSIBLE = INACCESSIBLE
    VERSION_UNKNOWN = VERSION_UNKNOWN
    SUPERSEDED = SUPERSEDED
    OUT_OF_SCOPE = OUT_OF_SCOPE
    UNREGISTERED = UNREGISTERED


def _normalize_root(root):
    """Return a canonical, trailing-slash-free string for a root URI."""
    return str(Path(root)).rstrip('/')


def _normalize_relpath(relpath):
    """Normalize a dataset-relative path to a POSIX-style string."""
    relpath = str(relpath).replace('\\', '/').lstrip('/')
    return relpath


def _read_bytes(local_path):
    """Read bytes through the upath/fsspec interface (read-only)."""
    p = Path(local_path)
    with p.fs.open(p.path, 'rb') as handle:
        return handle.read()


def _file_digest(local_path, algorithm='sha256'):
    """Compute a content digest for a locally accessible file."""
    hasher = hashlib.new(algorithm)
    hasher.update(_read_bytes(local_path))
    return hasher.hexdigest()


@dataclasses.dataclass(frozen=True)
class ContentRecord:
    """Trusted content evidence for one relative path.

    Attributes
    ----------
    relpath : str
        Dataset-relative path (identical in every archive root).
    digest : str or None
        Trustworthy content digest (``"algorithm:hexdigest"``). ``None`` means
        no digest is available (only a version condition).
    version : str or None
        A version condition that can be shipped to a remote node.

    """

    relpath: str
    digest: str | None = None
    version: str | None = None

    @property
    def algorithm(self):
        """The digest algorithm name (e.g. ``"sha256"``) or ``None``."""
        if not self.digest or ':' not in self.digest:
            return None
        return self.digest.split(':', 1)[0]

    def to_dict(self):
        """Serialize the record to a plain JSON-compatible mapping."""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data):
        """Rebuild a record from a serialized mapping."""
        return cls(
            relpath=_normalize_relpath(data['relpath']),
            digest=data.get('digest'),
            version=data.get('version'),
        )


class ContentManifest:
    """An immutable, trustworthy map of relative path -> content evidence."""

    def __init__(self, records=None):
        self._records = {}
        for record in records or []:
            if not isinstance(record, ContentRecord):
                record = ContentRecord.from_dict(record)
            if record.relpath in self._records:
                raise ValueError(f'Duplicate manifest entry for {record.relpath}')
            self._records[record.relpath] = record

    def __contains__(self, relpath):
        return _normalize_relpath(relpath) in self._records

    def __len__(self):
        return len(self._records)

    def __iter__(self):
        return iter(self._records)

    def get(self, relpath):
        """Return the :class:`ContentRecord` for ``relpath`` or ``None``."""
        return self._records.get(_normalize_relpath(relpath))

    def keys(self):
        """Iterate over the indexed relative paths."""
        return self._records.keys()

    def to_dict(self):
        """Serialize the manifest to a plain JSON-compatible mapping."""
        return {rel: rec.to_dict() for rel, rec in self._records.items()}

    @classmethod
    def from_mapping(cls, mapping):
        """Build a manifest from ``{relpath: {"digest": ..., "version": ...}}``."""
        records = [
            ContentRecord(relpath=_normalize_relpath(rel), **(evidence or {}))
            for rel, evidence in mapping.items()
        ]
        return cls(records)

    @classmethod
    def from_json(cls, path):
        """Load a manifest from a JSON file of relative-path evidence."""
        with Path(path).fs.open(Path(path).path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        return cls.from_mapping(data)


class RemoteArchiveRegistry:
    """The set of remote archive roots the node is allowed to consult.

    Roots are content-agnostic locations (URIs). Two archives may expose the
    exact same relative paths; the registry only answers "is this URI inside a
    known root?" and never treats one archive as a fallback for another's
    content.
    """

    def __init__(self, roots=None):
        self._roots = []
        for root in roots or []:
            self.add(root)

    def add(self, root):
        """Register an archive root and return its normalized form."""
        normalized = _normalize_root(root)
        if normalized not in self._roots:
            self._roots.append(normalized)
        return normalized

    @property
    def roots(self):
        """The normalized, ordered tuple of registered roots."""
        return tuple(self._roots)

    def is_registered(self, uri):
        """Return True only when ``uri`` is inside a registered root."""
        return self.root_for(uri) is not None

    def root_for(self, uri):
        """Return the registered root containing ``uri`` or ``None``.

        Matching is prefix-based on path boundaries, so a URI that merely
        shares a string prefix with a root (e.g. a sibling directory with a
        near-identical name) is *not* considered registered.
        """
        normalized = _normalize_root(uri)
        matches = [
            root for root in self._roots if normalized == root or normalized.startswith(root + '/')
        ]
        if not matches:
            return None
        # Most specific (longest) root wins.
        return max(matches, key=len)

    def require_registered(self, uri):
        """Return the root for ``uri`` or raise :class:`UnregisteredRootError`."""
        root = self.root_for(uri)
        if root is None:
            raise UnregisteredRootError(
                f'{uri} is not contained by any registered archive root '
                f'(known roots: {list(self._roots)})'
            )
        return root

    def split(self, uri):
        """Split a registered URI into ``(root, relpath)``."""
        root = self.require_registered(uri)
        relpath = _normalize_root(uri)[len(root) :].lstrip('/')
        return root, _normalize_relpath(relpath)

    def suggest(self, uri, n=3):
        """Return close-but-unregistered root names, for a helpful error."""
        import difflib

        return difflib.get_close_matches(_normalize_root(uri), self._roots, n=n)


@dataclasses.dataclass(frozen=True)
class MirrorMount:
    """One locally mounted view of an archive root.

    Attributes
    ----------
    root : str
        The archive root this mount mirrors.
    mount_point : str
        Local directory the archive is mounted at.
    write_scope : tuple[str, ...]
        Relative-path prefixes the node is permitted to write/fetch. An empty
        tuple means nothing is writable; ``("",)`` means the whole mount.
    readable : bool
        Whether the mount is currently accessible (read-only checks only).

    """

    root: str
    mount_point: str
    write_scope: tuple[str, ...] = ('',)
    readable: bool = True

    def __post_init__(self):
        object.__setattr__(self, 'root', _normalize_root(self.root))
        object.__setattr__(
            self,
            'write_scope',
            tuple(_normalize_relpath(s) for s in (self.write_scope or ('',))),
        )

    def covers(self, relpath):
        """Return whether this mount is permitted to write ``relpath``."""
        relpath = _normalize_relpath(relpath)
        return any(
            scope == '' or relpath == scope or relpath.startswith(scope + '/')
            for scope in self.write_scope
        )

    def local_path(self, relpath):
        """Return the local path that ``relpath`` would occupy on this mount."""
        return Path(self.mount_point) / _normalize_relpath(relpath)


class MirrorMap:
    """The current set of mirror mounts, relocatable and replaceable.

    A mirror map holds the *current* view of where each archive is mounted and
    what may be written. Replacing a map (remounting) never mutates a saved
    plan; plans are re-checked against whatever map is current.
    """

    def __init__(self, mounts=None):
        self._mounts = {}
        for mount in mounts or []:
            self.add(mount)

    def add(self, mount):
        """Register a :class:`MirrorMount`, replacing any mount for its root."""
        if not isinstance(mount, MirrorMount):
            raise TypeError('mounts must be MirrorMount instances')
        self._mounts[mount.root] = mount
        return mount

    def remount(self, root, mount_point, write_scope=('',), readable=True):
        """Relocate (or replace the scope of) a single archive's mount."""
        root = _normalize_root(root)
        self._mounts[root] = MirrorMount(
            root=root,
            mount_point=mount_point,
            write_scope=write_scope,
            readable=readable,
        )
        return self._mounts[root]

    def get(self, root):
        """Return the current mount for ``root`` or ``None``."""
        return self._mounts.get(_normalize_root(root))

    def __contains__(self, root):
        return _normalize_root(root) in self._mounts

    @property
    def mounts(self):
        """The current tuple of registered :class:`MirrorMount` objects."""
        return tuple(self._mounts.values())

    def candidates(self, root, relpath):
        """Return currently permitted local paths for ``root``/``relpath``.

        Only the mount for the *given* root is considered; another archive's
        same-named file is never returned as a fallback. Inaccessible mounts
        and mounts whose write scope does not cover the path yield nothing.
        """
        mount = self.get(root)
        if mount is None or not mount.readable or not mount.covers(relpath):
            return []
        return [mount.local_path(relpath)]


@dataclasses.dataclass(frozen=True)
class PlanItem:
    """One content-pinned, immutable fetch instruction."""

    source_uri: str
    root: str
    relpath: str
    digest: str | None
    version: str | None

    def to_dict(self):
        """Serialize the item to a plain JSON-compatible mapping."""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data):
        """Rebuild an item from a serialized mapping."""
        return cls(
            source_uri=data['source_uri'],
            root=_normalize_root(data['root']),
            relpath=_normalize_relpath(data['relpath']),
            digest=data.get('digest'),
            version=data.get('version'),
        )


class DownloadPlan:
    """An immutable set of :class:`PlanItem` fetch instructions.

    Plans serialize to plain JSON so they can be handed to an offline node and
    re-checked after remounts. A plan never stores a local target directory;
    target locations are always derived from the *current* :class:`MirrorMap`,
    which is what allows relocation while keeping the pinned content fixed.
    """

    def __init__(self, items=None, plan_id=None):
        items = list(items or [])
        seen = set()
        for item in items:
            if not isinstance(item, PlanItem):
                raise TypeError('plan items must be PlanItem instances')
            if item.relpath in seen:
                raise ValueError(f'duplicate plan item for {item.relpath}')
            seen.add(item.relpath)
        self._items = tuple(items)
        self.plan_id = plan_id

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    @property
    def items(self):
        """The immutable tuple of :class:`PlanItem` instructions."""
        return self._items

    def get(self, relpath):
        """Return the pinned item for ``relpath`` or ``None``."""
        relpath = _normalize_relpath(relpath)
        for item in self._items:
            if item.relpath == relpath:
                return item
        return None

    def to_dict(self):
        """Serialize the plan to a plain JSON-compatible mapping."""
        return {
            'plan_id': self.plan_id,
            'items': [item.to_dict() for item in self._items],
        }

    def save(self, path):
        """Write the plan as JSON so an offline node can re-check it."""
        p = Path(path)
        with p.fs.open(p.path, 'w', encoding='utf-8') as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)

    @classmethod
    def load(cls, path):
        """Load a previously saved plan without altering its pinned content."""
        p = Path(path)
        with p.fs.open(p.path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        return cls(
            items=[PlanItem.from_dict(d) for d in data.get('items', [])],
            plan_id=data.get('plan_id'),
        )


@dataclasses.dataclass
class PlanReport:
    """Outcome of planning or re-checking a single relative path."""

    relpath: str
    state: FileState
    source_uri: str
    root: str | None = None
    required_digest: str | None = None
    observed_digest: str | None = None
    local_path: str | None = None
    detail: str | None = None

    @property
    def is_satisfied(self):
        """True only when the exact pinned content is locally present."""
        return self.state is FileState.PRESENT

    def to_dict(self):
        """Serialize the report to a plain JSON-compatible mapping."""
        data = dataclasses.asdict(self)
        data['state'] = self.state.value
        return data


class MirrorPlanner:
    """Build and verify :class:`DownloadPlan` objects against a saved layout.

    The planner is strictly read-only with respect to the network and to the
    layout. It only reads local files through the current :class:`MirrorMap`
    and queries the already-indexed :class:`~bids.layout.BIDSLayout`.

    Parameters
    ----------
    layout : bids.layout.BIDSLayout
        A saved/indexed layout. File queries, source URIs and associations are
        taken from it and are never modified.
    registry : RemoteArchiveRegistry
        The archive roots the node may consult.
    manifest : ContentManifest
        Trusted content evidence keyed by relative path.
    mirror_map : MirrorMap
        The current mount/scope view.

    """

    def __init__(self, layout, registry, manifest, mirror_map):
        self.layout = layout
        self.registry = registry
        self.manifest = manifest
        self.mirror_map = mirror_map

    # -- helpers -----------------------------------------------------------

    def _indexed_relpaths(self):
        """All indexed non-directory relative paths, unchanged by planning."""
        return sorted(bf.relpath for bf in self.layout.get(return_type='object') if not bf.is_dir)

    def _source_uri(self, relpath):
        bf = self.layout.get_file(relpath)
        if bf is None:
            # Fall back to composing from the layout root (still read-only).
            return str(Path(self.layout.root) / relpath)
        return bf.path

    def _local_evidence(self, root, relpath, required_digest):
        """Inspect permitted local locations without any network access.

        Returns a ``(state, observed_digest, local_path, detail)`` tuple.
        """
        candidates = self.mirror_map.candidates(root, relpath)
        if not candidates:
            mount = self.mirror_map.get(root)
            if mount is None:
                return FileState.MISSING, None, None, 'no mounted location for this root'
            if not mount.readable:
                return FileState.INACCESSIBLE, None, None, 'mount is not accessible'
            if not mount.covers(relpath):
                return FileState.OUT_OF_SCOPE, None, None, 'outside permitted write scope'
            return FileState.MISSING, None, None, 'no permitted candidate location'

        local_path = candidates[0]
        if not local_path.exists():
            return FileState.MISSING, None, str(local_path), 'file not present at mount'

        if required_digest is None:
            return FileState.VERSION_UNKNOWN, None, str(local_path), 'no digest to verify against'

        observed = _file_digest(local_path, algorithm=required_digest.split(':', 1)[0])
        if observed == required_digest.split(':', 1)[1]:
            return FileState.PRESENT, observed, str(local_path), None
        # Something is locally available, but it is not the pinned content.
        return (
            FileState.SUPERSEDED,
            observed,
            str(local_path),
            'local content differs from pinned digest',
        )

    # -- planning ----------------------------------------------------------

    def plan(self, relpaths=None):
        """Create a content-pinned plan for the given (or all indexed) paths.

        Files whose exact content is already present locally are omitted from
        the plan (nothing to fetch). Files that need fetching produce an
        immutable :class:`PlanItem`. URIs outside every registered root are
        reported as :attr:`FileState.UNREGISTERED` and never planned.
        """
        all_relpaths = self._indexed_relpaths()
        if relpaths is not None:
            wanted = {_normalize_relpath(r) for r in relpaths}
            all_relpaths = [r for r in all_relpaths if r in wanted]

        items = []
        reports = []
        for relpath in all_relpaths:
            report = self.evaluate(relpath)
            reports.append(report)
            if report.state is FileState.PRESENT or report.state is FileState.UNREGISTERED:
                # Exact content already local, or URI belongs to no archive.
                continue
            record = self.manifest.get(relpath)
            if record is None or (record.digest is None and record.version is None):
                # Nothing trustworthy to pin; the report still records the state.
                continue
            items.append(
                PlanItem(
                    source_uri=report.source_uri,
                    root=report.root,
                    relpath=relpath,
                    digest=record.digest,
                    version=record.version,
                )
            )
        return DownloadPlan(items=items), reports

    def evaluate_uri(self, source_uri, relpath=None):
        """Evaluate an arbitrary source URI (read-only).

        Useful for URIs that are not indexed. A URI that shares a string prefix
        with a registered root but is not inside one is reported as
        :attr:`FileState.UNREGISTERED` rather than silently accepted.
        """
        source_uri = _normalize_root(source_uri)
        root = self.registry.root_for(source_uri)
        if root is None:
            suggestions = self.registry.suggest(source_uri)
            detail = 'no registered root contains this URI'
            if suggestions:
                detail += f'; did you mean {suggestions}?'
            return PlanReport(
                relpath=_normalize_relpath(relpath) if relpath else source_uri,
                state=FileState.UNREGISTERED,
                source_uri=source_uri,
                root=None,
                detail=detail,
            )
        if relpath is None:
            _, relpath = self.registry.split(source_uri)
        return self.evaluate(_normalize_relpath(relpath))

    def evaluate(self, relpath):
        """Evaluate one relative path against registry, manifest and mounts."""
        relpath = _normalize_relpath(relpath)
        source_uri = self._source_uri(relpath)

        root = self.registry.root_for(source_uri)
        if root is None:
            suggestions = self.registry.suggest(source_uri)
            detail = 'no registered root contains this URI'
            if suggestions:
                detail += f'; did you mean {suggestions}?'
            return PlanReport(
                relpath=relpath,
                state=FileState.UNREGISTERED,
                source_uri=source_uri,
                root=None,
                detail=detail,
            )

        record = self.manifest.get(relpath)
        required_digest = record.digest if record else None
        state, observed, local_path, detail = self._local_evidence(root, relpath, required_digest)
        return PlanReport(
            relpath=relpath,
            state=state,
            source_uri=source_uri,
            root=root,
            required_digest=required_digest,
            observed_digest=observed,
            local_path=local_path,
            detail=detail,
        )

    # -- verification of a previously saved plan ---------------------------

    def verify(self, plan):
        """Re-check a saved plan against the current map.

        The plan's pinned evidence is treated as authoritative: a newer local
        file can never satisfy an item pinned to older content. Each item is
        only ever relocated to a currently permitted location for its own
        root; same-named content in another archive is not a fallback.
        """
        reports = []
        for item in plan:
            # The registry/map may have changed; re-resolve from the pinned URI.
            root = self.registry.root_for(item.source_uri)
            if root is None:
                reports.append(
                    PlanReport(
                        relpath=item.relpath,
                        state=FileState.UNREGISTERED,
                        source_uri=item.source_uri,
                        root=None,
                        required_digest=item.digest,
                        detail='source URI no longer belongs to a registered root',
                    )
                )
                continue

            state, observed, local_path, detail = self._local_evidence(
                root, item.relpath, item.digest
            )
            if state is FileState.SUPERSEDED:
                # An old plan must not be marked satisfied by a newer file.
                detail = (detail or '') + '; saved plan remains pinned to the original content'
            reports.append(
                PlanReport(
                    relpath=item.relpath,
                    state=state,
                    source_uri=item.source_uri,
                    root=root,
                    required_digest=item.digest,
                    observed_digest=observed,
                    local_path=local_path,
                    detail=detail,
                )
            )
        return reports

    def assert_executable(self, plan):
        """Raise :class:`PlanValidationError` if any plan item cannot run.

        A plan is executable only when every item can be relocated to a
        currently permitted, accessible location for its own root. Present
        exact content is fine; superseding content, inaccessible mounts,
        out-of-scope targets and unregistered URIs are not.
        """
        reports = self.verify(plan)
        # Executable means every item relocates to a permitted, accessible
        # location for its own root. PRESENT/MISSING/VERSION_UNKNOWN all have a
        # valid location (exact content, awaiting fetch, or present-but-unverified).
        # SUPERSEDED, INACCESSIBLE, OUT_OF_SCOPE and UNREGISTERED cannot run.
        blocked = {
            FileState.SUPERSEDED,
            FileState.INACCESSIBLE,
            FileState.OUT_OF_SCOPE,
            FileState.UNREGISTERED,
        }
        failures = [r for r in reports if r.state in blocked]
        if failures:
            summary = '; '.join(f'{r.relpath}={r.state.value}' for r in failures)
            raise PlanValidationError(
                'plan is not executable under the current mirror map: ' + summary
            )
        return reports

    # -- queries -----------------------------------------------------------

    def reports_by_state(self, reports):
        """Group reports by :class:`FileState` for easy status queries."""
        grouped = {}
        for report in reports:
            grouped.setdefault(report.state, []).append(report)
        return grouped

    def query_state(self, state):
        """Return reports for all indexed files currently in ``state``."""
        state = FileState(state)
        return [
            report
            for report in (self.evaluate(rel) for rel in self._indexed_relpaths())
            if report.state is state
        ]
