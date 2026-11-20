"""Per-source write receipts for partial batch outcomes.

The ``/api/v2/write`` endpoint returns a body only when a request fails and, at
best, names the first line that failed. A single HTTP request however carries
lines from several upstream sources and the batching writer may split one
logical write into several requests. A recovery program therefore cannot tell
from the regular batch callback which *input* lines were confirmed, which were
definitely rejected and which simply have no verifiable outcome.

This module is purely client-side bookkeeping. It correlates the lines that
were actually emitted in an HTTP request with the source and the per-source
occurrence they came from, and turns a (possibly partial) server response into
a :class:`WriteReceipt`. It never adds tags, fields or any other marker to the
line protocol sent over the wire.

Evidence rules on purpose stay conservative:

* a whole request is only fully confirmed when the whole request succeeded;
* a line is only claimed when the response names its exact in-request position
  with an interpretable per-line conclusion;
* aggregate rejection counts, identical line text or numbers mentioned in an
  error message are never used to infer a conclusion;
* everything else, including a lost response or contradictory diagnostics,
  remains :attr:`LineStatus.UNKNOWN`.
"""

import json
import logging
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger('influxdb_client.client.write.receipt')


class LineStatus(str, Enum):
    """Outcome of a single emitted line protocol line."""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


# Keys accepted when a test service reports per-line diagnostics. Only the
# position and the conclusion are treated as evidence; everything else is
# carried along as a human readable reason.
_INDEX_KEYS = ("line_index", "lineIndex", "line_position", "linePosition", "index", "position", "line")
_STATUS_KEYS = ("status", "result", "outcome", "conclusion", "decision")
_REASON_KEYS = ("reason", "message", "error", "detail", "details")
_LIST_KEYS = ("lines", "results", "details", "diagnostics", "line_results", "lineResults")

_ACCEPTED_TOKENS = frozenset((
    "accepted", "accept", "success", "successful", "ok", "written", "confirmed", "confirm",
))
_REJECTED_TOKENS = frozenset((
    "rejected", "reject", "error", "failed", "failure", "invalid",
))


class LineOrigin(object):
    """Provenance of one line protocol line before it is batched into a request."""

    def __init__(self, source, occurrence: Optional[int], line: bytes) -> None:
        self.source = source
        self.occurrence = occurrence
        self.line = line

    def __repr__(self) -> str:
        return "LineOrigin[source={!r}, occurrence={!r}]".format(self.source, self.occurrence)


class LineResult(object):
    """Conclusion for one line, addressed by its position inside the HTTP request."""

    def __init__(self, request_index: int, line_index: int, origin: LineOrigin,
                 status: LineStatus, reason: Optional[str] = None) -> None:
        self.request_index = request_index
        self.line_index = line_index
        self.origin = origin
        self.status = status
        self.reason = reason

    @property
    def source(self):
        return self.origin.source

    @property
    def occurrence(self):
        return self.origin.occurrence

    @property
    def line(self) -> bytes:
        return self.origin.line

    def __repr__(self) -> str:
        return "LineResult[request={}, line={}, source={!r}, occurrence={!r}, status={}]".format(
            self.request_index, self.line_index, self.source, self.occurrence, self.status.value)


class SourceReceipt(object):
    """Per-line evidence aggregated for one upstream source within a single request."""

    def __init__(self, source) -> None:
        self.source = source
        self.confirmed: List[int] = []
        self.rejected: List[LineResult] = []
        self.unknown: List[int] = []

    @property
    def is_fully_confirmed(self) -> bool:
        return not self.rejected and not self.unknown

    def __repr__(self) -> str:
        return "SourceReceipt[source={!r}, confirmed={}, rejected={}, unknown={}]".format(
            self.source, self.confirmed,
            [(r.occurrence, r.status.value) for r in self.rejected], self.unknown)


class WriteReceipt(object):
    """The handoff object for one HTTP write request, grouped by upstream source."""

    def __init__(self, request_index: int = 0) -> None:
        self.request_index = request_index
        self._results: List[LineResult] = []
        self._by_source: Dict[object, SourceReceipt] = {}

    def add_result(self, result: LineResult) -> None:
        self._results.append(result)
        source_receipt = self._by_source.setdefault(result.source, SourceReceipt(result.source))
        if result.status is LineStatus.CONFIRMED:
            source_receipt.confirmed.append(result.occurrence)
        elif result.status is LineStatus.REJECTED:
            source_receipt.rejected.append(result)
        else:
            source_receipt.unknown.append(result.occurrence)

    @property
    def lines(self) -> List[LineResult]:
        """All lines in request/line position order, including unattributed ones."""
        return list(self._results)

    @property
    def sources(self) -> List[object]:
        return list(self._by_source.keys())

    def for_source(self, source) -> SourceReceipt:
        return self._by_source[source]

    def status_of(self, source, occurrence: int) -> LineStatus:
        source_receipt = self._by_source.get(source)
        if source_receipt is None:
            raise KeyError(source)
        if occurrence in source_receipt.confirmed:
            return LineStatus.CONFIRMED
        if occurrence in source_receipt.unknown:
            return LineStatus.UNKNOWN
        for result in source_receipt.rejected:
            if result.occurrence == occurrence:
                return LineStatus.REJECTED
        raise KeyError((source, occurrence))

    def __iter__(self):
        return iter(self._results)

    def __len__(self) -> int:
        return len(self._results)


