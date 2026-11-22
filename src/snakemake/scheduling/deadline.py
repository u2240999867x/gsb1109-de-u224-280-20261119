"""Deadline based job dispatch.

This module provides an optional scheduler plugin that orders ready jobs by

1. their original Snakemake priority (highest first),
2. their absolute deadline (earliest first, jobs without a deadline last), and
3. the rule name followed by the sorted wildcard values (for a stable order).

Deadlines are absolute UNIX timestamps. They can be assigned per rule and
wildcard combination, e.g. ``sample=foo``.  A deadline that has already passed
is not treated specially: the job is still dispatched, because finishing late is
usually still preferable to not finishing at all.

Job groups are treated as a single unit.  The deadline of a group is the
earliest deadline of any of its member jobs.

The actual resource admission (does a job fit?) is done while selecting jobs.
Jobs that are not ready are never passed to the scheduler, and a job that does
not fit into the currently available resources is skipped without consuming a
resource slot.  Later (smaller) jobs may still be selected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Iterable,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from snakemake_interface_scheduler_plugins.base import SchedulerBase
from snakemake_interface_scheduler_plugins.interfaces.jobs import (
    GroupJobSchedulerInterface,
    JobSchedulerInterface,
)
from snakemake_interface_scheduler_plugins.settings import SchedulerSettingsBase


def _wildcards_of(job: Any) -> Mapping[str, Any]:
    wildcards = getattr(job, "wildcards_dict", None)
    if wildcards is None:
        wildcards = getattr(job, "wildcards", None)
    if wildcards is None:
        return {}
    if isinstance(wildcards, Mapping):
        return wildcards
    # Wildcards objects behave like mappings but do not derive from Mapping.
    try:
        return dict(wildcards.items())
    except AttributeError:
        return {}


def _rule_name_of(job: Any) -> str:
    rule = getattr(job, "rule", None)
    if rule is not None:
        name = getattr(rule, "name", None)
        if name is not None:
            return str(name)
    name = getattr(job, "name", None)
    return "" if name is None else str(name)


def _is_group(job: Any) -> bool:
    is_group = getattr(job, "is_group", None)
    if callable(is_group):
        try:
            return bool(is_group())
        except TypeError:
            return bool(is_group)
    return is_group is True or isinstance(job, GroupJobSchedulerInterface)


def _group_members(job: Any) -> Sequence[Any]:
    jobs = getattr(job, "jobs", None)
    if callable(jobs):
        jobs = jobs()
    if jobs is None:
        return ()
    return tuple(jobs)


def wildcard_sort_values(wildcards: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the wildcard values in a deterministic, name-sorted order."""
    return tuple(str(value) for _, value in sorted(wildcards.items()))


def parse_absolute_time(value: Any) -> float:
    """Parse an absolute deadline.

    Accepts UNIX timestamps (int/float or numeric strings) and ISO-8601
    strings.  Naive ISO timestamps are interpreted as UTC.
    """
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        value = value.strip()
        try:
            return float(value)
        except ValueError:
            pass
        iso = value
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError as e:
            raise ValueError(
                f"Invalid absolute deadline {value!r}. Expected a UNIX timestamp "
                "or an ISO-8601 datetime."
            ) from e
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise TypeError(
        f"Invalid absolute deadline {value!r}. Expected a number, numeric string, "
        "ISO-8601 string, or datetime."
    )


def _normalize_wildcards(
    wildcards: Optional[Mapping[str, Any]],
) -> tuple[tuple[str, str], ...]:
    if not wildcards:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in wildcards.items()))


