#!/usr/bin/env python3
"""One-command replay of the whole GSB1109 diagnostic.

    python diagnostics/GSB1109/run_all.py

Rebuilds the deterministic CDF-5 inputs, reruns the online/offline analysis,
the permutation/window invariance check, and the decision table, then runs the
regression tests.  Any input that no longer reproduces its recorded sha256 is
reported.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PY = sys.executable
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def run(module: str) -> None:
    print(f"\n$ {PY.split('/')[-1]} -m {module}")
    subprocess.run([PY, "-m", module], cwd=REPO, check=True)


def verify_manifest() -> bool:
    manifest = json.loads((HERE / "expected" / "manifest.json").read_text(encoding="utf-8"))
    from diagnostics.GSB1109 import cdf5

    ok = True
    for key, entry in manifest["cases"].items():
        path = HERE / "data" / entry["file"]
        actual = cdf5.sha256(path)
        if actual != entry["sha256"]:
            ok = False
            print(f"MISMATCH {key}: expected {entry['sha256']} got {actual}")
    print(f"input checksums: {'OK' if ok else 'MISMATCH'}")
    return ok


def main() -> int:
    run("diagnostics.GSB1109.build_cases")
    if not verify_manifest():
        return 1
    run("diagnostics.GSB1109.analysis")
    run("diagnostics.GSB1109.invariance")
    run("diagnostics.GSB1109.decisions")
    print(f"\n$ {PY.split('/')[-1]} -m pytest diagnostics/GSB1109/tests -q")
    result = subprocess.run(
        [PY, "-m", "pytest", "diagnostics/GSB1109/tests", "-q"],
        cwd=REPO,
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
