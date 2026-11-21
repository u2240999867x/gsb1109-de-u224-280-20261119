"""Batch path-renaming previews.

This module plans a coordinated rename without touching the filesystem.  The
caller supplies source files, the naming patterns that are acceptable in
priority order, and the configuration the results must read back under.  The
result is a complete old-to-new mapping, or a structured account of why no such
mapping exists.

This is deliberately separate from :func:`bids.layout.writing.build_path`.
``build_path`` answers "what is the first pattern that fits this one file?".
A batch rename has a stronger requirement: the entire set of files must be
placed at once, so a pattern that fits a file in isolation may be unusable
because it steals a destination another file needs, because an existing file
already owns it, or because it would break a sidecar's inheritance relation.
The planner therefore searches assignments globally and honours the pattern
priority across the batch instead of committing each file to its own first
choice.

Association preservation is defined over the *hit set*.  A JSON sidecar applies
to a data file when the sidecar is in an ancestor directory, has the same
suffix, and its remaining entities are a subset of the data file's entities.
An auxiliary file (events, physio, stim, sbref, bval, bvec) intends an image
when their entities agree apart from suffix and extension.  The preview
compares these relations before and after the rename, so a shared sidecar has
to remain in a directory that is still a common ancestor of every run it used
to describe.
"""

import re
from dataclasses import dataclass, field

from upath import UPath as Path

from ..utils import listify
from .models import Config
from .writing import _expand_options, build_path

__all__ = [
    'RenameCandidate',
    'RenameConflict',
    'RenamePlan',
    'plan_renames',
    'preview_renames',
]

_AUX_SUFFIXES = {'events', 'physio', 'stim', 'sbref'}


@dataclass(frozen=True)
class RenameCandidate:
    """One acceptable destination for a source file.

    Attributes
    ----------
    source : str
        Original relative path.
    path : str
        Proposed relative destination path.
    pattern : str
        Pattern that produced this candidate.
    priority : int
        Index of the pattern in the caller's list; lower is preferred.
    readback : dict
        Entities recovered from ``path`` by the target configuration.
    defaults : dict
        Entities supplied by a pattern default because the source did not
        define them.  Kept separate so a default is distinguishable from a
        value the source actually carried.
    available : bool
        False when the destination reads back correctly but is already owned by
        a non-moving file.  Such candidates document why a pattern was blocked.
    blocker : str or None
        The path that owns an ``available=False`` candidate.

    """

    source: str
    path: str
    pattern: str
    priority: int
    readback: dict = field(hash=False)
    defaults: dict = field(hash=False, default_factory=dict)
    available: bool = True
    blocker: str | None = None

    def __hash__(self):
        return hash((self.source, self.path, self.priority))


@dataclass(frozen=True)
class RenameConflict:
    """A reason a source or the batch as a whole cannot be renamed.

    Attributes
    ----------
    kind : str
        One of ``'occupied'``, ``'no-candidate'``, ``'readback'``,
        ``'collision'``, ``'association'``, or ``'abandoned'``.
    source : str or None
        Affected source path, when the conflict is source-specific.
    path : str or None
        Destination path involved, when applicable.
    detail : str
        Human-readable explanation.
    blocking_entities : tuple
        Entity names or values that caused the conflict.
    blockers : tuple
        Other sources or placeholders responsible for the conflict.

    """

    kind: str
    source: str | None
    path: str | None
    detail: str
    blocking_entities: tuple = ()
    blockers: tuple = ()


