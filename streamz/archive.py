"""Device hour archive: event-time windowing over multiple observation
sources with per-epoch watermarks and reader-head handoff.

Each device is observed by several sources ("legs").  Every source
publishes watermarks declaring that no new observations older than a
given UTC second will be sent.  A source may replace its reader head:
it then emits a handoff record for the device stating the last sequence
number the old epoch may send and the sequence where the new epoch
starts.  The two epochs replay an overlapping range, so observations
with the same device/source/sequence are counted only once, and a
watermark from the old epoch only vouches for old-epoch records.

A window is sealed only when every epoch of every relevant source is
settled for that window: active epochs need a watermark past the window
end, ended epochs need every sequence in the handoff interval accounted
for.  Taking the minimum watermark of the currently active sources is
not sufficient.

Sealed windows are emitted downstream as ``window_sealed`` events.
Observations arriving for an already sealed window are routed to the
``late`` outlet.  Writes are acknowledged explicitly: only the exact
batch of raw observations that was emitted can be confirmed, a failed
write can be retried with identical content, and late watermarks from
an old reader head never change sealed or following windows.
"""

from __future__ import absolute_import, division, print_function

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .core import Stream

__all__ = ["DeviceHourArchive"]


def _normalize_time(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, timedelta):
        raise TypeError("expected a timestamp, got a timedelta")
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


class _DeviceState(object):
    def __init__(self):
        self.sources = set()
        self.epochs = defaultdict(set)        # source -> {epoch, ...}
        self.handoffs = defaultdict(list)     # source -> [handoff, ...]
        self.ended = {}                       # (source, epoch) -> last_seq
        self.windows = defaultdict(dict)      # window_start -> {(source, seq): record}
        self.seq_seen = defaultdict(set)      # source -> {seq, ...}
        self.sealed = {}                      # window_start -> batch info
        self.duplicates = 0


