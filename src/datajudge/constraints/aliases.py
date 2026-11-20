from __future__ import annotations

from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa

from ..data_source import DataSource, TableDataSource
from ..db_access import DataReference
from .base import Constraint, TestResult

StoreId = tuple[Any, Any]


def _stable(value: Any) -> tuple[int, Any]:
    """Return a deterministic, total ordering key for arbitrary database values."""
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, str(value))
    if isinstance(value, (int, float)):
        return (2, value)
    if isinstance(value, str):
        return (3, value)
    return (4, repr(value))


def _node_key(node: StoreId) -> tuple[tuple[int, Any], ...]:
    return tuple(_stable(value) for value in node)


def _node_string(node: StoreId) -> str:
    namespace, code = node
    return f"{namespace!r}.{code!r}"


def coerce_data_source(value: DataSource | str | tuple) -> DataSource:
    """Turn a ``DataSource``, table name or ``(db, schema, table)`` into a source."""
    if isinstance(value, DataSource):
        return value
    if isinstance(value, str):
        return TableDataSource(db_name="", table_name=value)
    if isinstance(value, tuple):
        if len(value) == 2:
            db_name, table_name = value
            return TableDataSource(
                db_name=db_name,
                table_name=table_name,
                schema_name=None,
            )
        if len(value) == 3:
            db_name, schema_name, table_name = value
            return TableDataSource(
                db_name=db_name,
                table_name=table_name,
                schema_name=schema_name,
            )
    raise TypeError(
        "Expected a DataSource, a table name, or a (db, schema, table) tuple."
    )


@dataclass(frozen=True)
class AliasDependencyConfig:
    """Column names and validation conventions for alias-aware dependencies.

    The defaults describe a registration table with ``source_namespace`` and
    ``source_code`` columns, an alias table with ``source_namespace``, ``alias``,
    ``target_namespace`` and ``target_code`` columns, and an optional approved
    region-equivalence table with ``region`` and ``canonical_region`` columns.

    ``detail_identity_columns`` identifies a registration row in evidence. When
    it is ``None``, every column of the detail source is retained, so the
    original row can be looked up again. ``detail_report_id_column`` is only
    used to break ties deterministically between otherwise identical identities.
    """

    detail_namespace_column: str = "source_namespace"
    detail_code_column: str = "source_code"
    detail_region_column: str = "region"
    detail_identity_columns: tuple[str, ...] | None = None
    detail_report_id_column: str | None = None
    detail_confirmed_column: str | None = None
    detail_confirmed_values: tuple[Any, ...] = (
        True,
        1,
        "true",
        "TRUE",
        "yes",
        "Y",
    )

    alias_namespace_column: str = "source_namespace"
    alias_code_column: str = "alias"
    alias_target_namespace_column: str = "target_namespace"
    alias_target_code_column: str = "target_code"

    equivalence_region_column: str = "region"
    equivalence_canonical_column: str = "canonical_region"

    unknown_region_values: tuple[Any, ...] = (None, "")
    strip_region_whitespace: bool = False
    fail_on_unknown: bool = False
    fail_on_unconfirmable: bool = False
    max_evidence_per_region: int = 1

    def __post_init__(self) -> None:
        if self.max_evidence_per_region < 1:
            raise ValueError("max_evidence_per_region must be at least one.")


@dataclass(frozen=True)
class RegistrationEvidence:
    """One original registration row that supports an attribution."""

    row: Mapping[str, Any]
    row_key: tuple[Any, ...]
    source_namespace: Any
    source_code: Any
    raw_region: Any
    canonical_region: Any | None
    store: StoreId | None
    alias_path: tuple[StoreId, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "row": dict(self.row),
            "row_key": self.row_key,
            "source_namespace": self.source_namespace,
            "source_code": self.source_code,
            "raw_region": self.raw_region,
            "canonical_region": self.canonical_region,
            "store": self.store,
            "alias_path": self.alias_path,
        }


@dataclass(frozen=True)
class UnconfirmableRecord:
    """A registration row whose store or region cannot be determined uniquely."""

    reason: str
    evidence: RegistrationEvidence
    candidate_stores: tuple[StoreId, ...] = ()
    candidate_paths: tuple[tuple[StoreId, tuple[StoreId, ...]], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "evidence": self.evidence.as_dict(),
            "candidate_stores": self.candidate_stores,
            "candidate_paths": self.candidate_paths,
        }


