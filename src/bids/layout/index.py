"""File-indexing functionality."""

import json
import os
import re
import warnings
from collections import defaultdict
from functools import cache, partial

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
        self._bind(layout)

        all_bfs, all_tag_dicts = self._index_dir(self._layout._root, self._config)

        self.session.bulk_save_objects(all_bfs)
        self.session.bulk_insert_mappings(Tag, all_tag_dicts)
        self.session.commit()

        if self.index_metadata:
            self._index_metadata()

    def _bind(self, layout):
        """Attach the indexer to a layout and initialize path patterns."""
        self._layout = layout
        self._config = list(layout.config.values())
        # Keep a handle on the indexer that built this layout so its
        # configuration (filters, validation, indexing options) can be reused
        # by incremental metadata refreshes.
        layout._indexer = self

        ignore, force = validate_indexing_args(self.ignore, self.force_index, self._layout._root)

        # Do not accept string patterns
        self._include_patterns = [
            _regexfy(patt, root=self._layout._root) for patt in listify(force)
        ]
        self._exclude_patterns = [
            _regexfy(patt, root=self._layout._root) for patt in listify(ignore)
        ]

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
        # Process JSON files first if we're indexing metadata
        all_files = self._layout.get(**self._metadata_filters())

        all_entities, all_tags = self._load_entities_and_tags()

        load_json = self._make_json_loader()
        file_data, filenames = self._build_file_data(all_files, load_json)

        seen_assocs = set()
        create_association_pair = self._make_association_factory(seen_assocs)

        all_objs = []
        all_tag_dicts = []
        for bf in filenames:
            objs, tag_dicts = self._process_file_metadata(
                bf, file_data, all_entities, all_tags, create_association_pair
            )
            all_objs += objs
            all_tag_dicts += tag_dicts

        self.session.bulk_save_objects(all_objs)
        self.session.bulk_insert_mappings(Tag, all_tag_dicts)
        self.session.commit()

    def _metadata_filters(self):
        """Return the get() filters used to select files for metadata indexing."""
        filters = dict(self.filters)

        if filters:
            # ensure we are returning objects
            filters['return_type'] = 'object'

            if filters.get('extension'):
                filters['extension'] = listify(filters['extension'])
                # ensure json files are being indexed
                if '.json' not in filters['extension']:
                    filters['extension'].append('.json')

        return filters

    def _load_entities_and_tags(self):
        """Load all known Entities and the existing Tag lookup table.

        Returns
        -------
        all_entities : dict
            Mapping of entity name to Entity, including Entities created from
            metadata during previous indexing passes.
        all_tags : dict
            Mapping of ``'<file_path>_<entity_name>'`` to the stored string
            value, used to detect filename/sidecar conflicts and duplicates.

        """
        all_entities = {e.name: e for e in self.session.query(Entity).all()}

        # If key/value pairs in JSON files duplicate ones extracted from files,
        # we can end up with Tag collisions in the DB. To prevent this, we
        # store all filename/entity pairs and the value, and then check against
        # that before adding each new Tag.
        all_tags = {}
        for t in self.session.query(Tag).all():
            key = f'{t.file_path}_{t.entity_name}'
            all_tags[key] = str(t.value)

        return all_entities, all_tags

    @staticmethod
    def _load_json_file(path):
        with Path(path).fs.open(Path(path).path, 'r', encoding='utf-8') as handle:
            try:
                return json.load(handle)
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise OSError(
                    f'Error occurred while trying to decode JSON from file {path}'
                ) from e

    def _make_json_loader(self):
        """Memoizing JSON loader.

        Only JSON files that correspond to processed data files are read.
        """

        @cache
        def load_json(bf):
            return self._load_json_file(bf.path)

        return load_json

    def _build_file_data(self, all_files, load_json):
        """Organize indexed files into the inheritance lookup store.

        The store looks like: ``{extension/suffix: dirname: [(entities, payload, path)]}``.
        The payload is ``None`` for non-JSON files and a lazy JSON loader for
        sidecars.
        """
        # We build up a store of all file data as we iterate files.
        file_data = {}
        filenames = []
        for bf in all_files:
            if 'suffix' in bf.entities and 'extension' in bf.entities:
                # Filename-derived entities only: data files already carry
                # metadata Tags during a refresh, which must not leak into
                # inheritance subsetting.
                file_ents = bf.get_entities(metadata=False)
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

        return file_data, filenames

    @staticmethod
    def _make_association_factory(seen_assocs):
        """Build the deduplicating association-pair factory."""

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

        return create_association_pair

    def _process_file_metadata(
        self, bf, file_data, all_entities, all_tags, create_association_pair
    ):
        """Compute metadata Tags and FileAssociations for a single data file."""
        all_objs = []
        all_tag_dicts = []

        # Use filename-derived entities only. During a refresh the file already
        # carries metadata Tags from a previous index; including them would
        # pollute inheritance matching and the bold/dwi association lookups.
        file_ents = bf.get_entities(metadata=False)
        suffix = file_ents.pop('suffix', None)
        ext = file_ents.pop('extension', None)
        file_ent_keys = set(file_ents.keys())

        if suffix is None or ext is None:
            return all_objs, all_tag_dicts

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
            return all_objs, all_tag_dicts

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

        return all_objs, all_tag_dicts

    def refresh_metadata(self, sidecar_paths=None):
        """Incrementally refresh metadata after JSON sidecar changes.

        Only the metadata derived from sidecars is rebuilt; data files are not
        re-scanned and their filename entities are left untouched. The refresh
        handles edited sidecars, deleted sidecars (values revert to inherited
        parents), newly added sidecars, and explicit ``null`` overrides.

        Parameters
        ----------
        sidecar_paths : str or path-like or list, optional
            One or more JSON sidecar paths that were added, edited, or deleted.
            Paths may be absolute or relative to the dataset root. The set of
            data files that is actually recomputed is derived from every
            existing metadata dependency and any newly applicable sidecar match,
            not just the keys currently in effect. If ``None``, all currently
            indexed JSON sidecars are treated as changed (newly created sidecars
            must still be passed explicitly).

        Returns
        -------
        dict
            A summary with the refreshed ``sidecars``, the ``affected_data``
            files whose metadata was recomputed, and the ``added_sidecars``
            indexed for the first time.

        Raises
        ------
        OSError
            If a sidecar in scope cannot be parsed. No partially recomputed
            index is published in that case; the previous index remains in
            effect.

        """
        if not self.index_metadata:
            raise RuntimeError(
                'Metadata indexing is disabled for this BIDSLayoutIndexer '
                '(index_metadata=False); cannot refresh metadata.'
            )

        session = self.session
        root = Path(self._layout._root.path)

        normalized = self._normalize_sidecar_paths(sidecar_paths, root)
        explicit = sidecar_paths is not None
        if not normalized:
            return {'sidecars': [], 'affected_data': [], 'added_sidecars': []}

        # Everything below either commits as a single unit or rolls back, so a
        # failed refresh never leaves a mix of old and freshly computed rows.
        try:
            result = self._refresh_metadata(normalized, explicit)
        except Exception:
            session.rollback()
            raise

        session.expire_all()
        return result

    def _normalize_sidecar_paths(self, sidecar_paths, root):
        if sidecar_paths is None:
            return [bf.path for bf in self._layout.get(extension='.json')]

        normalized = []
        for raw in listify(sidecar_paths):
            path = Path(raw)
            if not path.is_absolute():
                path = root / path
            normalized.append(str(path.absolute()))
        return normalized

    @staticmethod
    def _is_relative_to(path, other):
        """Return True if ``path`` is ``other`` or located beneath it."""
        path = os.path.normpath(str(path))
        other = os.path.normpath(str(other))
        return path == other or path.startswith(other + os.sep)

    def _potential_data_matches(self, sidecar_bf, data_files):
        """Return indexed data files a sidecar can supply metadata to."""
        js_ents = sidecar_bf.get_entities(metadata=False)
        js_suffix = js_ents.pop('suffix', None)
        js_ents.pop('extension', None)
        js_dir = sidecar_bf._dirname
        js_keys = set(js_ents)

        matches = []
        for df in data_files:
            df_ents = df.get_entities(metadata=False)
            if df_ents.get('suffix') != js_suffix:
                continue
            if not self._is_relative_to(df._dirname, js_dir):
                continue
            if js_keys - set(df_ents):
                continue
            if all(df_ents[name] == js_ents[name] for name in js_keys):
                matches.append(df)
        return matches

    def _index_new_sidecar(self, path):
        """Create the BIDSFile record and filename Tags for a new JSON sidecar."""
        abs_fn = Path(path)
        if not self._validate_file(abs_fn):
            raise ValueError(
                f'New sidecar {path} is excluded from indexing by the current '
                'ignore/force_index/validate settings.'
            )

        config_entities = {}
        for config in self._config:
            config_entities.update(config.entities)

        bf, tag_dicts = self._index_file(abs_fn, config_entities)
        self.session.add(bf)
        self.session.bulk_insert_mappings(Tag, tag_dicts)
        return bf

    def _refresh_metadata(self, sidecar_paths, explicit):
        session = self.session

        indexed_json = {bf.path: bf for bf in self._layout.get(extension='.json')}
        all_data_files = [
            bf
            for bf in self._layout.get(**self._metadata_filters())
            if bf.entities.get('extension') != '.json'
        ]

        added_bfs = []
        edited_paths = []
        deleted_paths = []

        for path in sidecar_paths:
            if path in indexed_json:
                if Path(path).exists():
                    edited_paths.append(path)
                else:
                    deleted_paths.append(path)
            elif Path(path).exists():
                added_bfs.append(self._index_new_sidecar(path))
            elif explicit:
                raise FileNotFoundError(
                    f'Sidecar {path} is neither indexed nor present on disk.'
                )

        # Flush so newly added sidecars participate in entity matching and in
        # the metadata lookup store built below.
        session.flush()

        changed_sidecar_paths = set(edited_paths) | {bf.path for bf in added_bfs}
        # Parse every sidecar in scope up front. A malformed sidecar aborts the
        # refresh before any stale rows are removed, so the old index survives.
        for path in sorted(changed_sidecar_paths):
            self._load_json_file(path)

        affected = set()

        # Existing dependencies: data files currently linked to a changed or
        # deleted sidecar. This must run while the old associations still exist
        # (before the deleted sidecar's rows are removed), and catches keys that
        # were previously masked and may only become effective after the edit.
        touched = edited_paths + deleted_paths
        if touched:
            metadata_deps = (
                session.query(FileAssociation)
                .filter_by(kind='Metadata')
                .filter(FileAssociation.src.in_(touched) | FileAssociation.dst.in_(touched))
                .all()
            )
            sidecar_endpoints = set(indexed_json) | {bf.path for bf in added_bfs}
            for assoc in metadata_deps:
                for endpoint in (assoc.src, assoc.dst):
                    if endpoint not in sidecar_endpoints:
                        affected.add(endpoint)

        # Newly applicable matches (including brand-new sidecars), computed from
        # entity subsetting and directory inheritance rather than current tags.
        refreshed_json = [indexed_json[p] for p in edited_paths] + added_bfs
        for sidecar_bf in refreshed_json:
            for df in self._potential_data_matches(sidecar_bf, all_data_files):
                affected.add(df.path)

        # Auto links (IntendedFor/InformedBy) are owned by the non-image side of
        # the relationship; make sure those owners are recomputed too.
        self._expand_association_owners(affected)

        # Now that the affected scope is derived from the existing edges, remove
        # sidecars that disappeared from disk so they don't leak into the rebuilt
        # inheritance store.
        for path in deleted_paths:
            self._delete_sidecar_record(path)

        if not affected:
            session.commit()
            return {
                'sidecars': sorted(sidecar_paths),
                'affected_data': [],
                'added_sidecars': sorted(bf.path for bf in added_bfs),
            }

        self._clear_stale_metadata(affected, changed_sidecar_paths | set(deleted_paths))

        # Rebuild the lookup store from the current (post-edit) index, then
        # recompute only the affected data files.
        load_json = self._make_json_loader()
        current_files = self._layout.get(**self._metadata_filters())
        file_data, _ = self._build_file_data(current_files, load_json)

        all_entities, all_tags = self._load_entities_and_tags()

        existing_assoc_pks = {
            (a.src, a.dst, a.kind) for a in session.query(FileAssociation).all()
        }
        create_association_pair = self._make_association_factory(existing_assoc_pks)

        affected_bfs = (
            session.query(BIDSFile)
            .filter(BIDSFile.path.in_(affected), BIDSFile.is_dir == False)  # noqa: E712
            .all()
        )

        all_objs = []
        all_tag_dicts = []
        for bf in affected_bfs:
            objs, tag_dicts = self._process_file_metadata(
                bf, file_data, all_entities, all_tags, create_association_pair
            )
            all_objs += objs
            all_tag_dicts += tag_dicts

        session.bulk_save_objects(all_objs)
        session.bulk_insert_mappings(Tag, all_tag_dicts)
        session.commit()

        return {
            'sidecars': sorted(sidecar_paths),
            'affected_data': sorted(affected),
            'added_sidecars': sorted(bf.path for bf in added_bfs),
        }

    def _delete_sidecar_record(self, path):
        session = self.session
        session.query(Tag).filter_by(file_path=path).delete(synchronize_session=False)
        session.query(FileAssociation).filter(
            (FileAssociation.src == path) | (FileAssociation.dst == path)
        ).delete(synchronize_session=False)
        session.query(BIDSFile).filter_by(path=path).delete(synchronize_session=False)
        session.flush()

    def _expand_association_owners(self, affected):
        """Add the owning file of every association touching affected files."""
        session = self.session
        while True:
            rows = (
                session.query(FileAssociation)
                .filter(FileAssociation.src.in_(affected) | FileAssociation.dst.in_(affected))
                .all()
            )
            added = set()
            for row in rows:
                if row.kind == 'IntendedFor':
                    owner = row.src
                elif row.kind == 'InformedBy':
                    owner = row.dst
                else:
                    continue
                if owner not in affected:
                    added.add(owner)
            if not added:
                break
            affected |= added

    def _clear_stale_metadata(self, affected_paths, changed_sidecar_paths):
        session = self.session

        # Drop metadata-derived tags so vanished keys leave no stale query tags.
        session.query(Tag).filter(
            Tag.file_path.in_(affected_paths),
            Tag.is_metadata == True,  # noqa: E712
        ).delete(synchronize_session=False)

        # Metadata and intent associations are always owned by an affected data
        # file and are fully regenerated below.
        session.query(FileAssociation).filter(
            (FileAssociation.src.in_(affected_paths) | FileAssociation.dst.in_(affected_paths))
            & FileAssociation.kind.in_(['Metadata', 'IntendedFor', 'InformedBy'])
        ).delete(synchronize_session=False)

        # Inheritance edges incident to changed/added/deleted sidecars must be
        # rebuilt; edges between untouched sidecars stay valid and are preserved.
        session.query(FileAssociation).filter(
            (
                FileAssociation.src.in_(changed_sidecar_paths)
                | FileAssociation.dst.in_(changed_sidecar_paths)
            )
            & FileAssociation.kind.in_(['Child', 'Parent'])
        ).delete(synchronize_session=False)
        session.flush()


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
