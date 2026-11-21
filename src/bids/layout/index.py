"""File-indexing functionality."""

import json
import re
import warnings
from collections import defaultdict
from functools import cache, partial

import sqlalchemy as sa
from bids_validator import BIDSValidator
from upath import UPath as Path

from ..exceptions import BIDSConflictingValuesError
from ..utils import listify, make_bidsfile
from .models import BIDSFile, Config, Entity, FileAssociation, Tag, _create_tag_dict
from .validation import validate_indexing_args


def _regexfy(patt, root=None):
    if hasattr(patt, 'search'):
        return patt

    patt = Path(patt)

    if patt.is_absolute():
        patt = str(patt.relative_to(root or '/'))

    return re.compile(r'^/' + str(patt) + r'.*')


def _extract_entities(bidsfile, entities):
    match_vals = {}
    for e in entities.values():
        m = e.match_file(bidsfile)
        if m is None and e.mandatory:
            break
        if m is not None:
            match_vals[e.name] = (e, m)
    return match_vals


def _check_path_matches_patterns(path, patterns, root=None):
    """Check if the path matches at least one of the provided patterns."""
    if not patterns:
        return False

    path = path.absolute()
    if root is not None:
        if isinstance(path, Path):
            path = Path('/') / Path(path.path).relative_to(Path(root.path))
        else:
            path = Path('/') / path.relative_to(root)

    # Path now can be downcast to str
    path = str(path)

    for patt in patterns:
        if patt.search(path):
            return True
    return False


def _validate_path(path, incl_patt=None, excl_patt=None, root=None):
    if _check_path_matches_patterns(path, incl_patt, root=root):
        return True

    if _check_path_matches_patterns(path, excl_patt, root=root):
        return False


