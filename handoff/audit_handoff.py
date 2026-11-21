#!/usr/bin/env python3
"""Read-only audit of the PyBIDS archive-handoff fixture.

The audit answers four questions with stored data and actual queries:

1. What rules did each SQLite index actually store?  Read from
   layout_info / configs / entities / config_to_entity_map / tags, never from
   the JSON currently on disk.
2. What does reopening the archive DB report, and are a new `config` or
   `root` argument honoured?
3. What does a genuine rebuild at another path store and return?
4. Which statements about a legacy delivery (DB + current JSON only) are
   provable, and which need the build-time evidence?

It never calls reset_database on the supplied archive.  Reopens are performed
on a disposable copy of the database tree, so a missing/renamed derivative
cannot silently create a child DB inside the delivery.  One extra disposable
copy is deliberately reset to show what that flag does.

Run:
    .venv/bin/python handoff/build_fixture.py --out handoff/.work --reset
    .venv/bin/python handoff/audit_handoff.py --work handoff/.work \
        --report handoff/EVIDENCE.json --manifest handoff/BINDING_MANIFEST.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import warnings
from pathlib import Path
from typing import Any

from bids.layout import BIDSLayout

TARGET = "sub-01/micr/sub-01_sample-A_stain-HE_desc-foo_TEM.png"
TARGET_PLAIN = "sub-01/micr/sub-01_sample-A_stain-HE_TEM.png"
ORPHAN_FILE = "orphan/sub-01/anat/sub-01_T1w.nii.gz"
NO_DESC_DERIV_FILE = "derivatives/no_description/sub-01/anat/sub-01_T1w.nii.gz"


def sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_sha(path: Path) -> str | None:
    """Return a SHA-256 digest, or None if the file is absent."""
    return sha256(path) if path.exists() else None


def canon(obj: Any) -> str:
    """Return a stable SHA-256 over canonical JSON."""
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def tree_sha(root: Path) -> str:
    """Return an aggregate digest over every file in a tree."""
    entries = [
        f"{p.relative_to(root).as_posix()}:{sha256(p)}"
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]
    return canon(entries)


def inventory(root: Path) -> dict[str, dict]:
    """Return path -> size/digest for every file under root."""
    return {
        p.relative_to(root).as_posix(): {"sha256": sha256(p), "size": p.stat().st_size}
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def db_schema(db_dir: Path) -> dict[str, list[dict]]:
    """Return the SQLite table/column catalogue."""
    con = sqlite3.connect(f"file:{db_dir / 'layout_index.sqlite'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        tables = [
            r["name"]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        return {
            table: [dict(r) for r in con.execute(f'PRAGMA table_info("{table}")')]
            for table in tables
        }
    finally:
        con.close()


def read_db(db_dir: Path) -> dict:
    """Dump the persisted rules, tags and fingerprints of one index."""
    """Read only the SQLite file; no ORM, no filesystem scan."""
    db_file = db_dir / "layout_index.sqlite"
    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        info = dict(con.execute("SELECT * FROM layout_info").fetchone())
        configs = {
            r["name"]: {
                "default_path_patterns": json.loads(r["_default_path_patterns"])
                if r["_default_path_patterns"] not in (None, "null")
                else None
            }
            for r in con.execute("SELECT * FROM configs")
        }
        entities = {}
        for r in con.execute("SELECT * FROM entities ORDER BY name"):
            entities[r["name"]] = {
                "pattern": r["pattern"],
                "mandatory": bool(r["mandatory"]),
                "directory": r["directory"],
                "dtype": r["_dtype"],
            }
        mapping: dict[str, list[str]] = {}
        for r in con.execute(
            "SELECT * FROM config_to_entity_map ORDER BY config, entity"
        ):
            mapping.setdefault(r["config"], []).append(r["entity"])
        files = [r["path"] for r in con.execute("SELECT path FROM files ORDER BY path")]
        tags = [
            {
                "path": r["file_path"],
                "entity": r["entity_name"],
                "value": r["_value"],
                "metadata": bool(r["is_metadata"]),
            }
            for r in con.execute("SELECT * FROM tags ORDER BY file_path, entity_name")
        ]
    finally:
        con.close()

    rule_snapshot = {
        "configs": configs,
        "config_entities": mapping,
        "entities": entities,
    }
    return {
        "db_file": str(db_file),
        "db_sha256": sha256(db_file),
        "layout_info": info,
        "configs": configs,
        "config_entities": mapping,
        "entities": entities,
        "rule_sha256": canon(rule_snapshot),
        "n_files": len(files),
        "n_tags": len(tags),
        "files": files,
        "tags": tags,
        "schema": db_schema(db_dir),
    }


def tags_by_path(db: dict) -> dict[str, dict[str, str]]:
    """Group the tag dump by file path."""
    out: dict[str, dict[str, str]] = {}
    for t in db["tags"]:
        out.setdefault(t["path"], {})[t["entity"]] = t["value"]
    return out


def rel_tags(db: dict, db_root: str, path_suffix: str) -> dict[str, str]:
    """Return tags for one path suffix."""
    for full, tags in tags_by_path(db).items():
        if full.endswith(path_suffix):
            return tags
    return {}


def describe_root(root: Path) -> dict:
    """Read the live description and config of a root."""
    dd = root / "dataset_description.json"
    cfg = root / "configs" / "incoming_entities.json"
    return {
        "dataset_description": json.loads(dd.read_text()) if dd.exists() else None,
        "dataset_description_sha256": file_sha(dd),
        "config_at_passed_path": json.loads(cfg.read_text()) if cfg.exists() else None,
        "config_sha256": file_sha(cfg),
    }


def derivative_declarations(root: Path) -> dict[str, dict]:
    """Summarize how each derivative root declares its pipeline."""
    out = {}
    for dd in sorted((root / "derivatives").glob("*/dataset_description.json")):
        desc = json.loads(dd.read_text())
        generated = [x.get("Name") for x in desc.get("GeneratedBy", []) if x.get("Name")]
        pipeline = desc.get("PipelineDescription", {}).get("Name")
        out[dd.parent.name] = {
            "folder_name": dd.parent.name,
            "description_sha256": file_sha(dd),
            "DatasetType": desc.get("DatasetType"),
            "GeneratedBy_names": generated,
            "PipelineDescription_name": pipeline,
            "declared_pipeline_name": generated[0] if generated else pipeline,
            "declaration_field": "GeneratedBy" if generated else (
                "PipelineDescription" if pipeline else None
            ),
        }
    return out


def try_call(fn):
    """Run a query, returning the exception as evidence on failure."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - the failure mode is evidence
        return {"error": type(exc).__name__, "message": str(exc)}