@dataclass(frozen=True)
class StoreRegionRelation:
    """A deduplicated (store, canonical region) relation with lookback evidence."""

    store: StoreId
    region: Any
    evidence: tuple[RegistrationEvidence, ...]
    raw_regions: tuple[Any, ...]
    candidate_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "store": self.store,
            "region": self.region,
            "evidence": [item.as_dict() for item in self.evidence],
            "raw_regions": self.raw_regions,
            "candidate_count": self.candidate_count,
        }


@dataclass(frozen=True)
class StoreConflict:
    """A store with more than one canonical region and the supporting rows."""

    store: StoreId
    regions: tuple[Any, ...]
    evidence: tuple[RegistrationEvidence, ...]
    candidate_counts: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "store": self.store,
            "regions": self.regions,
            "evidence": [item.as_dict() for item in self.evidence],
            "candidate_counts": self.candidate_counts,
        }


@dataclass(frozen=True)
class AliasDependencyReport:
    """The complete, deterministic result of an alias-aware dependency check."""

    conflicts: tuple[StoreConflict, ...] = ()
    unknown_regions: tuple[RegistrationEvidence, ...] = ()
    unconfirmable: tuple[UnconfirmableRecord, ...] = ()
    relations: tuple[StoreRegionRelation, ...] = ()
    detail_row_count: int = 0
    evaluated_row_count: int = 0
    _failure_message: str | None = field(default=None, repr=False)

    @property
    def satisfied(self) -> bool:
        return not self.conflicts

    @property
    def failure_message(self) -> str | None:
        return self._failure_message

    def as_dict(self) -> dict[str, Any]:
        return {
            "satisfied": self.satisfied,
            "detail_row_count": self.detail_row_count,
            "evaluated_row_count": self.evaluated_row_count,
            "conflicts": [conflict.as_dict() for conflict in self.conflicts],
            "unknown_regions": [item.as_dict() for item in self.unknown_regions],
            "unconfirmable": [item.as_dict() for item in self.unconfirmable],
            "relations": [relation.as_dict() for relation in self.relations],
        }


class _AliasGraph:
    def __init__(self, edges: Iterable[tuple[StoreId, StoreId]]):
        adjacency: dict[StoreId, set[StoreId]] = defaultdict(set)
        nodes: set[StoreId] = set()
        for source, target in edges:
            nodes.add(source)
            nodes.add(target)
            if source != target:
                adjacency[source].add(target)
        self._adjacency = {
            node: tuple(sorted(targets, key=_node_key))
            for node, targets in adjacency.items()
        }
        self._nodes = nodes
        self._terminals = {node for node in nodes if not self._adjacency.get(node)}
        self._cache: dict[StoreId, dict[StoreId, tuple[StoreId, ...]]] = {}

    def resolve(self, start: StoreId) -> dict[StoreId, tuple[StoreId, ...]]:
        """Return reachable terminal stores and one deterministic path each."""
        if start in self._cache:
            return self._cache[start]

        if start not in self._nodes or start in self._terminals:
            single: dict[StoreId, tuple[StoreId, ...]] = {start: (start,)}
            self._cache[start] = single
            return single

        queue: deque[tuple[StoreId, tuple[StoreId, ...]]] = deque([(start, (start,))])
        visited: set[StoreId] = set()
        resolved: dict[StoreId, tuple[StoreId, ...]] = {}
        while queue:
            node, path = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            if node in self._terminals:
                resolved[node] = path
                continue
            for target in self._adjacency.get(node, ()):
                if target in visited:
                    continue
                queue.append((target, path + (target,)))

        self._cache[start] = resolved
        return resolved


class _RegionMap:
    """Approved region spelling groups, merged transitively.

    The table lists approved equivalent spellings in its two region columns.
    Groups are merged transitively, so pairs and chains of spellings work alike.
    Within a group, the spelling most often named as the canonical value wins;
    ties use the deterministic smallest spelling. Every original spelling is
    still retained on the evidence rows.
    """

    def __init__(
        self,
        rows: Iterable[Mapping[str, Any]],
        config: AliasDependencyConfig,
    ):
        self._config = config
        parent: dict[Any, Any] = {}
        canonical_votes: dict[Any, Counter] = defaultdict(Counter)

        def find(key: Any) -> Any:
            parent.setdefault(key, key)
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        for row in rows:
            left = _region_lookup_key(row[config.equivalence_region_column], config)
            right = _region_lookup_key(row[config.equivalence_canonical_column], config)
            if left is None or right is None:
                continue
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root
            canonical_votes[left][right] += 1
            canonical_votes[right][right] += 1

        groups: dict[Any, list[Any]] = defaultdict(list)
        for key in parent:
            groups[find(key)].append(key)
        self._canonical: dict[Any, Any] = {}
        for members in groups.values():
            votes: Counter = Counter()
            for member in members:
                votes.update(canonical_votes[member])
            self._canonical.update(
                {member: self._select_canonical(votes, members) for member in members}
            )

    @staticmethod
    def _select_canonical(votes: Counter, members: Sequence[Any]) -> Any:
        if not votes:
            return min(members, key=_stable)
        best_count = max(votes.values())
        return min(
            (member for member, count in votes.items() if count == best_count),
            key=_stable,
        )

    def canonicalize(self, raw: Any) -> tuple[str, Any | None]:
        if _is_unknown_region(raw, self._config):
            return ("unknown", None)
        key = _region_lookup_key(raw, self._config)
        return ("known", self._canonical.get(key, key))


