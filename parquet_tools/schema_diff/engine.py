"""Footer-only schema comparison with conservative field identity matching.

No data pages, statistics, or value counts are read.  Every conclusion is
explicitly scoped to ``footer_only``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from parquet_tools.schema_diff.model import (
    ELEMENT_SEGMENT,
    Node,
    SchemaTree,
    render_path,
)

STATUS_COMPATIBLE = "compatible"
STATUS_INCOMPATIBLE = "incompatible"
STATUS_UNVERIFIABLE = "unverifiable"

IMPACT_DISPLAY_ONLY = "display_only"
IMPACT_METADATA_ONLY = "metadata_only"
IMPACT_MIGRATION = "migration_required"
IMPACT_INCOMPATIBLE = "incompatible"
IMPACT_UNVERIFIABLE = "unverifiable"

IMPACT_ORDER = {
    IMPACT_DISPLAY_ONLY: 0,
    IMPACT_METADATA_ONLY: 1,
    IMPACT_MIGRATION: 2,
    IMPACT_INCOMPATIBLE: 3,
    IMPACT_UNVERIFIABLE: 4,
}

BASIS_FIELD_ID = "field_id"
BASIS_LOGICAL_PATH = "logical_path"
BASIS_SIBLING_NAME = "sibling_name_under_matched_parent"
BASIS_LIST_ELEMENT = "list_element_position"
BASIS_NONE = "none"


@dataclass(frozen=True)
class Policy:
    allow_display_order: bool = True
    allowed_rename_ids: Tuple[int, ...] = ()

    def as_dict(self) -> Dict[str, object]:
        return {
            "allow_display_order": self.allow_display_order,
            "allowed_rename_ids": sorted(self.allowed_rename_ids),
        }


@dataclass(frozen=True)
class Match:
    old: Node
    new: Node
    basis: str
    confidence: str
    evidence: str

    def as_dict(self) -> Dict[str, object]:
        return {
            "old_path": self.old.physical_path_text,
            "new_path": self.new.physical_path_text,
            "old_logical_path": self.old.path_text,
            "new_logical_path": self.new.path_text,
            "identity_basis": self.basis,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "old_field_id": self.old.field_id,
            "new_field_id": self.new.field_id,
        }


@dataclass
class PendingGroup:
    kind: str
    reason: str
    key: Tuple[object, ...]
    old_nodes: List[Node]
    new_nodes: List[Node]

    def sort_key(self) -> Tuple[object, ...]:
        return (
            self.kind,
            self.key,
            tuple(_node_sort_key(node) for node in self.old_nodes),
            tuple(_node_sort_key(node) for node in self.new_nodes),
        )

    def as_dict(self) -> Dict[str, object]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "old_candidates": _describe_candidates(self.old_nodes),
            "new_candidates": _describe_candidates(self.new_nodes),
            "status": "pending_confirmation",
            "evidence_scope": "footer_only",
        }


def _node_sort_key(node: Node) -> Tuple[object, ...]:
    return (
        node.physical_path_text,
        node.path_text,
        node.field_id if node.field_id is not None else -1,
        node.kind,
    )


def _structural_fingerprint(node: Node):
    """Shape/type/repetition identity, intentionally excluding field names."""
    return (
        node.kind,
        node.repetition,
        node.physical_repetition,
        node.field_id,
        node.type_signature(),
        tuple(
            sorted(
                repr(_structural_fingerprint(child))
                for child in node.child_fields()
            )
        ),
    )


def _candidate_multisets_equal(
    old_nodes: Sequence[Node], new_nodes: Sequence[Node]
) -> bool:
    if len(old_nodes) != len(new_nodes):
        return False
    old = sorted(repr(_structural_fingerprint(node)) for node in old_nodes)
    new = sorted(repr(_structural_fingerprint(node)) for node in new_nodes)
    return old == new


class _Pairing:
    def __init__(self, old_tree: SchemaTree, new_tree: SchemaTree) -> None:
        self.old_tree = old_tree
        self.new_tree = new_tree
        self.old_to_new: Dict[int, Match] = {}
        self.new_to_old: Dict[int, Match] = {}
        self.matches: List[Match] = []
        self.pending: List[PendingGroup] = []
        self._pending_keys: Set[Tuple[object, ...]] = set()
        self.blocked_old: Set[int] = set()
        self.blocked_new: Set[int] = set()
        self.silent_old: Set[int] = set()
        self.silent_new: Set[int] = set()

    def _add(self, old: Node, new: Node, basis: str, confidence: str, evidence: str) -> bool:
        if id(old) in self.old_to_new or id(new) in self.new_to_old:
            return False
        if id(old) in self.blocked_old or id(new) in self.blocked_new:
            return False
        match = Match(old, new, basis, confidence, evidence)
        self.old_to_new[id(old)] = match
        self.new_to_old[id(new)] = match
        self.matches.append(match)
        return True

    def _add_pending(
        self,
        kind: str,
        key: Tuple[object, ...],
        old_nodes: Sequence[Node],
        new_nodes: Sequence[Node],
        reason: str,
        *,
        suppress_if_equal: bool = True,
    ) -> None:
        if key in self._pending_keys:
            return
        if suppress_if_equal and _candidate_multisets_equal(old_nodes, new_nodes):
            # Nothing differs inside the ambiguous candidate set, so there is
            # no structural change for identity matching to decide.  Suppress
            # add/remove inference for these candidates as well.
            self.silent_old.update(id(node) for node in old_nodes)
            self.silent_new.update(id(node) for node in new_nodes)
            return
        self._pending_keys.add(key)
        self.pending.append(
            PendingGroup(
                kind=kind,
                reason=reason,
                key=key,
                old_nodes=list(old_nodes),
                new_nodes=list(new_nodes),
            )
        )

    def run(self) -> None:
        self._block_duplicate_field_ids()
        self._pair_unique_field_ids()
        self._pair_root()
        self._pair_children_until_stable()
        self._collect_unresolved_by_parent()
        self._collect_residual_name_conflicts()

    def _block_duplicate_field_ids(self) -> None:
        """A field ID that repeats on one side is not identity evidence.

        Such nodes cannot be matched by path, name, or position either; doing
        so would silently choose one of the colliding candidates.  If the same
        ID also occurs on the other side, those nodes are blocked and listed in
        the same pending group, even when that side has only one occurrence.
        """
        old_ids = self.old_tree.field_ids
        new_ids = self.new_tree.field_ids
        all_ids = sorted(set(old_ids) | set(new_ids))
        for field_id in all_ids:
            old_nodes = old_ids.get(field_id, [])
            new_nodes = new_ids.get(field_id, [])
            if len(old_nodes) < 2 and len(new_nodes) < 2:
                continue
            if len(old_nodes) >= 2:
                self.blocked_old.update(id(node) for node in old_nodes)
            if len(new_nodes) >= 2:
                self.blocked_new.update(id(node) for node in new_nodes)
            if len(old_nodes) >= 2 and len(new_nodes) >= 1:
                self.blocked_new.update(id(node) for node in new_nodes)
            if len(new_nodes) >= 2 and len(old_nodes) >= 1:
                self.blocked_old.update(id(node) for node in old_nodes)
            counts = f"old={len(old_nodes)}, new={len(new_nodes)}"
            self._add_pending(
                "duplicate_field_id",
                ("duplicate_id", field_id),
                old_nodes,
                new_nodes,
                f"field_id={field_id} occurs more than once or ambiguously "
                f"({counts}); it cannot identify a single field",
                suppress_if_equal=False,
            )

    def _pair_unique_field_ids(self) -> None:
        old_ids = self.old_tree.field_ids
        new_ids = self.new_tree.field_ids
        for field_id in sorted(set(old_ids) & set(new_ids)):
            old_nodes = old_ids[field_id]
            new_nodes = new_ids[field_id]
            if len(old_nodes) == 1 and len(new_nodes) == 1:
                self._add(
                    old_nodes[0],
                    new_nodes[0],
                    BASIS_FIELD_ID,
                    "stable",
                    f"unique field_id={field_id} on both sides",
                )

    def _pair_root(self) -> None:
        self._add(
            self.old_tree.root,
            self.new_tree.root,
            BASIS_LOGICAL_PATH,
            "stable",
            "schema root",
        )

    def _pair_children_until_stable(self) -> None:
        changed = True
        while changed:
            changed = False
            for match in list(self.matches):
                old = match.old
                new = match.new
                if old.is_list or new.is_list:
                    if not (old.is_list and new.is_list):
                        continue
                    if old.list_element is not None and new.list_element is not None:
                        if (
                            id(old.list_element) not in self.old_to_new
                            and id(new.list_element) not in self.new_to_old
                        ):
                            same_name = old.list_element.name == new.list_element.name
                            if self._add(
                                old.list_element,
                                new.list_element,
                                BASIS_LIST_ELEMENT,
                                "stable" if same_name else "provisional",
                                (
                                    "single LIST element position"
                                    + (
                                        f"; element name {old.list_element.name!r}"
                                        if same_name
                                        else f"; element names {old.list_element.name!r} -> {new.list_element.name!r}"
                                    )
                                ),
                            ):
                                changed = True
                    continue
                old_children: Dict[str, List[Node]] = {}
                new_children: Dict[str, List[Node]] = {}
                for child in old.children:
                    if id(child) not in self.old_to_new:
                        old_children.setdefault(child.name, []).append(child)
                for child in new.children:
                    if id(child) not in self.new_to_old:
                        new_children.setdefault(child.name, []).append(child)
                for name in sorted(set(old_children) & set(new_children)):
                    old_candidates = old_children[name]
                    new_candidates = new_children[name]
                    if len(old_candidates) == 1 and len(new_candidates) == 1:
                        if self._add(
                            old_candidates[0],
                            new_candidates[0],
                            BASIS_SIBLING_NAME,
                            "provisional",
                            f"unique sibling name {name!r} under matched parent",
                        ):
                            changed = True

    def _collect_residual_name_conflicts(self) -> None:
        """Report same-named leftovers not already covered by a parent group."""
        blocked = self.blocked_old | self.blocked_new
        covered: Set[int] = set()
        for group in self.pending:
            for node in group.old_nodes + group.new_nodes:
                covered.add(id(node))
        unmatched_old = [
            node
            for node in self.old_tree.nodes
            if id(node) not in self.old_to_new
            and id(node) not in blocked
            and id(node) not in covered
        ]
        unmatched_new = [
            node
            for node in self.new_tree.nodes
            if id(node) not in self.new_to_old
            and id(node) not in blocked
            and id(node) not in covered
        ]
        old_by_name: Dict[str, List[Node]] = {}
        new_by_name: Dict[str, List[Node]] = {}
        for node in unmatched_old:
            old_by_name.setdefault(node.name, []).append(node)
        for node in unmatched_new:
            new_by_name.setdefault(node.name, []).append(node)
        for name in sorted(set(old_by_name) & set(new_by_name)):
            self._add_pending(
                "name_conflict",
                ("residual-name", name),
                old_by_name[name],
                new_by_name[name],
                "same name remains but no unique ID or matched parent "
                "establishes identity",
            )

    def _collect_unresolved_by_parent(self) -> None:
        """Group unresolved same-named siblings under the same parent pair."""
        groups: Dict[Tuple[object, ...], Tuple[List[Node], List[Node], str]] = {}
        for old in self.old_tree.nodes:
            if id(old) in self.old_to_new or id(old) in self.blocked_old:
                continue
            parent_key = self._parent_key(old, "old")
            key = ("parent", parent_key, old.name)
            entry = groups.setdefault(key, ([], [], parent_key[1]))
            entry[0].append(old)
        for new in self.new_tree.nodes:
            if id(new) in self.new_to_old or id(new) in self.blocked_new:
                continue
            parent_key = self._parent_key(new, "new")
            key = ("parent", parent_key, new.name)
            entry = groups.setdefault(key, ([], [], parent_key[1]))
            entry[1].append(new)
        for key in sorted(groups, key=repr):
            old_nodes, new_nodes, parent_label = groups[key]
            if not old_nodes or not new_nodes:
                continue
            if len(old_nodes) == 1 and len(new_nodes) == 1:
                continue
            self._add_pending(
                "sibling_name_conflict",
                key,
                old_nodes,
                new_nodes,
                f"multiple unresolved same-named siblings under "
                f"{parent_label!r}; no candidate can be selected",
            )

    def _parent_key(self, node: Node, side: str) -> Tuple[str, str]:
        parent = node.parent
        if parent is None or parent.kind == "root":
            return ("root", "")
        match = (
            self.old_to_new.get(id(parent))
            if side == "old"
            else self.new_to_old.get(id(parent))
        )
        if match is not None:
            return ("matched", match.old.physical_path_text)
        return ("unmatched", parent.physical_path_text)

    def pending_node_ids(self) -> Set[int]:
        result: Set[int] = set()
        for group in self.pending:
            for node in group.old_nodes + group.new_nodes:
                result.add(id(node))
        result |= self.silent_old | self.silent_new
        # Suppress add/remove noise under an unresolved parent subtree.
        expanded = set(result)
        for tree in (self.old_tree, self.new_tree):
            for node in tree.nodes:
                ancestor = node
                while ancestor is not None:
                    if id(ancestor) in result:
                        expanded.add(id(node))
                        break
                    ancestor = ancestor.parent
        return expanded


def _describe_candidates(nodes: Sequence[Node]) -> List[Dict[str, object]]:
    descriptions: List[Dict[str, object]] = []
    seen: Dict[str, int] = {}
    for node in nodes:
        descriptor = _public_descriptor(node)
        base = node.physical_path_text
        seen[base] = seen.get(base, 0) + 1
        descriptor["occurrence"] = seen[base]
        descriptions.append(descriptor)
    return descriptions


def _public_descriptor(node: Node) -> Dict[str, object]:
    return node.describe()


def _descendants(node: Node):
    for child in node.child_fields():
        yield child
        yield from _descendants(child)


def _change_level_for(node: Node) -> str:
    """Describe which structural level of the schema a node belongs to."""
    if node.is_list:
        return "field"
    parent = node.parent
    if parent is None or parent.kind == "root":
        return "field"
    if parent.is_list:
        return "element" if node.kind == "leaf" else "element_struct"
    if node.kind == "leaf":
        return "inner_field"
    return "inner_group"


def _jsonable_logical(
    logical: Optional[Tuple[str, object]],
) -> Optional[Dict[str, object]]:
    if logical is None:
        return None
    name, payload = logical
    if isinstance(payload, tuple):
        payload = list(payload)
    return {"name": name, "parameters": payload}


def _direction_migrates(change: "_Change", name: str) -> bool:
    direction = change.directions[name]
    explicit = direction.get("requires_migration")
    if explicit is not None:
        return bool(explicit)
    return change.requires_migration


def _describe_sources(
    entries: Sequence[Tuple[Tuple[object, ...], str, Node]],
) -> List[Dict[str, object]]:
    return [
        {
            "role": role,
            "logical_path": node.path_text,
            "physical_path": node.physical_path_text,
        }
        for _, role, node in entries
    ]


def _direction(
    status: str,
    reason: str,
    *,
    requires_migration: Optional[bool] = None,
    value_verification: str = "not_needed",
) -> Dict[str, object]:
    return {
        "status": status,
        "reason": reason,
        "requires_migration": requires_migration,
        "value_verification": value_verification,
    }


def _both(
    status: str,
    reason: str,
    *,
    requires_migration: Optional[bool] = None,
    value_verification: str = "not_needed",
) -> Dict[str, object]:
    return {
        "old_to_new": _direction(
            status,
            reason,
            requires_migration=requires_migration,
            value_verification=value_verification,
        ),
        "new_to_old": _direction(
            status,
            reason,
            requires_migration=requires_migration,
            value_verification=value_verification,
        ),
    }


@dataclass
class _Change:
    kind: str
    change_level: str
    old: Optional[Node]
    new: Optional[Node]
    identity: Dict[str, object]
    directions: Dict[str, Dict[str, object]]
    impact: str
    requires_migration: bool
    details: Dict[str, object] = field(default_factory=dict)
    value_verification: str = "not_needed"

    def sort_key(self) -> Tuple[object, ...]:
        return (
            IMPACT_ORDER.get(self.impact, 99),
            self.old.path_text if self.old is not None else "",
            self.new.path_text if self.new is not None else "",
            self.kind,
            self.change_level,
            self.old.physical_path_text if self.old is not None else "",
            self.new.physical_path_text if self.new is not None else "",
            repr(self.details),
        )

    def as_dict(self, change_id: str) -> Dict[str, object]:
        return {
            "change_id": change_id,
            "kind": self.kind,
            "change_level": self.change_level,
            "old": _public_descriptor(self.old) if self.old is not None else None,
            "new": _public_descriptor(self.new) if self.new is not None else None,
            "identity": self.identity,
            "compatibility": self.directions,
            "impact": self.impact,
            "requires_migration": self.requires_migration,
            "value_verification": self.value_verification,
            "evidence_scope": "footer_only",
            "details": self.details,
        }


def _repetition_direction(
    old_repetition: Optional[str],
    new_repetition: Optional[str],
) -> Tuple[Dict[str, Dict[str, object]], str, bool, str]:
    """Return directions, impact, requires_migration, value_verification."""
    if old_repetition == new_repetition:
        return _both(STATUS_COMPATIBLE, "repetition is unchanged"), IMPACT_DISPLAY_ONLY, False, "not_needed"
    pair = {old_repetition, new_repetition}
    if pair == {"optional", "required"}:
        tightening = old_repetition == "optional" and new_repetition == "required"
        if tightening:
            directions = {
                "old_to_new": _direction(
                    STATUS_INCOMPATIBLE,
                    "target requires non-null values but the old data may contain nulls",
                    requires_migration=True,
                    value_verification="may_resolve_violation",
                ),
                "new_to_old": _direction(
                    STATUS_COMPATIBLE,
                    "target accepts optional values",
                    requires_migration=False,
                ),
            }
        else:
            directions = {
                "old_to_new": _direction(
                    STATUS_COMPATIBLE,
                    "target accepts optional values",
                    requires_migration=False,
                ),
                "new_to_old": _direction(
                    STATUS_INCOMPATIBLE,
                    "target requires non-null values but the new data may contain nulls",
                    requires_migration=True,
                    value_verification="may_resolve_violation",
                ),
            }
        return directions, IMPACT_INCOMPATIBLE, True, "recommended"
    # required/optional <-> repeated changes alter the physical value model.
    return (
        _both(
            STATUS_INCOMPATIBLE,
            f"repetition changed from {old_repetition!r} to {new_repetition!r}",
            requires_migration=True,
        ),
        IMPACT_MIGRATION,
        True,
        "not_needed",
    )


class SchemaDiffer:
    def __init__(self, old_tree: SchemaTree, new_tree: SchemaTree, policy: Policy) -> None:
        self.old_tree = old_tree
        self.new_tree = new_tree
        self.policy = policy
        self.pairing = _Pairing(old_tree, new_tree)
        self.changes: List[_Change] = []
        self._change_keys: Set[Tuple[object, ...]] = set()

    def run(self) -> Dict[str, object]:
        self.pairing.run()
        self._compare_matches()
        self._detect_parent_relocations()
        self._detect_repetition_relocations()
        self._detect_column_order()
        self._detect_unmatched()
        return self._report()

    def _emit(self, change: _Change) -> None:
        key = (
            change.kind,
            change.change_level,
            id(change.old) if change.old is not None else None,
            id(change.new) if change.new is not None else None,
            repr(change.details),
        )
        if key in self._change_keys:
            return
        self._change_keys.add(key)
        self.changes.append(change)

    def _compare_matches(self) -> None:
        for match in sorted(
            self.pairing.matches,
            key=lambda m: (m.old.path_text, m.new.path_text, m.old.physical_path_text),
        ):
            old = match.old
            new = match.new
            if old is self.old_tree.root:
                continue
            identity = {
                "basis": match.basis,
                "confidence": match.confidence,
                "evidence": match.evidence,
            }

            if old.kind != new.kind:
                is_element = old.parent is not None and old.parent.is_list
                self._emit(
                    _Change(
                        kind=(
                            "list_element_structure_change"
                            if is_element
                            else "structure_change"
                        ),
                        change_level=_change_level_for(old),
                        old=old,
                        new=new,
                        identity=identity,
                        directions=_both(
                            STATUS_INCOMPATIBLE,
                            f"field kind changed from {old.kind!r} to {new.kind!r}",
                        ),
                        impact=IMPACT_MIGRATION,
                        requires_migration=True,
                        details={"old_kind": old.kind, "new_kind": new.kind},
                    )
                )
                continue

            if old.name != new.name and not (
                old.parent is not None and old.parent.is_list
            ):
                allowed = (
                    match.basis == BASIS_FIELD_ID
                    and old.field_id is not None
                    and old.field_id == new.field_id
                    and old.field_id in self.policy.allowed_rename_ids
                )
                if allowed:
                    directions = _both(
                        STATUS_COMPATIBLE,
                        f"rename is allowed for stable field_id={old.field_id}",
                    )
                    impact = IMPACT_METADATA_ONLY
                    requires_migration = False
                elif match.basis == BASIS_FIELD_ID and old.field_id == new.field_id:
                    directions = _both(
                        STATUS_INCOMPATIBLE,
                        f"rename is not allowed by policy for field_id={old.field_id}",
                    )
                    impact = IMPACT_INCOMPATIBLE
                    requires_migration = True
                else:
                    directions = _both(
                        STATUS_UNVERIFIABLE,
                        "names differ but no stable, allowed field ID establishes the rename",
                    )
                    impact = IMPACT_UNVERIFIABLE
                    requires_migration = True
                self._emit(
                    _Change(
                        kind="field_rename",
                        change_level="field",
                        old=old,
                        new=new,
                        identity=identity,
                        directions=directions,
                        impact=impact,
                        requires_migration=requires_migration,
                        details={
                            "old_name": old.name,
                            "new_name": new.name,
                            "allowed_rename_ids": sorted(self.policy.allowed_rename_ids),
                        },
                    )
                )

            if (
                old.field_id is not None
                and new.field_id is not None
                and old.field_id != new.field_id
            ):
                self._emit(
                    _Change(
                        kind="field_id_change",
                        change_level="field",
                        old=old,
                        new=new,
                        identity=identity,
                        directions=_both(
                            STATUS_INCOMPATIBLE,
                            f"stable identity changed from field_id={old.field_id} "
                            f"to field_id={new.field_id}",
                        ),
                        impact=IMPACT_MIGRATION,
                        requires_migration=True,
                        details={
                            "old_field_id": old.field_id,
                            "new_field_id": new.field_id,
                        },
                    )
                )
            elif (old.field_id is None) != (new.field_id is None):
                present = old.field_id if old.field_id is not None else new.field_id
                self._emit(
                    _Change(
                        kind="field_id_added_or_removed",
                        change_level="field",
                        old=old,
                        new=new,
                        identity=identity,
                        directions=_both(
                            STATUS_COMPATIBLE,
                            f"field ID {present} is present on only one side; "
                            "identity for this pair came from another basis",
                        ),
                        impact=IMPACT_METADATA_ONLY,
                        requires_migration=False,
                        details={
                            "old_field_id": old.field_id,
                            "new_field_id": new.field_id,
                            "identity_basis": match.basis,
                        },
                    )
                )

            if not old.is_list and not new.is_list:
                self._compare_repetition(old, new, identity)
            elif old.parent is not None and old.parent.is_list:
                self._compare_repetition(old, new, identity)
            self._compare_lists(old, new, identity)
            self._compare_element_name(old, new, match)
            self._compare_type(old, new, identity)

    def _compare_element_name(
        self,
        old: Node,
        new: Node,
        match: Match,
    ) -> None:
        if old.parent is None or not old.parent.is_list or old.name == new.name:
            return
        if match.basis == BASIS_LIST_ELEMENT:
            # The element is identified by its single LIST position.  Its
            # name is not a logical identity (and legal wrappers may name it
            # differently), so a pure name difference is display-only.
            self._emit(
                _Change(
                    kind="list_element_name_change",
                    change_level="element",
                    old=old,
                    new=new,
                    identity={
                        "basis": match.basis,
                        "confidence": "provisional",
                        "evidence": match.evidence,
                    },
                    directions=_both(
                        STATUS_COMPATIBLE,
                        "LIST element identity is positional; "
                        "the physical element name differs but is not logical identity",
                    ),
                    impact=IMPACT_DISPLAY_ONLY,
                    requires_migration=False,
                    details={
                        "old_element_name": old.name,
                        "new_element_name": new.name,
                        "old_physical_path": old.physical_path_text,
                        "new_physical_path": new.physical_path_text,
                        "physical_name_only": True,
                    },
                )
            )
            return
        allowed = (
            match.basis == BASIS_FIELD_ID
            and old.field_id is not None
            and old.field_id == new.field_id
            and old.field_id in self.policy.allowed_rename_ids
        )
        if allowed:
            directions = _both(
                STATUS_COMPATIBLE,
                f"element rename is allowed for stable field_id={old.field_id}",
            )
            impact = IMPACT_METADATA_ONLY
            requires_migration = False
        elif match.basis == BASIS_FIELD_ID and old.field_id == new.field_id:
            directions = _both(
                STATUS_INCOMPATIBLE,
                f"element rename is not allowed by policy for field_id={old.field_id}",
            )
            impact = IMPACT_INCOMPATIBLE
            requires_migration = True
        else:
            directions = _both(
                STATUS_UNVERIFIABLE,
                "LIST element names differ and no adjacent field ID establishes the rename; "
                "the element is paired by structure only",
            )
            impact = IMPACT_UNVERIFIABLE
            requires_migration = True
        self._emit(
            _Change(
                kind="list_element_rename",
                change_level="element",
                old=old,
                new=new,
                identity={
                    "basis": match.basis,
                    "confidence": match.confidence,
                    "evidence": match.evidence,
                },
                directions=directions,
                impact=impact,
                requires_migration=requires_migration,
                details={
                    "old_element_name": old.name,
                    "new_element_name": new.name,
                    "old_physical_path": old.physical_path_text,
                    "new_physical_path": new.physical_path_text,
                },
            )
        )

    def _compare_repetition(
        self, old: Node, new: Node, identity: Dict[str, object]
    ) -> None:
        if old.repetition == new.repetition:
            return
        directions, impact, migration, verification = _repetition_direction(
            old.repetition, new.repetition
        )
        level = _change_level_for(old)
        is_element_leaf = (
            old.parent is not None
            and old.parent.is_list
            and old.kind == "leaf"
        )
        if is_element_leaf:
            kind = "list_element_nullability_change"
        elif level == "inner_field":
            kind = "inner_field_repetition_change"
        else:
            kind = "repetition_change"
        self._emit(
            _Change(
                kind=kind,
                change_level=level,
                old=old,
                new=new,
                identity=identity,
                directions=directions,
                impact=impact,
                requires_migration=migration,
                value_verification=verification,
                details={
                    "old_repetition": old.repetition,
                    "new_repetition": new.repetition,
                    "old_physical_repetition": old.physical_repetition,
                    "new_physical_repetition": new.physical_repetition,
                    "old_max_definition_level": old.max_definition_level,
                    "new_max_definition_level": new.max_definition_level,
                    "old_max_repetition_level": old.max_repetition_level,
                    "new_max_repetition_level": new.max_repetition_level,
                    "old_optional_sources": _describe_sources(
                        self._source_entries(old, "optional", "old")
                    ),
                    "new_optional_sources": _describe_sources(
                        self._source_entries(new, "optional", "new")
                    ),
                },
            )
        )

    def _compare_lists(
        self, old: Node, new: Node, identity: Dict[str, object]
    ) -> None:
        if not (old.is_list and new.is_list):
            return
        old_layout = old.list_layout
        new_layout = new.list_layout
        old_element = old.list_element
        new_element = new.list_element
        if (
            old_layout is not None
            and new_layout is not None
            and old_element is not None
            and new_element is not None
        ):
            layout_changed = (
                old_layout.encoding,
                old_layout.wrapper_name,
                old_layout.wrapper_field_id,
            ) != (
                new_layout.encoding,
                new_layout.wrapper_name,
                new_layout.wrapper_field_id,
            )
            if layout_changed:
                self._emit(
                    _Change(
                        kind="list_wrapper_change",
                        change_level="wrapper",
                        old=old,
                        new=new,
                        identity=identity,
                        directions=_both(
                            STATUS_COMPATIBLE,
                            "both layouts are legal encodings of the same logical LIST",
                        ),
                        impact=IMPACT_DISPLAY_ONLY,
                        requires_migration=False,
                        details={
                            "old_layout": old_layout.as_dict(),
                            "new_layout": new_layout.as_dict(),
                            "old_physical_element_path": old_element.physical_path_text,
                            "new_physical_element_path": new_element.physical_path_text,
                            "logical_element_path": render_path(
                                old.logical_path + (ELEMENT_SEGMENT,)
                            ),
                        },
                    )
                )
            if not old_layout.valid or not new_layout.valid:
                self._emit(
                    _Change(
                        kind="invalid_list_layout",
                        change_level="wrapper",
                        old=old,
                        new=new,
                        identity=identity,
                        directions=_both(
                            STATUS_UNVERIFIABLE,
                            "a LIST layout is not a legal 2-level or 3-level encoding",
                        ),
                        impact=IMPACT_UNVERIFIABLE,
                        requires_migration=True,
                        details={
                            "old_layout": old_layout.as_dict(),
                            "new_layout": new_layout.as_dict(),
                        },
                    )
                )
        self._compare_repetition(old, new, identity)
        # Element nullability and element kind are compared through the
        # matched element node itself (paired by LIST element position), so
        # this function must not emit them a second time.

    def _compare_type(
        self, old: Node, new: Node, identity: Dict[str, object]
    ) -> None:
        if old.physical_type != new.physical_type:
            self._emit(
                _Change(
                    kind="physical_type_change",
                    change_level="type",
                    old=old,
                    new=new,
                    identity=identity,
                    directions=_both(
                        STATUS_INCOMPATIBLE,
                        f"physical type changed from {old.physical_type!r} "
                        f"to {new.physical_type!r}",
                    ),
                    impact=IMPACT_INCOMPATIBLE,
                    requires_migration=True,
                    details={
                        "old_physical_type": old.physical_type,
                        "new_physical_type": new.physical_type,
                    },
                )
            )
        if old.type_length != new.type_length:
            self._emit(
                _Change(
                    kind="type_length_change",
                    change_level="type",
                    old=old,
                    new=new,
                    identity=identity,
                    directions=_both(
                        STATUS_INCOMPATIBLE,
                        f"type_length changed from {old.type_length!r} "
                        f"to {new.type_length!r}",
                    ),
                    impact=IMPACT_INCOMPATIBLE,
                    requires_migration=True,
                    details={
                        "old_type_length": old.type_length,
                        "new_type_length": new.type_length,
                    },
                )
            )
        if old.logical_type != new.logical_type:
            self._emit(
                _Change(
                    kind="logical_type_change",
                    change_level="type",
                    old=old,
                    new=new,
                    identity=identity,
                    directions=_both(
                        STATUS_INCOMPATIBLE,
                        f"logical type changed from {old.logical_type!r} "
                        f"to {new.logical_type!r}",
                    ),
                    impact=IMPACT_INCOMPATIBLE,
                    requires_migration=True,
                    value_verification="recommended",
                    details={
                        "old_logical_type": _jsonable_logical(old.logical_type),
                        "new_logical_type": _jsonable_logical(new.logical_type),
                    },
                )
            )
        elif (old.scale, old.precision) != (new.scale, new.precision):
            # DECIMAL parameters are already part of logical_type; only report
            # them separately when the logical annotation itself is unchanged.
            self._emit(
                _Change(
                    kind="decimal_parameter_change",
                    change_level="type",
                    old=old,
                    new=new,
                    identity=identity,
                    directions=_both(
                        STATUS_INCOMPATIBLE,
                        "decimal scale/precision changed",
                    ),
                    impact=IMPACT_INCOMPATIBLE,
                    requires_migration=True,
                    details={
                        "old_scale": old.scale,
                        "new_scale": new.scale,
                        "old_precision": old.precision,
                        "new_precision": new.precision,
                    },
                )
            )

    def _canonical_key(self, node: Node, side: str) -> Tuple[object, ...]:
        if side == "old":
            match = self.pairing.old_to_new.get(id(node))
            if match is not None:
                return ("old", id(match.old))
            return ("unmatched-old", id(node))
        match = self.pairing.new_to_old.get(id(node))
        if match is not None:
            return ("old", id(match.old))
        return ("unmatched-new", id(node))

    def _source_entries(
        self, node: Node, role: str, side: str
    ) -> List[Tuple[Tuple[object, ...], str, Node]]:
        chain: List[Node] = []
        current: Optional[Node] = node
        while current is not None and current.kind != "root":
            chain.append(current)
            current = current.parent
        chain.reverse()
        entries: List[Tuple[Tuple[object, ...], str, Node]] = []
        for item in chain:
            key = self._canonical_key(item, side)
            if role == "repeated":
                if item.is_list:
                    entries.append((key, "list_occurrence", item))
                if item.repetition == "repeated":
                    entries.append((key, "repeated", item))
            else:
                if item.repetition == "optional":
                    entries.append((key, "optional", item))
        return entries

    def _detect_parent_relocations(self) -> None:
        for match in self.pairing.matches:
            old = match.old
            new = match.new
            if old.kind == "root" or new.kind == "root":
                continue
            old_parent = old.parent
            new_parent = new.parent
            if old_parent is None or new_parent is None:
                continue
            old_match = self.pairing.old_to_new.get(id(old_parent))
            new_match = self.pairing.new_to_old.get(id(new_parent))
            if old_match is None or new_match is None:
                continue
            if old_match is new_match:
                continue
            self._emit(
                _Change(
                    kind="parent_path_change",
                    change_level="field",
                    old=old,
                    new=new,
                    identity={
                        "basis": match.basis,
                        "confidence": match.confidence,
                        "evidence": match.evidence,
                    },
                    directions=_both(
                        STATUS_INCOMPATIBLE,
                        "the field was moved under a different parent",
                    ),
                    impact=IMPACT_MIGRATION,
                    requires_migration=True,
                    details={
                        "old_parent_path": old_parent.physical_path_text,
                        "new_parent_path": new_parent.physical_path_text,
                        "old_parent_logical_path": old_parent.path_text,
                        "new_parent_logical_path": new_parent.path_text,
                    },
                )
            )

    def _detect_repetition_relocations(self) -> None:
        for match in self.pairing.matches:
            old = match.old
            new = match.new
            if old.kind != "leaf" or new.kind != "leaf":
                continue
            identity = {
                "basis": match.basis,
                "confidence": match.confidence,
                "evidence": match.evidence,
            }
            old_repeated = self._source_entries(old, "repeated", "old")
            new_repeated = self._source_entries(new, "repeated", "new")
            if (
                old.max_repetition_level == new.max_repetition_level
                and [entry[0] for entry in old_repeated]
                != [entry[0] for entry in new_repeated]
            ):
                self._emit(
                    _Change(
                        kind="repetition_level_relocated",
                        change_level="leaf",
                        old=old,
                        new=new,
                        identity=identity,
                        directions=_both(
                            STATUS_INCOMPATIBLE,
                            "the repeated level moved to a different schema node",
                        ),
                        impact=IMPACT_MIGRATION,
                        requires_migration=True,
                        details={
                            "old_repeated_at": _describe_sources(old_repeated),
                            "new_repeated_at": _describe_sources(new_repeated),
                            "old_max_repetition_level": old.max_repetition_level,
                            "new_max_repetition_level": new.max_repetition_level,
                            "old_physical_max_repetition_level": old.raw_repetition_level,
                            "new_physical_max_repetition_level": new.raw_repetition_level,
                            "leaf_physical_type": new.physical_type,
                        },
                    )
                )
            old_optional = self._source_entries(old, "optional", "old")
            new_optional = self._source_entries(new, "optional", "new")
            if (
                old.max_definition_level == new.max_definition_level
                and old.repetition == new.repetition
                and [entry[0] for entry in old_optional]
                != [entry[0] for entry in new_optional]
            ):
                self._emit(
                    _Change(
                        kind="nullability_source_relocated",
                        change_level="leaf",
                        old=old,
                        new=new,
                        identity=identity,
                        directions=_both(
                            STATUS_INCOMPATIBLE,
                            "the optional level is attached to a different schema node",
                        ),
                        impact=IMPACT_MIGRATION,
                        requires_migration=True,
                        details={
                            "old_optional_at": _describe_sources(old_optional),
                            "new_optional_at": _describe_sources(new_optional),
                            "old_max_definition_level": old.max_definition_level,
                            "new_max_definition_level": new.max_definition_level,
                            "leaf_physical_type": new.physical_type,
                        },
                    )
                )

    def _detect_column_order(self) -> None:
        old_positions: Dict[int, int] = {
            id(leaf): index for index, leaf in enumerate(self.old_tree.leaves)
        }
        new_positions: Dict[int, int] = {
            id(leaf): index for index, leaf in enumerate(self.new_tree.leaves)
        }
        ordered = sorted(
            (
                match
                for match in self.pairing.matches
                if match.old.kind == "leaf" and match.new.kind == "leaf"
            ),
            key=lambda match: old_positions[id(match.old)],
        )
        if len(ordered) < 2:
            return
        matched_in_new = sorted(
            ordered,
            key=lambda item: new_positions[id(item.new)],
        )
        new_rank = {
            id(match.new): index for index, match in enumerate(matched_in_new)
        }
        ranks = [new_rank[id(match.new)] for match in ordered]
        if ranks == sorted(ranks):
            # Insertions/removals elsewhere may shift absolute leaf indexes,
            # but the relative order of matched columns is unchanged.
            return
        moved_matches = [
            match
            for index, match in enumerate(ordered)
            if ranks[index] != index
        ]
        moved_paths = sorted({match.new.path_text for match in moved_matches})
        old_sequence = [match.old.path_text for match in ordered]
        new_sequence = [match.new.path_text for match in matched_in_new]
        allowed = self.policy.allow_display_order
        if allowed:
            directions = _both(
                STATUS_COMPATIBLE,
                "column order is treated as a display-only change by policy",
            )
            impact = IMPACT_DISPLAY_ONLY
            requires_migration = False
        else:
            directions = _both(
                STATUS_INCOMPATIBLE,
                "column order changes are not allowed by policy",
            )
            impact = IMPACT_INCOMPATIBLE
            requires_migration = True
        self._emit(
            _Change(
                kind="column_order_change",
                change_level="schema",
                old=None,
                new=None,
                identity={
                    "basis": "matched leaves",
                    "confidence": "stable",
                    "evidence": "relative physical leaf order",
                },
                directions=directions,
                impact=impact,
                requires_migration=requires_migration,
                details={
                    "old_order": old_sequence,
                    "new_order": new_sequence,
                    "moved": moved_paths,
                    "position_basis": "physical column order in the footer",
                },
            )
        )

    def _detect_unmatched(self) -> None:
        pending_ids = self.pairing.pending_node_ids()
        structural_ids: Set[int] = set()
        # Children of a node whose kind changed are explained by that
        # structure change; do not also report them as field additions.
        for change in self.changes:
            if change.kind != "list_element_structure_change":
                continue
            if change.new is not None:
                for descendant in _descendants(change.new):
                    structural_ids.add(id(descendant))
            if change.old is not None:
                for descendant in _descendants(change.old):
                    structural_ids.add(id(descendant))
        for node in self.old_tree.nodes:
            if (
                id(node) in self.pairing.old_to_new
                or id(node) in pending_ids
                or id(node) in structural_ids
            ):
                continue
            self._emit(
                _Change(
                    kind="field_removed",
                    change_level="field",
                    old=node,
                    new=None,
                    identity={
                        "basis": BASIS_NONE,
                        "confidence": "unmatched",
                        "evidence": "no matching field in the new footer",
                    },
                    directions={
                        "old_to_new": _direction(
                            STATUS_COMPATIBLE,
                            "target does not require the removed field; "
                            "a reader may project it away",
                        ),
                        "new_to_old": _direction(
                            STATUS_COMPATIBLE
                            if node.repetition == "optional"
                            else STATUS_INCOMPATIBLE,
                            (
                                "target can null-fill the missing optional field"
                                if node.repetition == "optional"
                                else "target requires a field absent from the source schema"
                            ),
                        ),
                    },
                    impact=IMPACT_MIGRATION,
                    requires_migration=True,
                    value_verification="not_needed",
                    details={
                        "repetition": node.repetition,
                        "data_loss": True,
                    },
                )
            )
        for node in self.new_tree.nodes:
            if (
                id(node) in self.pairing.new_to_old
                or id(node) in pending_ids
                or id(node) in structural_ids
            ):
                continue
            self._emit(
                _Change(
                    kind="field_added",
                    change_level="field",
                    old=None,
                    new=node,
                    identity={
                        "basis": BASIS_NONE,
                        "confidence": "unmatched",
                        "evidence": "field does not exist in the old footer",
                    },
                    directions={
                        "old_to_new": _direction(
                            STATUS_COMPATIBLE
                            if node.repetition == "optional"
                            else STATUS_INCOMPATIBLE,
                            (
                                "target can null-fill the new optional field"
                                if node.repetition == "optional"
                                else "target requires a field absent from the old schema"
                            ),
                        ),
                        "new_to_old": _direction(
                            STATUS_COMPATIBLE,
                            "target does not require the added field; "
                            "a reader may project it away",
                        ),
                    },
                    impact=(
                        IMPACT_METADATA_ONLY
                        if node.repetition == "optional"
                        else IMPACT_INCOMPATIBLE
                    ),
                    requires_migration=node.repetition != "optional",
                    details={"repetition": node.repetition},
                )
            )

    def _report(self) -> Dict[str, object]:
        self.changes.sort(key=lambda change: change.sort_key())
        rendered_changes: List[Dict[str, object]] = []
        for index, change in enumerate(self.changes, start=1):
            rendered_changes.append(change.as_dict(f"CHG-{index:04d}"))

        pending = [
            group.as_dict()
            for group in sorted(self.pairing.pending, key=lambda group: group.sort_key())
        ]

        identity_pairs = [
            match.as_dict()
            for match in sorted(
                (
                    match
                    for match in self.pairing.matches
                    if match.old.kind != "root"
                ),
                key=lambda match: (
                    match.old.path_text,
                    match.new.path_text,
                    match.old.physical_path_text,
                ),
            )
        ]

        direction_summary = {
            "old_to_new": self._direction_summary("old_to_new"),
            "new_to_old": self._direction_summary("new_to_old"),
        }
        impact_counts = {name: 0 for name in IMPACT_ORDER}
        for change in self.changes:
            impact_counts[change.impact] = impact_counts.get(change.impact, 0) + 1

        value_checks: List[Dict[str, object]] = []
        for change in self.changes:
            for direction_name in ("old_to_new", "new_to_old"):
                direction = change.directions[direction_name]
                if direction["value_verification"] in (
                    "may_resolve_violation",
                    "recommended",
                ):
                    value_checks.append(
                        {
                            "kind": change.kind,
                            "change_level": change.change_level,
                            "direction": direction_name,
                            "old_path": (
                                change.old.physical_path_text
                                if change.old is not None
                                else None
                            ),
                            "new_path": (
                                change.new.physical_path_text
                                if change.new is not None
                                else None
                            ),
                            "footer_verdict": direction["status"],
                            "value_verification": direction["value_verification"],
                            "evidence_scope": "footer_only",
                            "note": (
                                "footer bytes cannot determine whether a "
                                "required-value constraint is satisfied; "
                                "a separate value scan is needed"
                            ),
                        }
                    )

        return {
            "tool": "parquet-footer-diff",
            "report_version": 1,
            "read_only": True,
            "evidence_scope": "footer_only",
            "data_pages_scanned": False,
            "policy": self.policy.as_dict(),
            "summary": {
                "directions": direction_summary,
                "impact_counts": impact_counts,
                "change_count": len(rendered_changes),
                "pending_confirmation_count": len(pending),
                "pending_identity_count": len(self.pairing.pending),
                "value_verification_count": len(value_checks),
                "matched_field_count": len(self.pairing.matches),
            },
            "changes": rendered_changes,
            "identity_pairs": identity_pairs,
            "pending_confirmations": pending,
            "value_verification": value_checks,
        }

    def _direction_summary(self, name: str) -> Dict[str, object]:
        statuses = [
            change.directions[name]["status"] for change in self.changes
        ]
        incompatible = statuses.count(STATUS_INCOMPATIBLE)
        unverifiable = statuses.count(STATUS_UNVERIFIABLE)
        pending = len(self.pairing.pending)
        migrations = sum(
            1
            for change in self.changes
            if _direction_migrates(change, name)
        )
        if incompatible:
            overall = STATUS_INCOMPATIBLE
        elif unverifiable or pending:
            overall = STATUS_UNVERIFIABLE
        elif migrations:
            overall = "compatible_with_migration"
        else:
            overall = STATUS_COMPATIBLE
        return {
            "status": overall,
            "compatible": incompatible == 0,
            "footer_can_decide": unverifiable == 0 and pending == 0,
            "requires_migration": migrations > 0,
            "incompatible_changes": incompatible,
            "unverifiable_changes": unverifiable,
            "pending_identity_groups": pending,
            "migration_changes": migrations,
        }


def compare_schemas(
    old_tree: SchemaTree,
    new_tree: SchemaTree,
    policy: Policy,
) -> Dict[str, object]:
    return SchemaDiffer(old_tree, new_tree, policy).run()
