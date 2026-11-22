"""Opt-in forensic tracing for same-path missing-input diagnosis.

Enable with SNAKEMAKE_DIAG_TRACE=<path to .jsonl>. No-op otherwise, so normal
workflow runs are unaffected.

The trace deliberately records observations, not conclusions: file opens,
producer candidates, constraint outcomes, checkpoint completeness and the order
in which input edges were resolved.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading

_ENV = "SNAKEMAKE_DIAG_TRACE"
_WATCH_ENV = "SNAKEMAKE_DIAG_WATCH"
_session = {"phase": "startup", "audit_installed": False, "seq": 0}
_local = threading.local()
_lock = threading.Lock()


def _enabled():
    return bool(os.environ.get(_ENV))


def enabled():
    return _enabled()


def _trace_path():
    return os.environ.get(_ENV)


def _skip_path(path):
    if not isinstance(path, str):
        return True
    if path == _trace_path():
        return True
    if "site-packages" in path or "/lib/python" in path:
        return True
    watch = os.environ.get(_WATCH_ENV)
    if watch:
        prefixes = [os.path.abspath(p) for p in watch.split(os.pathsep) if p]
        return not any(os.path.abspath(path) == p or os.path.abspath(path).startswith(p + os.sep) for p in prefixes)
    return False


def _write(event, **fields):
    path = _trace_path()
    if not path:
        return
    with _lock:
        _session["seq"] += 1
        seq = _session["seq"]
    record = {
        "seq": seq,
        "phase": _session["phase"],
        "rule": getattr(_local, "rule", None),
        "wildcards": getattr(_local, "wildcards", None),
        "event": event,
    }
    record.update(fields)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def _audit_hook(event, args):
    if event != "open" or not _enabled():
        return
    if getattr(_local, "busy", False):
        return
    path = args[0]
    if not isinstance(path, (str, bytes, os.PathLike)):
        return
    try:
        path = os.fspath(path)
        if isinstance(path, bytes):
            path = path.decode()
        path = os.path.abspath(path)
        if _skip_path(path):
            return
        if ".snakemake" in path.split(os.sep):
            return
        _local.busy = True
        try:
            mode = args[1]
            if mode is None:
                return
            _write("file_open", path=path, mode=mode)
        finally:
            _local.busy = False
    except Exception:
        return


def install_audit_hook():
    if not _enabled() or _session["audit_installed"]:
        return
    sys.addaudithook(_audit_hook)
    _session["audit_installed"] = True


@contextlib.contextmanager
def rule_context(rule_name, wildcards=None):
    previous_rule = getattr(_local, "rule", None)
    previous_wildcards = getattr(_local, "wildcards", None)
    _local.rule = rule_name
    try:
        _local.wildcards = dict(wildcards) if wildcards is not None else None
    except TypeError:
        _local.wildcards = None
    try:
        yield
    finally:
        _local.rule = previous_rule
        _local.wildcards = previous_wildcards


def set_phase(phase):
    _session["phase"] = phase


def trace_session_start(**fields):
    _write("session_start", **fields)


def trace_probe(path, exists, readable, size=None, mtime=None):
    _write(
        "path_probe",
        path=os.path.abspath(str(path)),
        exists=exists,
        readable=readable,
        size=size,
        mtime=mtime,
    )


def trace_input_plan(rule_name, paths, wildcards=None):
    _write(
        "input_plan",
        rule=rule_name,
        paths=[os.path.abspath(str(path)) for path in paths],
        wildcards=dict(wildcards) if wildcards is not None else {},
    )


def trace_rule_match(target, candidate_rules, selected_rules, rejections, details=None):
    """Record prefix/suffix candidates versus constraint-validated producers."""
    _write(
        "producer_selection",
        target=os.path.abspath(str(target)),
        prefix_suffix_candidates=sorted(r.name for r in candidate_rules),
        constraint_valid_producers=sorted(r.name for r in selected_rules),
        rejected_by_regex=sorted(r.name for r in rejections),
        details=details or [],
    )


def trace_regex_outcome(rule_name, pattern, value, constraint):
    matched = bool(constraint.match(value)) if constraint is not None else None
    _write(
        "wildcard_constraint",
        rule=rule_name,
        pattern=str(pattern),
        value=value,
        constraint=constraint.pattern if constraint is not None else None,
        matched=matched,
    )


def trace_checkpoint_get(rule_name, wildcards, created, missing, verdict):
    _write(
        "checkpoint_get",
        rule=rule_name,
        wildcards=dict(wildcards),
        created_output=sorted(str(o) for o in created),
        missing_output=sorted(str(o) for o in missing),
        verdict=verdict,
    )


def trace_input_edge(rule_name, index, path, wildcards, status):
    _write(
        "input_edge",
        rule=rule_name,
        index=index,
        path=os.path.abspath(str(path)),
        wildcards=dict(wildcards) if wildcards is not None else {},
        status=status,
    )


def trace_missing_input(rule_name, files, wildcards):
    _write(
        "missing_input",
        rule=rule_name,
        wildcards=dict(wildcards) if wildcards is not None else {},
        files=sorted(os.path.abspath(str(f)) for f in files),
    )


def trace_artifact(path, role, **fields):
    """Record what a produced artifact contains or was derived from."""
    _write("artifact", path=os.path.abspath(str(path)), role=role, **fields)


def trace_read_source(path, exists, digest=None, size=None):
    _write(
        "read_source",
        path=os.path.abspath(str(path)),
        exists=exists,
        digest=digest,
        size=size,
    )


def trace_exception(exc):
    _write(
        "exception",
        exception_class=type(exc).__name__,
        message=str(exc),
    )


if _enabled():
    install_audit_hook()
