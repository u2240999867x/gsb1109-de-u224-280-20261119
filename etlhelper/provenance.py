"""
Optional provenance (source row) tracking for transform functions.

A transform function normally returns rows of data only.  Some workflows,
however, create a single output row from one or more input rows, and it is
useful to know which input rows were responsible for each output if the
database rejects that output.

Wrap an output row in an :class:`Output` to attach its source rows::

    def transform(chunk):
        by_station = {row["station"]: row for row in chunk}
        yield Output(flag_row, sources=[by_station["A"]])
        yield Output(comparison_row, sources=[by_station["A"],
                                              by_station["B"]])

Sources are matched to the input rows of the same transform call by object
identity.  Values, IDs and position within the chunk are deliberately not used
to infer a source, because two different rows can have exactly the same
values.

A source may also be declared explicitly absent with :func:`no_source`.  Plain
rows (the historical transform return value) carry no provenance at all.
"""
from __future__ import annotations

from collections.abc import (
    Iterable,
    Mapping,
)
from typing import (
    Any,
    Optional,
)

from etlhelper.exceptions import (
    ETLHelperInvalidSourceError,
)

# Sentinel used to distinguish "no sources argument supplied" from an explicit
# declaration that no source can be confirmed.
_UNSET = object()


class _NoSource:
    """Singleton representing an explicit declaration of no source rows."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "NO_SOURCE"

    def __bool__(self):
        return False


#: Explicitly declare that no input row can be confirmed as the source.
NO_SOURCE = _NoSource()

#: Alias for :data:`NO_SOURCE`.
UNKNOWN_SOURCE = NO_SOURCE


class SourceRows(tuple):
    """
    Immutable collection of source rows.

    It behaves as a tuple but compares equal to any sequence with the same
    items, so both ``failed.sources == (a, b)`` and
    ``failed.sources == [a, b]`` hold.
    """

    __slots__ = ()

    def __eq__(self, other):
        if isinstance(other, (list, tuple)):
            return tuple(self) == tuple(other)
        return tuple.__eq__(self, other)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    __hash__ = tuple.__hash__

    def __repr__(self):
        return f"SourceRows({list(self)!r})"


class Provenance:
    """
    Source information attached to a transform output.

    :param sources: source rows, or None when no source is known
    :param known: True if the caller explicitly provided source information
                  (including an explicit "no source" declaration)
    """

    __slots__ = ("sources", "known")

    def __init__(self, sources=None, known=True):
        if sources is None:
            self.sources = None
        else:
            self.sources = SourceRows(sources)
        self.known = known

    @property
    def source_rows(self):
        """Alias for :attr:`sources`."""
        return self.sources

    @property
    def inputs(self):
        """Alias for :attr:`sources`."""
        return self.sources

    @property
    def has_sources(self):
        """Return True when at least one source row is recorded."""
        return bool(self.sources)

    def __eq__(self, other):
        if not isinstance(other, Provenance):
            return NotImplemented
        return (self.sources, self.known) == (other.sources, other.known)

    def __hash__(self):
        return hash((self.sources, self.known))

    def __repr__(self):
        return f"Provenance(sources={self.sources!r}, known={self.known!r})"


class SourceRef:
    """
    An unambiguous reference to an input row.

    Source rows can be passed directly; this wrapper is provided for cases
    where a list or tuple is itself a business row and would otherwise be
    confused with a collection of sources.
    """

    __slots__ = ("row",)

    def __init__(self, row):
        self.row = row

    @property
    def source_row(self):
        """Alias for :attr:`row`."""
        return self.row

    def __repr__(self):
        return f"SourceRef({self.row!r})"


class Output:
    """
    A transform output row together with optional source information.

    :param row: the business row to insert
    :param sources: one source row, an iterable of source rows, None, or
                    :data:`NO_SOURCE`
    :param provenance: a :class:`Provenance` instance (alternative to sources)
    :param source: a single source row (alternative to sources)
    :param inputs: an iterable of source rows (alternative to sources)

    The wrapper behaves like the wrapped row for normal access (equality,
    indexing, iteration and attribute access), but database functions unwrap
    it so that only business columns are sent to the database.
    """

    __slots__ = ("row", "_provenance")

    def __init__(
            self,
            row: Any,
            sources: Any = _UNSET,
            *,
            provenance: Any = _UNSET,
            source: Any = _UNSET,
            inputs: Any = _UNSET,
            ) -> None:
        supplied = [
            name for name, value in (
                ("sources", sources),
                ("provenance", provenance),
                ("source", source),
                ("inputs", inputs),
            ) if value is not _UNSET
        ]
        if len(supplied) > 1:
            raise TypeError(
                "Only one of 'sources', 'provenance', 'source' or 'inputs' "
                "may be supplied."
            )

        if provenance is not _UNSET:
            if isinstance(provenance, Provenance):
                prov = provenance
            else:
                prov = Provenance(_normalise_sources(provenance), known=True)
        elif source is not _UNSET:
            prov = Provenance(_normalise_single_source(source), known=True)
        elif inputs is not _UNSET:
            prov = Provenance(_normalise_sources(inputs), known=True)
        elif sources is not _UNSET:
            prov = Provenance(_normalise_sources(sources), known=True)
        else:
            # No source information was supplied; this is a plain, legacy
            # output rather than an explicit "no source" declaration.
            prov = None

        self.row = row
        self._provenance = prov

    # -- Provenance access -------------------------------------------------
    @property
    def provenance(self) -> Optional[Provenance]:
        return self._provenance

    @property
    def sources(self):
        return None if self._provenance is None else self._provenance.sources

    @property
    def source_rows(self):
        return self.sources

    @property
    def inputs(self):
        return self.sources

    @property
    def has_sources(self):
        return bool(self.sources)

    @property
    def source_known(self):
        return bool(self._provenance is not None and self._provenance.known)

    # -- Row proxy ---------------------------------------------------------
    def __getitem__(self, key):
        return self.row[key]

    def __setitem__(self, key, value):
        self.row[key] = value

    def __iter__(self):
        return iter(self.row)

    def __len__(self):
        return len(self.row)

    def __contains__(self, item):
        return item in self.row

    def __getattr__(self, name):
        # __slots__ means missing attributes are not found on the instance.
        # Delegate everything else to the wrapped row (e.g. namedtuple fields,
        # ``keys``, ``_asdict``, ``_replace``).
        if name in ("row", "_provenance"):
            raise AttributeError(name)
        return getattr(self.row, name)

    def __eq__(self, other):
        if isinstance(other, Output):
            return self.row == other.row
        return self.row == other

    def __hash__(self):
        return hash(self.row)

    def __repr__(self):
        if self._provenance is None:
            return f"Output({self.row!r})"
        return (f"Output({self.row!r}, sources={self._provenance.sources!r}, "
                f"known={self._provenance.known!r})")


# Convenient aliases for the wrapper.  All refer to the same class.
TransformOutput = Output
TransformResult = Output
OutputRow = Output
RowWithSources = Output
RowWithProvenance = Output
SourcedRow = Output
ProvenanceRow = Output


def _normalise_sources(sources):
    """
    Normalise the plural ``sources=`` argument into a tuple of rows (or None).

    No row values are inspected and nothing is inferred.  A plain tuple, list
    or set is treated as a collection of rows; use :class:`SourceRef` when a
    tuple-shaped or list-shaped object is itself a single row.
    """
    if sources is None or sources is NO_SOURCE:
        return None

    if isinstance(sources, Provenance):
        return sources.sources

    if isinstance(sources, SourceRef):
        return (sources.row,)

    if isinstance(sources, (str, bytes)):
        raise ETLHelperInvalidSourceError(
            f"Invalid source reference: {sources!r}"
        )

    if hasattr(sources, "_fields") or isinstance(sources, Mapping):
        return (sources,)

    if isinstance(sources, Iterable):
        return tuple(_unwrap_reference(item) for item in sources)

    return (sources,)


def _normalise_single_source(source):
    """Convert a ``source=`` argument into a tuple with one row, or None."""
    if source is None or source is NO_SOURCE:
        return None
    if isinstance(source, SourceRef):
        return (source.row,)
    if hasattr(source, "_fields") or isinstance(source, Mapping):
        return (source,)
    # ``source=`` is documented as a single row, so it is never split even if
    # the row itself is a tuple or list.
    return (source,)


def _unwrap_reference(item):
    if isinstance(item, SourceRef):
        return item.row
    return item


def with_sources(row, *sources):
    """
    Return an :class:`Output` for ``row`` with the given source rows.

    ``sources`` can be supplied as separate arguments or as a single iterable::

        with_sources(out, source_a, source_b)
        with_sources(out, [source_a, source_b])

    A namedtuple or mapping is always treated as one row.  Use
    :class:`SourceRef` when a tuple-shaped or list-shaped object is itself a
    single row.
    """
    if len(sources) == 1 and not isinstance(sources[0], SourceRef):
        candidate = sources[0]
        if candidate is NO_SOURCE or candidate is None:
            return Output(row, sources=candidate)
        if hasattr(candidate, "_fields") or isinstance(candidate, Mapping):
            return Output(row, sources=[candidate])
        if isinstance(candidate, Iterable) and not isinstance(
                candidate, (str, bytes)):
            return Output(row, sources=list(candidate))
    return Output(row, sources=list(sources))


def with_source(row, source):
    """Return an :class:`Output` for ``row`` derived from one source row."""
    return Output(row, source=source)


def with_provenance(row, sources):
    """Alias for :func:`with_sources`."""
    return Output(row, sources=sources)


def no_source(row):
    """
    Return an :class:`Output` that explicitly has no confirmable source row.
    """
    return Output(row, sources=NO_SOURCE)


def without_source(row):
    """Alias for :func:`no_source`."""
    return no_source(row)


def unknown_source(row):
    """Alias for :func:`no_source`."""
    return no_source(row)


def unwrap_row(row):
    """Return the business row from an :class:`Output`, or ``row`` unchanged."""
    while isinstance(row, Output):
        row = row.row
    return row


def get_sources(row):
    """
    Return the source rows attached to ``row``.

    Plain rows (without provenance) return None.
    """
    if isinstance(row, Output):
        return row.sources
    return None


def get_provenance(row):
    """Return the :class:`Provenance` attached to ``row`` (or None)."""
    if isinstance(row, Output):
        return row.provenance
    return None


_ROW_WRAPPER_ATTRS = (
    ("row", "sources"),
    ("data", "sources"),
    ("value", "sources"),
    ("row", "source_rows"),
    ("data", "source_rows"),
    ("row", "inputs"),
    ("data", "inputs"),
)

_RESERVED_WRAPPER_KEYS = (
    ("row", "sources"),
    ("row", "source"),
    ("row", "source_rows"),
    ("row", "inputs"),
    ("output", "sources"),
    ("output", "source_rows"),
)


def _coerce_duck_typed_output(item, input_rows=None):
    """Recognise user-defined wrapper objects."""
    for row_attr, sources_attr in _ROW_WRAPPER_ATTRS:
        if hasattr(item, row_attr) and hasattr(item, sources_attr):
            return Output(getattr(item, row_attr),
                          sources=getattr(item, sources_attr))
    return None


def pragmatic_output_row(item):
    """
    Best-effort unwrap of an output for use where validation is impossible.

    ``load`` applies the transform to a single copied row to discover the
    insert columns before the real chunks are processed.  At that point the
    source rows cannot be validated, but the business row must still be
    extracted from any of the supported wrapper forms.
    """
    output = _coerce_output(item, None)
    if output is not None:
        return unwrap_row(output)
    return unwrap_row(item)


def prepare_output(item, input_rows, validate=True):
    """
    Normalise one item returned by a transform into either a plain row or an
    :class:`Output`.

    The input rows are the exact objects passed to the transform function.  If
    ``validate`` is true, every supplied source must be one of those objects
    (identity comparison).  Otherwise an
    :class:`~etlhelper.exceptions.ETLHelperInvalidSourceError` is raised.

    :param item: a transform output
    :param input_rows: the rows that were passed to the transform
    :param validate: whether to validate source references
    :return: a plain row or an :class:`Output`
    """
    output = _coerce_output(item, input_rows)

    if output is None:
        return item

    if validate:
        _validate_sources(output, input_rows)
    return output


def _coerce_output(item, input_rows):
    """Return the item as an :class:`Output`, or None when it is a plain row."""
    if isinstance(item, Output):
        return item

    if isinstance(item, Mapping) and _is_wrapper_mapping(item):
        row = next(item[k] for k in ("row", "output") if k in item)
        if "sources" in item:
            sources = _normalise_sources(item["sources"])
        elif "source" in item:
            sources = _normalise_single_source(item["source"])
        elif "source_rows" in item:
            sources = _normalise_sources(item["source_rows"])
        else:
            sources = _normalise_sources(item.get("inputs", NO_SOURCE))
        return Output(row, sources=sources)

    if isinstance(item, (tuple, list)) and len(item) == 2:
        # Support the compact ``yield row, sources`` form.  The second item
        # must consist of this transform's input rows (by identity), or be an
        # explicit NO_SOURCE/Provenance marker, so that a plain two-column
        # tuple row is never misinterpreted.
        candidate_row, candidate_sources = item
        if _is_explicit_source_argument(candidate_sources, input_rows):
            return Output(candidate_row, sources=candidate_sources)

    output = _coerce_duck_typed_output(item, input_rows)
    if output is not None:
        return output

    return None


def _is_wrapper_mapping(item):
    if "__etlhelper_provenance__" in item:
        return True
    keys = set(item.keys())
    return any(set(pair).issubset(keys) for pair in _RESERVED_WRAPPER_KEYS)


def _is_explicit_source_argument(value, input_rows=None):
    """Return True only for values that are unambiguously source information."""
    if value is NO_SOURCE or isinstance(value, (Provenance, SourceRef, Output)):
        return True
    if isinstance(value, (list, tuple, set, frozenset)):
        if not value:
            return False
        if all(isinstance(v, (SourceRef, Output)) for v in value):
            return True
        if input_rows is not None and all(
                any(_same_source(v, candidate) for candidate in input_rows)
                for v in value):
            return True
    return False


def _same_source(source, candidate):
    source_row = source.row if isinstance(source, SourceRef) else source
    source_row = unwrap_row(source_row)
    return source_row is unwrap_row(candidate)


def _validate_sources(output, input_rows):
    """Ensure every source in ``output`` was part of this transform input."""
    if output.provenance is None:
        return

    sources = output.sources
    if not sources:
        return

    input_rows = list(input_rows)
    for source in sources:
        source_row = source.row if isinstance(source, SourceRef) else source
        if isinstance(source_row, Output):
            source_row = source_row.row

        # Identity, not equality: equal-valued (or equal-ID) rows and their
        # position in the chunk must never be used to infer a source.
        if not any(source_row is candidate for candidate in input_rows):
            raise ETLHelperInvalidSourceError(
                "Transform output refers to a source row that was not part of "
                f"the same input chunk: {source_row!r}"
            )


def sources_of(failed_row):
    """Return the source rows reported for a failed row."""
    return getattr(failed_row, "sources", None)