@dataclass(frozen=True)
class Deadline:
    """An absolute deadline for one (or all) instances of a rule."""

    rule: str
    at: float
    wildcards: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "rule", str(self.rule))
        object.__setattr__(self, "at", parse_absolute_time(self.at))
        object.__setattr__(
            self, "wildcards", _normalize_wildcards(dict(self.wildcards))
        )

    @property
    def wildcards_dict(self) -> dict[str, str]:
        return dict(self.wildcards)

    def matches(self, rule: str, wildcards: Mapping[str, Any]) -> bool:
        if self.rule != str(rule):
            return False
        actual = {str(k): str(v) for k, v in wildcards.items()}
        return all(actual.get(k) == v for k, v in self.wildcards)

    @classmethod
    def parse(cls, spec: Union[str, "Deadline", Mapping[str, Any]]) -> "Deadline":
        """Parse a deadline specification.

        String form::

            RULE[@WILDCARD=VALUE[,WILDCARD=VALUE...]][@]ABSOLUTE_TIME

        For convenience, the wildcard part may also be omitted, in which case
        the deadline applies to every instance of the rule.  Examples::

            align:sample=foo@1763852400
            align@2026-11-23T00:00:00Z
        """
        if isinstance(spec, Deadline):
            return spec
        if isinstance(spec, Mapping):
            if "rule" not in spec:
                raise ValueError(f"Deadline mapping needs a 'rule' key: {spec!r}")
            wildcards = spec.get("wildcards") or spec.get("wildcard") or {}
            at = spec.get("at", spec.get("deadline"))
            if at is None:
                raise ValueError(
                    f"Deadline mapping needs an 'at'/'deadline' key: {spec!r}"
                )
            return cls(
                rule=str(spec["rule"]), at=at, wildcards=_normalize_wildcards(wildcards)
            )
        if not isinstance(spec, str):
            raise TypeError(f"Unsupported deadline specification: {spec!r}")

        text = spec.strip()
        if not text:
            raise ValueError("Empty deadline specification")

        time_part: Optional[str] = None
        identity = text
        # Split at the last '@'. This keeps ISO timezone offsets (which contain
        # '+', not '@') intact.
        if "@" in text:
            identity, time_part = text.rsplit("@", 1)
        if not time_part:
            # Maybe the spec is "rule:wildcards@time"; rsplit above handles it.
            raise ValueError(
                f"Deadline specification {spec!r} is missing the absolute time "
                "(expected RULE[:WILDCARD=VALUE,...]@TIME)."
            )

        wildcards: tuple[tuple[str, str], ...] = ()
        rule = identity
        if ":" in identity:
            rule, wildcard_part = identity.split(":", 1)
            wildcards = _parse_wildcard_part(wildcard_part)
        return cls(rule=rule, at=time_part, wildcards=wildcards)


def _parse_wildcard_part(part: str) -> tuple[tuple[str, str], ...]:
    part = part.strip()
    if not part:
        return ()
    result = []
    for item in part.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(
                f"Invalid wildcard constraint {item!r}; expected NAME=VALUE."
            )
        name, value = item.split("=", 1)
        result.append((name.strip(), value.strip()))
    return tuple(sorted(result))


def normalize_deadlines(
    deadlines: Optional[
        Union[
            Iterable[Union[str, Deadline, Mapping[str, Any]]],
            Mapping[str, Any],
        ]
    ],
) -> list[Deadline]:
    """Normalize the many accepted deadline representations into a list."""
    if deadlines is None:
        return []
    if isinstance(deadlines, Deadline):
        return [deadlines]
    if isinstance(deadlines, str):
        return [Deadline.parse(deadlines)]
    if isinstance(deadlines, Mapping):
        # Two convenient mapping forms:
        #   {"rule": 1234.0}
        #   {"rule": {"sample": {"foo": 1234.0}}}
        #   {"rule": {"wildcards": {...}, "deadline": 1234.0}}
        result = []
        for rule, value in deadlines.items():
            if isinstance(value, Mapping):
                if "at" in value or "deadline" in value:
                    result.append(
                        Deadline.parse({"rule": rule, **dict(value)})
                    )
                    continue
                # rule -> {wildcard_name: {value: deadline}}
                for wildcard_name, by_value in value.items():
                    if not isinstance(by_value, Mapping):
                        # rule -> {wildcard_name: deadline} would be ambiguous;
                        # treat this as a rule-wide deadline keyed by name.
                        result.append(
                            Deadline.parse(
                                {
                                    "rule": rule,
                                    "wildcards": {wildcard_name: by_value},
                                    "deadline": by_value,
                                }
                            )
                        )
                        continue
                    for wildcard_value, at in by_value.items():
                        result.append(
                            Deadline(
                                rule=rule,
                                at=at,
                                wildcards=((str(wildcard_name), str(wildcard_value)),),
                            )
                        )
            else:
                result.append(Deadline(rule=rule, at=value))
        return result
    return [Deadline.parse(item) for item in deadlines]


