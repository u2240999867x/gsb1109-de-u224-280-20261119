#!/usr/bin/env bash
# One-command reproduction. Read-only with respect to the PyBIDS source tree.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
$PY handoff/build_fixture.py --out handoff/.work --reset
$PY handoff/audit_handoff.py --work handoff/.work \
    --report handoff/EVIDENCE.json --manifest handoff/BINDING_MANIFEST.json
$PY handoff/relocation_probe.py --work handoff/.work --report handoff/RELOCATION.json
echo "OK: EVIDENCE.json, BINDING_MANIFEST.json, RELOCATION.json regenerated"
