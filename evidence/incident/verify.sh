#!/bin/sh
# Rebuild the incident collection and regenerate every evidence artifact.
# No network, no listening sockets.
set -e
cd "$(dirname "$0")/../.."
.venv/bin/python evidence/incident/build.py >/dev/null
.venv/bin/python evidence/incident/run_checks.py > evidence/incident/checks.out 2>&1
.venv/bin/python evidence/incident/diagnose.py > evidence/incident/diagnosis.json
.venv/bin/python evidence/incident/byte_identity.py > evidence/incident/byte_identity.json
.venv/bin/python evidence/incident/forensic.py > evidence/incident/forensic.json
.venv/bin/python evidence/incident/warc_offsets.py > evidence/incident/warc_offsets.json
.venv/bin/python evidence/incident/minimal_search.py > evidence/incident/minimal_search.out
rm -rf evidence/incident/variants evidence/incident/__pycache__
grep -q 'FAIL' evidence/incident/checks.out && { echo 'checks failed'; exit 1; }
echo 'all checks passed'
