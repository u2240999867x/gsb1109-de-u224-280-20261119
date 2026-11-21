"""
Quality reporting over a transient json-stream parse.

An opt-in layer that observes an otherwise ordinary transient stream.  Every
``readings`` array (and any array nested inside one) that closes while its
enclosing device is live produces a *staged* :class:`ArraySummary` carrying its
own identity.  A summary is only *confirmed* when its device object closes
complete with a ``quality`` field equal to ``"accepted"``; a rejected or
truncated device *retracts* every summary it had staged.

The layer never reads ahead of the business code.  It only consumes the
remainder of an array when the business code moves on without draining it,
which is exactly what the underlying transient stream already does, so
implicit skipping and explicit iteration yield identical identities and
totals.
"""
from collections import Counter
from itertools import count

from json_stream.base import (
    StreamingJSONBase,
    StreamingJSONList,
    StreamingJSONObject,
    TransientStreamingJSONList,
    TransientStreamingJSONObject,
)

READINGS_KEY = "readings"
QUALITY_KEY = "quality"
ACCEPTED = "accepted"


def _type_name(value):
    if isinstance(value, StreamingJSONList):
        return "list"
    if isinstance(value, StreamingJSONObject):
        return "dict"
    return type(value).__name__


class ArraySummary:
    """Aggregate quality information for a single readings array."""

    __slots__ = ("identity", "path", "device_path", "types", "number_sum", "count")

    def __init__(self, identity, path, device_path):
        self.identity = identity
        self.path = path
        # path of the device object whose quality decision governs this summary
        self.device_path = device_path
        self.types = Counter()
        self.number_sum = 0
        self.count = 0

    def add(self, value):
        self.count += 1
        self.types[_type_name(value)] += 1
        # bool is a subclass of int but is not treated as a numeric reading
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        self.number_sum += value

    def as_dict(self):
        return {
            "identity": self.identity,
            "path": self.path,
            "count": self.count,
            "types": dict(self.types),
            "number_sum": self.number_sum,
        }

    def __eq__(self, other):
        if not isinstance(other, ArraySummary):
            return NotImplemented
        return self.identity == other.identity

    def __hash__(self):
        return hash(self.identity)

    def __repr__(self):  # pragma: no cover
        return f"ArraySummary({self.as_dict()!r})"


class _NodeState:
    """Reporting bookkeeping shared by the transient node subclasses."""

    def _init_report(self, report, parent, path, in_readings):
        self._report = report
        self._parent = parent
        self.path = path
        # true once this node is inside a readings-array subtree
        self.in_readings = in_readings
        # path of the device object whose quality decision governs this node
        self.device_path = parent.device_path if parent is not None else ()

    def _on_truncate(self):
        # the stream was cut: no further device decisions can happen, so every
        # summary still pending is retracted (grouped by its owning device)
        self._report._abort()


class _ReportingObject(_NodeState, TransientStreamingJSONObject):
    """A transient object that tracks its quality field and device lifecycle."""

    def __init__(self, token_stream, report, parent, path, in_readings):
        TransientStreamingJSONObject.__init__(self, token_stream)
        self._init_report(report, parent, path, in_readings)
        # an object outside a readings array is itself a device
        if not in_readings:
            self.device_path = path
        self.quality = None
        self.has_quality = False
        self._finalized = False

    def _load_item(self):
        key, value = super()._load_item()
        if key == QUALITY_KEY and not isinstance(value, StreamingJSONBase):
            self.has_quality = True
            self.quality = value
        return key, value

    def _create_child(self, token, token_stream):
        key = self._current_key
        path = self.path + (key,)
        if token == '{':
            return _ReportingObject(token_stream, self._report, self, path,
                                    self.in_readings)
        if key == READINGS_KEY:
            return _ReportingList(token_stream, self._report, self, path, True)
        return _BareList(token_stream, self._report, self, path,
                         self.in_readings)

    def _on_close(self):
        if self._finalized:
            return
        self._finalized = True
        # only devices (objects outside a readings array) can make a quality
        # decision; quality-less grouping objects leave summaries to an ancestor
        if self.in_readings:
            return
        if self.has_quality or self._parent is None:
            self._report._decide(self)


