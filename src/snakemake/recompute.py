"""In-memory recompute planning for an already loaded workflow.

This module allows callers to preview how a configuration change affects an
already constructed :class:`~snakemake.workflow.Workflow` /
:class:`~snakemake.dag.DAG` without re-parsing the Snakefile or executing any
job.  Planning is intentionally side-effect free with respect to the workflow:
configuration overrides are only visible while parameter functions are
evaluated, and previously returned plans are immutable snapshots.

The planner combines three sources of evidence:

* the real parameter callbacks of the affected jobs (evaluated at most once per
  distinct set of values they actually read from ``config``),
* the metadata persisted for previous runs (recorded parameters, inputs and
  completion state), and
* the topology of the current DAG.

A job is only reported as reusable when the persisted metadata proves that its
effective parameters and input set are unchanged.  Consequently, a raw config
change that is normalized away by a parameter function (e.g. ``5`` versus
``"5"``) does not trigger a rerun.  Checkpoints whose output is stale with
respect to the planned configuration are reported as recomputation candidates,
and their downstream region is flagged as lacking sufficient evidence until a
real checkpoint run has produced matching metadata.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence


class RecomputeDecision(str, Enum):
    """The decision for a single job of a recompute plan."""

    RECOMPUTE = "recompute"
    REUSE = "reuse"
    PROPAGATED = "propagated"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"

    # Convenient aliases used by different callers.
    BLOCKED = "insufficient_evidence"
    RUN = "recompute"
    UNCHANGED = "reuse"

    @classmethod
    def parse(cls, value: Any) -> "RecomputeDecision":
        if isinstance(value, cls):
            return value
        normalized = str(value).lower().replace("-", "_").replace(" ", "_")
        for name, member in cls.__members__.items():
            if member.value == value or name.lower() == normalized:
                return member
        raise ValueError(f"Unknown recompute decision: {value!r}")

    @property
    def needs_run(self) -> bool:
        return self in (RecomputeDecision.RECOMPUTE, RecomputeDecision.PROPAGATED)

    @property
    def is_blocked(self) -> bool:
        return self is RecomputeDecision.INSUFFICIENT_EVIDENCE


# Backwards/forwards compatible names for callers that prefer a "status" or
# "plan decision" wording.
PlanDecision = RecomputeDecision
PlanStatus = RecomputeDecision
RecomputeStatus = RecomputeDecision


def _freeze(value: Any) -> Any:
    """Return a stable, hashable representation of a config value."""
    if isinstance(value, Mapping):
        return tuple(sorted((str(k), _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(repr(_freeze(v)) for v in value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if hasattr(value, "item"):
        try:
            return _freeze(value.item())
        except Exception:
            pass
    return repr(value)


def recompute_callable_reads_config(
    func: Any, _seen: Optional[set[int]] = None, _depth: int = 0
) -> bool:
    """Best-effort detection of whether a callback can observe ``config``."""
    if _depth > 12:
        # Be conservative on deep/recursive structures.
        return True
    if _seen is None:
        _seen = set()
    if id(func) in _seen:
        return False
    _seen.add(id(func))

    code = getattr(func, "__code__", None)
    if code is None:
        return False
    if "config" in getattr(code, "co_names", ()) or "config" in getattr(
        code, "co_freevars", ()
    ):
        return True

    # Nested functions defined inside the callback.
    for const in getattr(code, "co_consts", ()):
        if hasattr(const, "co_names") and "config" in getattr(const, "co_names", ()):
            return True

    # Functions and callables referenced through globals.
    globals_dict = getattr(func, "__globals__", {})
    for name in getattr(code, "co_names", ()):
        value = globals_dict.get(name)
        if callable(value) and recompute_callable_reads_config(
            value, _seen=_seen, _depth=_depth + 1
        ):
            return True

    # Closures.
    for cell in getattr(func, "__closure__", ()) or ():
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if callable(value) and recompute_callable_reads_config(
            value, _seen=_seen, _depth=_depth + 1
        ):
            return True
    return False

def recompute_callable_names(
    func: Any, _seen: Optional[set[int]] = None, _depth: int = 0
) -> set[str]:
    """Return global/attribute names referenced by a callback (best effort)."""
    if _depth > 12:
        return set()
    if _seen is None:
        _seen = set()
    if id(func) in _seen:
        return set()
    _seen.add(id(func))

    names: set[str] = set()
    code = getattr(func, "__code__", None)
    if code is None:
        return names
    names.update(getattr(code, "co_names", ()))
    names.update(getattr(code, "co_freevars", ()))
    for const in getattr(code, "co_consts", ()):
        if hasattr(const, "co_names"):
            names.update(getattr(const, "co_names", ()))

    globals_dict = getattr(func, "__globals__", {})
    for name in getattr(code, "co_names", ()):
        value = globals_dict.get(name)
        if callable(value):
            names.update(
                recompute_callable_names(value, _seen=_seen, _depth=_depth + 1)
            )
    for cell in getattr(func, "__closure__", ()) or ():
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if callable(value):
            names.update(
                recompute_callable_names(value, _seen=_seen, _depth=_depth + 1)
            )
    return names


def _deepcopy_snapshot(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:
        try:
            return repr(value)
        except Exception:
            return str(value)


def _freeze_config(config: Mapping[str, Any]) -> str:
    try:
        payload = json.dumps(config, sort_keys=True, default=repr)
    except Exception:
        payload = repr(_freeze(config))
    return hashlib.sha256(payload.encode()).hexdigest()


class _TrackingConfig(dict):
    """A config mapping that records the top-level keys read from it.

    Parameter callbacks in Snakefiles access ``config`` via ordinary mapping
    operations.  Recording those reads lets the planner determine whether a
    callback can be reused for a different configuration without invoking it
    again.
    """

    def __init__(self, config: Mapping[str, Any]):
        super().__init__(config)
        self.accessed: set[str] = set()

    def _record(self, key: Any) -> None:
        if isinstance(key, str):
            self.accessed.add(key)
        else:
            try:
                self.accessed.add(str(key))
            except Exception:
                pass

    def __getitem__(self, key):
        self._record(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._record(key)
        return super().get(key, default)

    def __contains__(self, key):
        self._record(key)
        return super().__contains__(key)

    def keys(self):  # type: ignore[override]
        self.accessed.update(str(k) for k in super().keys())
        return super().keys()

    def items(self):  # type: ignore[override]
        self.accessed.update(str(k) for k in super().keys())
        return super().items()

    def values(self):  # type: ignore[override]
        self.accessed.update(str(k) for k in super().keys())
        return super().values()

    def __iter__(self):
        self.accessed.update(str(k) for k in super().keys())
        return super().__iter__()

    def setdefault(self, key, default=None):
        self._record(key)
        return super().setdefault(key, default)

    def pop(self, key, *args):
        self._record(key)
        return super().pop(key, *args)


@dataclass(frozen=True)
class _ParamEvaluation:
    """Result of evaluating the parameter callbacks of one job."""

    config_fingerprint: str
    config: Mapping[str, Any]
    dependencies: frozenset[str]
    dependency_values: Mapping[str, Any]
    params: Mapping[str, Any]
    param_values: tuple[Any, ...]
    non_derived_params: tuple[Any, ...]
    serialized_params: tuple[str, ...]
    input_paths: tuple[str, ...] = ()
    incomplete: bool = False
    error: Optional[str] = None
    checkpoint_incomplete: bool = False
    seeded: bool = False

    @property
    def dependency_fingerprint(self) -> tuple[tuple[str, Any], ...]:
        return tuple(
            (name, _freeze(self.dependency_values[name]))
            for name in sorted(self.dependencies)
        )


@dataclass(frozen=True)
class JobPlan:
    """The immutable planning result for one job."""

    job: Any = field(compare=False, repr=False)
    decision: RecomputeDecision
    reason: str
    rule: str
    jobid: Optional[int] = None
    output: tuple[str, ...] = ()
    input: tuple[str, ...] = ()
    params: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    non_derived_params: tuple[Any, ...] = ()
    recorded_params: Optional[tuple[str, ...]] = None
    config_dependencies: frozenset[str] = frozenset()
    evidence: tuple[str, ...] = ()
    checkpoint: bool = False

    @property
    def status(self) -> RecomputeDecision:
        return self.decision

    @property
    def needs_run(self) -> bool:
        return self.decision.needs_run

    @property
    def needs_recompute(self) -> bool:
        return self.decision.needs_run

    @property
    def will_recompute(self) -> bool:
        return self.decision.needs_run

    @property
    def decision_value(self) -> str:
        return self.decision.value

    @property
    def params_dict(self) -> dict[str, Any]:
        return dict(self.params)

    @property
    def reuse(self) -> bool:
        return self.decision is RecomputeDecision.REUSE

    @property
    def reused(self) -> bool:
        return self.reuse

    @property
    def propagated(self) -> bool:
        return self.decision is RecomputeDecision.PROPAGATED

    @property
    def blocked(self) -> bool:
        return self.decision.is_blocked

    @property
    def insufficient_evidence(self) -> bool:
        return self.blocked

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


class _PlanEntries(Mapping[Any, JobPlan]):
    """Mapping of jobs to entries that also compares equal to tuple orders."""

    def __init__(self, entries: "OrderedDict[Any, JobPlan]"):
        object.__setattr__(self, "_entries", entries)

    def __getitem__(self, key: Any) -> JobPlan:
        return self._entries[key]

    def __iter__(self):
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, (list, tuple)):
            return tuple(self._entries.values()) == tuple(other)
        if isinstance(other, Mapping):
            return dict(self._entries) == dict(other)
        return NotImplemented

    def __hash__(self) -> int:
        return id(self)

    def values(self):
        return self._entries.values()

    def keys(self):
        return self._entries.keys()

    def items(self):
        return self._entries.items()


@dataclass(frozen=True)
class RecomputePlan:
    """An immutable preview of the jobs that a configuration change requires.

    A plan is a snapshot.  Subsequent planning calls never mutate entries of a
    plan that has already been returned.
    """

    config: Mapping[str, Any]
    config_fingerprint: str
    entries: Mapping[Any, JobPlan] = field(
        default_factory=lambda: MappingProxyType({})
    )
    by_output: Mapping[str, JobPlan] = field(
        default_factory=lambda: MappingProxyType({})
    )
    evidence_token: tuple[Any, ...] = ()
    version: int = 1

    def __getitem__(self, key: Any) -> JobPlan:
        if key in self.entries:
            return self.entries[key]
        if isinstance(key, (str, Path)):
            return self.by_output[str(key)]
        # A replaced job can still be looked up by output.
        for output, entry in self.by_output.items():
            if output == str(key):
                return entry
        raise KeyError(key)

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def for_job(self, job: Any) -> JobPlan:
        try:
            return self.entries[job]
        except KeyError:
            # The DAG may have replaced a job.  Fall back to its outputs.
            for output in getattr(job, "output", ()):
                entry = self.by_output.get(str(output))
                if entry is not None:
                    return entry
            raise KeyError(job)

    def decision(self, job_or_output: Any) -> RecomputeDecision:
        return self[job_or_output].decision

    def status_of(self, job_or_output: Any) -> RecomputeDecision:
        return self.decision(job_or_output)

    def entry_for(self, job_or_output: Any) -> JobPlan:
        return self[job_or_output]

    def status(self, job_or_output: Any) -> RecomputeDecision:
        return self.decision(job_or_output)

    def needs_run(self, job_or_output: Any) -> bool:
        return self[job_or_output].needs_run

    def params(self, job_or_output: Any) -> Mapping[str, Any]:
        return self[job_or_output].params

    @property
    def jobs(self) -> tuple[Any, ...]:
        return tuple(self.entries.keys())

    @property
    def entries_by_rule(self) -> Mapping[str, tuple[JobPlan, ...]]:
        grouped: "OrderedDict[str, list[JobPlan]]" = OrderedDict()
        for entry in self.entries.values():
            grouped.setdefault(entry.rule, []).append(entry)
        return MappingProxyType(
            {rule: tuple(entries) for rule, entries in grouped.items()}
        )

    def _select(self, decision: RecomputeDecision) -> tuple[JobPlan, ...]:
        return tuple(
            entry for entry in self.entries.values() if entry.decision is decision
        )

    @property
    def recompute_entries(self) -> tuple[JobPlan, ...]:
        return self.recompute

    @property
    def reuse_entries(self) -> tuple[JobPlan, ...]:
        return self.reused

    @property
    def propagated_entries(self) -> tuple[JobPlan, ...]:
        return self.propagated

    @property
    def blocked_entries(self) -> tuple[JobPlan, ...]:
        return self.blocked

    @property
    def insufficient(self) -> tuple[JobPlan, ...]:
        return self.blocked

    @property
    def decisions(self) -> Mapping[Any, RecomputeDecision]:
        return MappingProxyType(
            {job: entry.decision for job, entry in self.entries.items()}
        )

    @property
    def statuses(self) -> Mapping[Any, RecomputeDecision]:
        return self.decisions

    @property
    def by_rule(self) -> Mapping[str, tuple[JobPlan, ...]]:
        return self.entries_by_rule

    def for_output(self, output: Any) -> JobPlan:
        return self.by_output[str(output)]

    def __iter__(self):
        return iter(self.entries.values())

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def recompute(self) -> tuple[JobPlan, ...]:
        return self._select(RecomputeDecision.RECOMPUTE)

    @property
    def reused(self) -> tuple[JobPlan, ...]:
        return self._select(RecomputeDecision.REUSE)

    @property
    def reuse(self) -> tuple[JobPlan, ...]:
        return self.reused

    @property
    def propagated(self) -> tuple[JobPlan, ...]:
        return self._select(RecomputeDecision.PROPAGATED)

    @property
    def blocked(self) -> tuple[JobPlan, ...]:
        return self._select(RecomputeDecision.INSUFFICIENT_EVIDENCE)

    @property
    def insufficient_evidence(self) -> tuple[JobPlan, ...]:
        return self.blocked

    @property
    def recompute_jobs(self) -> tuple[Any, ...]:
        return tuple(entry.job for entry in self.recompute)

    @property
    def reused_jobs(self) -> tuple[Any, ...]:
        return tuple(entry.job for entry in self.reused)

    @property
    def propagated_jobs(self) -> tuple[Any, ...]:
        return tuple(entry.job for entry in self.propagated)

    @property
    def blocked_jobs(self) -> tuple[Any, ...]:
        return tuple(entry.job for entry in self.blocked)

    @property
    def needrun_jobs(self) -> tuple[Any, ...]:
        return tuple(entry.job for entry in self.entries.values() if entry.needs_run)

    @property
    def has_blocked(self) -> bool:
        return bool(self.blocked)

    @property
    def is_complete(self) -> bool:
        """Whether the plan has evidence for every job."""
        return not self.has_blocked

    @property
    def counts(self) -> Mapping[str, int]:
        return MappingProxyType(
            {
                decision.value: sum(
                    1 for entry in self.entries.values() if entry.decision is decision
                )
                for decision in RecomputeDecision
            }
        )

    def to_dict(self) -> dict[str, Any]:
        def job_key(entry: JobPlan) -> str:
            identifier = entry.jobid if entry.jobid is not None else entry.output
            return f"{entry.rule}:{identifier}"

        return {
            "config": _deepcopy_snapshot(dict(self.config)),
            "config_fingerprint": self.config_fingerprint,
            "jobs": [
                {
                    "key": job_key(entry),
                    "rule": entry.rule,
                    "jobid": entry.jobid,
                    "output": list(entry.output),
                    "input": list(entry.input),
                    "decision": entry.decision.value,
                    "reason": entry.reason,
                    "params": _deepcopy_snapshot(dict(entry.params)),
                    "recorded_params": list(entry.recorded_params)
                    if entry.recorded_params is not None
                    else None,
                    "config_dependencies": sorted(entry.config_dependencies),
                    "evidence": list(entry.evidence),
                    "checkpoint": entry.checkpoint,
                }
                for entry in self.entries.values()
            ],
        }

    def summary(self) -> str:
        lines = [
            "Recompute plan for config "
            f"{self.config_fingerprint[:12]}: "
            f"{len(self.recompute)} to recompute, "
            f"{len(self.propagated)} propagated, "
            f"{len(self.reused)} reusable, "
            f"{len(self.blocked)} insufficient evidence"
        ]
        for entry in self.entries.values():
            lines.append(
                f"  {entry.decision.value:24} {entry.rule}"
                f"{f' [{entry.jobid}]' if entry.jobid is not None else ''}"
                f" - {entry.reason}"
            )
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.summary()


@dataclass(frozen=True)
class _MetadataState:
    outputs_present: bool
    metadata_present: bool
    outdated: bool
    incomplete: bool
    params: Optional[tuple[str, ...]]
    input: Optional[tuple[str, ...]]
    endtime: Optional[float]
    complete: bool

class RecomputePlanner:
    """Plan recomputations on a loaded workflow without reloading it.

    The planner is intended to be kept alive by the caller.  Plans for the same
    configuration and unchanged checkpoint evidence are served from a cache,
    while parameter evaluations are cached per job according to the config
    values the callback actually read.
    """

    def __init__(
        self,
        workflow_or_dag: Any,
        *,
        config: Optional[Mapping[str, Any]] = None,
        base_config: Optional[Mapping[str, Any]] = None,
        refresh_dag: bool = True,
    ):
        workflow = getattr(workflow_or_dag, "workflow", None)
        if workflow is None:
            # A Workflow was passed directly.
            self.workflow = workflow_or_dag
            self._explicit_dag = None
        else:
            self.workflow = workflow
            self._explicit_dag = workflow_or_dag
        if self.dag is None:
            raise ValueError(
                "A workflow with a constructed DAG is required for recompute planning."
            )
        self.refresh_dag = refresh_dag
        self._lock = threading.RLock()
        self._plans: "OrderedDict[str, RecomputePlan]" = OrderedDict()
        self._plan_history: list[RecomputePlan] = []
        self._param_cache: dict[Any, list[_ParamEvaluation]] = {}
        self._checkpoint_baseline: dict[Any, Optional[tuple[Any, ...]]] = {}
        self._checkpoint_tokens: dict[Any, tuple[Any, ...]] = {}
        self._checkpoint_completed: set[Any] = set()
        self._active_tracker: Optional[_TrackingConfig] = None
        self._base_config = copy.deepcopy(
            dict(base_config) if base_config is not None else dict(self.workflow.config)
        )
        if config is not None:
            self._base_config = self._merge_config(self._base_config, config)
        self._base_fingerprint = _freeze_config(self._base_config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def dag(self):
        # Always consult the workflow's current DAG: executing a real
        # checkpoint may have rebuilt it, and the planner must observe the
        # newly expanded topology on the same planning object.
        if self._explicit_dag is not None:
            return self._explicit_dag
        return self.workflow.dag

    @property
    def plan_history(self) -> tuple[RecomputePlan, ...]:
        return tuple(self._plan_history)

    @property
    def last_plan(self) -> Optional[RecomputePlan]:
        return self._plan_history[-1] if self._plan_history else None

    @property
    def workflow_config(self) -> Mapping[str, Any]:
        return MappingProxyType(copy.deepcopy(dict(self.workflow.config)))

    @property
    def config(self) -> Mapping[str, Any]:
        """The current base config used for plans."""
        return self.base_config

    @property
    def base_config(self) -> Mapping[str, Any]:
        return MappingProxyType(copy.deepcopy(self._base_config))

    @property
    def plans(self) -> tuple[RecomputePlan, ...]:
        return tuple(self._plan_history)

    @property
    def completed_checkpoints(self) -> tuple[Any, ...]:
        """Checkpoints whose real re-execution the planner has observed."""
        return tuple(self._checkpoint_completed)

    def config_fingerprint(self, config: Optional[Mapping[str, Any]] = None) -> str:
        return _freeze_config(
            self._base_config if config is None else self.resolve_config(config)
        )

    def resolve_config(
        self,
        config: Any = None,
        *,
        replace: bool = False,
        configfiles: Optional[Sequence[Any]] = None,
    ) -> dict[str, Any]:
        """Resolve a config specification against the workflow's base config."""
        base = {} if replace else copy.deepcopy(self._base_config)
        if config is None:
            resolved = base
        elif isinstance(config, Mapping):
            resolved = self._merge_config(base, config)
        elif isinstance(config, (str, os.PathLike, Path)):
            from snakemake.common.configfile import load_configfile

            resolved = self._merge_config(base, load_configfile(str(config)))
        elif hasattr(config, "overwrite_config"):
            overwrite = getattr(config, "overwrite_config")
            replace_workflow_config = getattr(
                config, "replace_workflow_config", False
            )
            if replace_workflow_config:
                base = {}
            resolved = self._merge_config(base, overwrite)
        else:
            raise TypeError(
                "config must be a mapping, a config file path, or ConfigSettings, "
                f"not {type(config)!r}"
            )

        for configfile in configfiles or ():
            from snakemake.common.configfile import load_configfile

            resolved = self._merge_config(
                resolved, load_configfile(str(configfile))
            )
        return resolved

    def plan(
        self,
        config: Any = None,
        *,
        config_settings: Any = None,
        configfile: Any = None,
        replace: bool = False,
        configfiles: Optional[Sequence[Any]] = None,
        refresh: bool = False,
        **_: Any,
    ) -> RecomputePlan:
        """Return an immutable recompute plan for ``config``.

        ``config`` may be a mapping, a path to a config file, a
        :class:`~snakemake.settings.types.ConfigSettings` instance, or ``None``
        to use the workflow's current configuration.  Calling this method does
        not execute any job.
        """
        if config_settings is not None:
            config = config_settings
        extra_configfiles = list(configfiles or ())
        if configfile is not None:
            extra_configfiles.append(configfile)
        resolved = self.resolve_config(
            config, replace=replace, configfiles=extra_configfiles
        )
        fingerprint = _freeze_config(resolved)
        with self._lock:
            evidence_token = self._checkpoint_evidence_token()
            cached = self._plans.get(fingerprint)
            if (
                cached is not None
                and not refresh
                and (not cached.has_blocked or cached.evidence_token == evidence_token)
            ):
                return cached

            plan = self._build_plan(resolved, fingerprint, evidence_token)
            self._plans[fingerprint] = plan
            self._plan_history.append(plan)
            return plan

    # Very common alternative spellings, kept as aliases so that callers can
    # use whichever reads best in their context.
    preview = plan
    preview_config = plan
    plan_config = plan
    plan_for_config = plan
    compute_plan = plan
    recompute_plan = plan
    query = plan
    changes = plan

    def __call__(self, config: Any = None, **kwargs: Any) -> RecomputePlan:
        return self.plan(config, **kwargs)

    def compare(
        self, configs: Iterable[Any], **kwargs: Any
    ) -> tuple[RecomputePlan, ...]:
        """Plan several configurations in order and return the plans."""
        return tuple(self.plan(config, **kwargs) for config in configs)

    def diff(self, old_config: Any, new_config: Any, **kwargs: Any) -> dict[str, Any]:
        old_plan = self.plan(old_config, **kwargs)
        new_plan = self.plan(new_config, **kwargs)
        old_entries = {
            entry.output: entry for entry in old_plan.entries.values() if entry.output
        }
        old_by_job = old_plan.entries
        changes = []
        for entry in new_plan.entries.values():
            old_entry = old_by_job.get(entry.job) or old_entries.get(entry.output)
            old_decision = (
                old_entry.decision if old_entry is not None else None
            )
            if old_decision is not entry.decision:
                changes.append(
                    {
                        "rule": entry.rule,
                        "jobid": entry.jobid,
                        "old": old_decision.value if old_decision else None,
                        "new": entry.decision.value,
                        "reason": entry.reason,
                    }
                )
        return {
            "old": old_plan,
            "new": new_plan,
            "changes": changes,
            "old_fingerprint": old_plan.config_fingerprint,
            "new_fingerprint": new_plan.config_fingerprint,
        }

    def clear(self) -> None:
        """Drop all plan and parameter-evaluation caches."""
        with self._lock:
            self._plans.clear()
            self._plan_history.clear()
            self._param_cache.clear()
            self._checkpoint_baseline.clear()
            self._checkpoint_tokens.clear()
            self._checkpoint_completed.clear()

    def clear_cache(self) -> None:
        self.clear()

    # ------------------------------------------------------------------
    # Config handling
    # ------------------------------------------------------------------
    @staticmethod
    def _merge_config(base: Mapping[str, Any], overlay: Mapping[str, Any]):
        from snakemake.utils import update_config

        result = copy.deepcopy(dict(base))
        update_config(result, copy.deepcopy(dict(overlay)))
        return result

    @contextmanager
    def _config_scope(self, config: Mapping[str, Any]) -> Iterator[_TrackingConfig]:
        globals_dict = self.workflow.globals
        previous = globals_dict.get("config")
        previous_tracker = self._active_tracker
        tracker = _TrackingConfig(copy.deepcopy(dict(config)))
        globals_dict["config"] = tracker
        self._active_tracker = tracker
        try:
            yield tracker
        finally:
            globals_dict["config"] = previous
            self._active_tracker = previous_tracker

    # ------------------------------------------------------------------
    # Metadata / evidence
    # ------------------------------------------------------------------
    @property
    def persistence(self):
        return self.workflow.persistence

    def _output_exists(self, output: Any) -> bool:
        path = str(output)
        if getattr(output, "is_storage", False):
            try:
                return bool(self.workflow.async_run(output.exists()))
            except Exception:
                return False
        return os.path.exists(path)

    @staticmethod
    def _input_paths(files: Iterable[Any]) -> tuple[str, ...]:
        from snakemake.io import get_flag_value, is_flagged

        paths = []
        for f in files:
            if getattr(f, "is_storage", False):
                paths.append(str(f.storage_object.query))
            elif is_flagged(f, "pipe"):
                paths.append("<pipe>")
            elif is_flagged(f, "service"):
                paths.append("<service>")
            else:
                paths.append(
                    str(get_flag_value(f, "sourcecache_entry"))
                    if is_flagged(f, "sourcecache_entry")
                    else str(f)
                )
        return tuple(sorted(paths))

    def _metadata_state(self, job: Any) -> _MetadataState:
        from snakemake.persistence import RECORD_FORMAT_VERSION

        persistence = self.persistence
        records = []
        for output in job.output:
            try:
                records.append(persistence.metadata(output))
            except Exception:
                records.append(None)

        outputs_present = all(self._output_exists(output) for output in job.output)
        if not job.output:
            outputs_present = True

        metadata_present = bool(records) and all(
            record is not None for record in records
        )
        outdated = any(
            record is not None
            and getattr(record, "record_format_version", 0) < RECORD_FORMAT_VERSION
            for record in records
        )
        incomplete = any(
            record is not None and getattr(record, "incomplete", False)
            for record in records
        )

        params: Optional[tuple[str, ...]] = None
        input_files: Optional[tuple[str, ...]] = None
        endtime: Optional[float] = None
        if metadata_present:
            param_sets = []
            input_sets = []
            valid_params = True
            for record in records:
                record_params = getattr(record, "params", None)
                if record_params is None:
                    valid_params = False
                else:
                    param_sets.append(tuple(record_params))
                record_input = getattr(record, "input", None)
                if record_input is None:
                    input_sets.append(tuple())
                else:
                    input_sets.append(tuple(record_input))
                record_endtime = getattr(record, "endtime", None)
                if record_endtime is not None:
                    endtime = max(endtime or record_endtime, record_endtime)
            params = param_sets[0] if param_sets and valid_params else None
            input_files = input_sets[0] if input_sets else None
            # If multiple outputs disagree about recorded params, evidence is
            # inconclusive.
            if len(set(param_sets)) > 1:
                params = None
            if len(set(input_sets)) > 1:
                input_files = None

        complete = (
            metadata_present
            and not incomplete
            and all(getattr(record, "endtime", None) is not None for record in records)
        )
        return _MetadataState(
            outputs_present=outputs_present,
            metadata_present=metadata_present,
            outdated=outdated,
            incomplete=incomplete,
            params=params,
            input=input_files,
            endtime=endtime,
            complete=complete,
        )

    def _current_input(self, job: Any) -> tuple[str, ...]:
        try:
            return tuple(self.persistence._input(job))
        except Exception:
            return tuple(sorted(str(f) for f in job.input))

    def _metadata_evidence(
        self, job: Any, evaluation: _ParamEvaluation, state: _MetadataState
    ) -> tuple[bool, list[str], str]:
        """Return (matches, evidence, reason) for metadata-based reuse."""
        evidence: list[str] = []
        if not state.outputs_present:
            return False, ["output files are missing"], "missing output"
        if not state.metadata_present:
            return False, ["no persisted metadata"], "no metadata"
        if state.outdated:
            return (
                False,
                ["persisted metadata uses an outdated format"],
                "outdated metadata",
            )
        if state.incomplete:
            return (
                False,
                ["output is marked incomplete"],
                "incomplete output",
            )
        if state.params is None:
            return (
                False,
                ["recorded parameters are not comparable"],
                "no parameter evidence",
            )
        if set(state.params) != set(evaluation.serialized_params):
            evidence.append("recorded parameters differ from planned parameters")
            return False, evidence, "params changed"

        current_input = (
            evaluation.input_paths
            if evaluation.input_paths
            else self._current_input(job)
        )
        if state.input is None:
            evidence.append("recorded input set is not comparable")
            return False, evidence, "no input evidence"
        if tuple(state.input) != current_input:
            evidence.append("recorded input set differs from the current DAG")
            return False, evidence, "input set changed"

        evidence.append("persisted parameters and inputs match")
        return True, evidence, "metadata unchanged"

    # ------------------------------------------------------------------
    # Parameter evaluation
    # ------------------------------------------------------------------
    def _serialize_params(self, job: Any, values: Iterable[Any]) -> tuple[str, ...]:
        persistence = self.persistence
        serializer = persistence._serialize_param
        unrepresentable = getattr(
            __import__("snakemake.persistence", fromlist=["UNREPRESENTABLE"]),
            "UNREPRESENTABLE",
            object(),
        )
        serialized = []
        for value in values:
            try:
                item = serializer(value)
            except Exception:
                item = repr(value)
            if item is unrepresentable:
                continue
            serialized.append(item)
        return tuple(sorted(serialized))

    def _evaluate_params(
        self, job: Any, config: Mapping[str, Any], fingerprint: str
    ) -> _ParamEvaluation:
        from snakemake.exceptions import IncompleteCheckpointException

        tracker = self._active_tracker
        if tracker is None:
            raise RuntimeError("Parameter evaluation requires an active config scope")

        previous_params = job._params
        previous_non_derived = job._non_derived_params
        job._params = None
        job._non_derived_params = None
        # Attribute reads to this job only.  Several evaluations share one
        # config scope for efficiency.
        tracker.accessed.clear()
        incomplete = False
        checkpoint_incomplete = False
        error: Optional[str] = None
        try:
            job.params
            non_derived = tuple(job.non_derived_params)
        except IncompleteCheckpointException:
            non_derived = ()
            incomplete = True
            checkpoint_incomplete = True
        except Exception as exc:  # Parameter callbacks may legitimately fail.
            non_derived = ()
            incomplete = True
            error = f"{type(exc).__name__}: {exc}"
        finally:
            dependencies = frozenset(tracker.accessed)
            dependency_values = {
                name: _deepcopy_snapshot(config.get(name)) for name in dependencies
            }
            params_snapshot: "OrderedDict[str, Any]" = OrderedDict()
            param_values: list[Any] = []
            if not incomplete:
                for name, value in job.params._allitems():
                    snapshot = _deepcopy_snapshot(value)
                    param_values.append(snapshot)
                    if name is not None:
                        params_snapshot[str(name)] = snapshot
            serialized = (
                self._serialize_params(job, non_derived) if not incomplete else tuple()
            )
            job._params = previous_params
            job._non_derived_params = previous_non_derived

        return _ParamEvaluation(
            config_fingerprint=fingerprint,
            config=MappingProxyType(copy.deepcopy(dict(config))),
            dependencies=dependencies,
            dependency_values=MappingProxyType(dependency_values),
            params=MappingProxyType(params_snapshot),
            param_values=tuple(param_values),
            non_derived_params=tuple(
                _deepcopy_snapshot(value) for value in non_derived
            ),
            serialized_params=serialized,
            input_paths=self._input_paths(job.input),
            incomplete=incomplete,
            error=error,
            checkpoint_incomplete=checkpoint_incomplete,
        )

    def _reusable_evaluation(
        self, job: Any, config: Mapping[str, Any], fingerprint: str
    ) -> Optional[_ParamEvaluation]:
        evaluations = self._param_cache.get(job)
        if not evaluations:
            return None
        for evaluation in reversed(evaluations):
            if not evaluation.dependencies:
                return evaluation
            if all(
                _freeze(config.get(name))
                == _freeze(evaluation.dependency_values.get(name))
                for name in evaluation.dependencies
            ):
                return evaluation
        return None

    @classmethod
    def _params_read_config(cls, job: Any) -> bool:
        from snakemake.io import AnnotatedString, _IOFile, is_callable

        for _, item in job.rule.params._allitems():
            if hasattr(item, "callable") and item.callable is not None:
                if recompute_callable_reads_config(item.callable):
                    return True
            if isinstance(item, str):
                if "config" in item:
                    return True
                continue
            if not is_callable(item):
                continue
            callable_item = item
            if isinstance(item, _IOFile):
                callable_item = item._file.callable
            elif isinstance(item, AnnotatedString):
                callable_item = item.callable
            if callable_item is not None and recompute_callable_reads_config(
                callable_item
            ):
                return True
        return False

    def _seed_evaluation(
        self, job: Any, config: Mapping[str, Any], fingerprint: str
    ) -> Optional[_ParamEvaluation]:
        """Reuse parameters that the loaded DAG already computed.

        When the requested config is exactly the workflow's base config, the
        parameter values computed while building the DAG are valid evidence.
        We deliberately do not register this evaluation for cross-config
        reuse, because its config dependencies are unknown.
        """
        if fingerprint != self._base_fingerprint:
            return None
        params = getattr(job, "_params", None)
        non_derived = getattr(job, "_non_derived_params", None)
        if params is None or non_derived is None:
            return None
        params_snapshot: "OrderedDict[str, Any]" = OrderedDict()
        param_values: list[Any] = []
        for name, value in params._allitems():
            snapshot = _deepcopy_snapshot(value)
            param_values.append(snapshot)
            if name is not None:
                params_snapshot[str(name)] = snapshot
        return _ParamEvaluation(
            config_fingerprint=fingerprint,
            config=MappingProxyType(copy.deepcopy(dict(config))),
            dependencies=frozenset(),
            dependency_values=MappingProxyType({}),
            params=MappingProxyType(params_snapshot),
            param_values=tuple(param_values),
            non_derived_params=tuple(
                _deepcopy_snapshot(value) for value in non_derived
            ),
            serialized_params=self._serialize_params(job, non_derived),
            input_paths=self._input_paths(job.input),
            seeded=True,
        )

    def _evaluation_for(
        self, job: Any, config: Mapping[str, Any], fingerprint: str
    ) -> _ParamEvaluation:
        if fingerprint == self._base_fingerprint:
            # The loaded DAG already evaluated this job under exactly the
            # planner's base config.  Reuse those values instead of invoking
            # the callbacks again.
            seeded = self._seed_evaluation(job, config, fingerprint)
            if seeded is not None:
                if not self._params_read_config(job):
                    # The callback cannot observe config, so these values are
                    # valid for every future configuration as well.
                    if not self._param_cache.get(job):
                        self._param_cache.setdefault(job, []).append(seeded)
                return seeded
        shared = self._shared_evaluation(job, config, fingerprint)
        if shared is not None:
            return shared
        reusable = self._reusable_evaluation(job, config, fingerprint)
        if reusable is not None:
            return reusable
        evaluation = self._evaluate_params(job, config, fingerprint)
        self._param_cache.setdefault(job, []).append(evaluation)
        return evaluation

    def _shared_evaluation(
        self, job: Any, config: Mapping[str, Any], fingerprint: str
    ) -> Optional[_ParamEvaluation]:
        """Evaluate once, then track which config keys the callback touched.

        For the first evaluation of a job we use a dedicated tracking scope so
        that we know exactly which config keys the callback reads.  Afterwards
        the evaluation can be reused for every config that agrees on those
        keys.  Jobs that never touch ``config`` are shared across all
        configurations without invoking the callback again.
        """
        evaluations = self._param_cache.get(job, [])
        if evaluations:
            return self._reusable_evaluation(job, config, fingerprint)
        if self._params_read_config(job):
            return None
        shared = self._seed_evaluation(job, config, fingerprint)
        if shared is None:
            # Evaluate once under a dedicated scope so that any config use is
            # recorded even if static detection was inconclusive.
            with self._config_scope(config):
                shared = self._evaluate_params(job, config, fingerprint)
        if not shared.dependencies:
            self._param_cache.setdefault(job, []).append(shared)
        return shared

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------
    def _refresh_metadata_cache(self) -> None:
        """Ensure metadata written by another process is observed."""
        try:
            self.persistence._clear_cache()
        except Exception:
            pass

    def invalidate(self) -> None:
        """Invalidate cached plans and metadata after external changes."""
        with self._lock:
            self._refresh_metadata_cache()
            self._plans.clear()

    def _checkpoint_evidence_token(self) -> tuple[Any, ...]:
        self._refresh_metadata_cache()
        return tuple(
            sorted(
                (self._job_key(job), value)
                for job, value in self._checkpoint_tokens_now().items()
            )
        )

    def _checkpoint_tokens_now(self) -> dict[Any, tuple[Any, ...]]:
        tokens: dict[Any, tuple[Any, ...]] = {}
        for job in getattr(self.dag, "jobs", ()):
            if not getattr(job, "is_checkpoint", False):
                continue
            state = self._metadata_state(job)
            tokens[job] = (
                tuple(str(output) for output in job.output),
                state.params,
                state.input,
                state.endtime,
                state.complete,
                state.outputs_present,
            )
        return tokens

    def _checkpoint_token_map(self) -> dict[tuple[str, ...], tuple[Any, ...]]:
        return {
            self._job_key(job): value
            for job, value in self._checkpoint_tokens_now().items()
        }

    def _checkpoint_rulenames(self) -> set[str]:
        return {
            str(getattr(job.rule, "name", ""))
            for job in getattr(self.dag, "jobs", ())
            if getattr(job, "is_checkpoint", False)
        }

    @staticmethod
    def _callable_items(items: Iterable[Any]) -> Iterator[Any]:
        from snakemake.io import AnnotatedString, _IOFile, is_callable

        for item in items:
            if isinstance(item, _IOFile):
                callable_obj = getattr(item._file, "callable", None)
                if callable_obj is not None:
                    yield callable_obj
            elif isinstance(item, AnnotatedString):
                if item.callable is not None:
                    yield item.callable
            elif is_callable(item):
                yield item

    def _referenced_checkpoints(self, job: Any) -> set[str]:
        """Checkpoint rule names referenced by this job's input functions."""
        names = self._checkpoint_rulenames()
        if not names:
            return set()
        referenced: set[str] = set()
        rule_input = getattr(getattr(job, "rule", None), "input", ())
        for func in self._callable_items(rule_input):
            func_names = recompute_callable_names(func)
            if "checkpoints" in func_names:
                specific = func_names & names
                if specific:
                    referenced.update(specific)
                else:
                    referenced.update(names)
            else:
                referenced.update(func_names & names)
        return referenced

    def _uses_checkpoints(self, job: Any) -> bool:
        return bool(self._referenced_checkpoints(job))

    def _checkpoint_is_stale(
        self, job: Any, evaluation: _ParamEvaluation, state: _MetadataState
    ) -> tuple[bool, str]:
        if not job.output:
            return True, "checkpoint has no output"
        if not state.outputs_present:
            return True, "checkpoint output is missing"
        if not state.metadata_present:
            return True, "checkpoint output has no persisted metadata"
        if state.outdated:
            return True, "checkpoint metadata uses an outdated format"
        if state.incomplete:
            return True, "checkpoint output is marked incomplete"
        if state.params is None or set(state.params) != set(
            evaluation.serialized_params
        ):
            return True, "checkpoint parameters differ from recorded metadata"
        current_input = (
            evaluation.input_paths
            if evaluation.input_paths
            else self._current_input(job)
        )
        if state.input is None or tuple(state.input) != current_input:
            return True, "checkpoint input set differs from recorded metadata"
        return False, "checkpoint metadata matches the planned configuration"

    def _blocked_downstream(self, checkpoint_jobs: Iterable[Any]) -> set[Any]:
        blocked: set[Any] = set()
        queue: deque[Any] = deque()
        checkpoint_jobs = list(checkpoint_jobs)
        checkpoint_names = {
            str(getattr(job.rule, "name", "")) for job in checkpoint_jobs
        }
        for checkpoint in checkpoint_jobs:
            queue.extend(getattr(self.dag, "depending", {}).get(checkpoint, ()))
        # Jobs may consume a checkpoint via an input function without a
        # direct DAG edge.  Their input expansion is equally unknown.
        for job in getattr(self.dag, "jobs", ()):
            if self._referenced_checkpoints(job) & checkpoint_names:
                queue.append(job)
        while queue:
            job = queue.popleft()
            if job in blocked:
                continue
            blocked.add(job)
            queue.extend(getattr(self.dag, "depending", {}).get(job, ()))
        return blocked

    def refresh_after_checkpoint(
        self, completed: Iterable[Any], config: Optional[Mapping[str, Any]] = None
    ) -> dict[Any, Any]:
        """Refresh the DAG after real checkpoint completion.

        This is also available to callers that execute jobs through a separate
        route but keep planning on the same object.  It re-expands the direct
        dependents of the given checkpoints using ``config`` (default: the
        planner's base config) and returns a mapping of replaced jobs to their
        new instances.
        """
        if config is None:
            config = self._base_config
        replaced = self._refresh_completed_checkpoints(completed, config)
        with self._lock:
            self._checkpoint_tokens.update(self._checkpoint_token_map())
        return replaced

    def _refresh_completed_checkpoints(
        self, completed: Iterable[Any], config: Mapping[str, Any]
    ) -> dict[Any, Any]:
        """Re-expand jobs downstream of newly completed checkpoints.

        Only jobs whose real input expansion changed are replaced.  This keeps
        unaffected jobs (and their proven parameters) on the caller's existing
        object references.
        """
        replaced: dict[Any, Any] = {}
        completed = list(completed)
        if not self.refresh_dag or not completed:
            return replaced
        from snakemake.exceptions import IncompleteCheckpointException

        checkpoint_names = {
            str(getattr(job.rule, "name", "")) for job in completed
        }
        tracker = self._active_tracker
        previous = None
        if tracker is not None:
            previous = self.workflow.globals.get("config")
            self.workflow.globals["config"] = tracker
        try:
            for checkpoint in completed:
                for output in getattr(checkpoint, "output", ()):
                    if self._output_exists(output):
                        self.workflow.checkpoints.created_output.add(output)

            candidates: set[Any] = set()
            for checkpoint in completed:
                candidates.update(
                    getattr(self.dag, "depending", {}).get(checkpoint, ())
                )
            # A job may read a checkpoint from its input function without a
            # direct DAG edge to the checkpoint job.  Refresh those as well.
            for job in getattr(self.dag, "jobs", ()):
                if self._referenced_checkpoints(job) & checkpoint_names:
                    candidates.add(job)

            for dependent in candidates:
                if dependent not in getattr(self.dag, "_dependencies", {}):
                    continue
                old_inputs = tuple(str(f) for f in dependent.input)
                old_outputs = tuple(str(f) for f in dependent.output)
                try:
                    updated = self.workflow.async_run(dependent.updated())
                except IncompleteCheckpointException:
                    continue
                except Exception:
                    continue
                new_inputs = tuple(str(f) for f in updated.input)
                new_outputs = tuple(str(f) for f in updated.output)
                if old_inputs == new_inputs and old_outputs == new_outputs:
                    # The route did not change; keep the existing job and its
                    # already proven parameters.
                    continue
                try:
                    self.workflow.async_run(
                        self.dag.replace_job(dependent, updated, recursive=False)
                    )
                except Exception:
                    continue
                replaced[dependent] = updated
        finally:
            if tracker is not None:
                self.workflow.globals["config"] = previous
        return replaced

    # ------------------------------------------------------------------
    # Plan construction
    # ------------------------------------------------------------------
    @staticmethod
    def _job_key(job: Any) -> tuple[str, ...]:
        output = tuple(sorted(str(f) for f in getattr(job, "output", ())))
        return (str(getattr(getattr(job, "rule", None), "name", "?")),) + output

    def _ordered_jobs(self, *extra_jobs: Any) -> list[Any]:
        """Return the DAG's jobs in topological order (dependencies first)."""
        all_jobs = set(self.dag.jobs)
        all_jobs.update(extra_jobs)
        ordered: list[Any] = []
        seen: set[Any] = set()
        try:
            levels = list(
                self.dag.toposorted(
                    jobs=all_jobs
                )
            )
        except Exception:
            levels = [all_jobs]
        for level in levels:
            for job in sorted(
                level,
                key=lambda item: (
                    str(getattr(getattr(item, "rule", None), "name", "")),
                    tuple(sorted(str(f) for f in getattr(item, "output", ()))),
                ),
            ):
                if job in seen:
                    continue
                seen.add(job)
                ordered.append(job)
        for job in all_jobs:
            if job not in seen:
                ordered.append(job)
                seen.add(job)
        return ordered

    def _own_decision(
        self,
        job: Any,
        evaluation: _ParamEvaluation,
        state: _MetadataState,
    ) -> tuple[RecomputeDecision, str, list[str]]:
        evidence: list[str] = []
        if evaluation.incomplete:
            reason = (
                "parameter evaluation requires checkpoint evidence that is "
                "not yet available"
                if evaluation.checkpoint_incomplete
                else "parameter evaluation failed"
            )
            if evaluation.error:
                evidence.append(evaluation.error)
            return RecomputeDecision.INSUFFICIENT_EVIDENCE, reason, evidence

        matches, metadata_evidence, metadata_reason = self._metadata_evidence(
            job, evaluation, state
        )
        evidence.extend(metadata_evidence)
        if not matches:
            return RecomputeDecision.RECOMPUTE, metadata_reason, evidence
        if evaluation.seeded:
            evidence.append("parameters were already computed by the loaded DAG")
            return (
                RecomputeDecision.REUSE,
                "existing parameters are unchanged",
                evidence,
            )
        if evaluation.dependencies:
            evidence.append(
                "config dependencies: " + ", ".join(sorted(evaluation.dependencies))
            )
            return (
                RecomputeDecision.REUSE,
                "effective parameters are unchanged",
                evidence,
            )
        evidence.append("parameters do not read config")
        return RecomputeDecision.REUSE, "config independent", evidence

    def _build_plan(
        self, config: dict[str, Any], fingerprint: str, evidence_token: tuple[Any, ...]
    ) -> RecomputePlan:
        self._refresh_metadata_cache()
        jobs = self._ordered_jobs()
        evaluations: dict[Any, _ParamEvaluation] = {}
        states: dict[Any, _MetadataState] = {}
        own: dict[Any, tuple[RecomputeDecision, str, list[str]]] = {}

        def evaluate(job: Any, force: bool = False) -> None:
            if job in evaluations and not force:
                if job not in own:
                    own[job] = self._own_decision(
                        job, evaluations[job], states[job]
                    )
                return
            evaluations[job] = self._evaluation_for(job, config, fingerprint)
            states[job] = self._metadata_state(job)
            own[job] = self._own_decision(job, evaluations[job], states[job])

        with self._config_scope(config):
            for job in jobs:
                evaluate(job)

        # Determine which checkpoints are actually stale for this config.
        # A checkpoint is stale if its own evidence is stale or if any of its
        # upstream jobs will run (its inputs are then going to change).
        #
        # This pass follows topological order, so downstream decisions are not
        # needed to decide whether a checkpoint itself is stale.
        def checkpoint_staleness() -> tuple[set[Any], set[Any], dict[Any, JobPlan]]:
            dirty: set[Any] = set()
            blocked: set[Any] = set()
            result: dict[Any, JobPlan] = {}
            for job in jobs:
                decision, reason, evidence = own[job]
                dependency_entries = [
                    result[dependency]
                    for dependency in getattr(self.dag, "dependencies", {}).get(job, {})
                    if dependency in result
                ]
                blocked_deps = [
                    dep for dep in dependency_entries if dep.decision.is_blocked
                ]
                run_deps = [
                    dep for dep in dependency_entries if dep.decision.needs_run
                ]
                if blocked_deps:
                    decision = RecomputeDecision.INSUFFICIENT_EVIDENCE
                    reason = "upstream job lacks sufficient evidence"
                    evidence = list(evidence) + [
                        f"blocked upstream: {dep.rule}" for dep in blocked_deps
                    ]
                elif job in blocked:
                    decision = RecomputeDecision.INSUFFICIENT_EVIDENCE
                    reason = (
                        "dependency on a checkpoint whose old output cannot "
                        "prove the new configuration"
                    )
                    evidence = list(evidence) + [
                        "upstream checkpoint evidence is stale"
                    ]
                elif getattr(job, "is_checkpoint", False):
                    stale = (
                        decision is RecomputeDecision.RECOMPUTE
                        or bool(run_deps)
                    )
                    if stale:
                        dirty.add(job)
                        decision = RecomputeDecision.RECOMPUTE
                        reason = (
                            "checkpoint metadata or inputs do not prove the new "
                            "configuration"
                        )
                        evidence = list(evidence) + [
                            "downstream input expansion is not yet known"
                        ]
                        blocked.update(
                            self._blocked_downstream([job])
                        )
                    else:
                        reason = "checkpoint metadata proves the new configuration"
                elif run_deps and not decision.needs_run:
                    decision = RecomputeDecision.PROPAGATED
                    reason = "upstream dependency will be recomputed"
                    evidence = list(evidence) + [
                        f"upstream will run: {dep.rule}" for dep in run_deps
                    ]
                elif run_deps and decision is RecomputeDecision.RECOMPUTE:
                    evidence = list(evidence) + [
                        f"upstream will run: {dep.rule}" for dep in run_deps
                    ]
                result[job] = JobPlan(
                    job=job,
                    decision=decision,
                    reason=reason,
                    rule=str(getattr(getattr(job, "rule", None), "name", "?")),
                    output=tuple(str(f) for f in getattr(job, "output", ())),
                    params=MappingProxyType(dict(evaluations[job].params)),
                    recorded_params=states[job].params,
                    config_dependencies=evaluations[job].dependencies,
                    evidence=tuple(evidence),
                    checkpoint=bool(getattr(job, "is_checkpoint", False)),
                )
            return dirty, blocked, result

        dirty, blocked, result = checkpoint_staleness()

        # Detect checkpoints that have actually been executed since the last
        # planning call.  Per-checkpoint tokens include persisted parameters,
        # inputs, completion time and output existence, so a real checkpoint
        # run is distinguishable from a mere preview.
        tokens_now = self._checkpoint_tokens_now()
        token_map = {
            self._job_key(job): token for job, token in tokens_now.items()
        }
        completed_checkpoints: set[Any] = set()
        for job in tokens_now:
            key = self._job_key(job)
            previous_token = self._checkpoint_tokens.get(key)
            if previous_token is not None and previous_token != token_map[key]:
                completed_checkpoints.add(job)
                self._checkpoint_completed.add(job)
                self._checkpoint_baseline.pop(job, None)
        # Remember the tokens observed in this planning call.
        self._checkpoint_tokens.update(token_map)
        # A checkpoint that was stale before and whose token is unchanged is
        # still pending; make sure we keep a baseline for callers that inspect it.
        for job in dirty:
            self._checkpoint_baseline.setdefault(job, evidence_token)

        if completed_checkpoints:
            # Treat completed checkpoints as reusable and refresh the real
            # input expansions of their direct dependents.
            dirty = dirty - completed_checkpoints
            for job in completed_checkpoints:
                state = states[job]
                own[job] = (
                    RecomputeDecision.REUSE,
                    "checkpoint has been re-executed and now proves the new route",
                    ["new checkpoint metadata is current"],
                )
            with self._config_scope(config):
                replaced = self._refresh_completed_checkpoints(
                    completed_checkpoints, config
                )
                if replaced:
                    jobs = self._ordered_jobs(*replaced.values())
                    for old_job, new_job in replaced.items():
                        # The route changed, so the replacement job must be
                        # evaluated from scratch with its new inputs.
                        evaluations.pop(new_job, None)
                        states.pop(new_job, None)
                        own.pop(new_job, None)
                        evaluate(new_job, force=True)
                    # Replacement jobs may have new dependencies that are not
                    # yet part of the plan's job list.
                    for job in jobs:
                        evaluate(job)
            # Downstream of the completed checkpoint, the old expansion has
            # been replaced by a fresh one.  Recompute the final pass below.
            dirty, blocked, result = checkpoint_staleness()
            self._checkpoint_tokens.update(self._checkpoint_token_map())
            self._checkpoint_completed.update(completed_checkpoints)

        entries: "OrderedDict[Any, JobPlan]" = OrderedDict()
        by_output: dict[str, JobPlan] = {}
        decisions: dict[Any, JobPlan] = {}

        for job in jobs:
            entry = result[job]
            if entry.jobid is None:
                try:
                    jobid = self.dag.jobid(job)
                except Exception:
                    jobid = None
                entry = JobPlan(
                    job=job,
                    decision=entry.decision,
                    reason=entry.reason,
                    rule=entry.rule,
                    jobid=jobid,
                    output=entry.output,
                    input=entry.input,
                    params=entry.params,
                    non_derived_params=entry.non_derived_params,
                    recorded_params=entry.recorded_params,
                    config_dependencies=entry.config_dependencies,
                    evidence=entry.evidence,
                    checkpoint=entry.checkpoint,
                )
            decisions[job] = entry
            entries[job] = entry
            for output in entry.output:
                by_output[output] = entry

        return RecomputePlan(
            config=MappingProxyType(copy.deepcopy(config)),
            config_fingerprint=fingerprint,
            entries=_PlanEntries(entries),
            by_output=MappingProxyType(by_output),
            evidence_token=evidence_token,
        )


def plan_recompute(
    workflow_or_dag: Any,
    config: Any = None,
    *,
    base_config: Optional[Mapping[str, Any]] = None,
    refresh_dag: bool = True,
    **kwargs: Any,
) -> RecomputePlan:
    """Create a planner for ``workflow_or_dag`` and plan ``config``."""
    planner = RecomputePlanner(
        workflow_or_dag,
        base_config=base_config,
        refresh_dag=refresh_dag,
    )
    return planner.plan(config, **kwargs)


# Alternative names for the planner and its result, useful for callers that
# model this as a "planning session".
PlanningSession = RecomputePlanner
RecomputePlanningSession = RecomputePlanner
JobRecomputePlan = JobPlan
RecomputeResult = RecomputePlan