class BIDSLayoutIndexer:
    """Indexer class for BIDSLayout.

    Parameters
    ----------
    validate : bool, optional
        If True, all files are checked for BIDS compliance when first indexed,
        and non-compliant files are ignored. This provides a convenient way to
        restrict file indexing to only those files defined in the "core" BIDS
        spec, as setting ``validate=True`` will lead noncompliant files
        like ``sub-01/nonbidsfile.txt`` to be ignored.
    ignore : str or SRE_Pattern or list
        Path(s) to exclude from indexing. Each path is either a string or a
        SRE_Pattern object (i.e., compiled regular expression). If a string is
        passed, it must be either an absolute path, or be relative to the BIDS
        project root. If an SRE_Pattern is passed, the contained regular
        expression will be matched against the full (absolute) path of all
        files and directories. By default, indexing ignores all files in
        'code/', 'stimuli/', 'sourcedata/', 'models/', and any hidden
        files/dirs beginning with '.' at root level.
    force_index : str or SRE_Pattern or list
        Path(s) to forcibly index in the BIDSLayout, even if they would
        otherwise fail validation. See the documentation for the ignore
        argument for input format details. Note that paths in force_index takes
        precedence over those in ignore (i.e., if a file matches both ignore
        and force_index, it *will* be indexed).
        Note: NEVER include 'derivatives' here; use the derivatives argument
        (or :obj:`bids.layout.BIDSLayout.add_derivatives`) for that.
    index_metadata : bool
        If True, all metadata files are indexed. If False, metadata will not be
        available (but indexing will be faster).
    config_filename : str
        Optional name of filename within directories
        that contains configuration information.
    **filters
        keyword arguments passed to the .get() method of a
        :obj:`bids.layout.BIDSLayout` object. These keyword arguments define
        what files get selected for metadata indexing.

    """

    def __init__(
        self,
        validate=False,
        ignore=None,
        force_index=None,
        index_metadata=True,
        config_filename='layout_config.json',
        **filters,
    ):
        self.ignore = ignore
        self.force_index = force_index
        self.index_metadata = index_metadata
        self.config_filename = config_filename
        self.filters = filters
        self.validator = None

        if validate:
            self.validator = BIDSValidator(index_associated=True)

        # Layout-dependent attributes to be set in __call__()
        self._layout = None
        self._config = None
        self._include_patterns = None
        self._exclude_patterns = None

    def __call__(self, layout):  # noqa: D102
        self._layout = layout
        self._config = list(layout.config.values())

        ignore, force = validate_indexing_args(self.ignore, self.force_index, self._layout._root)

        # Do not accept string patterns
        self._include_patterns = [
            _regexfy(patt, root=self._layout._root) for patt in listify(force)
        ]
        self._exclude_patterns = [
            _regexfy(patt, root=self._layout._root) for patt in listify(ignore)
        ]

        all_bfs, all_tag_dicts = self._index_dir(self._layout._root, self._config)

        self.session.bulk_save_objects(all_bfs)
        self.session.bulk_insert_mappings(Tag, all_tag_dicts)
        self.session.commit()

        if self.index_metadata:
            self._index_metadata()

    def refresh(self, layout, old_paths=None, new_paths=None):
        """Incrementally refresh the index for renamed or moved subtrees.

        Only the affected subtrees are rescanned. Existing index records for
        ``old_paths`` are removed and replaced with records built by scanning
        ``new_paths``; metadata tags and derived file associations are rebuilt
        only for the affected region. The whole refresh runs in a single
        transaction, so any failure (e.g. an undecodable JSON sidecar) rolls
        the index back to its previous state without a full rebuild.

        Parameters
        ----------
        layout : :obj:`bids.layout.BIDSLayout`
            The layout whose index should be refreshed.
        old_paths : str or list or None
            One or more paths (relative to the layout root or absolute) that
            were the previous locations of the renamed subtrees/files.
        new_paths : str or list or None
            One or more paths (relative to the layout root or absolute) that
            are the current locations of the renamed subtrees/files.

        Returns
        -------
        dict
            Summary with the affected absolute path prefixes and the number of
            file records that were removed and added.

        """
        self._layout = layout
        self._config = list(layout.config.values())

        ignore, force = validate_indexing_args(self.ignore, self.force_index, self._layout._root)
        self._include_patterns = [
            _regexfy(patt, root=self._layout._root) for patt in listify(force)
        ]
        self._exclude_patterns = [
            _regexfy(patt, root=self._layout._root) for patt in listify(ignore)
        ]

        old_prefixes = self._normalize_refresh_paths(old_paths)
        new_prefixes = self._normalize_refresh_paths(new_paths)
        affected_prefixes = old_prefixes + new_prefixes

        try:
            # Clear every currently-indexed record below either scope. On the
            # first refresh the new subtree has no rows yet (a no-op); on a
            # repeated refresh this removes rows inserted previously, keeping
            # re-refreshes idempotent instead of colliding on primary keys.
            removed = self._remove_indexed_subtrees(affected_prefixes)
            added_bfs, added_tags = self._index_subtrees(new_prefixes)

            self.session.bulk_save_objects(added_bfs)
            self.session.bulk_insert_mappings(Tag, added_tags)
            # Flush so the new file rows are visible to metadata indexing and
            # any constraint violation aborts before derived records are built.
            self.session.flush()

            if self.index_metadata:
                self._refresh_metadata(affected_prefixes)

            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        finally:
            self.session.expire_all()

        return {
            'affected_paths': affected_prefixes,
            'removed_files': removed,
            'added_files': len(added_bfs),
        }

    def _normalize_refresh_paths(self, paths):
        """Turn user-supplied refresh scopes into absolute POSIX strings."""
        root = Path(self._layout._root.path)
        normalized = []
        for path in listify(paths) or []:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = root / candidate
            normalized.append(candidate.absolute().as_posix())
        return normalized

    @staticmethod
    def _path_is_under(path, prefixes):
        """Return True if ``path`` equals or is nested below any prefix."""
        for prefix in prefixes:
            if path == prefix or path.startswith(prefix.rstrip('/') + '/'):
                return True
        return False

    def _remove_indexed_subtrees(self, prefixes):
        """Delete files, tags and associations covered by ``prefixes``."""
        if not prefixes:
            return 0

        rows = self.session.query(BIDSFile.path).all()
        stale_paths = [row[0] for row in rows if self._path_is_under(row[0], prefixes)]
        if not stale_paths:
            return 0
        stale_set = set(stale_paths)

        self.session.query(Tag).filter(Tag.file_path.in_(stale_paths)).delete(
            synchronize_session=False
        )

        # Derived inheritance associations are fully rebuilt downstream: drop
        # every row touching a path that moves.
        self.session.query(FileAssociation).filter(
            FileAssociation.kind.in_(('Metadata', 'Child', 'Parent')),
            sa.or_(
                FileAssociation.src.in_(stale_paths),
                FileAssociation.dst.in_(stale_paths),
            ),
        ).delete(synchronize_session=False)

        # IntendedFor/InformedBy encode user-authored declarations: drop a row
        # only when its declaring endpoint moves with the subtree. Rows whose
        # declaring file stays are preserved, even if the other endpoint no
        # longer exists (i.e. a dangling declaration), rather than silently
        # repointed at a new target.
        declaration_rows = (
            self.session.query(FileAssociation)
            .filter(
                FileAssociation.kind.in_(('IntendedFor', 'InformedBy')),
                sa.or_(
                    FileAssociation.src.in_(stale_paths),
                    FileAssociation.dst.in_(stale_paths),
                ),
            )
            .all()
        )
        for assoc in declaration_rows:
            declaring = assoc.src if assoc.kind == 'IntendedFor' else assoc.dst
            if declaring in stale_set:
                self.session.delete(assoc)

        self.session.query(BIDSFile).filter(BIDSFile.path.in_(stale_paths)).delete(
            synchronize_session=False
        )
        self.session.flush()
        return len(stale_paths)

    def _index_subtrees(self, prefixes):
        """Scan the given subtrees and build file records and filename tags."""
        all_bfs = []
        all_tag_dicts = []
        for prefix in prefixes:
            subtree = Path(prefix)
            if subtree.is_dir():
                bfs, tag_dicts = self._index_dir(subtree, self._config)
                all_bfs += bfs
                all_tag_dicts += tag_dicts
            elif subtree.is_file():
                if self._validate_file(subtree):
                    bf, tag_dicts = self._index_file(
                        subtree, self._collect_config_entities(subtree)
                    )
                    all_bfs.append(bf)
                    all_tag_dicts += tag_dicts
            # A missing scope is not an error: nothing lives there anymore.
        return all_bfs, all_tag_dicts

    def _collect_config_entities(self, path):
        """Collect filename entities, honoring config files above ``path``."""
        root_path = Path(self._layout._root.path)
        config_entities = {}
        for cfg in self._config:
            config_entities.update(cfg.entities)

        # Mirror _index_dir: a layout_config.json in an ancestor directory may
        # contribute additional entities.
        current = path if path.is_dir() else path.parent
        config_locations = []
        while True:
            config_locations.append(current)
            parent = current.parent
            if parent == current or current == root_path:
                break
            current = parent
        for directory in reversed(config_locations):
            config_file = directory / self.config_filename
            if config_file.exists():
                cfg = Config.load(config_file, session=self.session)
                config_entities.update(cfg.entities)
        return config_entities

    def _refresh_metadata(self, affected_prefixes):
        """Rebuild metadata tags and derived associations for affected files.

        Stale records were already removed together with the affected file
        rows in :meth:`_remove_indexed_subtrees`; declaration rows whose
        declaring file stays outside the scopes (dangling declarations) were
        intentionally preserved.
        """
        self._rebuild_metadata_region(affected_prefixes)

    @property
    def session(self):  # noqa: D102
        return self._layout.connection_manager.session

    def _validate_file(self, f):
        matched_patt = _validate_path(
            f,
            incl_patt=self._include_patterns,
            excl_patt=self._exclude_patterns,
            root=self._layout._root,
        )

        if matched_patt is not None:
            return matched_patt

        if self.validator is None:
            return True

        # BIDS validator expects absolute paths, but really these are relative
        # to the BIDS project root.
        to_check = Path(f.path).relative_to(
            Path(self._layout._root.path)
        )  # use .path then Path() to drop the uri prefix
        # Pretend the path is an absolute path
        to_check = Path('/') / to_check
        # bids-validator works with posix paths only
        to_check = to_check.as_posix()
        return self.validator.is_bids(to_check)

    def _index_dir(self, path, config, force=None):
        root_path = Path(self._layout._root.path)  # drops the uri prefix if it is there
        abs_path = root_path / Path(path.path).relative_to(root_path)

        # Derivative directories must always be added separately
        if root_path.joinpath('derivatives') in abs_path.parents:
            return [], []

        config = list(config)  # Shallow copy

        # Check for additional config file in directory
        layout_file = self.config_filename
        config_file = abs_path / layout_file
        if config_file.exists():
            cfg = Config.load(config_file, session=self.session)
            config.append(cfg)

        # Track which entities are valid in filenames for this directory
        config_entities = {}
        for c in config:
            config_entities.update(c.entities)

        # Get lists of 1st-level subdirectories and files in the path directory
        try:
            _, dirnames, filenames = next(path.fs.walk(path.path))
        except StopIteration:
            # It's unclear when this gets hit, but tests will occasionally fail here.
            # Hopefully this only occurs when we enter an empty directory,
            # but even if not, there's nothing else to do in this case.
            return [], []

        # Symbolic links are returned as filenames even if they point to directories
        # Temporary list to store symbolic links that need to be removed from filenames
        links_to_dirs = []

        # Find directories in filenames
        for possible_link in filenames:
            full_path = path / possible_link
            if full_path.is_dir():
                links_to_dirs.append(possible_link)

        dirnames.extend(links_to_dirs)

        for link in links_to_dirs:
            filenames.remove(link)

        # Move any directories suffixed with .zarr to filenames
        zarrnames = [entry for entry in dirnames if Path(entry).suffix == '.zarr']
        dirnames = [entry for entry in dirnames if Path(entry).suffix != '.zarr']
        filenames.extend(zarrnames)

        # If layout configuration file exists, delete it
        if self.config_filename in filenames:
            filenames.remove(self.config_filename)

        all_bfs = []
        all_tag_dicts = []
        for f in filenames:
            abs_fn = path / f
            # Skip files that fail validation, unless forcibly indexing
            if force or self._validate_file(abs_fn):
                bf, tag_dicts = self._index_file(abs_fn, config_entities)
                all_tag_dicts += tag_dicts
                all_bfs.append(bf)

        # Recursively index subdirectories
        for d in dirnames:
            d = path / d
            force = _validate_path(
                d,
                incl_patt=self._include_patterns,
                excl_patt=self._exclude_patterns,
                root=self._layout._root,
            )
            if force is not False:
                dir_bfs, dir_tag_dicts = self._index_dir(d, config, force=force)
                all_bfs += dir_bfs
                all_tag_dicts += dir_tag_dicts

        return all_bfs, all_tag_dicts

    def _index_file(self, abs_fn, entities):
        """Create DB record for file and its tags."""
        bf = make_bidsfile(abs_fn)

        # Entity patterns must not match directories above the dataset root.
        root = Path(self._layout._root.path)
        relative_fn = Path('/') / Path(abs_fn.path).relative_to(root)
        match_vals = _extract_entities(make_bidsfile(relative_fn), entities)

        # Create Entity <=> BIDSFile mappings
        tag_dicts = [
            _create_tag_dict(bf, ent, val, ent._dtype) for ent, val in match_vals.values()
        ]

        return bf, tag_dicts

    def _index_metadata(self):
        """Index metadata for all files in the BIDS dataset."""
        filters = self.filters

        if filters:
            # ensure we are returning objects
            filters['return_type'] = 'object'

            if filters.get('extension'):
                filters['extension'] = listify(filters['extension'])
                # ensure json files are being indexed
                if '.json' not in filters['extension']:
                    filters['extension'].append('.json')

        # Process JSON files first if we're indexing metadata
        all_files = self._layout.get(**filters)
        self._process_metadata(all_files)

    def _rebuild_metadata_region(self, affected_prefixes):
        """Rebuild metadata for affected files against the whole current index.

        All indexed files are loaded as inheritance/association candidates, but
        metadata records are rebuilt only for files below ``affected_prefixes``.
        """
        filters = self.filters

        if filters:
            filters = filters.copy()
            filters['return_type'] = 'object'

            if filters.get('extension'):
                filters['extension'] = listify(filters['extension'])
                if '.json' not in filters['extension']:
                    filters['extension'].append('.json')

        all_files = self._layout.get(**filters)
        process_paths = {
            bf.path for bf in all_files if self._path_is_under(bf.path, affected_prefixes)
        }
        self._process_metadata(all_files, process_paths=process_paths, commit=False)

    def _process_metadata(self, all_files, process_paths=None, commit=True):
        """Build metadata tags and derived associations for the given files.

        Parameters
        ----------
        all_files : list
            All indexed :obj:`bids.layout.BIDSFile` objects, used as candidates
            for JSON inheritance and file associations.
        process_paths : set or None
            If given, only these file paths receive new metadata records. Other
            files still participate as inheritance/association candidates.
        commit : bool
            If True, commit the session when done. Set to False to let an outer
            transaction control the commit boundary.

        """
        # Track ALL entities we've seen in file names or metadatas
        all_entities = {}
        for c in self._config:
            all_entities.update(c.entities)
        # Metadata-only entities may already exist (e.g. during a refresh);
        # reuse them instead of creating conflicting primary keys.
        for existing_entity in self.session.query(Entity).all():
            all_entities.setdefault(existing_entity.name, existing_entity)

        # If key/value pairs in JSON files duplicate ones extracted from files,
        # we can end up with Tag collisions in the DB. To prevent this, we
        # store all filename/entity pairs and the value, and then check against
        # that before adding each new Tag.
        all_tags = {}
        # Filename-only entities per file path. Inheritance matching and run
        # linking must never see metadata tags, which on a scoped refresh can
        # still be attached from a previous indexing pass. Building this from
        # the single Tag query below avoids an extra query per file.
        filename_entities = defaultdict(dict)
        for t in self.session.query(Tag).all():
            key = f'{t.file_path}_{t.entity_name}'
            all_tags[key] = str(t.value)
            if not t.is_metadata:
                filename_entities[t.file_path][t.entity_name] = t.value

        # We build up a store of all file data as we iterate files. It looks
        # like: { extension/suffix: dirname: [(entities, payload)]}}.
        # The payload is left empty for non-JSON files.
        file_data = {}

        # Memoizing JSON loader
        # Use as a function to allow lazy loading so only read JSON files
        # if they correspond to data files that are indexed
        @cache
        def load_json(path):
            with Path(path).fs.open(Path(path).path, 'r', encoding='utf-8') as handle:
                try:
                    return json.load(handle)
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    raise OSError(
                        f'Error occurred while trying to decode JSON from file {path}'
                    ) from e

        filenames = []
        for bf in all_files:
            # Inheritance and run-linking must consider filename entities only.
            # Metadata tags are rebuilt here and may still be attached to the
            # ORM objects from a previous indexing pass; including them would
            # over-constrain candidate lookups (e.g. linking events to a bold
            # run) on a scoped refresh.
            bf_entities = filename_entities[bf.path]
            if 'suffix' in bf_entities and 'extension' in bf_entities:
                file_ents = bf_entities.copy()
                suffix = file_ents.pop('suffix')
                ext = file_ents.pop('extension')
                key = f'{ext}/{suffix}'
                if key not in file_data:
                    file_data[key] = defaultdict(list)

                payload = None
                if ext == '.json':
                    payload = partial(load_json, bf)
                else:
                    filenames.append(bf)

                to_store = (file_ents, payload, bf.path)
                file_data[key][bf._dirname].append(to_store)

        if process_paths is not None:
            filenames = [bf for bf in filenames if bf.path in process_paths]

        # To avoid integrity errors, track primary keys we've seen. Seed with
        # associations already in the DB so a scoped rebuild never re-inserts
        # records that belong to untouched files.
        seen_assocs = {
            '#'.join([assoc.src, assoc.dst, assoc.kind])
            for assoc in self.session.query(FileAssociation).all()
        }

        def create_association_pair(src, dst, kind, kind2=None):
            objs = []
            kind2 = kind2 or kind
            pk1 = '#'.join([src, dst, kind])
            if pk1 not in seen_assocs:
                objs.append(FileAssociation(src=src, dst=dst, kind=kind))
                seen_assocs.add(pk1)
            pk2 = '#'.join([dst, src, kind2])
            if pk2 not in seen_assocs:
                objs.append(FileAssociation(src=dst, dst=src, kind=kind2))
                seen_assocs.add(pk2)

            return objs

        all_objs = []
        all_tag_dicts = []
        for bf in filenames:
            # Keep using filename-only entities so run-linking queries and
            # inheritance matching ignore rebuilt/stale metadata tags.
            file_ents = filename_entities[bf.path].copy()
            suffix = file_ents.pop('suffix', None)
            ext = file_ents.pop('extension', None)
            file_ent_keys = set(file_ents.keys())

            if suffix is None or ext is None:
                continue

            # Extract metadata associated with the file. The idea is
            # that we loop over parent directories, and if we find
            # payloads in the file_data store (indexing by directory
            # and current file suffix), we check to see if the
            # candidate JS file's entities are entirely consumed by
            # the current file. If so, it's a valid candidate, and we
            # add the payload to the stack. Finally, we invert the
            # stack and merge the payloads in order.
            ext_key = f'{ext}/{suffix}'
            json_key = f'.json/{suffix}'
            dirname = bf._dirname

            payloads = []
            ancestors = []

            while True:
                # Get JSON payloads
                json_data = file_data.get(json_key, {}).get(dirname, [])
                for js_ents, js_md, js_path in json_data:
                    js_keys = set(js_ents.keys())
                    if js_keys - file_ent_keys:
                        continue
                    matches = [js_ents[name] == file_ents[name] for name in js_keys]
                    if all(matches):
                        payloads.append((js_md, js_path))

                # Get all files this file inherits from
                candidates = file_data.get(ext_key, {}).get(dirname, [])
                for ents, _, path in candidates:
                    keys = set(ents.keys())
                    if keys - file_ent_keys:
                        continue
                    matches = [ents[name] == file_ents[name] for name in keys]
                    if all(matches):
                        ancestors.append(path)

                parent = dirname.parent

                if parent == dirname:
                    break
                dirname = parent

            if not payloads:
                continue

            # Missing data files can tolerate absent metadata files,
            # but we will try to load it anyway
            virtual_datafile = not bf._path.exists()

            # Create DB records for metadata associations
            js_file = payloads[0][1]
            all_objs += create_association_pair(js_file, bf.path, 'Metadata')

            # Consolidate metadata by looping over inherited JSON files
            file_md = {}
            for pl, js_file in payloads[::-1]:  # noqa: B007
                try:
                    file_md.update(pl())
                except FileNotFoundError:
                    if not virtual_datafile:
                        raise
                    # Drop metadata if any files are missing
                    # Otherwise missing overrides could give misleading metadata
                    file_md = {}
                    break

            # Create FileAssociation records for JSON inheritance
            n_pl = len(payloads)
            for i, (pl, js_file) in enumerate(payloads):  # noqa: B007
                if (i + 1) < n_pl:
                    other = payloads[i + 1][1]
                    all_objs += create_association_pair(js_file, other, 'Child', 'Parent')

            # Inheritance for current file
            n_pl = len(ancestors)
            for i, src in enumerate(ancestors):
                if (i + 1) < n_pl:
                    dst = ancestors[i + 1]
                    all_objs += create_association_pair(src, dst, 'Child', 'Parent')

            # Files with IntendedFor field always get mapped to targets
            intended = listify(file_md.get('IntendedFor', []))
            for target in intended:
                # IntendedFor paths should use BIDS URIs
                # Previously (now deprecated), paths may be relative to participant sub-dir
                target = _resolve_intent(target, self._layout._root, bf.entities['subject'])
                if target is None:
                    warnings.warn(f'Skipping association for {target}', stacklevel=2)
                    continue
                all_objs += create_association_pair(
                    bf.path, str(target), 'IntendedFor', 'InformedBy'
                )

            # Link files to BOLD runs
            if suffix in ['physio', 'stim', 'events', 'sbref']:
                images = self._layout.get(
                    extension=['.nii', '.nii.gz'],
                    suffix='bold',
                    return_type='filename',
                    **file_ents,
                )
                for img in images:
                    all_objs += create_association_pair(bf.path, img, 'IntendedFor', 'InformedBy')

            # Link files to DWI runs
            if suffix == 'sbref' or ext in ['bvec', 'bval']:
                images = self._layout.get(
                    extension=['.nii', '.nii.gz'],
                    suffix='dwi',
                    return_type='filename',
                    **file_ents,
                )
                for img in images:
                    all_objs += create_association_pair(bf.path, img, 'IntendedFor', 'InformedBy')

            # Create Tag <-> Entity mappings, and any newly discovered Entities
            for md_key, md_val in file_md.items():
                # Treat null entries (deserialized to None) as absent
                # Alternative is to cast None to null in layout.models._create_tag_dict
                if md_val is None:
                    continue
                tag_string = f'{bf.path}_{md_key}'
                # Skip pairs that were already found in the filenames
                if tag_string in all_tags:
                    file_val = all_tags[tag_string]
                    if str(md_val) != file_val:
                        msg = (
                            "Conflicting values found for entity '{}' in "
                            "filename {} (value='{}') versus its JSON sidecar "
                            "(value='{}'). Please reconcile this discrepancy."
                        )
                        raise BIDSConflictingValuesError(
                            msg.format(md_key, bf.path, file_val, md_val)
                        )
                    continue
                if md_key not in all_entities:
                    all_entities[md_key] = Entity(md_key)
                    self.session.add(all_entities[md_key])
                tag = _create_tag_dict(bf, all_entities[md_key], md_val, is_metadata=True)
                all_tag_dicts.append(tag)

        self.session.bulk_save_objects(all_objs)
        self.session.bulk_insert_mappings(Tag, all_tag_dicts)
        if commit:
            self.session.commit()
        else:
            self.session.flush()


def _resolve_intent(intent: str, root: Path, subject: str) -> str | None:
    """Resolve IntendedFor paths as absolute paths.

    Path may be either a BIDS URI (prefix bids:: or bids:<name>:) or
    a relative path from subject directory.

    Examples
    --------
    >>> _resolve_intent('bids::sub-01/anat/sub-01_T1w.nii.gz', Path(), '01')
    'sub-01/anat/sub-01_T1w.nii.gz'
    >>> _resolve_intent('anat/sub-01_T1w.nii.gz', Path(), '01')
    'sub-01/anat/sub-01_T1w.nii.gz'
    >>> _resolve_intent('bids:named_dataset:sub-01/anat/sub-01_T1w.nii.gz', Path(), '01')

    """
    if not intent.startswith('bids:'):
        return str(root / f'sub-{subject}' / intent)
    if intent.startswith('bids::'):
        return str(root / intent[6:])