class _ReportingList(_NodeState, TransientStreamingJSONList):
    """A readings array whose direct items feed an :class:`ArraySummary`."""

    def __init__(self, token_stream, report, parent, path, in_readings):
        TransientStreamingJSONList.__init__(self, token_stream)
        self._init_report(report, parent, path, in_readings)
        self.summary = ArraySummary(next(report._identities), path,
                                    self.device_path)

    def _load_item(self):
        item = super()._load_item()
        self.summary.add(item)
        return item

    def _create_child(self, token, token_stream):
        # the list increments its index only after the item is built
        path = self.path + (self._index + 1,)
        if token == '{':
            return _ReportingObject(token_stream, self._report, self, path, True)
        # an inner supplementary array gets its own summary; its numbers are
        # never added to this enclosing array's total
        return _ReportingList(token_stream, self._report, self, path, True)

    def _on_close(self):
        self._report._stage(self)


class _BareList(_NodeState, TransientStreamingJSONList):
    """A non-readings array: streamed transparently, never summarised itself."""

    def __init__(self, token_stream, report, parent, path, in_readings):
        TransientStreamingJSONList.__init__(self, token_stream)
        self._init_report(report, parent, path, in_readings)

    def _create_child(self, token, token_stream):
        path = self.path + (self._index + 1,)
        if token == '{':
            return _ReportingObject(token_stream, self._report, self, path,
                                    self.in_readings)
        return _BareList(token_stream, self._report, self, path,
                         self.in_readings)


class QualityReport:
    """
    Drives quality reporting for a parse through user-supplied callbacks.

    As the business code consumes the stream the report invokes:

    * ``on_stage(summary)`` when a readings array closes; the summary carries
      its own identity and is held only until its device is decided;
    * ``on_confirm(device_path, summaries)`` once, when a device object closes
      complete with ``quality == "accepted"``;
    * ``on_retract(device_path, summaries)`` when a device is rejected, lacks
      a usable quality decision, or is truncated.

    Only summaries still awaiting a device decision are retained (visible via
    :attr:`staged`); confirmed and retracted summaries are handed to the
    callbacks and dropped, so the report never stockpiles whole arrays or the
    complete set of summaries.  Any callback may be omitted.
    """

    def __init__(self, on_stage=None, on_confirm=None, on_retract=None):
        self._on_stage = on_stage
        self._on_confirm = on_confirm
        self._on_retract = on_retract
        self._identities = count()
        self._live = {}

    def _root(self, token, token_stream):
        if token == '[':
            # a top-level array of devices: the array itself is not a device
            return _BareList(token_stream, self, None, (), False)
        return _ReportingObject(token_stream, self, None, (), False)

    def _stage(self, node):
        self._live[node.summary.identity] = node.summary
        if self._on_stage is not None:
            self._on_stage(node.summary)

    def _decide(self, device):
        summaries = self._take(device)
        if not summaries:
            return
        if device.has_quality and device.quality == ACCEPTED:
            if self._on_confirm is not None:
                self._on_confirm(device.path, summaries)
        elif self._on_retract is not None:
            self._on_retract(device.path, summaries)

    def _abort(self):
        # a truncated parse can no longer reach any device's closing brace:
        # retract every summary still pending, grouped by owning device
        grouped = {}
        for identity, summary in list(self._live.items()):
            grouped.setdefault(summary.device_path, []).append(summary)
            del self._live[identity]
        if self._on_retract is not None:
            for device_path, summaries in grouped.items():
                self._on_retract(device_path, summaries)

    def _take(self, device):
        summaries = [
            summary
            for identity, summary in list(self._live.items())
            if summary.device_path == device.path
        ]
        for summary in summaries:
            del self._live[summary.identity]
        return summaries

    @property
    def staged(self):
        """Summaries produced by closed arrays but not yet decided on."""
        return list(self._live.values())
