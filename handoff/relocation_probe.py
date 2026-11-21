#!/usr/bin/env python3
"""Show why a PyBIDS index cannot simply be copied to another machine.

`files.path`, `tags.file_path` and `associations.src/dst` store absolute
paths captured at build time.  `layout_info.root` is also absolute.  Copying
the DB changes neither, so on the receiving machine the ORM reports the old
root while every row still points at the old disk path.

The probe works only on copies inside --scratch.  It also tests whether a
manual rewrite of layout_info.root is sufficient (it is not: file rows stay
absolute), and whether a missing root description / derivative description is
tolerated on reopen.

    .venv/bin/python handoff/relocation_probe.py --work handoff/.work \
        --report handoff/RELOCATION.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import warnings
from pathlib import Path

from bids.layout import BIDSLayout


def rewrite_root(db_dir: Path, new_root: Path) -> None:
    """Edit layout_info.root in a disposable DB copy."""
    con = sqlite3.connect(db_dir / "layout_index.sqlite")
    con.execute("UPDATE layout_info SET root = ?", (str(new_root.resolve()),))
    con.commit()
    con.close()


def open_layout(db_dir: Path, **kwargs):
    """Open a DB copy and record path/reopen behaviour."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            layout = BIDSLayout(database_path=db_dir, derivatives=True, **kwargs)
        except Exception as exc:  # noqa: BLE001
            return {"error": type(exc).__name__, "message": str(exc)}, [
                str(w.message) for w in caught
            ]
        files = layout.get(return_type="file")
        relpaths = {}
        for f in layout.get(return_type="object")[:3]:
            try:
                relpaths[f.path] = f.relpath
            except Exception as exc:  # noqa: BLE001
                relpaths[f.path] = {"error": type(exc).__name__, "message": str(exc)}
        sample_rel = None
        for f in layout.get(return_type="object"):
            if "sub-01/anat/sub-01_T1w.nii.gz" in f.path:
                sample_rel = "sub-01/anat/sub-01_T1w.nii.gz"
                break
        return {
            "reported_root": layout.root,
            "first_file": files[0] if files else None,
            "first_files": files[:3],
            "relpaths": relpaths,
            "get_file_relative": (
                (lambda hit: hit.path if hit else None)(layout.get_file(sample_rel))
                if sample_rel else None
            ),
            "derivatives": sorted(layout.derivatives),
        }, [str(w.message) for w in caught]