@dataclass
class RenamePlan:
    """Result of a batch-rename preview.

    Attributes
    ----------
    moves : dict
        Mapping from source path to :class:`RenameCandidate`.
    conflicts : list
        Reasons some sources or combinations were rejected.
    patterns_used : dict
        Mapping from source path to the pattern string ultimately used.
    feasible : bool
        True when every source received a destination and all constraints hold.
    associations_before : set
        Typed relation pairs among moving and fixed files before the rename.
    associations_after : set
        The same pairs predicted for the selected destinations.
    notes : list
        Statements about relations the preview cannot enforce by path choice,
        such as IntendedFor entries declared inside JSON content.
    candidates : dict
        Every usable destination considered for each source, in priority order.
        This is the option space a maintainer needs in order to see why a chosen
        pattern was preferred over its alternatives.

    """

    moves: dict = field(default_factory=dict)
    conflicts: list = field(default_factory=list)
    patterns_used: dict = field(default_factory=dict)
    feasible: bool = False
    associations_before: set = field(default_factory=set)
    associations_after: set = field(default_factory=set)
    notes: list = field(default_factory=list)
    candidates: dict = field(default_factory=dict)

    @property
    def mapping(self):
        """Return a plain ``source -> destination`` dictionary."""
        return {src: cand.path for src, cand in self.moves.items()}

    @property
    def blocking_sources(self):
        """Sources that could not be placed, in path order."""
        blocked = {c.source for c in self.conflicts if c.source and c.source not in self.moves}
        return tuple(sorted(blocked))

    def __bool__(self):
        return self.feasible

    def explain(self):
        """Return a short human-readable account of the plan."""
        if self.feasible:
            return f'Feasible rename of {len(self.moves)} files.'
        lines = [f'No complete rename for {len(self.blocking_sources)} source(s).']
        for conflict in self.conflicts:
            if conflict.kind == 'abandoned':
                continue
            lines.append(f'- {conflict.kind}: {conflict.detail}')
        return '\n'.join(lines)

    def to_dict(self):
        """Serialize the preview to JSON-friendly primitives."""
        return {
            'feasible': self.feasible,
            'moves': {
                src: {
                    'path': cand.path,
                    'pattern': cand.pattern,
                    'priority': cand.priority,
                    'readback': {k: str(v) for k, v in cand.readback.items()},
                    'defaults': {k: str(v) for k, v in cand.defaults.items()},
                }
                for src, cand in sorted(self.moves.items())
            },
            'conflicts': [
                {
                    'kind': c.kind,
                    'source': c.source,
                    'path': c.path,
                    'detail': c.detail,
                    'blocking_entities': list(c.blocking_entities),
                    'blockers': list(c.blockers),
                }
                for c in self.conflicts
            ],
            'notes': list(self.notes),
            'candidates': {
                src: [
                    {
                        'path': cand.path,
                        'pattern': cand.pattern,
                        'priority': cand.priority,
                        'available': cand.available,
                        'blocker': cand.blocker,
                        'selected': self.moves.get(src) is cand,
                    }
                    for cand in options
                ]
                for src, options in sorted(self.candidates.items())
            },
        }


def _posix(path):
    return Path(str(path)).as_posix()


def _relpath(path, root):
    """Return ``path`` relative to ``root`` when it lives below it."""
    path = Path(str(path)).absolute()
    root = Path(str(root)).absolute()
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _dirname(path):
    return str(Path(_posix(path)).parent)


def _is_ancestor_dir(ancestor, descendant):
    """True when ``descendant`` lives in or below ``ancestor``'s directory.

    A sidecar at the dataset root has ``'.'`` as its directory, which is an
    ancestor of every relative path.  Handling that explicitly matters because
    root-level sidecars are routinely shared by many subjects and runs.
    """
    ancestor_dir = _dirname(ancestor)
    descendant_dir = _dirname(descendant)
    if ancestor_dir in ('.', ''):
        return True
    return descendant_dir == ancestor_dir or descendant_dir.startswith(
        ancestor_dir.rstrip('/') + '/'
    )


def _suffix(entities):
    value = entities.get('suffix')
    return None if value is None else str(value)


def _extension(entities):
    value = entities.get('extension')
    return None if value is None else str(value)


def _normalized(entities):
    """Normalize entities to strings, dropping absent values.

    ``PaddedInt`` keeps leading zeros through ``str``, so ``01`` and ``1``
    remain distinguishable even though they compare equal as integers.
    """
    return {k: str(v) for k, v in entities.items() if v not in (None, '')}


def _inheritance_entities(entities):
    return {k: v for k, v in _normalized(entities).items() if k not in ('suffix', 'extension')}


def _is_subset(sidecar, data):
    return all(data.get(key) == value for key, value in sidecar.items())


def _pattern_defaults(pattern):
    """Map entity name to the default declared after a pipe in a pattern."""
    defaults = {}
    for match in re.finditer(r'\{(\w+)(?:<[^>]+>)?\|([^}]+)\}', pattern):
        defaults[match.group(1)] = match.group(2)
    return defaults


class _Readback:
    """Read filename entities from a target config, independently of a layout."""

    def __init__(self, configs):
        self.configs = configs
        self.entities = {}
        for config in configs:
            self.entities.update(config.entities)
        self.names = set(self.entities)

    def parse(self, path):
        """Return entities the config finds in ``path``.

        Config regexes often require a separator before a directory component
        (e.g. ``/sub-01``).  Prefixing a slash lets relative candidate paths
        parse exactly as they would once rooted in the dataset, without
        altering the extension or suffix regexes.
        """
        candidate = '/' + _posix(path).lstrip('/')
        result = {}
        for entity in self.entities.values():
            if entity.regex is None:
                continue
            match = entity.regex.search(candidate)
            if match is not None:
                result[entity.name] = entity._astype(match.group(1))
        return result


def _load_configs(config):
    """Normalize a config argument to a list of Config objects."""
    if config is None:
        config = 'bids'
    configs = []
    for item in listify(config):
        if isinstance(item, Config):
            configs.append(item)
        else:
            configs.append(Config.load(item))
    return configs


def _candidate_defaults(pattern, source_entities, readback):
    """Identify values a candidate received from a pattern default."""
    defaults = {}
    source = _normalized(source_entities)
    for name, value in _pattern_defaults(pattern).items():
        if name in source:
            continue
        parsed = readback.get(name)
        defaults[name] = str(value if parsed is None else parsed)
    return defaults