def _region_lookup_key(value: Any, config: AliasDependencyConfig) -> Any:
    if config.strip_region_whitespace and isinstance(value, str):
        return value.strip()
    return value


def _is_unknown_region(value: Any, config: AliasDependencyConfig) -> bool:
    normalized = _region_lookup_key(value, config)
    return any(
        (unknown is None and normalized is None) or normalized == unknown
        for unknown in config.unknown_region_values
    )


def _row_key(row: Mapping[str, Any], config: AliasDependencyConfig) -> tuple[Any, ...]:
    columns = (
        config.detail_identity_columns
        if config.detail_identity_columns is not None
        else tuple(sorted(row.keys(), key=_stable))
    )
    return tuple(row[column] for column in columns)


def _evidence_sort_key(
    evidence: RegistrationEvidence, config: AliasDependencyConfig
) -> tuple[Any, ...]:
    report_id = (
        evidence.row.get(config.detail_report_id_column)
        if config.detail_report_id_column is not None
        else None
    )
    return (
        _stable(evidence.source_namespace),
        _stable(evidence.source_code),
        _stable(evidence.raw_region),
        tuple(_stable(value) for value in evidence.row_key),
        _stable(report_id),
        tuple(
            (_stable(column), _stable(value))
            for column, value in sorted(
                evidence.row.items(), key=lambda item: _stable(item[0])
            )
        ),
        tuple(_stable(value) for value in evidence.alias_path[0]),
    )


def _build_evidence(
    row: Mapping[str, Any],
    config: AliasDependencyConfig,
    source: StoreId,
    store: StoreId | None,
    region: Any,
    canonical_region: Any | None,
    path: tuple[StoreId, ...],
) -> RegistrationEvidence:
    return RegistrationEvidence(
        row=dict(row),
        row_key=_row_key(row, config),
        source_namespace=source[0],
        source_code=source[1],
        raw_region=region,
        canonical_region=canonical_region,
        store=store,
        alias_path=path,
    )


def _format_path(path: Sequence[StoreId]) -> str:
    return " -> ".join(_node_string(node) for node in path)


def _format_row(evidence: RegistrationEvidence) -> str:
    return ", ".join(f"{column}={value!r}" for column, value in evidence.row.items())


def _format_evidence_list(
    items: Sequence[RegistrationEvidence],
) -> list[str]:
    return [
        f"  row=({_format_row(evidence)}), "
        f"source={evidence.source_namespace!r}.{evidence.source_code!r}, "
        f"raw_region={evidence.raw_region!r}, "
        f"alias_path={_format_path(evidence.alias_path)}"
        for evidence in items
    ]


def _format_failure_message(report: AliasDependencyReport) -> str:
    lines: list[str] = []
    for conflict in report.conflicts:
        namespace, code = conflict.store
        lines.append(
            f"Store {namespace!r}.{code!r} has conflicting regions "
            f"{list(conflict.regions)!r}:"
        )
        for index, evidence in enumerate(conflict.evidence, start=1):
            lines.append(
                f"  evidence {index}: row=({_format_row(evidence)}), "
                f"source={evidence.source_namespace!r}."
                f"{evidence.source_code!r}, "
                f"raw_region={evidence.raw_region!r}, "
                f"canonical_region={evidence.canonical_region!r}, "
                f"alias_path={_format_path(evidence.alias_path)}"
            )
        if any(count > 1 for count in conflict.candidate_counts):
            lines.append(
                "  additional candidate rows exist for the same regions; "
                "the selected evidence rows are the deterministic minimum keys: "
                f"{list(conflict.candidate_counts)!r}."
            )

    if report.unknown_regions:
        lines.append(
            "Unknown regions (excluded from the dependency): "
            f"{len(report.unknown_regions)} record(s)."
        )
        lines.extend(_format_evidence_list(report.unknown_regions))

    if report.unconfirmable:
        lines.append(
            "Unconfirmable attributions (excluded from the dependency): "
            f"{len(report.unconfirmable)} record(s)."
        )
        for record in report.unconfirmable:
            evidence = record.evidence
            candidates = ", ".join(
                _node_string(candidate) for candidate in record.candidate_stores
            )
            suffix = f", candidates=[{candidates}]" if candidates else ""
            lines.append(
                f"  reason={record.reason}, "
                f"row=({_format_row(evidence)}), "
                f"source={evidence.source_namespace!r}."
                f"{evidence.source_code!r}, "
                f"raw_region={evidence.raw_region!r}{suffix}"
            )

    return "\n".join(lines)