def main() -> None:
    """Run the relocation and missing-sidecar probe."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work", type=Path, default=Path("handoff/.work"))
    ap.add_argument("--report", type=Path, default=Path("handoff/RELOCATION.json"))
    args = ap.parse_args()

    work = args.work.resolve()
    scratch = work / "_relocation_scratch"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    original_root = (work / "archive_root").resolve()
    out = {}

    # 1. Copy the DB alone, leave the root where it is.
    shutil.copytree(work / "archive_db", scratch / "db_copy_only")
    out["db_copied_root_unchanged"], out["db_copied_root_unchanged_warnings"] = open_layout(
        scratch / "db_copy_only"
    )

    # 2. Move the root and copy the DB; the DB still names the old location.
    shutil.copytree(work / "archive_root", scratch / "moved_root")
    shutil.copytree(work / "archive_db", scratch / "db_old_root")
    out["moved_root_stale_db_root"], out["moved_root_stale_db_root_warnings"] = open_layout(
        scratch / "db_old_root"
    )

    # 3. Move the root, copy the DB, and rewrite layout_info.root (parent and children).
    moved_root = (scratch / "moved_root").resolve()
    shutil.copytree(work / "archive_db", scratch / "db_rewritten_root")
    rewrite_root(scratch / "db_rewritten_root", moved_root)
    for child in (scratch / "db_rewritten_root" / "derivatives").glob("*/layout_index.sqlite"):
        rewrite_root(
            child.parent, moved_root / "derivatives" / child.parent.name
        )
    out["moved_root_rewritten_layout_info"], out[
        "moved_root_rewritten_layout_info_warnings"
    ] = open_layout(scratch / "db_rewritten_root")

    # 4. Missing root description on reopen.
    shutil.copytree(work / "archive_root", scratch / "root_no_description")
    (scratch / "root_no_description" / "dataset_description.json").unlink()
    shutil.copytree(work / "archive_db", scratch / "db_root_no_description")
    rewrite_root(scratch / "db_root_no_description", (scratch / "root_no_description").resolve())
    out["missing_root_description_validate_true"], out[
        "missing_root_description_validate_true_warnings"
    ] = open_layout(scratch / "db_root_no_description")
    out["missing_root_description_validate_false"], out[
        "missing_root_description_validate_false_warnings"
    ] = open_layout(scratch / "db_root_no_description", validate=False)

    # 5. Missing derivative description: discovered by directory glob, so it vanishes.
    shutil.copytree(work / "archive_root", scratch / "root_no_deriv_description")
    (scratch / "root_no_deriv_description" / "derivatives" / "deriv_legacy"
     / "dataset_description.json").unlink()
    shutil.copytree(work / "archive_db", scratch / "db_no_deriv_description")
    rewrite_root(
        scratch / "db_no_deriv_description",
        (scratch / "root_no_deriv_description").resolve(),
    )
    out["missing_derivative_description"], out[
        "missing_derivative_description_warnings"
    ] = open_layout(scratch / "db_no_deriv_description")
    leftover_child_db = (
        scratch / "db_no_deriv_description" / "derivatives" / "deriv_legacy"
        / "layout_index.sqlite"
    )
    out["missing_derivative_description_child_db_still_present"] = leftover_child_db.exists()

    # 6. Missing the config at the path recorded in layout_info._config.
    shutil.copytree(work / "archive_root", scratch / "root_no_config")
    (scratch / "root_no_config" / "configs" / "incoming_entities.json").unlink()
    shutil.copytree(work / "archive_db", scratch / "db_no_config")
    rewrite_root(scratch / "db_no_config", (scratch / "root_no_config").resolve())
    out["missing_config_file"], out["missing_config_file_warnings"] = open_layout(
        scratch / "db_no_config"
    )

    # 7. Same files with validate=True: complement of the archived raw root.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        validated = BIDSLayout(
            work / "archive_root",
            derivatives=False,
            validate=True,
            reset_database=True,
        )
        validated_files = sorted(
            str(Path(f).relative_to((work / "archive_root").resolve()))
            for f in validated.get(return_type="file")
        )
    out["current_root_validate_true"] = {
        "files": validated_files,
        "orphan_included": any("orphan/" in f for f in validated_files),
    }
    out["original_root"] = str(original_root)
    out["interpretation"] = {
        "absolute_paths": (
            "files.path / tags.file_path / associations store absolute build-time "
            "paths. Copying or moving the archive without rewriting every row makes "
            "the index report files from the old machine, even after layout_info.root "
            "is edited."
        ),
        "root_binding": (
            "layout_info.root is the only location field, and it is authoritative on "
            "reopen: the root argument is ignored."
        ),
        "descriptions_are_external": (
            "dataset_description.json is read live on every open and is never stored "
            "in the DB; deleting it breaks reopen under the default validate=True and "
            "silently removes a derivative child under derivatives/."
        ),
    }

    args.report.write_text(json.dumps(out, indent=2, default=str) + "\n")
    print(json.dumps({
        "report": str(args.report),
        "copied_db_reports_old_root": out["db_copied_root_unchanged"].get("reported_root"),
        "rewritten_root_still_returns_old_file": out[
            "moved_root_rewritten_layout_info"
        ].get("first_file"),
        "rewritten_root_relpath_ok": all(
            isinstance(v, str)
            for v in out["moved_root_rewritten_layout_info"].get("relpaths", {}).values()
        ),
        "missing_derivative_description_children": out[
            "missing_derivative_description"
        ].get("derivatives"),
        "missing_config_open": "error" if "error" in out["missing_config_file"] else "ok",
        "get_file_relative_after_relocation": out[
            "moved_root_rewritten_layout_info"
        ].get("get_file_relative"),
        "validate_true_orphan_included": out["current_root_validate_true"][
            "orphan_included"
        ],
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