class DeviceHourArchive(Stream):
    """Aggregate observations into event-time windows per device.

    Parameters
    ----------
    upstream: Stream
        Stream of event dicts.  Supported kinds:

        - ``observation``: ``device``, ``source``, ``seq``,
          ``event_time`` and optionally ``epoch`` (default 0).
        - ``watermark``: ``source``, ``time`` and optionally ``epoch``
          and ``device`` (a device-less watermark is source-wide).
        - ``handoff``: ``device``, ``source``, ``old_epoch``,
          ``new_epoch``, ``old_last_seq``, ``new_start_seq``.
        - ``epoch_end``: ``device``, ``source``, ``epoch`` and
          optionally ``last_seq``.
    window: int, optional
        Window size in seconds, aligned to UTC boundaries (default 60).

    Emits ``window_sealed`` events downstream.  Use the ``.sealed`` and
    ``.late`` views to split sealed windows from late observations.
    """

    def __init__(self, upstream, window=60, **kwargs):
        self.window_seconds = int(window)
        self._states = defaultdict(_DeviceState)
        self._wm_source = {}   # (source, epoch) -> datetime
        self._wm_device = {}   # (device, source, epoch) -> datetime
        self._wm_sources = set()
        Stream.__init__(self, upstream, **kwargs)

        self.sealed = self.filter(lambda event: event["kind"] == "window_sealed")
        self.late = self.filter(lambda event: event["kind"] == "late")

    # ------------------------------------------------------------------
    # event intake
    # ------------------------------------------------------------------
    def update(self, x, who=None, metadata=None):
        kind = x.get("kind", "observation")
        if kind == "observation":
            self._on_observation(x, metadata)
        elif kind == "watermark":
            self._on_watermark(x, metadata)
        elif kind == "handoff":
            self._on_handoff(x, metadata)
        elif kind == "epoch_end":
            self._on_epoch_end(x, metadata)
        else:
            raise ValueError("unknown event kind: %r" % (kind,))
        self._maybe_seal_all(metadata)

    def _on_observation(self, record, metadata):
        device = record["device"]
        source = record["source"]
        epoch = record.get("epoch", 0)
        seq = record["seq"]
        event_time = _normalize_time(record["event_time"])
        state = self._states[device]
        state.sources.add(source)
        state.epochs[source].add(epoch)

        key = (source, seq)
        if seq in state.seq_seen[source]:
            state.duplicates += 1
            return
        start = self._window_start(event_time)
        if start in state.sealed:
            self._emit({
                "kind": "late",
                "device": device,
                "source": source,
                "window_start": start,
                "record": record,
                "reason": "window_already_sealed",
            }, metadata=metadata)
            return
        state.seq_seen[source].add(seq)
        state.windows[start][key] = record

    def _on_watermark(self, event, metadata):
        source = event["source"]
        epoch = event.get("epoch", 0)
        moment = _normalize_time(event["time"])
        device = event.get("device")
        table = self._wm_device if device is not None else self._wm_source
        key = (device, source, epoch) if device is not None else (source, epoch)
        if key in table and table[key] >= moment:
            return
        table[key] = moment
        self._wm_sources.add(source)

    def _on_handoff(self, event, metadata):
        device = event["device"]
        source = event["source"]
        state = self._states[device]
        state.sources.add(source)
        state.epochs[source].add(event["old_epoch"])
        state.epochs[source].add(event["new_epoch"])
        state.handoffs[source].append({
            "old_epoch": event["old_epoch"],
            "new_epoch": event["new_epoch"],
            "old_last_seq": event["old_last_seq"],
            "new_start_seq": event["new_start_seq"],
        })

    def _on_epoch_end(self, event, metadata):
        device = event["device"]
        source = event["source"]
        epoch = event["epoch"]
        state = self._states[device]
        state.sources.add(source)
        state.epochs[source].add(epoch)
        last_seq = event.get("last_seq")
        if last_seq is None:
            lasts = [h["old_last_seq"] for h in state.handoffs[source]
                     if h["old_epoch"] == epoch]
            last_seq = max(lasts) if lasts else None
        state.ended[(source, epoch)] = last_seq

    # ------------------------------------------------------------------
    # window bookkeeping
    # ------------------------------------------------------------------
    def _window_start(self, moment):
        epoch_seconds = int(moment.timestamp())
        aligned = epoch_seconds - (epoch_seconds % self.window_seconds)
        return datetime.fromtimestamp(aligned, tz=timezone.utc)

    def _watermark(self, device, source, epoch):
        candidates = []
        device_wm = self._wm_device.get((device, source, epoch))
        source_wm = self._wm_source.get((source, epoch))
        if device_wm is not None:
            candidates.append(device_wm)
        if source_wm is not None:
            candidates.append(source_wm)
        return max(candidates) if candidates else None

    def _gaps(self, state, source):
        """Missing sequence segments for a source, as (start, end) ranges."""
        segments = []
        seen = state.seq_seen.get(source, set())
        if not seen:
            return segments
        lows = [min(seen)]
        highs = []
        for handoff in state.handoffs.get(source, []):
            lows.append(handoff["new_start_seq"])
            highs.append(handoff["old_last_seq"])
        for (src, _epoch), last_seq in state.ended.items():
            if src == source and last_seq is not None:
                highs.append(last_seq)
        if not highs:
            return segments
        lo, hi = min(lows), max(highs)
        missing = [seq for seq in range(lo, hi + 1) if seq not in seen]
        for seq in missing:
            if segments and seq == segments[-1][1] + 1:
                segments[-1][1] = seq
            else:
                segments.append([seq, seq])
        return [tuple(seg) for seg in segments]

    def _gap_windows(self, state, source, gap):
        """Windows a missing segment may belong to, from its neighbours."""
        seen = state.seq_seen.get(source, set())
        prev_seq = max((s for s in seen if s < gap[0]), default=None)
        next_seq = min((s for s in seen if s > gap[1]), default=None)
        moments = []
        for window_records in state.windows.values():
            for (src, seq), record in window_records.items():
                if src == source and seq in (prev_seq, next_seq):
                    moments.append(_normalize_time(record["event_time"]))
        for info_start, info in state.sealed.items():
            for record in info["records"]:
                if (record["source"] == source
                        and record["seq"] in (prev_seq, next_seq)):
                    moments.append(_normalize_time(record["event_time"]))
        if not moments:
            return set()
        first = self._window_start(min(moments))
        last = self._window_start(max(moments))
        step = timedelta(seconds=self.window_seconds)
        windows = set()
        current = first
        while current <= last:
            windows.add(current)
            current += step
        return windows

    def _pending_reasons(self, device, state, window_start):
        reasons = []
        window_end = window_start + timedelta(seconds=self.window_seconds)
        sources = set(state.sources) | self._wm_sources
        for source in sorted(sources):
            epochs = state.epochs.get(source) or {0}
            for epoch in sorted(epochs):
                if (source, epoch) in state.ended:
                    continue  # settled via sequence accounting below
                watermark = self._watermark(device, source, epoch)
                if watermark is None or watermark < window_end:
                    reasons.append({
                        "kind": "watermark_behind",
                        "device": device,
                        "source": source,
                        "epoch": epoch,
                        "watermark": watermark,
                        "required": window_end,
                        "window_start": window_start,
                    })
            for gap in self._gaps(state, source):
                if window_start not in self._gap_windows(state, source, gap):
                    continue
                old_epochs = {h["old_epoch"] for h in state.handoffs.get(source, [])}
                replay_from = {h["new_start_seq"] for h in state.handoffs.get(source, [])}
                reasons.append({
                    "kind": "missing_sequences",
                    "device": device,
                    "source": source,
                    "epoch": sorted(old_epochs)[0] if old_epochs else None,
                    "seq_start": gap[0],
                    "seq_end": gap[1],
                    "coverable_by_replay": bool(replay_from)
                    and gap[0] >= min(replay_from),
                    "window_start": window_start,
                })
        return reasons

    def _maybe_seal_all(self, metadata):
        for device in sorted(self._states):
            state = self._states[device]
            for window_start in sorted(list(state.windows)):
                if window_start in state.sealed:
                    continue
                if self._pending_reasons(device, state, window_start):
                    continue
                self._seal(device, state, window_start, metadata)

    def _seal(self, device, state, window_start, metadata):
        records = sorted(
            state.windows.pop(window_start).values(),
            key=lambda r: (_normalize_time(r["event_time"]), r["source"], r["seq"]),
        )
        info = {
            "records": records,
            "confirmed": False,
            "failed": False,
            "attempts": 1,
        }
        state.sealed[window_start] = info
        self._emit({
            "kind": "window_sealed",
            "device": device,
            "window_start": window_start,
            "window_end": window_start + timedelta(seconds=self.window_seconds),
            "records": records,
            "attempt": info["attempts"],
        }, metadata=metadata)

    # ------------------------------------------------------------------
    # write acknowledgement
    # ------------------------------------------------------------------
    def window_state(self, device, window_start):
        state = self._states.get(device)
        if state is None:
            return None
        start = _normalize_time(window_start)
        info = state.sealed.get(start)
        if info is not None:
            if info["confirmed"]:
                return "confirmed"
            if info["failed"]:
                return "failed"
            return "sealed"
        if start in state.windows:
            return "open"
        return None

    def confirm_write(self, device, window_start, records):
        """Confirm the batch that was actually written for a window.

        Only the exact batch of raw observations that was emitted when
        the window sealed is accepted.
        """
        state = self._states.get(device)
        if state is None:
            return False
        info = state.sealed.get(_normalize_time(window_start))
        if info is None or info["confirmed"]:
            return False
        if list(records) != list(info["records"]):
            return False
        info["confirmed"] = True
        info["failed"] = False
        return True

    def fail_write(self, device, window_start):
        state = self._states.get(device)
        if state is None:
            return False
        info = state.sealed.get(_normalize_time(window_start))
        if info is None or info["confirmed"]:
            return False
        info["failed"] = True
        return True

    def retry_write(self, device, window_start, metadata=None):
        """Re-emit a sealed window with exactly the same content."""
        state = self._states.get(device)
        if state is None:
            return None
        start = _normalize_time(window_start)
        info = state.sealed.get(start)
        if info is None or info["confirmed"]:
            return None
        info["attempts"] += 1
        info["failed"] = False
        event = {
            "kind": "window_sealed",
            "device": device,
            "window_start": start,
            "window_end": start + timedelta(seconds=self.window_seconds),
            "records": list(info["records"]),
            "attempt": info["attempts"],
        }
        self._emit(event, metadata=metadata)
        return event

    def pending_reasons(self, device=None, window_start=None):
        """Explain why windows are still waiting and which segment is missing."""
        reasons = []
        devices = [device] if device is not None else sorted(self._states)
        for dev in devices:
            state = self._states.get(dev)
            if state is None:
                continue
            starts = ([_normalize_time(window_start)]
                      if window_start is not None
                      else sorted(state.windows))
            for start in starts:
                if start in state.sealed:
                    continue
                reasons.extend(self._pending_reasons(dev, state, start))
        return reasons

    def duplicates(self, device):
        state = self._states.get(device)
        return state.duplicates if state is not None else 0


Stream.register_api(attribute_name="device_hour_archive")(DeviceHourArchive)