def _normalize_status(value) -> Optional[LineStatus]:
    if not isinstance(value, str):
        return None
    token = value.strip().lower()
    if token in _ACCEPTED_TOKENS:
        return LineStatus.CONFIRMED
    if token in _REJECTED_TOKENS:
        return LineStatus.REJECTED
    return None


def _extract_entries(payload) -> Optional[List[dict]]:
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = None
        for key in _LIST_KEYS:
            if isinstance(payload.get(key), list):
                entries = payload[key]
                break
        if entries is None:
            return None
    else:
        return None

    if not entries or not all(isinstance(entry, dict) for entry in entries):
        return None
    return entries


def parse_line_diagnostics(body, expected_count: int) -> Optional[Dict[int, LineStatus]]:
    """
    Parse verifiable per-line conclusions out of an error response body.

    Returns a mapping of zero-based in-request line position to a status, or
    ``None`` when the body carries no usable per-line information. Raises
    :class:`ValueError` when the same position is assigned conflicting
    conclusions, so callers can refuse to claim any line from such a response.
    """
    if body is None:
        return None
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(body, str) or not body.strip():
        return None

    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None

    entries = _extract_entries(payload)
    if entries is None:
        return None

    diagnostics: Dict[int, LineStatus] = {}
    for entry in entries:
        index = next((entry[key] for key in _INDEX_KEYS if key in entry), None)
        raw_status = next((entry[key] for key in _STATUS_KEYS if key in entry), None)
        if not isinstance(index, int) or isinstance(index, bool):
            return None
        if index < 0 or index >= expected_count:
            return None
        status = _normalize_status(raw_status)
        if status is None:
            # A position without an interpretable conclusion is not evidence.
            continue
        if index in diagnostics and diagnostics[index] != status:
            raise ValueError(
                "Conflicting per-line conclusions for line index {}: {} vs {}".format(
                    index, diagnostics[index].value, status.value))
        diagnostics[index] = status

    return diagnostics or None


def _reason_for(entry: Optional[dict]) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    for key in _REASON_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _diagnostic_entries(body) -> Optional[List[dict]]:
    if body is None:
        return None
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(body, str) or not body.strip():
        return None
    try:
        return _extract_entries(json.loads(body))
    except (ValueError, TypeError):
        return None


def build_receipt(line_origins: List[LineOrigin], http_status: Optional[int],
                  response_body, request_index: int = 0) -> WriteReceipt:
    """
    Build a :class:`WriteReceipt` for the lines actually sent in one request.

    :param line_origins: provenance of every emitted line, aligned by position
                         with the lines of the request body
    :param http_status: HTTP status of the response, or ``None`` if no response
                        was received
    :param response_body: decoded response body (for example a JSON error with
                          per-line diagnostics)
    """
    receipt = WriteReceipt(request_index=request_index)
    total = len(line_origins)

    diagnostics: Optional[Dict[int, LineStatus]] = None
    reasons: Dict[int, str] = {}
    conflicting = False

    if http_status is not None:
        try:
            diagnostics = parse_line_diagnostics(response_body, total)
        except ValueError as conflict:
            # Contradictory per-line conclusions are not trustworthy evidence.
            logger.warning("Ignoring unreliable per-line write diagnostics: %s", conflict)
            conflicting = True
            diagnostics = None

        if diagnostics is not None:
            for entry in (_diagnostic_entries(response_body) or []):
                index = next((entry[key] for key in _INDEX_KEYS if key in entry), None)
                if isinstance(index, int) and not isinstance(index, bool) and index in diagnostics:
                    reason = _reason_for(entry)
                    if reason is not None:
                        reasons[index] = reason
        elif not conflicting and 200 <= http_status < 300:
            # An entire successful request without per-line diagnostics confirms every line.
            diagnostics = {index: LineStatus.CONFIRMED for index in range(total)}

    for line_index, origin in enumerate(line_origins):
        status = diagnostics.get(line_index, LineStatus.UNKNOWN) if diagnostics is not None else LineStatus.UNKNOWN
        receipt.add_result(LineResult(
            request_index=request_index,
            line_index=line_index,
            origin=origin,
            status=status,
            reason=reasons.get(line_index),
        ))

    return receipt
