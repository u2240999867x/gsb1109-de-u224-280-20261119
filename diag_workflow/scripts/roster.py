"""Readable old-round roster, plus an optional access helper.

The file itself is a plain two-column TSV that ordinary tools (cat, column,
awk) can read. The helper only exists so the diagnostic workflow can record
whether this specific old artifact was opened, and what it contained, without
depending on the directory listing.
"""

from __future__ import annotations

import hashlib
import os

try:
    from snakemake import diag_trace
except ImportError:  # imported by the standalone reproducer outside Snakemake
    diag_trace = None


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_roster(path):
    """Return roster lines and record the access as an observation.

    Raises the same FileNotFoundError a plain open() would, so a successful
    open is not silently turned into a valid basis.
    """
    path = os.path.abspath(path)
    if not os.path.exists(path):
        if diag_trace is not None:
            diag_trace.trace_read_source(path, exists=False)
        with open(path) as handle:
            return handle.read().splitlines()

    digest = sha256_file(path)
    with open(path) as handle:
        lines = [line.rstrip("\n") for line in handle]
    if diag_trace is not None:
        diag_trace.trace_read_source(
            path, exists=True, digest=digest, size=os.path.getsize(path)
        )
    return lines


def roster_entries(lines):
    """Parse readable roster lines into stable, non-empty records.

    Lines may be commented with '#'. The first column is the region scope and
    the remaining columns are targets. This is intentionally permissive so a
    malformed-but-openable old roster is visible as content, not as success.
    """
    entries = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        columns = [column.strip() for column in line.split("\t")]
        if len(columns) == 1:
            columns = [column.strip() for column in line.split() if column.strip()]
        if not columns:
            continue
        entries.append({"scope": columns[0], "fields": columns[1:]})
    return entries