def layout_report(layout: BIDSLayout) -> dict:
    """Collect the user-visible facts of an open layout."""
    def files(scope: str) -> list[str]:
        return sorted(f.relpath for f in layout.get(scope=scope))

    def query(**kwargs) -> Any:
        return try_call(lambda: sorted(f.relpath for f in layout.get(**kwargs)))

    deriv = {}
    for key, child in sorted(layout.derivatives.items()):
        deriv[key] = {
            "root": child.root,
            "folder_name": Path(child.root).name,
            "source_pipeline": child.source_pipeline,
            "live_description": child.description,
            "config_names": sorted(child.config),
            "self_entities": sorted(child.get_entities(scope="self")),
            "files": sorted(f.relpath for f in child.get(return_type="object")),
        }

    return {
        "reported_root": layout.root,
        "live_description": layout.description,
        "config_names": sorted(layout.config),
        "self_entities": sorted(layout.get_entities(scope="self")),
        "all_entities": sorted(layout.get_entities()),
        "raw_files": files("raw"),
        "derivative_files": files("derivatives"),
        "target_tags": rel_tags(
            {"tags": [
                {"path": f.path, "entity": k, "value": v}
                for f in layout.get(return_type="object")
                for k, v in f.get_entities().items()
            ]},
            layout.root,
            TARGET,
        ),
        "plain_target_tags": rel_tags(
            {"tags": [
                {"path": f.path, "entity": k, "value": v}
                for f in layout.get(return_type="object")
                for k, v in f.get_entities().items()
            ]},
            layout.root,
            TARGET_PLAIN,
        ),
        "parse_target": try_call(
            lambda: layout.parse_file_entities(TARGET, scope="self")
        ),
        "query_staining_HE": query(staining="HE"),
        "query_stain_HE": query(stain="HE"),
        "query_description_foo": query(description="foo"),
        "query_subject_T1w_raw": query(subject="01", suffix="T1w", scope="raw"),
        "query_all_subject_01_raw": query(subject="01", scope="raw"),
        "query_scope_deriv_legacy": query(scope="deriv_legacy"),
        "query_scope_deriv_modern": query(scope="deriv_modern"),
        "derivatives": deriv,
        "get_pipeline_deriv_legacy": try_call(
            lambda: Path(layout.derivatives.get_pipeline("deriv_legacy").root).name
        ),
        "get_pipeline_post_archive_display": try_call(
            lambda: Path(layout.derivatives.get_pipeline("post_archive_display").root).name
        ),
        "getitem_deriv_legacy": try_call(
            lambda: Path(layout.derivatives["deriv_legacy"].root).name
        ),
        "getitem_derivatives_deriv_legacy": try_call(
            lambda: Path(layout.derivatives["derivatives/deriv_legacy"].root).name
        ),
        "getitem_deriv_modern": try_call(
            lambda: Path(layout.derivatives["deriv_modern"].root).name
        ),
    }