def _metadata_relation(sidecar, data):
    """True when ``sidecar`` (JSON) applies to ``data`` after both are renamed."""
    if _extension(sidecar.readback) != '.json':
        return False
    if _extension(data.readback) == '.json':
        return False
    if _suffix(sidecar.readback) is None:
        return False
    if _suffix(sidecar.readback) != _suffix(data.readback):
        return False
    if not _is_ancestor_dir(sidecar.path, data.path):
        return False
    return _is_subset(
        _inheritance_entities(sidecar.readback),
        _inheritance_entities(data.readback),
    )


def _intended_relation(aux, image):
    """True when ``aux`` intends ``image`` under pybids' matching rules.

    Sidecars are metadata, not intended targets: pybids creates IntendedFor
    links from data files only (events, physio, stim, sbref, bval, bvec).
    """
    aux_suffix = _suffix(aux.readback)
    image_suffix = _suffix(image.readback)
    aux_ext = _extension(aux.readback)
    image_ext = _extension(image.readback)

    if aux_ext == '.json':
        return False
    if image_ext not in ('.nii', '.nii.gz'):
        return False

    if image_suffix == 'bold':
        if aux_suffix not in _AUX_SUFFIXES:
            return False
    elif image_suffix == 'dwi':
        if aux_suffix != 'sbref' and aux_ext not in ('.bval', '.bvec'):
            return False
    else:
        return False

    # pybids queries images using every entity the auxiliary file carries, so
    # an auxiliary file without ``run`` can intend several runs, while one with
    # ``run`` only matches that run.  Subset, not equality, captures this.
    return _is_subset(
        _inheritance_entities(aux.readback),
        _inheritance_entities(image.readback),
    )


def _relation_types(left, right):
    """Return the typed relations that hold between two candidates."""
    types = set()
    if _metadata_relation(left, right):
        types.add('metadata')
    if _metadata_relation(right, left):
        types.add('metadata')
    if _intended_relation(left, right):
        types.add('intended')
    if _intended_relation(right, left):
        types.add('intended')
    return types


def _association_set(items, focus=None):
    """Build typed relation pairs for a path->candidate mapping.

    When ``focus`` is given, only pairs with at least one endpoint in the focus
    set are returned.  This keeps the comparison small when many fixed files
    participate only as context.
    """
    paths = sorted(items)
    result = set()
    for i, left in enumerate(paths):
        for right in paths[i + 1 :]:
            if focus is not None and left not in focus and right not in focus:
                continue
            for kind in _relation_types(items[left], items[right]):
                result.add((kind, left, right))
                result.add((kind, right, left))
    return result


def _source_candidate(source, entities, pattern, priority, reader, is_occupied):
    """Build a candidate for one source/pattern pair, or explain rejection.

    The returned candidate is ``None`` when the destination cannot be used.  A
    usable candidate may still be marked ``available=False`` when the path
    reads back correctly but is already owned by a non-moving file.  Keeping
    that distinction lets the preview say "this is the right name, but the slot
    is taken" instead of collapsing it into "no candidate".
    """
    built = build_path(entities, [pattern])
    if built is None:
        required = _required_entities(pattern, entities)
        if required:
            detail = (
                f'pattern {priority} does not apply: it requires entities '
                f'{sorted(required)} that this source does not define'
            )
        else:
            allowed = _constrained_entities(pattern, entities)
            if allowed:
                detail = (
                    f'pattern {priority} does not apply: entity values '
                    f'{allowed} are not accepted by the pattern selector'
                )
            else:
                detail = (
                    f'pattern {priority} does not apply to this source '
                    '(build_path produced no path)'
                )
        return None, RenameConflict(
            'no-candidate',
            source,
            None,
            detail,
            blocking_entities=tuple(sorted(required or allowed or ())),
        )
    if isinstance(built, list):
        built = built[0]
    dest = _posix(built)

    occupied, blocker = is_occupied(dest, source)

    readback = reader.parse(dest)
    produced = _normalized(readback)
    required = {k: v for k, v in _normalized(entities).items() if k in reader.names}
    mismatched = tuple(sorted(k for k, v in required.items() if produced.get(k) != v))
    if mismatched:
        details = ', '.join(
            f'{name}={required[name]!r} read back as {produced.get(name)!r}' for name in mismatched
        )
        return None, RenameConflict(
            'readback',
            source,
            dest,
            f'{dest!r} does not read back entities {mismatched} ({details})',
            blocking_entities=mismatched,
        )

    defaults = _candidate_defaults(pattern, entities, readback)
    candidate = RenameCandidate(
        source,
        dest,
        pattern,
        priority,
        dict(readback),
        defaults,
        available=not occupied,
        blocker=blocker if occupied else None,
    )
    if occupied:
        conflict = RenameConflict(
            'occupied',
            source,
            dest,
            f'{dest!r} already exists and must not be overwritten',
            blocking_entities=tuple(sorted(_normalized(entities))),
            blockers=(blocker,),
        )
        return candidate, conflict
    return candidate, None


