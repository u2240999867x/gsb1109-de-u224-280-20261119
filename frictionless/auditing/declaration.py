from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..exception import FrictionlessException

SUPPORTED_MODES = ("natural", "surrogate", "pure")
SUPPORTED_COMPARISONS = ("source", "normalized")
SUPPORTED_SCOPE_KINDS = ("complete", "segments", "keys")


def _fail(note: str):
    raise FrictionlessException(f"Invalid audit declaration: {note}")


def _require(declaration: Dict[str, Any], key: str, where: str) -> Any:
    if key not in declaration:
        _fail(f'missing "{key}" in {where}')
    return declaration[key]


def _string_list(value: Any, where: str) -> List[str]:
    if isinstance(value, str):
        entries = [entry.strip() for entry in value.split(",") if entry.strip()]
    elif isinstance(value, list) and all(isinstance(entry, str) for entry in value):
        entries = list(value)
    else:
        _fail(f'"{where}" must be a string or a list of strings')
    if len(set(entries)) != len(entries):
        _fail(f'"{where}" contains duplicate names: {entries}')
    return entries


def _mapping(value: Any, where: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail(f'"{where}" must be a mapping')
    return value


class ColumnRule:
    """One logical business column with its old/new sources and comparison rule."""

    def __init__(
        self,
        name: str,
        *,
        old: str,
        new: str,
        normalize: Dict[str, List[Any]],
        compare: str,
        key: bool,
    ):
        self.name = name
        self.old = old
        self.new = new
        self.normalize = normalize
        self.compare = compare
        self.key = key

    def source(self, side: str) -> str:
        return self.old if side == "old" else self.new

    def normalize_for(self, side: str) -> List[Any]:
        return self.normalize[side]

    def to_descriptor(self) -> Dict[str, Any]:
        return {
            "old": self.old,
            "new": self.new,
            "normalize": self.normalize,
            "compare": self.compare,
            "key": self.key,
        }


class Scope:
    """Verified coverage declared for one side of one table."""

    def __init__(self, declaration: Optional[Dict[str, Any]], *, where: str):
        self.declared = declaration is not None
        self.complete = False
        self.segments: List[Dict[str, Any]] = []
        self.keys: List[str] = []
        self.raw = declaration
        if declaration is None:
            return
        if not isinstance(declaration, dict):
            _fail(f'"{where}" must be a mapping')
        if declaration.get("complete") is True:
            self.complete = True
            return
        if "complete" in declaration and not isinstance(declaration["complete"], bool):
            _fail(f'"{where}.complete" must be a boolean')
        segments = declaration.get("segments", [])
        keys = declaration.get("keys", [])
        if not isinstance(segments, list) or not isinstance(keys, list):
            _fail(f'"{where}.segments" and "{where}.keys" must be lists')
        for segment in segments:
            if not isinstance(segment, dict):
                _fail(f'"{where}.segments" entries must be mappings')
            if not ({"from", "to"} <= set(segment) or "keys" in segment):
                _fail(
                    f'"{where}.segments" entries need "from"/"to" or "keys": {segment!r}'
                )
            self.segments.append(segment)
        self.keys = [str(key) for key in keys]

    def to_descriptor(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"declared": self.declared}
        if self.complete:
            result["complete"] = True
        elif self.declared:
            result["complete"] = False
            result["segments"] = self.segments
            result["keys"] = self.keys
        return result


class TableAudit:
    """One audited table: two versions, logical column correspondence, and scope."""

    def __init__(
        self,
        name: str,
        *,
        old_resource: str,
        new_resource: str,
        key: List[str],
        columns: Dict[str, ColumnRule],
        old_scope: Scope,
        new_scope: Scope,
        old_mode: str,
        new_mode: str,
        old_real_key: List[str],
        new_real_key: List[str],
        references: List[Dict[str, Any]],
        stats: bool,
    ):
        self.name = name
        self.old_resource = old_resource
        self.new_resource = new_resource
        self.key = key
        self.columns = columns
        self.old_scope = old_scope
        self.new_scope = new_scope
        self.old_mode = old_mode
        self.new_mode = new_mode
        self.old_real_key = old_real_key
        self.new_real_key = new_real_key
        self.references = references
        self.stats = stats

    @property
    def key_columns(self) -> List[ColumnRule]:
        return [self.columns[name] for name in self.key]

    @property
    def value_names(self) -> List[str]:
        return [name for name in self.columns if name not in self.key]

    def source_columns(self, side: str) -> List[str]:
        return [rule.source(side) for rule in self.columns.values()]

    def missing_columns(self, side: str, available: List[str]) -> List[str]:
        return [column for column in self.source_columns(side) if column not in available]

    def to_descriptor(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "old": {"resource": self.old_resource, "mode": self.old_mode},
            "new": {"resource": self.new_resource, "mode": self.new_mode},
            "key": self.key,
            "columns": {
                name: rule.to_descriptor() for name, rule in self.columns.items()
            },
            "scope": {
                "old": self.old_scope.to_descriptor(),
                "new": self.new_scope.to_descriptor(),
            },
        }
        if self.old_real_key:
            result["old"]["realKey"] = self.old_real_key
        if self.new_real_key:
            result["new"]["realKey"] = self.new_real_key
        if self.references:
            result["references"] = self.references
        result["stats"] = self.stats
        return result


class AuditDeclaration:
    """Parsed, validated caller declaration for an audit run."""

    def __init__(self, descriptor: Dict[str, Any]):
        if not isinstance(descriptor, dict):
            _fail("the declaration must be a mapping")
        self.descriptor = descriptor
        self.tables: Dict[str, TableAudit] = {}
        raw_tables = _mapping(descriptor.get("tables"), "tables")
        if not raw_tables:
            _fail('"tables" must declare at least one table')
        for name, table in raw_tables.items():
            self.tables[name] = self._parse_table(name, table)
        # References are validated after all tables are known
        for table in self.tables.values():
            for reference in table.references:
                holder = reference["holding"]
                if holder not in self.tables:
                    _fail(
                        f'tables["{table.name}"] references undeclared holding '
                        f'table "{holder}"'
                    )
                if reference["column"] not in self.tables[holder].columns:
                    _fail(
                        f'tables["{table.name}"] reference column '
                        f'"{reference["column"]}" is not declared by "{holder}"'
                    )
                if holder == table.name:
                    _fail(f'tables["{table.name}"] cannot reference itself as a holder')

    # Parsing

    def _parse_table(self, name: str, declaration: Any) -> TableAudit:
        where = f'tables["{name}"]'
        if not isinstance(declaration, dict):
            _fail(f"{where} must be a mapping")

        old = _mapping(declaration.get("old"), f"{where}.old")
        new = _mapping(declaration.get("new"), f"{where}.new")
        old_resource = old.get("resource", name + "-old")
        new_resource = new.get("resource", name + "-new")

        key = _string_list(_require(declaration, "key", where), f"{where}.key")
        columns = self._parse_columns(name, declaration, key)

        old_scope = self._parse_scope(declaration, old, name, "old")
        new_scope = self._parse_scope(declaration, new, name, "new")

        old_mode = old.get("mode", "natural")
        new_mode = new.get("mode", "natural")
        for side, mode in (("old", old_mode), ("new", new_mode)):
            if mode not in SUPPORTED_MODES:
                _fail(
                    f'{where}.{side}.mode must be one of {SUPPORTED_MODES}, got "{mode}"'
                )

        old_real_key = _optional_list(old.get("realKey"), f"{where}.old.realKey")
        new_real_key = _optional_list(new.get("realKey"), f"{where}.new.realKey")

        references = declaration.get("references", [])
        if references is None:
            references = []
        if not isinstance(references, list):
            _fail(f"{where}.references must be a list")
        parsed_references: List[Dict[str, Any]] = []
        for reference in references:
            if not isinstance(reference, dict):
                _fail(f"{where}.references entries must be mappings")
            parsed_references.append(self._parse_reference(name, reference, key))

        stats = bool(declaration.get("stats", False))
        return TableAudit(
            name,
            old_resource=old_resource,
            new_resource=new_resource,
            key=key,
            columns=columns,
            old_scope=old_scope,
            new_scope=new_scope,
            old_mode=old_mode,
            new_mode=new_mode,
            old_real_key=old_real_key,
            new_real_key=new_real_key,
            references=parsed_references,
            stats=stats,
        )

    def _parse_columns(
        self, table: str, declaration: Dict[str, Any], key: List[str]
    ) -> Dict[str, ColumnRule]:
        where = f'tables["{table}"].columns'
        raw = _mapping(declaration.get("columns"), where)
        if not raw:
            _fail(f'"{where}" must declare the logical business columns')
        columns: Dict[str, ColumnRule] = {}
        for name, rule in raw.items():
            if not isinstance(rule, dict):
                _fail(f'{where}["{name}"] must be a mapping')
            old = rule.get("old", name)
            new = rule.get("new", name)
            if not isinstance(old, str) or not isinstance(new, str):
                _fail(f'{where}["{name}"].old/new must be strings')
            normalize = self._parse_normalize(
                rule.get("normalize", []), f'{where}["{name}"].normalize'
            )
            compare = rule.get("compare", "source")
            if compare not in SUPPORTED_COMPARISONS:
                _fail(
                    f'{where}["{name}"].compare must be one of '
                    f'{SUPPORTED_COMPARISONS}, got "{compare}"'
                )
            columns[name] = ColumnRule(
                name,
                old=old,
                new=new,
                normalize=normalize,
                compare=compare,
                key=name in key,
            )
        for name in key:
            if name not in columns:
                _fail(f'{where} is missing business-key column "{name}"')
            if not columns[name].key:
                columns[name].key = True
        if len(columns) < len(key):
            _fail(f"{where} must declare every business-key column")
        return columns

    def _parse_reference(
        self, table: str, reference: Dict[str, Any], key: List[str]
    ) -> Dict[str, Any]:
        where = f'tables["{table}"].references'
        holding = _require(reference, "holding", where)
        if not isinstance(holding, str):
            _fail(f'"{where}.holding" must be a string')
        column = reference.get("column", reference.get("key"))
        if column is None:
            if len(key) != 1:
                _fail(
                    f'"{where}" must give "column" or "key" when the target key '
                    "has more than one column"
                )
            column = key[0]
        if not isinstance(column, str):
            _fail(f'"{where}.column" must be a string')
        return {
            "holding": holding,
            "column": column,
            "old": reference.get("old"),
            "new": reference.get("new"),
        }

    def _parse_scope(
        self, declaration: Dict[str, Any], side: Dict[str, Any], table: str, name: str
    ) -> Scope:
        where = f'tables["{table}"].scope.{name}'
        scope = None
        if isinstance(declaration.get("scope"), dict):
            scope = declaration["scope"].get(name)
        if scope is None and "scope" in side:
            scope = side["scope"]
        if scope is None and "coverage" in side:
            scope = side["coverage"]
        return Scope(scope, where=where)

    def _parse_normalize(self, value: Any, where: str) -> Dict[str, List[Any]]:
        if value is None:
            value = []
        if isinstance(value, dict) and set(value) <= {"old", "new"}:
            result: Dict[str, List[Any]] = {}
            for side in ("old", "new"):
                operations = value.get(side, [])
                if operations is None:
                    operations = []
                if isinstance(operations, (str, dict)):
                    operations = [operations]
                if not isinstance(operations, list):
                    _fail(f'"{where}.{side}" must be a list')
                result[side] = operations
            return result
        if isinstance(value, (str, dict)):
            value = [value]
        if isinstance(value, list):
            return {"old": list(value), "new": list(value)}
        _fail(f'"{where}" must be a list or an {{"old", "new"}} mapping')

    # Lookups

    def table(self, name: str) -> TableAudit:
        try:
            return self.tables[name]
        except KeyError:
            _fail(f'tables["{name}"] is not declared')


def _optional_list(value: Any, where: str) -> List[str]:
    if value is None:
        return []
    return _string_list(value, where)