@dataclass
class DeadlineRegistry:
    """Look up absolute deadlines by rule name and wildcard values."""

    deadlines: list[Deadline] = field(default_factory=list)

    def __post_init__(self):
        self.deadlines = list(self.deadlines)

    @classmethod
    def from_any(cls, deadlines: Any) -> "DeadlineRegistry":
        return cls(normalize_deadlines(deadlines))

    def add(self, deadline: Union[str, Deadline, Mapping[str, Any]]) -> None:
        self.deadlines.append(Deadline.parse(deadline))

    def __bool__(self) -> bool:
        return bool(self.deadlines)

    def __len__(self) -> int:
        return len(self.deadlines)

    def deadline_for(
        self, rule: Union[str, Any], wildcards: Optional[Mapping[str, Any]] = None
    ) -> Optional[float]:
        """Return the earliest matching deadline, or ``None``.

        ``rule`` may also be a job object, in which case the rule name and
        wildcards are taken from it (groups use the earliest member deadline).
        """
        if not isinstance(rule, str):
            return self.deadline_for_job(rule)
        wildcards = wildcards or {}
        matching = [
            deadline.at
            for deadline in self.deadlines
            if deadline.matches(rule, wildcards)
        ]
        return min(matching) if matching else None

    def deadline_for_job(self, job: Any) -> Optional[float]:
        if _is_group(job):
            deadlines = [
                self.deadline_for_job(member) for member in _group_members(job)
            ]
            known = [deadline for deadline in deadlines if deadline is not None]
            if known:
                return min(known)
            explicit = getattr(job, "deadline", None)
            return None if explicit is None else parse_absolute_time(explicit)
        explicit = getattr(job, "deadline", None)
        if explicit is not None and not callable(explicit):
            return parse_absolute_time(explicit)
        return self.deadline_for(_rule_name_of(job), _wildcards_of(job))

    def __call__(self, job: Any) -> Optional[float]:
        return self.deadline_for_job(job)


def dispatch_identity(job: Any) -> tuple[str, tuple[str, ...]]:
    """Return the stable tie-breaking identity (rule name, sorted wildcards)."""
    if _is_group(job):
        members = _group_members(job)
        if members:
            return min(
                (dispatch_identity(member) for member in members),
                key=lambda identity: identity,
            )
        return (getattr(job, "name", "<group>"), ())
    return (_rule_name_of(job), wildcard_sort_values(_wildcards_of(job)))


def _group_tiebreak(
    job: Any, registry: Optional[DeadlineRegistry]
) -> tuple[float, str, tuple[str, ...]]:
    """Pick the member that determines a group's sort position.

    The group is represented by its most urgent member: earliest deadline
    first, then rule name and sorted wildcard values.
    """
    best = None
    for member in _group_members(job):
        deadline = (
            registry.deadline_for_job(member) if registry is not None else None
        )
        rule_name, wildcards = dispatch_identity(member)
        key = (
            float("inf") if deadline is None else float(deadline),
            rule_name,
            wildcards,
        )
        if best is None or key < best:
            best = key
    if best is None:
        return (float("inf"), getattr(job, "name", "<group>"), ())
    return best


def dispatch_sort_key(
    job: Any,
    deadline: Optional[float] = None,
    deadline_registry: Optional[DeadlineRegistry] = None,
) -> tuple:
    """Sort key implementing the deadline dispatch policy.

    Order:
      1. original priority, highest first;
      2. jobs with a deadline before jobs without one;
      3. earliest deadline first (expired deadlines are *not* filtered out);
      4. rule name;
      5. sorted wildcard values.

    For a job group, its deadline is the earliest member deadline and its
    tie-break identity is that of the most urgent member.
    """
    priority = getattr(job, "priority", 0)
    # Job.priority is an int. Be defensive if a plugin exposes a sequence.
    if isinstance(priority, (tuple, list)):
        priority = priority[0] if priority else 0
    if _is_group(job) and _group_members(job):
        member_deadline, rule_name, wildcard_values = _group_tiebreak(
            job, deadline_registry
        )
        if deadline is None:
            deadline = None if member_deadline == float("inf") else member_deadline
    else:
        if deadline is None and deadline_registry is not None:
            deadline = deadline_registry.deadline_for_job(job)
        rule_name, wildcard_values = dispatch_identity(job)
    if deadline is None:
        return (0, -priority, 1, 0.0, rule_name, wildcard_values)
    return (0, -priority, 0, float(deadline), rule_name, wildcard_values)


def job_attr(job: Any, name: str) -> Any:
    value = getattr(job, name, False)
    if callable(value):
        try:
            return value()
        except TypeError:
            return value
    return value


def _fit(
    resources: Mapping[str, Union[str, int]],
    available: Mapping[str, Union[str, int]],
) -> bool:
    for name, value in resources.items():
        if isinstance(value, str):
            continue
        capacity = available.get(name)
        if capacity is None:
            continue
        if isinstance(capacity, str):
            continue
        try:
            if value > capacity:
                return False
        except TypeError:
            # Non-numeric resources cannot be compared; ignore them here.
            continue
    return True