def _constrained_entities(pattern, entities):
    """Return ``name=value`` strings whose value a pattern selector rejects.

    A pattern like ``{suffix<events|physio>}`` fails for a source whose suffix
    is ``bold`` even though the entity is present.  Naming that value is more
    useful to a maintainer than repeating "no candidate".
    """
    available = _normalized(entities)
    rejected = []
    for match in re.finditer(r'\{(\w+)<([^>]+)>', pattern):
        name, selector = match.group(1), match.group(2)
        value = available.get(name)
        if value is None:
            continue
        options = [v for part in selector.split('|') for v in _expand_options(part)]
        if options and value not in options:
            rejected.append(f'{name}={value!r}')
    return rejected


def _required_entities(pattern, entities):
    """Entity names a pattern needs that the source cannot supply.

    Entities in optional sections are only required when another entity from
    the same section is present (which makes the section mandatory), and
    entities with a pattern default are never required.
    """
    available = set(_normalized(entities))
    defaults = _pattern_defaults(pattern)
    required = set()

    def visit(text):
        for match in re.finditer(r'\[([^\]]*)\]|\{(\w+)(?:<[^>]+>)?(?:\|[^}]+)?\}', text):
            if match.group(2) is not None:
                name = match.group(2)
                if name not in available and name not in defaults:
                    required.add(name)
                continue
            section = match.group(1)
            names = re.findall(r'\{(\w+)', section)
            if any(name in available for name in names):
                visit(section)

    visit(pattern)
    return required


def _priority_score(moves, n_patterns):
    """Counts of files at each pattern priority, earliest first."""
    counts = [0] * n_patterns
    for cand in moves.values():
        counts[max(cand.priority, 0)] += 1
    return tuple(counts)


def _better(candidate, incumbent, n_sources, n_patterns):
    """True when ``candidate`` beats ``incumbent``.

    Complete assignments beat partial ones.  Among equals, prefer more files,
    then more files on earlier patterns, then the lexicographically smallest
    sorted destination list.  The final tie-break makes the choice unique and
    independent of the order sources were supplied.
    """
    if incumbent is None:
        return True
    cand_complete = len(candidate) == n_sources
    inc_complete = len(incumbent) == n_sources
    if cand_complete != inc_complete:
        return cand_complete
    if len(candidate) != len(incumbent):
        return len(candidate) > len(incumbent)
    cand_score = _priority_score(candidate, n_patterns)
    inc_score = _priority_score(incumbent, n_patterns)
    if cand_score != inc_score:
        return cand_score > inc_score
    cand_paths = tuple(sorted(m.path for m in candidate.values()))
    inc_paths = tuple(sorted(m.path for m in incumbent.values()))
    return cand_paths < inc_paths


def _layout_fixed_files(layout, source_paths, root):
    """Collect non-moving files that matter to the batch's associations.

    Only files that are actually associated with a moving source are fixed.
    Everything else in the dataset is merely protection: it must not be
    overwritten, but requiring it to preserve arbitrary relations to the
    batch would make a real dataset impossible to migrate.
    """
    fixed = {}
    protected = set()
    for path in layout.get_files(scope='all'):
        rel = _relpath(path, root)
        if rel not in source_paths:
            protected.add(rel)
    for path in source_paths:
        bf = layout.get_file(str(Path(root) / path))
        if bf is None:
            continue
        for assoc in bf.get_associations(include_parents=True):
            rel = _relpath(assoc.path, root)
            if rel not in source_paths:
                fixed[rel] = dict(assoc.get_entities(metadata=False, values='tags'))
    return fixed, protected


def _layout_association_pairs(layout, node_paths, root):
    """Read indexed association edges among ``node_paths``.

    Using the index rather than a theoretical model matters: real datasets
    contain relations the model does not enumerate (for example an fmap image
    intended for a functional run), and those must be preserved too.  Edges are
    returned as ``(kind, left, right)`` with relative paths.

    IntendedFor edges whose source is not one of BIDS' auxiliary data files are
    declared inside JSON content rather than derived from entities.  A rename
    preview cannot rewrite that content, so those edges are excluded here and
    reported separately by the caller.
    """
    from .models import FileAssociation

    node_paths = set(node_paths)
    results = set()
    for edge in layout.session.query(FileAssociation).all():
        left = _relpath(edge.src, root)
        right = _relpath(edge.dst, root)
        if left == right:
            continue
        if left not in node_paths or right not in node_paths:
            continue
        if edge.kind in ('IntendedFor', 'InformedBy'):
            aux = left if edge.kind == 'IntendedFor' else right
            suffix = str(Path(aux).name).split('_')[-1].split('.')[0]
            extension = ''.join(Path(aux).suffixes)
            if suffix not in _AUX_SUFFIXES and extension not in ('.bval', '.bvec'):
                continue
        results.add((edge.kind, left, right))
    return results


