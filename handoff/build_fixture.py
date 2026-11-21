#!/usr/bin/env python3
"""Build a small, self-contained PyBIDS archive-handoff fixture.

The fixture models one archive that was indexed once and then continued to
live on disk:

    archive_root/          the only root; its description and the JSON config
                           at the *same passed path* are edited after indexing
    build_snapshot/        byte-for-byte copy of archive_root at build time
                           (this is the evidence the archive must have kept)
    archive_db/            index built before the edits, never rebuilt
    rebuilt_db/            a genuinely new index made after the edits, at a
                           different path, from the current files

Derivative roots deliberately disagree about how to declare themselves:

    derivatives/deriv_legacy/   legacy PipelineDescription only
    derivatives/deriv_modern/   GeneratedBy whose Name equals "deriv_legacy",
                                i.e. it overlaps both the sibling folder name
                                and the sibling's display name

and one more directory exists to test discovery:

    derivatives/no_description/ compliant filenames, no description -> skipped
    orphan/                     compliant filenames, no description -> indexed
                                as raw when validate=False

The script does not modify the PyBIDS source tree.  Run:

    .venv/bin/python handoff/build_fixture.py --out handoff/.work --reset
"""

from __future__ import annotations

import argparse
import json
import shutil
import warnings
from pathlib import Path

OLD_CONFIG = {
    "name": "legacy-entities-v1",
    "entities": [
        {"name": "subject", "pattern": "[/\\\\]+sub-([a-zA-Z0-9+]+)"},
        {"name": "sample", "pattern": "[_/\\\\]+sample-([a-zA-Z0-9+]+)"},
        {"name": "staining", "pattern": "[_/\\\\]+stain-([a-zA-Z0-9+]+)"},
        {"name": "proc", "pattern": "[_/\\\\]+proc-([a-zA-Z0-9+]+)"},
        {"name": "suffix", "pattern": "(?:^|[_/\\\\])([a-zA-Z0-9+]+)\\.[^/\\\\]+$"},
        {"name": "extension", "pattern": "(\\.[^/\\\\.]+(?:\\.[^/\\\\.]+)?)$"},
        {"name": "datatype", "pattern": "[/\\\\]+(anat|func|dwi|micr)[/\\\\]"},
    ],
}

# Same file path, same config "name", different rules.  A name comparison is
# therefore useless; the entity patterns stored in the DB are the evidence.
NEW_CONFIG_AT_SAME_PATH = {
    "name": "legacy-entities-v1",
    "entities": [
        {"name": "subject", "pattern": "[/\\\\]+sub-([a-zA-Z0-9+]+)"},
        {"name": "sample", "pattern": "[_/\\\\]+sample-([a-zA-Z0-9+]+)"},
        {"name": "stain", "pattern": "[_/\\\\]+stain-([0-9a-zA-Z+]+)"},
        {"name": "processing", "pattern": "[_/\\\\]+proc-([0-9a-zA-Z+]+)"},
        {"name": "description", "pattern": "[_/\\\\]+desc-([0-9a-zA-Z+]+)"},
        {"name": "suffix", "pattern": "(?:^|[_/\\\\])([a-zA-Z0-9+]+)\\.[^/\\\\]+$"},
        {"name": "extension", "pattern": "(\\.[^/\\\\.]+(?:\\.[^/\\\\.]+)?)$"},
        {"name": "datatype", "pattern": "[/\\\\]+(anat|func|dwi|micr)[/\\\\]"},
    ],
}

RAW_DESCRIPTION = {
    "Name": "Handoff fixture",
    "BIDSVersion": "1.11.2",
    "DatasetType": "raw",
}

RELOCATED_DESCRIPTION = {
    "Name": "Handoff fixture (post-archive relabel)",
    "BIDSVersion": "1.9.0",
    "DatasetType": "raw",
    "GeneratedBy": [{"Name": "archive-relabel", "Version": "9.9"}],
}

DERIV_LEGACY_BUILD = {
    "Name": "deriv_legacy",
    "BIDSVersion": "1.11.2",
    "DatasetType": "derivative",
    "PipelineDescription": {"Name": "deriv_legacy", "Version": "1.0"},
}

# Rewritten after indexing: the live self-description no longer matches either
# the child DB's stored root/config or the build snapshot.
DERIV_LEGACY_LIVE = {
    "Name": "deriv_legacy",
    "BIDSVersion": "1.11.2",
    "DatasetType": "derivative",
    "GeneratedBy": [{"Name": "post_archive_display", "Version": "2.0"}],
}

