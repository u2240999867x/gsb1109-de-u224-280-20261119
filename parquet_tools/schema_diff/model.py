"""Read-only footer schema model with logical LIST normalization.

Every field has two paths:

* ``physical_path`` -- exactly the path recorded in the Parquet footer,
  including any LIST wrapper group.
* ``logical_path`` -- LIST wrappers removed and the element rendered as
  ``[]`` (for example ``tags[].element`` becomes ``tags[].element`` for a
  struct child, or just ``tags[]`` for a primitive element).

Both legal LIST encodings are folded into the same logical shape.  A
different legal wrapper therefore cannot produce an element rename, add, or
remove by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from parquet_tools.gen_py.parquet.ttypes import (
    ConvertedType,
    FieldRepetitionType,
    LogicalType,
    SchemaElement,
    Type,
)

REQUIRED = "required"
OPTIONAL = "optional"
REPEATED = "repeated"

REPETITION_NAMES = {
    FieldRepetitionType.REQUIRED: REQUIRED,
    FieldRepetitionType.OPTIONAL: OPTIONAL,
    FieldRepetitionType.REPEATED: REPEATED,
}

TYPE_NAMES = {value: name for value, name in Type._VALUES_TO_NAMES.items()}
CONVERTED_NAMES = {
    value: name for value, name in ConvertedType._VALUES_TO_NAMES.items()
}

LOGICAL_ARMS = (
    "STRING",
    "MAP",
    "LIST",
    "ENUM",
    "DECIMAL",
    "DATE",
    "TIME",
    "TIMESTAMP",
    "INTEGER",
    "UNKNOWN",
    "JSON",
    "BSON",
    "UUID",
)

ELEMENT_SEGMENT = "[]"


class SchemaDiffError(Exception):
    """Raised when a footer cannot be interpreted as a Parquet schema."""


def normalize_field_id(field_id: Optional[int]) -> Optional[int]:
    """Treat the conventional ``-1`` placeholder as "no field id"."""
    if field_id is None or field_id < 0:
        return None
    return field_id


def render_path(segments: Sequence[str]) -> str:
    rendered: List[str] = []
    for segment in segments:
        if segment == ELEMENT_SEGMENT and rendered:
            rendered[-1] += ELEMENT_SEGMENT
        else:
            rendered.append(segment)
    return ".".join(rendered)


def _logical_payload(
    logical_type: Optional[LogicalType],
) -> Optional[Tuple[str, object]]:
    if logical_type is None:
        return None
    present = [
        arm for arm in LOGICAL_ARMS if getattr(logical_type, arm, None) is not None
    ]
    if len(present) != 1:
        return ("INVALID", tuple(present))
    arm = present[0]
    value = getattr(logical_type, arm)
    if arm == "DECIMAL":
        payload: object = (value.scale, value.precision)
    elif arm in ("TIME", "TIMESTAMP"):
        unit_name = None
        if value.unit is not None:
            for candidate in ("MILLIS", "MICROS", "NANOS"):
                if getattr(value.unit, candidate, None) is not None:
                    unit_name = candidate
        payload = (value.isAdjustedToUTC, unit_name)
    elif arm == "INTEGER":
        payload = (value.bitWidth, value.isSigned)
    else:
        payload = ()
    return (arm, payload)


def canonical_logical(
    element: SchemaElement,
) -> Optional[Tuple[str, object]]:
    """Fold legacy ConvertedType aliases into LogicalType equivalents."""
    logical = _logical_payload(element.logicalType)
    if logical is not None:
        return logical
    converted = element.converted_type
    if converted is None:
        return None
    name = CONVERTED_NAMES.get(converted, str(converted))
    if name == "UTF8":
        return ("STRING", ())
    if name == "DECIMAL":
        return ("DECIMAL", (element.scale, element.precision))
    if name in ("TIME_MILLIS", "TIME_MICROS"):
        return ("TIME", (False, "MILLIS" if name == "TIME_MILLIS" else "MICROS"))
    if name in ("TIMESTAMP_MILLIS", "TIMESTAMP_MICROS"):
        return (
            "TIMESTAMP",
            (False, "MILLIS" if name == "TIMESTAMP_MILLIS" else "MICROS"),
        )
    if name.startswith("INT_") or name.startswith("UINT_"):
        bits = int(name.split("_", 1)[1])
        return ("INTEGER", (bits, name.startswith("INT_")))
    return (name, ())


@dataclass
class RawNode:
    """A node exactly as it appears in the footer, before LIST folding."""

    name: str
    physical_path: Tuple[str, ...]
    repetition: Optional[str]
    field_id: Optional[int]
    physical_type: Optional[str]
    type_length: Optional[int]
    converted_type: Optional[str]
    logical_type: Optional[Tuple[str, object]]
    scale: Optional[int]
    precision: Optional[int]
    max_definition_level: int
    max_repetition_level: int
    children: List["RawNode"] = field(default_factory=list)

    @property
    def is_leaf(self) -> bool:
        return self.physical_type is not None


@dataclass
class ListLayout:
    """Physical layout details of a LIST-annotated field."""

    encoding: str
    wrapper_name: Optional[str]
    wrapper_field_id: Optional[int]
    element_name: str
    element_physical_repetition: str
    element_required: bool
    valid: bool = True

    def as_dict(self) -> Dict[str, object]:
        return {
            "encoding": self.encoding,
            "wrapper_name": self.wrapper_name,
            "wrapper_field_id": self.wrapper_field_id,
            "element_name": self.element_name,
            "element_physical_repetition": self.element_physical_repetition,
            "element_required": self.element_required,
            "valid": self.valid,
        }


@dataclass
class Node:
    """A logical field.  LIST wrapper groups never become nodes of their own."""

    name: str
    physical_path: Tuple[str, ...]
    logical_path: Tuple[str, ...]
    repetition: Optional[str]
    physical_repetition: Optional[str]
    field_id: Optional[int]
    physical_type: Optional[str]
    type_length: Optional[int]
    converted_type: Optional[str]
    logical_type: Optional[Tuple[str, object]]
    scale: Optional[int]
    precision: Optional[int]
    kind: str
    raw_definition_level: int
    raw_repetition_level: int
    max_definition_level: int = 0
    max_repetition_level: int = 0
    children: List["Node"] = field(default_factory=list)
    list_element: Optional["Node"] = None
    list_layout: Optional[ListLayout] = None
    parent: Optional["Node"] = None
    repetition_sources: Tuple[Tuple[str, str], ...] = ()
    optional_sources: Tuple[Tuple[str, str], ...] = ()

    @property
    def is_list(self) -> bool:
        return self.kind == "list"

    @property
    def path_text(self) -> str:
        return render_path(self.logical_path)

    @property
    def physical_path_text(self) -> str:
        return ".".join(self.physical_path)

    def child_fields(self) -> List["Node"]:
        """Logical children used for matching (list wrapper is structural)."""
        if self.is_list:
            return [self.list_element] if self.list_element is not None else []
        return self.children

    def leaf_nodes(self) -> List["Node"]:
        if self.is_list:
            return self.list_element.leaf_nodes() if self.list_element else []
        if self.kind == "leaf":
            return [self]
        result: List[Node] = []
        for child in self.children:
            result.extend(child.leaf_nodes())
        return result

    def type_signature(self) -> Tuple[object, ...]:
        return (
            self.physical_type,
            self.type_length,
            self.logical_type,
            self.scale,
            self.precision,
        )

    def describe(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "path": self.physical_path_text,
            "physical_path": self.physical_path_text,
            "logical_path": self.path_text,
            "field_id": self.field_id,
            "repetition": self.repetition,
            "physical_repetition": self.physical_repetition,
            "kind": self.kind,
            "physical_type": self.physical_type,
            "type_length": self.type_length,
            "converted_type": self.converted_type,
            "logical_type": (
                list(self.logical_type) if self.logical_type is not None else None
            ),
            "max_definition_level": self.max_definition_level,
            "max_repetition_level": self.max_repetition_level,
            "physical_max_definition_level": self.raw_definition_level,
            "physical_max_repetition_level": self.raw_repetition_level,
        }


@dataclass
class SchemaTree:
    root: Node
    nodes: List[Node]
    leaves: List[Node]

    @property
    def field_ids(self) -> Dict[int, List[Node]]:
        result: Dict[int, List[Node]] = {}
        for node in self.nodes:
            if node.field_id is not None:
                result.setdefault(node.field_id, []).append(node)
        return result


def _build_raw(schema: Sequence[SchemaElement]) -> RawNode:
    if not schema:
        raise SchemaDiffError("footer contains an empty schema")
    position = 0

    def take() -> SchemaElement:
        nonlocal position
        if position >= len(schema):
            raise SchemaDiffError("footer schema ended before all children were read")
        element = schema[position]
        position += 1
        return element

    def build(
        element: SchemaElement,
        parent_physical: Tuple[str, ...],
        parent_definition: int,
        parent_repetition: int,
    ) -> RawNode:
        repetition = REPETITION_NAMES.get(element.repetition_type)
        definition = parent_definition
        if repetition in (OPTIONAL, REPEATED):
            definition += 1
        repetition_level = parent_repetition + (1 if repetition == REPEATED else 0)
        physical = parent_physical + (element.name,)
        node = RawNode(
            name=element.name,
            physical_path=physical,
            repetition=repetition,
            field_id=normalize_field_id(element.field_id),
            physical_type=TYPE_NAMES.get(element.type),
            type_length=element.type_length,
            converted_type=(
                CONVERTED_NAMES.get(element.converted_type)
                if element.converted_type is not None
                else None
            ),
            logical_type=canonical_logical(element),
            scale=element.scale,
            precision=element.precision,
            max_definition_level=definition,
            max_repetition_level=repetition_level,
        )
        for _ in range(element.num_children or 0):
            node.children.append(
                build(take(), physical, definition, repetition_level)
            )
        return node

    root_element = take()
    root = RawNode(
        name=root_element.name,
        physical_path=(),
        repetition=None,
        field_id=normalize_field_id(root_element.field_id),
        physical_type=None,
        type_length=None,
        converted_type=None,
        logical_type=None,
        scale=None,
        precision=None,
        max_definition_level=0,
        max_repetition_level=0,
    )
    for _ in range(root_element.num_children or 0):
        root.children.append(build(take(), (), 0, 0))
    if position != len(schema):
        raise SchemaDiffError("footer schema contains unreferenced trailing elements")
    return root


def _make_node(
    raw: RawNode,
    logical_path: Tuple[str, ...],
    kind: str,
    semantic_repetition: Optional[str] = None,
    list_layout: Optional[ListLayout] = None,
) -> Node:
    return Node(
        name=raw.name,
        physical_path=raw.physical_path,
        logical_path=logical_path,
        repetition=(
            semantic_repetition if semantic_repetition is not None else raw.repetition
        ),
        physical_repetition=raw.repetition,
        field_id=raw.field_id,
        physical_type=raw.physical_type,
        type_length=raw.type_length,
        converted_type=raw.converted_type,
        logical_type=raw.logical_type,
        scale=raw.scale,
        precision=raw.precision,
        kind=kind,
        raw_definition_level=raw.max_definition_level,
        raw_repetition_level=raw.max_repetition_level,
        list_layout=list_layout,
    )


def _list_layout(raw: RawNode) -> Tuple[ListLayout, RawNode]:
    """Return the canonical element and physical layout of a LIST node."""
    if len(raw.children) != 1:
        element = raw.children[0] if raw.children else raw
        return (
            ListLayout("invalid", None, None, element.name, "", False, valid=False),
            element,
        )
    outer = raw.children[0]
    if outer.repetition != REPEATED:
        return (
            ListLayout(
                "invalid",
                None,
                None,
                outer.name,
                outer.repetition or "",
                False,
                valid=False,
            ),
            outer,
        )
    if outer.is_leaf:
        # Legal 2-level LIST: repeated <primitive>.  Elements are required.
        return (
            ListLayout(
                encoding="two_level_primitive",
                wrapper_name=None,
                wrapper_field_id=None,
                element_name=outer.name,
                element_physical_repetition=REPEATED,
                element_required=True,
            ),
            outer,
        )
    legacy_names = {"array", f"{raw.name}_tuple"}
    if len(outer.children) == 1 and outer.name not in legacy_names:
        inner = outer.children[0]
        if inner.repetition == REPEATED:
            # The deprecated 3-level layout with a repeated child is not a
            # legal LIST element; report it rather than silently normalizing.
            return (
                ListLayout(
                    "invalid",
                    outer.name,
                    outer.field_id,
                    inner.name,
                    REPEATED,
                    False,
                    valid=False,
                ),
                inner,
            )
        required = inner.repetition != OPTIONAL
        return (
            ListLayout(
                encoding="three_level",
                wrapper_name=outer.name,
                wrapper_field_id=outer.field_id,
                element_name=inner.name,
                element_physical_repetition=inner.repetition or "",
                element_required=required,
            ),
            inner,
        )
    # Legal legacy 2-level group element.  The repeated group is the element
    # (a struct); its children are the struct fields.
    return (
        ListLayout(
            encoding="two_level_group",
            wrapper_name=None,
            wrapper_field_id=None,
            element_name=outer.name,
            element_physical_repetition=REPEATED,
            element_required=True,
        ),
        outer,
    )


def _is_list_annotated(raw: RawNode) -> bool:
    return raw.logical_type is not None and raw.logical_type[0] == "LIST"


def _normalize_field(raw: RawNode, parent_logical: Tuple[str, ...]) -> Node:
    logical = parent_logical + (raw.name,)
    if _is_list_annotated(raw):
        return _normalize_list(raw, logical)
    kind = "leaf" if raw.is_leaf else "group"
    node = _make_node(raw, logical, kind)
    node.children = [_normalize_field(child, logical) for child in raw.children]
    for child in node.children:
        child.parent = node
    return node


def _normalize_list(raw: RawNode, logical: Tuple[str, ...]) -> Node:
    layout, element_raw = _list_layout(raw)
    list_node = _make_node(raw, logical, "list", list_layout=layout)
    if element_raw is raw:
        # Malformed LIST with no usable child: keep the field but expose no
        # element, so the engine reports an invalid layout instead of looping.
        return list_node
    list_node.list_element = _normalize_element(
        element_raw,
        logical + (ELEMENT_SEGMENT,),
        REQUIRED if layout.element_required else OPTIONAL,
    )
    list_node.list_element.parent = list_node
    return list_node


def _normalize_element(
    raw: RawNode,
    logical: Tuple[str, ...],
    semantic_repetition: str,
) -> Node:
    if _is_list_annotated(raw):
        layout, element_raw = _list_layout(raw)
        node = _make_node(
            raw,
            logical,
            "list",
            semantic_repetition=semantic_repetition,
            list_layout=layout,
        )
        if element_raw is raw:
            return node
        node.list_element = _normalize_element(
            element_raw,
            logical + (ELEMENT_SEGMENT,),
            REQUIRED if layout.element_required else OPTIONAL,
        )
        node.list_element.parent = node
        return node
    kind = "leaf" if raw.is_leaf else "group"
    node = _make_node(
        raw,
        logical,
        kind,
        semantic_repetition=semantic_repetition,
    )
    node.children = [_normalize_field(child, logical) for child in raw.children]
    for child in node.children:
        child.parent = node
    return node


def _assign_levels(
    node: Node,
    parent_definition: int,
    parent_repetition: int,
    inherited_repeated: Tuple[Tuple[str, str], ...],
    inherited_optional: Tuple[Tuple[str, str], ...],
) -> None:
    if node.is_list:
        definition = parent_definition
        repetition = parent_repetition
        if node.repetition == OPTIONAL:
            definition += 1
        elif node.repetition == REPEATED:
            definition += 1
            repetition += 1
        # The list occurrence itself is a repeated level in the data model.
        definition += 1
        repetition += 1
        node.max_definition_level = definition
        node.max_repetition_level = repetition
        node.repetition_sources = inherited_repeated + (
            ("list", node.path_text),
        )
        node.optional_sources = inherited_optional + (
            (("list", node.path_text),)
            if node.repetition == OPTIONAL
            else ()
        )
        element = node.list_element
        if element is not None:
            _assign_levels(
                element,
                definition,
                repetition,
                node.repetition_sources,
                node.optional_sources,
            )
        return

    definition = parent_definition
    repetition = parent_repetition
    if node.repetition in (OPTIONAL, REPEATED):
        definition += 1
    if node.repetition == REPEATED:
        repetition += 1
    node.max_definition_level = definition
    node.max_repetition_level = repetition
    node.repetition_sources = inherited_repeated
    if node.repetition == REPEATED:
        node.repetition_sources = inherited_repeated + (
            ("repeated", node.path_text),
        )
    node.optional_sources = inherited_optional
    if node.repetition == OPTIONAL:
        node.optional_sources = inherited_optional + (
            ("optional", node.path_text),
        )
    for child in node.children:
        _assign_levels(
            child,
            definition,
            repetition,
            node.repetition_sources,
            node.optional_sources,
        )


def parse_schema(schema: Sequence[SchemaElement]) -> SchemaTree:
    raw_root = _build_raw(schema)
    root = Node(
        name=raw_root.name,
        physical_path=(),
        logical_path=(),
        repetition=None,
        physical_repetition=None,
        field_id=raw_root.field_id,
        physical_type=None,
        type_length=None,
        converted_type=None,
        logical_type=None,
        scale=None,
        precision=None,
        kind="root",
        raw_definition_level=0,
        raw_repetition_level=0,
    )
    root.children = [_normalize_field(child, ()) for child in raw_root.children]
    for child in root.children:
        child.parent = root

    nodes: List[Node] = []

    def walk(node: Node) -> None:
        nodes.append(node)
        if node.is_list:
            if node.list_element is not None:
                walk(node.list_element)
            return
        for child in node.children:
            walk(child)

    for child in root.children:
        walk(child)

    _assign_levels(root, 0, 0, (), ())
    leaves = [node for node in nodes if node.kind == "leaf"]
    return SchemaTree(root=root, nodes=nodes, leaves=leaves)


def iter_nodes(root: Node) -> Iterator[Node]:
    yield root
    for child in root.child_fields():
        yield from iter_nodes(child)