def analyze_alias_dependency(
    details: Iterable[Mapping[str, Any]],
    aliases: Iterable[Mapping[str, Any]],
    region_equivalence: Iterable[Mapping[str, Any]] | None = None,
    config: AliasDependencyConfig | None = None,
) -> AliasDependencyReport:
    """Analyze alias-aware store-to-region dependencies.

    Alias edges are scoped by ``(source_namespace, source_code)`` pairs, so the
    same business number in two registration systems resolves independently.
    Terminal alias targets are the canonical stores. A record whose alias chain
    reaches several terminal stores is reported as unconfirmable rather than
    being assigned to any of them.

    Approved region equivalences merge relations for a store. The original
    spelling is retained on every evidence row and in ``raw_regions``.
    """
    config = AliasDependencyConfig() if config is None else config
    detail_rows = list(details)
    alias_rows = list(aliases)
    equivalence_rows = list(region_equivalence or ())

    edges: list[tuple[StoreId, StoreId]] = []
    for row in alias_rows:
        source = (
            row[config.alias_namespace_column],
            row[config.alias_code_column],
        )
        target = (
            row[config.alias_target_namespace_column],
            row[config.alias_target_code_column],
        )
        if None in source or None in target:
            continue
        edges.append((source, target))

    graph = _AliasGraph(edges)
    regions = _RegionMap(equivalence_rows, config)

    relation_candidates: dict[tuple[StoreId, Any], list[RegistrationEvidence]] = (
        defaultdict(list)
    )
    unknown_candidates: list[RegistrationEvidence] = []
    unconfirmable_candidates: list[UnconfirmableRecord] = []
    evaluated_rows = 0

    for row in detail_rows:
        if config.detail_confirmed_column is not None:
            confirmed_value = row[config.detail_confirmed_column]
            if confirmed_value not in config.detail_confirmed_values:
                continue
        evaluated_rows += 1

        source = (
            row[config.detail_namespace_column],
            row[config.detail_code_column],
        )
        raw_region = row[config.detail_region_column]
        resolution = graph.resolve(source)
        terminal_stores = tuple(sorted(resolution, key=_node_key))

        if len(terminal_stores) > 1:
            first_store = terminal_stores[0]
            unconfirmable_candidates.append(
                UnconfirmableRecord(
                    reason="ambiguous_alias_target",
                    evidence=_build_evidence(
                        row,
                        config,
                        source,
                        None,
                        raw_region,
                        None,
                        resolution[first_store],
                    ),
                    candidate_stores=terminal_stores,
                    candidate_paths=tuple(
                        (store, resolution[store]) for store in terminal_stores
                    ),
                )
            )
            continue

        if not terminal_stores:
            unconfirmable_candidates.append(
                UnconfirmableRecord(
                    reason="unresolved_alias_chain",
                    evidence=_build_evidence(
                        row, config, source, None, raw_region, None, (source,)
                    ),
                )
            )
            continue

        store = terminal_stores[0]
        region_state, canonical_region = regions.canonicalize(raw_region)

        if region_state == "unknown":
            unknown_candidates.append(
                _build_evidence(
                    row,
                    config,
                    source,
                    store,
                    raw_region,
                    None,
                    resolution[store],
                )
            )
            continue

        if region_state == "ambiguous":
            unconfirmable_candidates.append(
                UnconfirmableRecord(
                    reason="ambiguous_region_equivalence",
                    evidence=_build_evidence(
                        row,
                        config,
                        source,
                        store,
                        raw_region,
                        None,
                        resolution[store],
                    ),
                    candidate_stores=(store,),
                    candidate_paths=((store, resolution[store]),),
                )
            )
            continue

        relation_candidates[(store, canonical_region)].append(
            _build_evidence(
                row,
                config,
                source,
                store,
                raw_region,
                canonical_region,
                resolution[store],
            )
        )

    relations: list[StoreRegionRelation] = []
    relation_by_store: dict[StoreId, list[StoreRegionRelation]] = defaultdict(list)
    for (store, region), candidates in relation_candidates.items():
        ordered = sorted(candidates, key=lambda item: _evidence_sort_key(item, config))
        raw_regions = tuple(sorted({item.raw_region for item in ordered}, key=_stable))
        evidence = tuple(ordered[: config.max_evidence_per_region])
        relation = StoreRegionRelation(
            store=store,
            region=region,
            evidence=tuple(evidence),
            raw_regions=raw_regions,
            candidate_count=len(ordered),
        )
        relations.append(relation)
        relation_by_store[store].append(relation)
    relations.sort(key=lambda item: (_node_key(item.store), _stable(item.region)))

    conflicts: list[StoreConflict] = []
    for store in sorted(relation_by_store, key=_node_key):
        store_relations = sorted(
            relation_by_store[store], key=lambda item: _stable(item.region)
        )
        if len(store_relations) < 2:
            continue
        conflicts.append(
            StoreConflict(
                store=store,
                regions=tuple(relation.region for relation in store_relations),
                evidence=tuple(relation.evidence[0] for relation in store_relations),
                candidate_counts=tuple(
                    relation.candidate_count for relation in store_relations
                ),
            )
        )

    report_without_message = AliasDependencyReport(
        conflicts=tuple(conflicts),
        unknown_regions=tuple(
            sorted(
                unknown_candidates, key=lambda item: _evidence_sort_key(item, config)
            )
        ),
        unconfirmable=tuple(_sort_unconfirmable(unconfirmable_candidates)),
        relations=tuple(relations),
        detail_row_count=len(detail_rows),
        evaluated_row_count=evaluated_rows,
    )
    return AliasDependencyReport(
        conflicts=report_without_message.conflicts,
        unknown_regions=report_without_message.unknown_regions,
        unconfirmable=report_without_message.unconfirmable,
        relations=report_without_message.relations,
        detail_row_count=report_without_message.detail_row_count,
        evaluated_row_count=report_without_message.evaluated_row_count,
        _failure_message=_format_failure_message(report_without_message) or None,
    )