def open_db_copy(source_db: Path, scratch: Path, label: str) -> tuple[BIDSLayout, Path]:
    """Open a disposable copy of an index tree."""
    target = scratch / f"db_copy_{label}"
    shutil.copytree(source_db, target)
    return BIDSLayout(database_path=target, derivatives=True), target


def config_rule_fingerprint(config_json: dict) -> dict:
    """Fingerprint a config exactly as the index would store it."""
    name = config_json["name"]
    entities = {}
    for ent in config_json.get("entities", []):
        entities[ent["name"]] = {
            "pattern": ent.get("pattern"),
            "mandatory": bool(ent.get("mandatory", False)),
            "directory": ent.get("directory"),
            "dtype": ent.get("dtype", "str"),
        }
    configs = {name: {"default_path_patterns": config_json.get("default_path_patterns")}}
    mapping = {name: sorted(entities)}
    snapshot = {"configs": configs, "config_entities": mapping, "entities": entities}
    return {"rule_sha256": canon(snapshot), "snapshot": snapshot}


def build_manifest(
    work: Path,
    archive: dict,
    rebuilt: dict,
    archive_children: dict[str, dict],
    rebuilt_children: dict[str, dict],
    build_declarations: dict,
    live_declarations: dict,
    build_fresh: dict,
    archive_reopen: dict,
    rebuilt_open: dict,
) -> dict:
    """Build the machine-readable root/index binding manifest."""
    root = work / "archive_root"
    snapshot = work / "build_snapshot"
    build_config = json.loads((snapshot / "configs" / "incoming_entities.json").read_text())
    expected = config_rule_fingerprint(build_config)

    roots = [
        {
            "root_id": "raw-primary",
            "parent_root_id": None,
            "kind": "raw",
            "logical_path": "archive_root",
            "build_absolute_path": archive["layout_info"]["root"],
            "current_path": str(root),
            "build_snapshot": {
                "path": str(snapshot),
                "tree_sha256": tree_sha(snapshot),
                "inventory": inventory(snapshot),
            },
            "description_at_build": {
                "path": "dataset_description.json",
                "sha256": file_sha(snapshot / "dataset_description.json"),
                "payload": json.loads((snapshot / "dataset_description.json").read_text()),
            },
            "description_now": {
                "path": "dataset_description.json",
                "sha256": file_sha(root / "dataset_description.json"),
                "payload": json.loads((root / "dataset_description.json").read_text()),
            },
            "config_argument": {
                "as_passed_at_build": str((root / "configs" / "incoming_entities.json").resolve()),
                "snapshot_path": "configs/incoming_entities.json",
                "sha256_at_build": file_sha(snapshot / "configs" / "incoming_entities.json"),
                "sha256_now": file_sha(root / "configs" / "incoming_entities.json"),
                "name": build_config["name"],
            },
            "index": {
                "database_dir": str(work / "archive_db"),
                "db_sha256": archive["db_sha256"],
                "layout_info": archive["layout_info"],
                "stored_configs": archive["configs"],
                "stored_config_entities": archive["config_entities"],
                "rule_sha256": archive["rule_sha256"],
                "n_files": archive["n_files"],
                "n_tags": archive["n_tags"],
            },
            "bindings": [
                {
                    "check": "layout_info.root == build_absolute_path",
                    "ok": archive["layout_info"]["root"] == str(root.resolve()),
                },
                {
                    "check": "layout_info._config == config argument path",
                    "ok": archive["layout_info"]["_config"]
                    == json.dumps([str((root / "configs" / "incoming_entities.json").resolve())]),
                },
                {
                    "check": "stored rules == build-time config content",
                    "ok": archive["rule_sha256"] == expected["rule_sha256"],
                    "stored": archive["rule_sha256"],
                    "expected": expected["rule_sha256"],
                },
                {
                    "check": "live config content == build-time config content",
                    "ok": file_sha(root / "configs" / "incoming_entities.json")
                    == file_sha(snapshot / "configs" / "incoming_entities.json"),
                },
                {
                    "check": "live root description == build-time root description",
                    "ok": file_sha(root / "dataset_description.json")
                    == file_sha(snapshot / "dataset_description.json"),
                },
            ],
            "queries_at_build_fresh": build_fresh,
            "queries_on_archive_db": archive_reopen,
        }
    ]

    for folder, decl in sorted(build_declarations.items()):
        child_archive = archive_children.get(folder)
        child_build_dir = snapshot / "derivatives" / folder
        child_live_dir = root / "derivatives" / folder
        if child_archive is None:
            continue
        roots.append(
            {
                "root_id": f"deriv-{folder}",
                "parent_root_id": "raw-primary",
                "kind": "derivative",
                "logical_path": f"derivatives/{folder}",
                "folder_name": folder,
                "build_absolute_path": child_archive["layout_info"]["root"],
                "current_path": str(child_live_dir),
                "declaration_at_build": decl,
                "declaration_now": live_declarations.get(folder),
                "description_at_build": {
                    "sha256": file_sha(child_build_dir / "dataset_description.json"),
                    "payload": json.loads(
                        (child_build_dir / "dataset_description.json").read_text()
                    ),
                },
                "description_now": {
                    "sha256": file_sha(child_live_dir / "dataset_description.json"),
                    "payload": json.loads(
                        (child_live_dir / "dataset_description.json").read_text()
                    ),
                },
                "index": {
                    "database_dir": str(work / "archive_db" / "derivatives" / folder),
                    "db_sha256": child_archive["db_sha256"],
                    "layout_info": child_archive["layout_info"],
                    "stored_configs": child_archive["configs"],
                    "stored_config_entities": child_archive["config_entities"],
                    "rule_sha256": child_archive["rule_sha256"],
                    "n_files": child_archive["n_files"],
                    "n_tags": child_archive["n_tags"],
                },
                "bindings": [
                    {
                        "check": "layout_info.root is under parent root/derivatives/<folder>",
                        "ok": child_archive["layout_info"]["root"]
                        == str((root / "derivatives" / folder).resolve()),
                    },
                    {
                        "check": "stored config is the default derivative stack",
                        "ok": sorted(child_archive["configs"]) == ["bids", "derivatives"],
                    },
                    {
                        "check": "child db is listed by parent folder, not display name",
                        "ok": (
                            work / "archive_db" / "derivatives" / folder / "layout_index.sqlite"
                        ).exists(),
                    },
                    {
                        "check": "build description still equals live description",
                        "ok": file_sha(child_build_dir / "dataset_description.json")
                        == file_sha(child_live_dir / "dataset_description.json"),
                    },
                ],
                "queries_on_archive_db": {
                    "source_pipeline_now": (
                        archive_reopen["derivatives"]
                        .get(f"derivatives/{folder}", {})
                        .get("source_pipeline")
                    ),
                    "files": (
                        archive_reopen["derivatives"]
                        .get(f"derivatives/{folder}", {})
                        .get("files")
                    ),
                },
                "queries_on_rebuilt_db": {
                    "source_pipeline_now": (
                        rebuilt_open["derivatives"]
                        .get(f"derivatives/{folder}", {})
                        .get("source_pipeline")
                    ),
                    "files": (
                        rebuilt_open["derivatives"]
                        .get(f"derivatives/{folder}", {})
                        .get("files")
                    ),
                },
            }
        )

    return {
        "manifest_schema": "pybids-archive-handoff/1",
        "roots": roots,
        "derivative_discovery_note": (
            "Child indexes are discovered by directory under derivatives/; the "
            "parent DB has no child registry. Bind by folder name plus child "
            "layout_info.root plus build-time description snapshot. Never bind by "
            "source_pipeline/display name alone."
        ),
        "not_stored_in_db": [
            "root or derivative dataset_description.json "
            "(Name, BIDSVersion, DatasetType, GeneratedBy, PipelineDescription)",
            "indexer options: validate, ignore, force_index, index_metadata, config_filename",
            "pybids / bidsschematools / sqlalchemy versions",
            "file content hashes, mtimes, sizes",
            "per-directory layout_config.json association and config precedence order",
            "derivative source_pipeline at build time",
            "build timestamp and command line",
        ],
        "same_config_argument_path": {
            "archive_db": archive["layout_info"]["_config"],
            "rebuilt_db": rebuilt["layout_info"]["_config"],
            "equal": archive["layout_info"]["_config"] == rebuilt["layout_info"]["_config"],
            "rules_equal": archive["rule_sha256"] == rebuilt["rule_sha256"],
        },
    }