def _canonical_kind(kind):
    """Collapse pybids' directional association labels to a relation family."""
    if kind in ('Metadata', 'MetadataIn', 'MetadataFor', 'Informs'):
        return 'metadata'
    if kind in ('IntendedFor', 'InformedBy'):
        return 'intended'
    if kind in ('Child', 'Parent'):
        return 'inheritance'
    return 'other'


def _ancestor_compatible(path_a, path_b):
    """True when two paths still share a directory relationship."""
    dir_a = _dirname(path_a)
    dir_b = _dirname(path_b)
    if dir_a in ('.', '') or dir_b in ('.', ''):
        return True
    return (
        dir_a == dir_b
        or dir_a.startswith(dir_b.rstrip('/') + '/')
        or dir_b.startswith(dir_a.rstrip('/') + '/')
    )


def _pair_preserved(kind, left, right, items):
    """Check that an indexed association edge still holds after the rename."""
    if left not in items or right not in items:
        return False
    left_cand = items[left]
    right_cand = items[right]
    family = _canonical_kind(kind)

    if family == 'metadata':
        sidecar, data = (
            (left_cand, right_cand)
            if _extension(left_cand.readback) == '.json'
            else (right_cand, left_cand)
        )
        return _metadata_relation(sidecar, data)

    if family == 'intended':
        image_exts = ('.nii', '.nii.gz')
        left_ext = _extension(left_cand.readback)
        right_ext = _extension(right_cand.readback)
        if left_ext in image_exts and right_ext not in image_exts:
            return _intended_relation(right_cand, left_cand)
        if right_ext in image_exts and left_ext not in image_exts:
            return _intended_relation(left_cand, right_cand)
        return _intended_relation(left_cand, right_cand) or _intended_relation(
            right_cand, left_cand
        )

    if family == 'inheritance':
        # Child/Parent is directional in the index: Child is the more specific
        # sidecar, Parent the file it inherits from.  Both must be JSON for a
        # metadata chain; otherwise only the directory relationship matters.
        left_is_json = _extension(left_cand.readback) == '.json'
        right_is_json = _extension(right_cand.readback) == '.json'
        if kind in ('Child', 'Parent') and left_is_json and right_is_json:
            child, parent = (left_cand, right_cand) if kind == 'Child' else (right_cand, left_cand)
            return _is_ancestor_dir(parent.path, child.path) and _is_subset(
                _inheritance_entities(parent.readback),
                _inheritance_entities(child.readback),
            )
        return _ancestor_compatible(left_cand.path, right_cand.path)

    return _ancestor_compatible(left_cand.path, right_cand.path)


