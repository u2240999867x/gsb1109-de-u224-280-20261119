"""Diagnostics for the selection of producer rules during DAG construction.

The classes in this module deliberately only store observations that were made
while the real DAG was built.  Rendering a report must never evaluate input
functions again, because input functions may have side effects and need not be
idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


class SelectionOutcome:
    SELECTED = "selected"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"
    PRESENT = "satisfied by existing file"
    PENDING = "pending"


class CandidateStatus:
    SELECTED = "selected"
    REJECTED = "rejected"
    NOT_EVALUATED = "not evaluated"
    AMBIGUOUS = "ambiguous"
    CANDIDATE = "candidate"


class RejectionReason:
    WILDCARD_CONSTRAINT = "wildcard-constraint"
    WILDCARD_RESOLUTION = "wildcard-resolution"
    INPUT_FUNCTION = "input-function"
    MISSING_INPUT = "missing-input"
    DEPENDENCY_FAILED = "dependency-failed"
    CYCLIC_GRAPH = "cyclic-graph"
    PERIODIC_WILDCARDS = "periodic-wildcards"
    RULEORDER = "ruleorder"
    AMBIGUITY = "ambiguity"
    NO_PRODUCER = "no-producer"
    NO_RULE = "no-rule"
    EVALUATION_ERROR = "evaluation-error"


def format_wildcards(wildcards: Optional[Mapping[str, object]]) -> str:
    if not wildcards:
        return ""
    return ", ".join(f"{name}={value}" for name, value in sorted(wildcards.items()))


@dataclass
class RuleSelectionCandidate:
    """One considered producer rule for a target file.

    ``path`` describes where in the dependency graph the target was needed:
    the first entry is the root target, the last entry is ``target``.
    """

    target: str
    rule: str
    status: str
    reason: Optional[str] = None
    detail: str = ""
    wildcards: Optional[Mapping[str, object]] = None
    evaluated: bool = False
    path: Tuple[str, ...] = ()
    exception_type: Optional[str] = None

    def format_line(self) -> str:
        parts = [f"target {self.target}: rule {self.rule}"]
        wildcards = format_wildcards(self.wildcards)
        if wildcards:
            parts.append(f"[{wildcards}]")
        parts.append(f"-> {self.status}")
        if self.status == CandidateStatus.NOT_EVALUATED:
            parts.append(f"({self.reason})")
        elif self.reason:
            parts.append(f"reason={self.reason}")
        if self.detail:
            parts.append("- " + " ".join(str(self.detail).split()))
        if len(self.path) > 1:
            parts.append("path: " + " -> ".join(self.path))
        return " ".join(parts)


@dataclass
class RuleSelectionTrace:
    """The selection decision for one target file on one dependency path."""

    target: str
    path: Tuple[str, ...]
    candidates: List[RuleSelectionCandidate] = field(default_factory=list)
    outcome: str = SelectionOutcome.PENDING
    selected_rule: Optional[str] = None
    detail: str = ""

    def add_candidate(self, candidate: RuleSelectionCandidate) -> None:
        self.candidates.append(candidate)

    def candidate(self, rule: str) -> Optional[RuleSelectionCandidate]:
        for candidate in self.candidates:
            if candidate.rule == rule:
                return candidate
        return None

    def format_lines(self) -> Iterable[str]:
        header = f"target {self.target}"
        if len(self.path) > 1:
            header += ": path " + " -> ".join(self.path)
        header += f": {self.outcome}"
        if self.selected_rule:
            header += f" rule {self.selected_rule}"
        if self.detail:
            header += f" ({self.detail})"
        yield header
        for candidate in self.candidates:
            yield "  " + candidate.format_line()


class RuleSelectionRecorder:
    """Collects rule selection decisions while the DAG is being constructed."""

    def __init__(self) -> None:
        self.traces: List[RuleSelectionTrace] = []
        self._by_key: Dict[Tuple[Tuple[str, ...], str], RuleSelectionTrace] = {}

    def begin(self, target: str, path: Sequence[str]) -> RuleSelectionTrace:
        path = tuple(path) or (target,)
        key = (path, target)
        try:
            return self._by_key[key]
        except KeyError:
            trace = RuleSelectionTrace(target=target, path=path)
            self._by_key[key] = trace
            self.traces.append(trace)
            return trace

    def find(self, target: str, path: Sequence[str]) -> Optional[RuleSelectionTrace]:
        return self._by_key.get((tuple(path) or (target,), target))

    def record(
        self,
        target: str,
        rule: str,
        path: Sequence[str],
        status: str,
        reason: Optional[str] = None,
        detail: str = "",
        wildcards: Optional[Mapping[str, object]] = None,
        evaluated: bool = False,
        exception_type: Optional[str] = None,
    ) -> RuleSelectionCandidate:
        trace = self.begin(target, path)
        existing = trace.candidate(rule)
        if existing is not None:
            existing.status = status
            existing.reason = reason
            existing.detail = detail
            existing.wildcards = wildcards
            existing.evaluated = evaluated
            existing.exception_type = exception_type
            return existing
        candidate = RuleSelectionCandidate(
            target=target,
            rule=rule,
            status=status,
            reason=reason,
            detail=detail,
            wildcards=wildcards,
            evaluated=evaluated,
            path=trace.path,
            exception_type=exception_type,
        )
        trace.add_candidate(candidate)
        return candidate

    def finish(
        self,
        target: str,
        path: Sequence[str],
        outcome: str,
        selected_rule: Optional[str] = None,
        detail: str = "",
    ) -> RuleSelectionTrace:
        trace = self.begin(target, path)
        trace.outcome = outcome
        trace.selected_rule = selected_rule
        trace.detail = detail
        return trace

    def report_lines(self, targets: Optional[Iterable[str]] = None) -> List[str]:
        if targets is not None:
            targets = set(targets)
            traces = [trace for trace in self.traces if trace.target in targets]
        else:
            traces = self.traces
        if not traces:
            return ["No rule selection decisions were recorded."]
        lines = ["Rule selection report:"]
        for trace in traces:
            lines.extend(trace.format_lines())
        return lines

    def report(self, targets: Optional[Iterable[str]] = None) -> str:
        return "\n".join(self.report_lines(targets))

    def as_dict(self, targets: Optional[Iterable[str]] = None) -> List[dict]:
        """Return a JSON-serializable representation of the recorded decisions."""
        if targets is not None:
            targets = set(targets)
            traces = [trace for trace in self.traces if trace.target in targets]
        else:
            traces = self.traces
        return [
            {
                "target": trace.target,
                "path": list(trace.path),
                "outcome": trace.outcome,
                "selected_rule": trace.selected_rule,
                "detail": trace.detail,
                "candidates": [
                    {
                        "rule": candidate.rule,
                        "status": candidate.status,
                        "reason": candidate.reason,
                        "detail": candidate.detail,
                        "wildcards": (
                            dict(candidate.wildcards) if candidate.wildcards else None
                        ),
                        "evaluated": candidate.evaluated,
                        "exception_type": candidate.exception_type,
                    }
                    for candidate in trace.candidates
                ],
            }
            for trace in traces
        ]