def main() -> None:
    """Run the read-only audit."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work", type=Path, default=Path("handoff/.work"))
    ap.add_argument("--report", type=Path, default=Path("handoff/EVIDENCE.json"))
    ap.add_argument("--manifest", type=Path, default=Path("handoff/BINDING_MANIFEST.json"))
    args = ap.parse_args()

    work = args.work.resolve()
    root = work / "archive_root"
    snapshot = work / "build_snapshot"
    archive_db = work / "archive_db"
    rebuilt_db = work / "rebuilt_db"

    protected = {
        str(p.relative_to(work)): sha256(p)
        for p in sorted(work.rglob("*"))
        if p.is_file() and not p.is_relative_to(work / "_audit_scratch")
    }

    archive = read_db(archive_db)
    rebuilt = read_db(rebuilt_db)
    archive_children = {
        child.parent.name: read_db(child.parent)
        for child in sorted((archive_db / "derivatives").glob("*/layout_index.sqlite"))
    }
    rebuilt_children = {
        child.parent.name: read_db(child.parent)
        for child in sorted((rebuilt_db / "derivatives").glob("*/layout_index.sqlite"))
    }

    build_declarations = derivative_declarations(snapshot)
    live_declarations = derivative_declarations(root)

    scratch = work / "_audit_scratch"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir()

    # A fresh in-memory layout of the build snapshot: descriptions, names and
    # queries as they were when the DB was built.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        build_fresh_layout = BIDSLayout(
            snapshot,
            derivatives=True,
            config=[snapshot / "configs" / "incoming_entities.json"],
            validate=False,
        )
        build_fresh = layout_report(build_fresh_layout)

    archive_copy_layout, archive_copy = open_db_copy(archive_db, scratch, "archive")
    archive_reopen = layout_report(archive_copy_layout)
    archive_reopen_with_new_config = layout_report(
        BIDSLayout(
            database_path=archive_copy,
            derivatives=True,
            config=[root / "configs" / "incoming_entities.json"],
        )
    )
    archive_reopen_with_bogus_root = layout_report(
        BIDSLayout(database_path=archive_copy, derivatives=True, root=work / "does_not_exist")
    )
    archive_reopen_derivatives_false = layout_report(
        BIDSLayout(database_path=archive_copy, derivatives=False)
    )

    rebuilt_copy_layout, rebuilt_copy = open_db_copy(rebuilt_db, scratch, "rebuilt")
    rebuilt_open = layout_report(rebuilt_copy_layout)

    # Deliberate destructive control on a disposable copy.
    reset_root = scratch / "reset_root"
    shutil.copytree(root, reset_root)
    reset_db = scratch / "reset_db"
    shutil.copytree(archive_db, reset_db)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        reset_layout = BIDSLayout(
            reset_root,
            database_path=reset_db,
            derivatives=True,
            config=[reset_root / "configs" / "incoming_entities.json"],
            reset_database=True,
            validate=False,
        )
        reset_report = layout_report(reset_layout)
    reset_after = read_db(reset_db)

    def drift(path: str) -> dict:
        old = rel_tags(archive, archive["layout_info"]["root"], path)
        new = rel_tags(rebuilt, rebuilt["layout_info"]["root"], path)
        return {
            "path": path,
            "archive_db": old,
            "rebuilt_db": new,
            "only_in_archive_db": {k: v for k, v in old.items() if k not in new},
            "only_in_rebuilt_db": {k: v for k, v in new.items() if k not in old},
        }

    same_hits = {
        "query": {"subject": "01", "suffix": "T1w", "scope": "raw"},
        "archive_db": archive_reopen["query_subject_T1w_raw"],
        "rebuilt_db": rebuilt_open["query_subject_T1w_raw"],
        "hits_equal": archive_reopen["query_subject_T1w_raw"]
        == rebuilt_open["query_subject_T1w_raw"],
        "rule_sha256_archive": archive["rule_sha256"],
        "rule_sha256_rebuilt": rebuilt["rule_sha256"],
        "rules_equal": archive["rule_sha256"] == rebuilt["rule_sha256"],
        "config_names_equal": sorted(archive["configs"]) == sorted(rebuilt["configs"]),
        "config_argument_string_equal": archive["layout_info"]["_config"]
        == rebuilt["layout_info"]["_config"],
    }

    db_columns_flat = {
        f"{table}.{col['name']}" for table, cols in archive["schema"].items() for col in cols
    }
    description_fields = [
        "BIDSVersion",
        "DatasetType",
        "GeneratedBy",
        "PipelineDescription",
        "source_pipeline",
        "validate",
        "ignore",
        "force_index",
        "index_metadata",
        "config_filename",
        "pybids_version",
        "build_time",
        "sha256",
        "mtime",
    ]
    description_persistence = {
        "db_tables": sorted(archive["schema"]),
        "db_columns": sorted(db_columns_flat),
        "fields_absent_from_schema": [
            f for f in description_fields
            if not any(f.lower() in c.lower() for c in db_columns_flat)
        ],
        "layout_info_columns": [c["name"] for c in archive["schema"]["layout_info"]],
        "files_columns": [c["name"] for c in archive["schema"]["files"]],
        "tags_columns": [c["name"] for c in archive["schema"]["tags"]],
        "conclusion": (
            "The DB stores the parsed rule snapshot (entities/configs) and the tag "
            "assignments, but stores no dataset_description.json content, no "
            "source_pipeline, no indexer options, and no file content hash."
        ),
    }

    no_description_check = {
        "orphan_path": ORPHAN_FILE,
        "orphan_in_archive_db": any(p.endswith(ORPHAN_FILE) for p in archive["files"]),
        "orphan_in_rebuilt_db": any(p.endswith(ORPHAN_FILE) for p in rebuilt["files"]),
        "no_description_derivative_path": NO_DESC_DERIV_FILE,
        "no_description_derivative_in_any_db": any(
            p.endswith(NO_DESC_DERIV_FILE)
            for p in archive["files"]
            + rebuilt["files"]
            + [f for c in archive_children.values() for f in c["files"]]
            + [f for c in rebuilt_children.values() for f in c["files"]]
        ),
        "interpretation": (
            "A description-less but compliant directory outside derivatives/ is "
            "indexed as raw when validate=False. A description-less pipeline under "
            "derivatives/ is silently skipped because derivative discovery requires "
            "dataset_description.json. Neither fact is stored in the index."
        ),
    }

    protected_after = {
        str(p.relative_to(work)): sha256(p)
        for p in sorted(work.rglob("*"))
        if p.is_file() and not p.is_relative_to(scratch)
    }

    report = {
        "fixture": {
            "live_root": str(root),
            "build_snapshot": str(snapshot),
            "archive_db": str(archive_db),
            "rebuilt_db": str(rebuilt_db),
            "live_root_state": describe_root(root),
            "build_snapshot_state": describe_root(snapshot),
            "build_declarations": build_declarations,
            "live_declarations": live_declarations,
        },
        "databases_as_stored": {
            "archive_db": archive,
            "rebuilt_db": rebuilt,
            "archive_children": archive_children,
            "rebuilt_children": rebuilt_children,
        },
        "description_persistence": description_persistence,
        "fresh_layout_from_build_snapshot": build_fresh,
        "reopen_archive_db": archive_reopen,
        "reopen_archive_db_with_new_config_argument": archive_reopen_with_new_config,
        "reopen_archive_db_with_bogus_root_argument": archive_reopen_with_bogus_root,
        "reopen_archive_db_with_derivatives_false": archive_reopen_derivatives_false,
        "open_rebuilt_db": rebuilt_open,
        "same_path_entity_drift": [drift(TARGET), drift(TARGET_PLAIN)],
        "same_hits_different_rule_snapshot": same_hits,
        "no_description_directories": no_description_check,
        "reset_database_control_on_copy": {
            "root": str(reset_root),
            "config_argument": str(reset_root / "configs" / "incoming_entities.json"),
            "stored_configs_after_reset": reset_after["configs"],
            "stored_entities_after_reset": sorted(reset_after["entities"]),
            "query_stain_HE_after_reset": reset_report["query_stain_HE"],
            "query_staining_HE_after_reset": reset_report["query_staining_HE"],
            "interpretation": (
                "reset_database=True ignores the old index and rebuilds with the "
                "passed root/config, overwriting the copy. This is destructive and "
                "must never be used to 'test' a delivered archive."
            ),
        },
        "same_config_argument_path": {
            "archive_db": archive["layout_info"]["_config"],
            "rebuilt_db": rebuilt["layout_info"]["_config"],
            "equal": archive["layout_info"]["_config"] == rebuilt["layout_info"]["_config"],
            "rules_equal": archive["rule_sha256"] == rebuilt["rule_sha256"],
        },
        "databases_unchanged_by_audit": protected == protected_after,
        "changed_protected_files_during_audit": sorted(
            set(protected) ^ set(protected_after)
        )
        + sorted(
            k for k in protected
            if k in protected_after and protected[k] != protected_after[k]
        ),
    }

    manifest = build_manifest(
        work,
        archive,
        rebuilt,
        archive_children,
        rebuilt_children,
        build_declarations,
        live_declarations,
        build_fresh,
        archive_reopen,
        rebuilt_open,
    )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, default=str) + "\n")
    args.manifest.write_text(json.dumps(manifest, indent=2, default=str) + "\n")

    print(json.dumps({
        "report": str(args.report),
        "manifest": str(args.manifest),
        "archive_rule_sha256": archive["rule_sha256"],
        "rebuilt_rule_sha256": rebuilt["rule_sha256"],
        "same_path_drift": [
            {
                "path": d["path"],
                "only_in_archive_db": d["only_in_archive_db"],
                "only_in_rebuilt_db": d["only_in_rebuilt_db"],
            }
            for d in (drift(TARGET), drift(TARGET_PLAIN))
        ],
        "same_hits": {
            "hits_equal": same_hits["hits_equal"],
            "rules_equal": same_hits["rules_equal"],
            "config_names_equal": same_hits["config_names_equal"],
            "config_argument_string_equal": same_hits["config_argument_string_equal"],
        },
        "new_config_argument_ignored": archive_reopen["config_names"]
        == archive_reopen_with_new_config["config_names"],
        "bogus_root_argument_ignored": archive_reopen["reported_root"]
        == archive_reopen_with_bogus_root["reported_root"],
        "derivatives_false_ignored": bool(archive_reopen_derivatives_false["derivatives"]),
        "build_lookup_deriv_legacy": build_fresh["get_pipeline_deriv_legacy"],
        "live_lookup_deriv_legacy": archive_reopen["get_pipeline_deriv_legacy"],
        "no_description_derivative_in_any_db": no_description_check[
            "no_description_derivative_in_any_db"
        ],
        "databases_unchanged_by_audit": report["databases_unchanged_by_audit"],
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
