"""
Order revision merging on top of python-benedict.

Merges order revisions coming from the warehouse (line removals/additions)
with delivery-date patches coming from the supplier, and produces a clean
result ready to be dispatched to the inbound (入库) system.

Matching rules:
- Lines are matched by their identifier (``line_id``) when the patch carries
  one, otherwise by ``sku``.
- A version whose lines contain the same ``sku`` twice cannot be matched
  unambiguously: an :class:`UnmatchableVersionError` is raised instead of
  applying the late-patch conflict handling.
- A patch field that is absent means "leave unchanged"; a field explicitly
  set to ``None`` clears the current value.
- A patch targeting a line that is no longer present (e.g. removed by an
  earlier warehouse revision) is reported as a conflict that locates both
  the source batch and the old line item.
- Batches are idempotent: re-sending an already applied batch (same
  ``batch_id``) is a no-op, so valid lines and conflicts stay unique.
"""

from benedict import benedict

__all__ = [
    "MergeResult",
    "OrderMerger",
    "UnmatchableVersionError",
]

# keys of a patch operation that identify the target line and are
# therefore never applied as field updates
_PATCH_META_KEYS = ("op", "line_id", "sku")

# line keys used only for matching, stripped from the inbound result
DEFAULT_INTERNAL_LINE_KEYS = ("line_id",)


class UnmatchableVersionError(ValueError):
    """
    Raised when the lines of a version cannot be matched unambiguously
    because the same sku appears on more than one line.
    """

    def __init__(self, version, sku):
        self.version = version
        self.sku = sku
        super().__init__(
            f"version {version!r} contains duplicate lines for sku {sku!r}: "
            "version data cannot be matched"
        )


class MergeResult:
    """
    Outcome of applying a single batch.
    """

    def __init__(self, applied, conflicts=None):
        self.applied = applied
        self.conflicts = conflicts if conflicts is not None else []

    def __repr__(self):
        return f"MergeResult(applied={self.applied!r}, conflicts={self.conflicts!r})"


class OrderMerger:
    """
    Merges revision batches into a base order kept as a benedict.
    """

    def __init__(self, order, internal_line_keys=DEFAULT_INTERNAL_LINE_KEYS):
        self.order = benedict(order)
        self.internal_line_keys = tuple(internal_line_keys)
        self.conflicts = []
        self._applied_batch_ids = set()
        self._removed_lines = {}

    def apply(self, batch):
        """
        Applies a revision batch to the current order.

        Returns a :class:`MergeResult`; re-applying an already processed
        ``batch_id`` is a no-op returning ``MergeResult(applied=False)``.
        Raises :class:`UnmatchableVersionError` when the version data
        contains duplicate skus.
        """
        batch = benedict(batch)
        batch_id = batch["batch_id"]
        if batch_id in self._applied_batch_ids:
            return MergeResult(applied=False)

        source = batch.get("source")
        working = self.order.clone()
        lines = working.setdefault("lines", [])
        self._check_unique_skus(working)

        staged_removed = {}
        new_conflicts = []
        for operation in batch.get("operations", []):
            kind = operation.get("op")
            if kind == "remove_line":
                self._remove_line(
                    lines, operation, source, batch_id, staged_removed, new_conflicts
                )
            elif kind == "add_line":
                self._add_line(working, lines, operation)
            elif kind == "patch_line":
                self._patch_line(
                    lines, operation, source, batch_id, staged_removed, new_conflicts
                )
            else:
                raise ValueError(f"unsupported operation: {kind!r}")

        if "version" in batch:
            working["version"] = batch["version"]

        self.order = working
        self._removed_lines.update(staged_removed)
        self.conflicts.extend(new_conflicts)
        self._applied_batch_ids.add(batch_id)
        return MergeResult(applied=True, conflicts=new_conflicts)

    def inbound_result(self):
        """
        Returns the merged order as a benedict containing only business
        fields, preserving the order of the lines that were not removed.
        """
        result = self.order.clone()
        result["lines"] = [
            {
                key: value
                for key, value in line.items()
                if key not in self.internal_line_keys
            }
            for line in result.get("lines", [])
        ]
        return result

    def _check_unique_skus(self, order):
        seen = set()
        for line in order.get("lines", []):
            sku = line.get("sku")
            if sku in seen:
                raise UnmatchableVersionError(order.get("version"), sku)
            seen.add(sku)

    def _find_line_index(self, lines, operation):
        line_id = operation.get("line_id")
        sku = operation.get("sku")
        for index, line in enumerate(lines):
            if line_id is not None:
                if line.get("line_id") != line_id:
                    continue
                if sku is not None and line.get("sku") != sku:
                    continue
                return index
            elif sku is not None and line.get("sku") == sku:
                return index
        return None

    def _remove_line(self, lines, operation, source, batch_id, removed, conflicts):
        index = self._find_line_index(lines, operation)
        if index is None:
            conflicts.append(
                self._new_conflict(source, batch_id, operation, old_line=None)
            )
            return
        line = lines.pop(index)
        line_id = line.get("line_id")
        if line_id is not None:
            removed[line_id] = dict(line)

    def _add_line(self, order, lines, operation):
        line = dict(operation["line"])
        sku = line.get("sku")
        if any(existing.get("sku") == sku for existing in lines):
            raise UnmatchableVersionError(order.get("version"), sku)
        lines.append(line)

    def _patch_line(self, lines, operation, source, batch_id, removed, conflicts):
        index = self._find_line_index(lines, operation)
        if index is None:
            old_line = removed.get(operation.get("line_id")) or self._removed_lines.get(
                operation.get("line_id")
            )
            conflicts.append(
                self._new_conflict(source, batch_id, operation, old_line=old_line)
            )
            return
        line = lines[index]
        for key, value in operation.items():
            if key in _PATCH_META_KEYS:
                continue
            # absent keys are left untouched; an explicit None clears the value
            line[key] = value

    @staticmethod
    def _new_conflict(source, batch_id, operation, old_line):
        return {
            "source": source,
            "batch_id": batch_id,
            "reason": "line_not_found",
            "patch": {
                key: value for key, value in operation.items() if key != "op"
            },
            "line": old_line,
        }