DERIV_MODERN = {
    "Name": "deriv_modern",
    "BIDSVersion": "1.11.2",
    "DatasetType": "derivative",
    "GeneratedBy": [{"Name": "deriv_legacy", "Version": "2.0"}],
}

RAW_FILES = [
    "sub-01/anat/sub-01_T1w.nii.gz",
    "sub-01/micr/sub-01_sample-A_stain-HE_TEM.png",
    "sub-01/micr/sub-01_sample-A_stain-HE_desc-foo_TEM.png",
    "sub-01/func/sub-01_task-rest_desc-raw_bold.nii.gz",
]

ORPHAN_FILES = [
    "sub-01/anat/sub-01_T1w.nii.gz",
    "sub-01/func/sub-01_task-rest_bold.nii.gz",
]

DERIV_FILES = {
    "deriv_legacy": "sub-01/func/sub-01_task-rest_desc-deriv_legacy_bold.nii.gz",
    "deriv_modern": "sub-01/func/sub-01_task-rest_desc-deriv_modern_bold.nii.gz",
}


def write_json(path: Path, payload: dict) -> None:
    """Write a JSON file with trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def touch(path: Path) -> None:
    """Create a small placeholder file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture\n")


def make_root(root: Path, *, raw_description: dict, deriv_legacy_description: dict) -> None:
    """Populate one root with the fixture content."""
    write_json(root / "dataset_description.json", raw_description)
    write_json(root / "configs" / "incoming_entities.json", OLD_CONFIG)

    for rel in RAW_FILES:
        touch(root / rel)

    write_json(root / "derivatives" / "deriv_legacy" / "dataset_description.json",
               deriv_legacy_description)
    touch(root / "derivatives" / "deriv_legacy" / DERIV_FILES["deriv_legacy"])

    write_json(root / "derivatives" / "deriv_modern" / "dataset_description.json",
               DERIV_MODERN)
    touch(root / "derivatives" / "deriv_modern" / DERIV_FILES["deriv_modern"])

    # Compliant filenames, no dataset_description.json.
    for rel in ORPHAN_FILES:
        touch(root / "orphan" / rel)
        touch(root / "derivatives" / "no_description" / rel)


def build(out: Path, reset: bool) -> None:
    """Create the snapshot, archive DB and rebuilt DB."""
    from bids.layout import BIDSLayout

    if out.exists():
        if not reset:
            raise SystemExit(f"{out} exists; pass --reset to rebuild")
        shutil.rmtree(out)
    out.mkdir(parents=True)

    root = out / "archive_root"
    make_root(root, raw_description=RAW_DESCRIPTION,
              deriv_legacy_description=DERIV_LEGACY_BUILD)

    # Build-time evidence: exact bytes the DB was built from.
    snapshot = out / "build_snapshot"
    shutil.copytree(root, snapshot)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        BIDSLayout(
            root,
            database_path=out / "archive_db",
            derivatives=True,
            config=[root / "configs" / "incoming_entities.json"],
            validate=False,
        )

    # Post-index edits: same root, same file names, same config path.
    write_json(root / "dataset_description.json", RELOCATED_DESCRIPTION)
    write_json(root / "configs" / "incoming_entities.json", NEW_CONFIG_AT_SAME_PATH)
    write_json(root / "derivatives" / "deriv_legacy" / "dataset_description.json",
               DERIV_LEGACY_LIVE)

    # A genuine rebuild at a different path, from the current files and the
    # current config at the same path.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        BIDSLayout(
            root,
            database_path=out / "rebuilt_db",
            derivatives=True,
            config=[root / "configs" / "incoming_entities.json"],
            validate=False,
        )

    print(f"built {out}")
    print(f"  archive db  : {out / 'archive_db'}")
    print(f"  rebuilt db  : {out / 'rebuilt_db'}")
    print(f"  live root   : {root}")
    print(f"  build bytes : {snapshot}")
    print("note: rebuilt_db is a genuine new index at a different path;")
    print("      it is not a repaired copy of archive_db.")


def main() -> None:
    """Parse arguments and build the fixture."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("handoff/.work"))
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    build(args.out, args.reset)


if __name__ == "__main__":
    main()