def _subtract(
    resources: Mapping[str, Union[str, int]],
    available: dict[str, Union[str, int]],
) -> None:
    for name, value in resources.items():
        if isinstance(value, str):
            continue
        capacity = available.get(name)
        if capacity is None or isinstance(capacity, str):
            continue
        try:
            available[name] = capacity - value
        except TypeError:
            continue


@dataclass
class SchedulerSettings(SchedulerSettingsBase):
    deadlines: list[str] = field(
        default_factory=list,
        metadata={
            "help": "Absolute deadlines of the form "
            "RULE[:WILDCARD=VALUE,...]@UNIXTIME (or ISO-8601). Can be given "
            "multiple times.",
            "nargs": "+",
            "type": str,
        },
    )
    deadline_file: Optional[Path] = field(
        default=None,
        metadata={
            "help": "JSON or YAML file containing absolute deadlines.",
            "type": Path,
        },
    )

    def __post_init__(self):
        if self.deadlines is None:
            self.deadlines = []

    def get_scheduler_settings(self) -> "SchedulerSettings":
        return self


def _append_specs(specs: list[Any], deadlines: Any) -> None:
    """Append deadline specifications, preserving mappings as one value."""
    if not deadlines:
        return
    if isinstance(deadlines, Mapping):
        specs.append(deadlines)
    elif isinstance(deadlines, (str, Deadline)):
        specs.append(deadlines)
    else:
        specs.extend(deadlines)


def _specs_from(scheduling_settings: Any) -> list[Any]:
    """Collect deadline specifications from the generic scheduler settings."""
    specs: list[Any] = []
    _append_specs(specs, getattr(scheduling_settings, "deadlines", None))
    deadline_file = getattr(scheduling_settings, "deadline_file", None)
    if deadline_file:
        specs.extend(load_deadlines(deadline_file))
    return specs


class Scheduler(SchedulerBase):
    """Scheduler plugin implementing deadline-first dispatch."""

    is_deadline_scheduler = True

    def __post_init__(self):
        specs: list[Any] = []
        if self.settings is not None:
            # Plugin-specific settings (set via --scheduler-deadline-...).
            _append_specs(specs, getattr(self.settings, "deadlines", None))
            deadline_file = getattr(self.settings, "deadline_file", None)
            if deadline_file is not None:
                specs.extend(load_deadlines(deadline_file))
        # Generic settings, so that DAGApi.execute_workflow(scheduling_settings=...)
        # works without a plugin-specific settings object.
        workflow = getattr(self.dag, "workflow", None)
        scheduling_settings = getattr(workflow, "scheduling_settings", None)
        if scheduling_settings is not None:
            for spec in _specs_from(scheduling_settings):
                if spec not in specs:
                    specs.append(spec)
        self.registry = DeadlineRegistry.from_any(specs)
    def select_jobs(
        self,
        selectable_jobs: Sequence[JobSchedulerInterface],
        remaining_jobs: Sequence[JobSchedulerInterface],
        available_resources: Mapping[str, Union[int, str]],
        input_sizes: Mapping[Any, int],
    ) -> Optional[Sequence[JobSchedulerInterface]]:
        ordered = sorted(
            selectable_jobs,
            key=lambda job: dispatch_sort_key(job, deadline_registry=self.registry),
        )
        available = dict(available_resources)
        selected = []
        for job in ordered:
            if job_attr(job, "is_local"):
                # Local rules are executed by the local executor even in remote
                # mode. They must not be blocked by remote admission accounting
                # and do not consume a remote submission permit.
                selected.append(job)
                continue
            resources = job.scheduler_resources
            if not _fit(resources, available):
                # Do not consume anything for a job that does not fit. A later,
                # smaller job may still fit.
                continue
            selected.append(job)
            _subtract(resources, available)
        return selected


# Name expected by external code spelling the plugin class differently.
DeadlineScheduler = Scheduler


def load_deadlines(path: Union[str, Path]) -> list[Deadline]:
    """Load deadlines from a JSON or YAML file.

    The file may contain a list of deadline specifications or a mapping in one
    of the forms accepted by :func:`normalize_deadlines`.
    """
    path = Path(path)
    text = path.read_text()
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(text)
    else:
        import json

        data = json.loads(text)
    return normalize_deadlines(data)