def _sort_unconfirmable(
    candidates: Sequence[UnconfirmableRecord],
) -> list[UnconfirmableRecord]:
    return sorted(
        candidates,
        key=lambda item: (
            item.reason,
            _stable(item.evidence.source_namespace),
            _stable(item.evidence.source_code),
            _stable(item.evidence.raw_region),
            tuple(_node_key(store) for store in item.candidate_stores),
            tuple(_stable(value) for value in item.evidence.row_key),
        ),
    )


class AliasFunctionalDependency(Constraint):
    """Functional dependency on canonical stores after resolving alias chains.

    The ordinary :class:`datajudge.constraints.miscs.FunctionalDependency`
    remains unchanged. This constraint combines three references: the detail
    rows, the namespaced alias table, and an approved region-equivalence table.
    """

    def __init__(
        self,
        ref: DataReference,
        alias_ref: DataReference,
        *,
        region_equivalence_ref: DataReference | None = None,
        config: AliasDependencyConfig | None = None,
        name: str | None = None,
        cache_size=None,
    ):
        super().__init__(ref, ref_value=object(), name=name, cache_size=cache_size)
        self._alias_ref = alias_ref
        self._region_equivalence_ref = region_equivalence_ref
        self.config = AliasDependencyConfig() if config is None else config
        self.last_report: AliasDependencyReport | None = None
        self._queries: list[str] = []

    def _fetch_rows(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> list[dict[str, Any]]:
        selection = ref.get_selection(engine)
        self._queries.append(
            str(selection.compile(engine, compile_kwargs={"literal_binds": True}))
        )
        with engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(selection)]

    def evaluate(self, engine: sa.engine.Engine) -> AliasDependencyReport:
        self._queries = []
        details = self._fetch_rows(engine, self._ref)
        aliases = self._fetch_rows(engine, self._alias_ref)
        equivalence = (
            self._fetch_rows(engine, self._region_equivalence_ref)
            if self._region_equivalence_ref is not None
            else None
        )
        report = analyze_alias_dependency(
            details,
            aliases,
            equivalence,
            config=self.config,
        )
        self.last_report = report
        return report

    def test(self, engine: sa.engine.Engine) -> TestResult:
        report = self.evaluate(engine)
        failed = bool(report.conflicts) or (
            self.config.fail_on_unknown and bool(report.unknown_regions)
        )
        failed = failed or (
            self.config.fail_on_unconfirmable and bool(report.unconfirmable)
        )
        if not failed:
            return TestResult.success()
        message = report.failure_message or "Alias-aware dependency check failed."
        return TestResult.failure(
            message,
            self.get_description(),
            self._queries,
            None,
        )