def plan_renames(
    sources,
    patterns,
    config='bids',
    layout=None,
    occupied=(),
    fixed=None,
    preserve_associations=True,
    allow_partial=False,
    root=None,
):
    """Plan a batch rename without writing anything.

    Parameters
    ----------
    sources : dict or list
        Either a mapping from relative source path to an entity dictionary, or
        a list of source paths when ``layout`` is supplied.  Every source must
        read back unchanged from its chosen destination.
    patterns : str or list
        Naming patterns in priority order, using the same syntax as
        :func:`bids.layout.writing.build_path`.  Lower index is preferred.
    config : str, dict, Config, or list, optional
        Target configuration used to read entities back from candidate paths.
        This is deliberately independent of ``layout`` so a rename cannot
        validate itself against the index that produced the source entities.
    layout : BIDSLayout, optional
        Layout used to obtain source entities, existing files, and the
        association context.  Not used for read-back validation.
    occupied : iterable, optional
        Existing paths not participating in the rename.  No candidate may use
        them.  Paths indexed by ``layout`` are added automatically.
    fixed : dict, optional
        Non-moving files that participate in associations, mapping path to an
        entity dictionary.  Files associated with a supplied ``layout`` are
        added automatically.
    preserve_associations : bool, optional
        If True (default), the association hit set among moving and fixed files
        must be identical before and after the rename.
    allow_partial : bool, optional
        If True, return the best subset instead of requiring every source to
        move.  Conflicts are still reported and ``feasible`` remains False
        unless every source moved.
    root : str or path, optional
        Dataset root.  When supplied, existing paths below it are treated as
        occupied even if not listed in ``occupied`` or indexed by ``layout``.

    Returns
    -------
    RenamePlan
        A preview containing the selected moves, association sets, and any
        blocking conflicts.

    Examples
    --------
    >>> patterns = [
    ...     'sub-{subject}/func/sub-{subject}_task-{task}[_acq-{acquisition}]'
    ...     '[_run-{run}]_{suffix}{extension}',
    ...     'sub-{subject}/func/acq-{acquisition}/sub-{subject}_task-{task}'
    ...     '[_run-{run}]_{suffix}{extension}',
    ... ]
    >>> sources = {
    ...     'sub-01/func/sub-01_task-rest_run-01_bold.nii.gz': {
    ...         'subject': '01',
    ...         'task': 'rest',
    ...         'run': '01',
    ...         'suffix': 'bold',
    ...         'extension': '.nii.gz',
    ...         'datatype': 'func',
    ...     },
    ... }
    >>> plan = plan_renames(sources, patterns, config='bids')
    >>> plan.feasible
    True
    >>> plan.mapping['sub-01/func/sub-01_task-rest_run-01_bold.nii.gz']
    'sub-01/func/sub-01_task-rest_run-01_bold.nii.gz'

    """
    configs = _load_configs(config)
    reader = _Readback(configs)

    if layout is not None:
        root = root or layout.root

    # Normalize sources.  Paths may arrive relative or absolute; everything
    # downstream (occupancy, build_path, read-back) speaks relative paths, so
    # convert absolute input against the dataset root first.
    def normalize_source(key):
        return _posix(_relpath(key, root)) if root is not None else _posix(key)

    if isinstance(sources, dict):
        source_entities = {normalize_source(key): dict(value) for key, value in sources.items()}
    elif layout is not None:
        source_entities = {}
        for source in sources:
            source_path = getattr(source, 'path', source)
            absolute = Path(str(source_path))
            if not absolute.is_absolute():
                absolute = Path(str(root)) / source_path
            rel = _relpath(absolute, root)
            bf = layout.get_file(str(absolute))
            if bf is None and rel != str(absolute):
                bf = layout.get_file(str(Path(root) / rel))
            if bf is None:
                raise ValueError(f'Source {source_path!r} is not indexed in the layout')
            source_entities[rel] = dict(bf.get_entities(metadata=False, values='tags'))
    else:
        raise TypeError(
            'Supply sources as a path->entities mapping or pass a layout with a path list'
        )

    source_paths = sorted(source_entities)
    patterns = [str(pattern) for pattern in listify(patterns)]
    if not patterns:
        raise ValueError('At least one naming pattern is required')

    fixed_entities = {_posix(key): dict(value) for key, value in (fixed or {}).items()}

    # A layout contributes association context and protects all indexed files.
    if layout is not None:
        derived_fixed, derived_protected = _layout_fixed_files(layout, set(source_paths), root)
        for path, ents in derived_fixed.items():
            fixed_entities.setdefault(path, ents)
        protected = derived_protected | set(source_paths) | set(fixed_entities)
    else:
        protected = set(source_paths) | set(fixed_entities)

    occupied_paths = {_posix(path) for path in occupied}
    protected |= occupied_paths

    def is_occupied(dest, source):
        """Check a destination against protected files and the live filesystem."""
        if dest == _posix(source):
            return False, None
        if dest in protected:
            return True, dest
        if root is not None and Path(root, dest).exists():
            return True, dest
        return False, None

    conflicts = []
    candidates = {}
    for path in source_paths:
        entities = source_entities[path]
        found = []
        for priority, pattern in enumerate(patterns):
            candidate, conflict = _source_candidate(
                path, entities, pattern, priority, reader, is_occupied
            )
            if candidate is not None:
                found.append(candidate)
            if conflict is not None:
                conflicts.append(conflict)
        # Several patterns can converge on one destination; keep the earliest.
        best_by_path = {}
        for candidate in found:
            previous = best_by_path.get(candidate.path)
            if previous is None or candidate.priority < previous.priority:
                best_by_path[candidate.path] = candidate
        candidates[path] = sorted(best_by_path.values(), key=lambda c: (c.priority, c.path))
        if not any(candidate.available for candidate in candidates[path]):
            conflicts.append(
                RenameConflict(
                    'no-candidate',
                    path,
                    None,
                    f'{path!r} has no available destination in the target configuration',
                    blocking_entities=tuple(sorted(_normalized(entities))),
                    blockers=tuple(
                        candidate.blocker for candidate in candidates[path] if candidate.blocker
                    ),
                )
            )

    # Fixed files keep their current paths; read their entities for relation checks.
    fixed_candidates = {}
    for path, entities in fixed_entities.items():
        readback = reader.parse(path)
        merged = dict(readback)
        merged.update(_normalized(entities))
        fixed_candidates[path] = RenameCandidate(path, path, '<fixed>', -1, merged)

    before_items = {}
    for path in source_paths:
        readback = reader.parse(path)
        merged = dict(readback)
        merged.update(_normalized(source_entities[path]))
        before_items[path] = RenameCandidate(path, path, '<source>', -1, merged)
    before_items.update(fixed_candidates)

    focus = set(source_paths)
    associations_before = _association_set(before_items, focus=focus)

    # When a real index is available, its edges are authoritative.  The
    # theoretical model above still covers batches built from entity dicts
    # without a layout, but it cannot know about every relation pybids derives.
    indexed_before = set()
    if layout is not None and fixed_entities:
        nodes = set(source_paths) | set(fixed_entities)
        indexed_before = _layout_association_pairs(layout, nodes, root)
        canonical_before = {
            (_canonical_kind(kind), left, right) for kind, left, right in indexed_before
        }
        associations_before = associations_before | canonical_before

    # Keep every old relation touching a moving source; relations among fixed
    # files cannot change.
    relevant_before = set(associations_before)

    order = source_paths
    best = {'moves': None}

    def _hopeless(index, chosen, incumbent):
        """Branch-and-bound: can this branch still beat the incumbent?

        Every unassigned source can do no better than its highest available
        priority, so if that optimistic bound cannot reach the incumbent's
        priority histogram, backtrack.  The check is a pure accelerator and
        never removes a branch that could win.
        """
        if incumbent is None or len(incumbent) != len(order):
            return False
        optimistic = [0] * len(patterns)
        for cand in chosen.values():
            optimistic[max(cand.priority, 0)] += 1
        for path in order[index:]:
            available = [c.priority for c in candidates[path] if c.available]
            if not available:
                return not allow_partial
            optimistic[max(min(available), 0)] += 1
        incumbent_score = _priority_score(incumbent, len(patterns))
        return tuple(optimistic) < incumbent_score

    def consistent(chosen):
        """Check association relations for every endpoint assigned so far."""
        items = dict(fixed_candidates)
        items.update(chosen)
        current = _association_set(items, focus=focus)
        assigned = set(items)

        if indexed_before:
            for kind, left, right in indexed_before:
                if left in assigned and right in assigned:
                    # Only canonical families are checked here; malformed or
                    # dynamic association labels fall back to ancestor logic.
                    if not _pair_preserved(kind, left, right, items):
                        return False

        for kind, left, right in relevant_before:
            if left in assigned and right in assigned:
                if kind in ('metadata', 'intended', 'inheritance'):
                    if not _pair_preserved(kind, left, right, items):
                        return False
                elif (kind, left, right) not in current:
                    return False

        for kind, left, right in current:
            if left in assigned and right in assigned:
                if (kind, left, right) not in associations_before:
                    if left in focus or right in focus:
                        return False
        return True

    def search(index, chosen, used_paths):
        if index == len(order):
            if _better(chosen, best['moves'], len(order), len(patterns)):
                best['moves'] = dict(chosen)
            return
        if _hopeless(index, chosen, best['moves']):
            return
        path = order[index]
        for candidate in candidates[path]:
            if not candidate.available:
                continue
            if candidate.path in used_paths:
                continue
            chosen[path] = candidate
            used_paths.add(candidate.path)
            if consistent(chosen):
                search(index + 1, chosen, used_paths)
            used_paths.remove(candidate.path)
            del chosen[path]

        if allow_partial:
            if _better(chosen, best['moves'], len(order), len(patterns)):
                best['moves'] = dict(chosen)
            search(index + 1, chosen, used_paths)

    search(0, {}, set())
    selected = best['moves'] or {}

    # Explain every source omitted from the selected plan and every earlier
    # candidate that had to be abandoned in favour of a later pattern.
    selected_by_dest = {cand.path: src for src, cand in selected.items()}

    def broken_relations(path, candidate):
        """Return old relations involving ``path`` that ``candidate`` breaks."""
        items = dict(fixed_candidates)
        items.update(selected)
        items[path] = candidate
        broken = []
        for kind, left, right in sorted(relevant_before):
            if path not in (left, right):
                continue
            if left not in items or right not in items:
                continue
            if not _pair_preserved(kind, left, right, items):
                other = right if left == path else left
                broken.append((kind, other))
        return broken

    for path in source_paths:
        entities = source_entities[path]
        if path not in selected:
            if not candidates[path]:
                continue
            for candidate in candidates[path]:
                if not candidate.available:
                    conflicts.append(
                        RenameConflict(
                            'occupied',
                            path,
                            candidate.path,
                            f'{candidate.path!r} is occupied by a non-moving file '
                            f'({candidate.blocker!r})',
                            blocking_entities=tuple(sorted(_normalized(entities))),
                            blockers=(candidate.blocker,) if candidate.blocker else (),
                        )
                    )
                    continue
                blocker = selected_by_dest.get(candidate.path)
                if blocker is not None and blocker != path:
                    detail = (
                        f'{candidate.path!r} is kept for {blocker!r} '
                        f'(pattern {selected[blocker].priority})'
                    )
                    kind = 'collision'
                    blockers = (blocker,)
                else:
                    occupied, occupied_by = is_occupied(candidate.path, path)
                    if occupied:
                        detail = (
                            f'{candidate.path!r} is occupied by a non-moving file '
                            f'({occupied_by!r})'
                        )
                        kind = 'occupied'
                        blockers = (occupied_by,)
                    else:
                        broken = broken_relations(path, candidate)
                        if broken:
                            relation, other = broken[0]
                            detail = (
                                f'choosing {candidate.path!r} would break the '
                                f'{relation} relation with {other!r}'
                            )
                            blockers = (other,)
                        else:
                            detail = (
                                f'choosing {candidate.path!r} would change the association hit set'
                            )
                            blockers = ()
                        kind = 'association'
                conflicts.append(
                    RenameConflict(
                        kind,
                        path,
                        candidate.path,
                        detail,
                        blocking_entities=tuple(sorted(_normalized(entities))),
                        blockers=blockers,
                    )
                )
            continue

        chosen = selected[path]
        for candidate in candidates[path]:
            if candidate.priority >= chosen.priority:
                continue
            if not candidate.available:
                conflicts.append(
                    RenameConflict(
                        'abandoned',
                        path,
                        candidate.path,
                        f'pattern {candidate.priority} was abandoned because '
                        f'{candidate.path!r} is occupied by a non-moving file '
                        f'({candidate.blocker!r})',
                        blocking_entities=tuple(sorted(_normalized(entities))),
                        blockers=(candidate.blocker,) if candidate.blocker else (),
                    )
                )
                continue
            blocker = selected_by_dest.get(candidate.path)
            if blocker is not None and blocker != path:
                detail = (
                    f'pattern {candidate.priority} was abandoned because '
                    f'{candidate.path!r} is reserved for {blocker!r} '
                    f'(pattern {selected[blocker].priority})'
                )
                blockers = (blocker,)
            else:
                occupied, occupied_by = is_occupied(candidate.path, path)
                if occupied:
                    detail = (
                        f'pattern {candidate.priority} was abandoned because '
                        f'{candidate.path!r} is occupied by a non-moving file '
                        f'({occupied_by!r})'
                    )
                else:
                    broken = broken_relations(path, candidate)
                    if broken:
                        relation, other = broken[0]
                        detail = (
                            f'pattern {candidate.priority} was abandoned because '
                            f'{candidate.path!r} would break the {relation} '
                            f'relation with {other!r}'
                        )
                    else:
                        detail = (
                            f'pattern {candidate.priority} was abandoned because '
                            f'{candidate.path!r} would change the association hit set'
                        )
                blockers = (occupied_by,) if occupied_by else ()
            conflicts.append(
                RenameConflict(
                    'abandoned',
                    path,
                    candidate.path,
                    detail,
                    blocking_entities=tuple(sorted(_normalized(entities))),
                    blockers=blockers,
                )
            )

    after_items = {**fixed_candidates, **selected}
    associations_after = _association_set(after_items, focus=focus) if selected else set()
    if indexed_before and selected:
        after_keys = {(kind, left, right) for kind, left, right in associations_after}
        for kind, left, right in indexed_before:
            if _pair_preserved(kind, left, right, after_items):
                family = _canonical_kind(kind)
                if (family, left, right) not in after_keys:
                    associations_after |= {
                        (family, left, right),
                        (family, right, left),
                    }

    feasible = bool(source_paths) and all(path in selected for path in source_paths)
    if feasible and preserve_associations:
        feasible = relevant_before <= associations_after

    # Deduplicate conflicts while preserving a stable order.
    unique_conflicts = []
    seen = set()
    for conflict in conflicts:
        key = (
            conflict.kind,
            conflict.source,
            conflict.path,
            conflict.detail,
            conflict.blocking_entities,
            conflict.blockers,
        )
        if key in seen:
            continue
        seen.add(key)
        unique_conflicts.append(conflict)

    patterns_used = {path: cand.pattern for path, cand in selected.items()}

    notes = []
    if layout is not None and selected:
        from .models import FileAssociation

        nodes = set(source_paths) | set(fixed_entities)
        for edge in layout.session.query(FileAssociation).all():
            if edge.kind not in ('IntendedFor', 'InformedBy'):
                continue
            left = _relpath(edge.src, root)
            right = _relpath(edge.dst, root)
            if left not in nodes or right not in nodes:
                continue
            aux = left if edge.kind == 'IntendedFor' else right
            image = right if edge.kind == 'IntendedFor' else left
            suffix = str(Path(aux).name).split('_')[-1].split('.')[0]
            extension = ''.join(Path(aux).suffixes)
            if suffix in _AUX_SUFFIXES or extension in ('.bval', '.bvec'):
                continue
            notes.append(
                f'{aux!r} declares IntendedFor {image!r} in JSON content; '
                'the preview cannot rewrite that field, so verify it after moving'
            )
    unique_notes = list(dict.fromkeys(notes))

    return RenamePlan(
        selected,
        unique_conflicts,
        patterns_used,
        feasible,
        associations_before,
        associations_after,
        unique_notes,
        candidates,
    )


def preview_renames(*args, **kwargs):
    """Alias for :func:`plan_renames`; readable at call sites that only preview."""
    return plan_renames(*args, **kwargs)
