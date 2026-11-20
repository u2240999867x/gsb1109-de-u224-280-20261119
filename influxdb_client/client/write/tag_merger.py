"""Previewable, non-mutating tag merge for structured write records.

The merger accepts the structured records whose tags can be decided without a
round-trip to InfluxDB - :class:`~influxdb_client.client.write.point.Point`
and dictionaries (both the nested ``tags`` structure and the flat structure
driven by ``record_tag_keys``). For every record it reports:

* the tags carried by the record itself (``TagOrigin.RECORD``),
* the tags that were missing and got filled from the configured default tags
  (``TagOrigin.DEFAULT``),
* the tags the record or the configuration explicitly says must not be written
  (``TagOrigin.OMITTED``).

Neither the input objects nor the writer's settings are modified, so the same
record can be merged again for another destination with different defaults.
"""
from __future__ import absolute_import

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional

from influxdb_client.client.write.point import DEFAULT_WRITE_PRECISION, Point
from influxdb_client.domain.write_precision import WritePrecision

#: Sentinel used on a record (Point or dictionary) to explicitly state that the
#: tag must not be written, even when a default tag with the same key is
#: configured. Presence of the key is therefore distinguishable from the key
#: being absent altogether.
OMIT_TAG = object()


class TagOrigin(Enum):
    """Origin of a tag within a merged record."""

    #: Tag was supplied on the incoming record.
    RECORD = "record"
    #: Tag was missing on the record and got filled from the default tags.
    DEFAULT = "default"
    #: Tag is explicitly excluded from the line protocol.
    OMITTED = "omitted"


@dataclass
class TagProvenance:
    """A single tag together with the reason why it is (or is not) written."""

    key: str
    value: Optional[Any]
    origin: TagOrigin


@dataclass
class TagMergeOptions:
    """Configuration driving a tag merge.

    :param default_tags: tags added only when the record does not carry the key
    :param omit_tag_keys: keys that must never be written, regardless of the
                          value found on the record
    """

    default_tags: Dict[str, Any] = field(default_factory=dict)
    omit_tag_keys: List[str] = field(default_factory=list)

    @classmethod
    def from_point_settings(cls, point_settings=None, omit_tag_keys=None):
        default_tags = dict(getattr(point_settings, "defaultTags", {}) or {})
        return cls(default_tags=default_tags, omit_tag_keys=list(omit_tag_keys or []))


@dataclass
class MergedRecord:
    """The merge result for a single structured record."""

    #: The record as handed in by the caller (never mutated by the merger).
    source: Any
    #: A fresh, writable :class:`Point` built from the merged tags and fields.
    point: Point
    #: Tags found on the incoming record (effective values only).
    record_tags: Dict[str, Any]
    #: Tags filled from the configured default tags.
    default_tags: Dict[str, Any]
    #: Keys that are explicitly excluded from the line protocol.
    omitted_tags: List[str]
    #: Every considered tag with its origin, sorted by key.
    tags: List[TagProvenance]
    #: Line protocol produced for :attr:`point` at merge time.
    line_protocol: str

    @property
    def discarded(self) -> bool:
        """True when serialization drops the record (empty line protocol).

        A :class:`Point` without serializable fields serializes to an empty
        string, so such a record must never be announced as writable.
        """
        return not self.line_protocol

    def tag_origins(self) -> Dict[str, TagOrigin]:
        return {provenance.key: provenance.origin for provenance in self.tags}


@dataclass
class MergedBatch:
    """The merge result for a whole batch of structured records."""

    records: List[MergedRecord]
    options: TagMergeOptions

    @property
    def line_protocol(self) -> str:
        """Line protocol of the writable records, one line per record."""
        return "\n".join(record.line_protocol for record in self.records if not record.discarded)

    def writable_records(self) -> List[MergedRecord]:
        return [record for record in self.records if not record.discarded]

    def discarded_records(self) -> List[MergedRecord]:
        return [record for record in self.records if record.discarded]


class TagMerger:
    """Merge configured default tags into structured records without mutation."""

    def __init__(self, default_tags: Mapping[str, Any] = None,
                 omit_tag_keys: Iterable[str] = None) -> None:
        self._options = TagMergeOptions(
            default_tags=dict(default_tags or {}),
            omit_tag_keys=list(omit_tag_keys or []),
        )

    @classmethod
    def from_options(cls, options: TagMergeOptions) -> "TagMerger":
        return cls(default_tags=options.default_tags, omit_tag_keys=options.omit_tag_keys)

    @property
    def options(self) -> TagMergeOptions:
        return self._options

    def merge_batch(self, records: Iterable[Any],
                    write_precision: WritePrecision = DEFAULT_WRITE_PRECISION,
                    **kwargs) -> MergedBatch:
        """Merge every record of an iterable, preserving its order."""
        merged = [self.merge(record, write_precision=write_precision, **kwargs)
                  for record in records]
        return MergedBatch(records=merged, options=self._options)

    def merge(self, record: Any,
              write_precision: WritePrecision = DEFAULT_WRITE_PRECISION,
              **kwargs) -> MergedRecord:
        """Merge a single :class:`Point` or dictionary record."""
        if isinstance(record, Point):
            return self._merge_point(record)
        if isinstance(record, dict):
            return self._merge_dict(record, write_precision, **kwargs)
        raise TypeError(
            "Tag merging only accepts structured records with decidable tags "
            "(Point or dict); got {}".format(type(record).__name__))

    def _merge_point(self, record: Point) -> MergedRecord:
        explicit_tags = dict(record._tags)
        effective_tags, omitted_keys = self._partition_tags(explicit_tags)

        point = Point(record._name)
        for key, value in effective_tags.items():
            point.tag(key, value)
        for field_key, field_value in record._fields.items():
            point.field(field_key, field_value)
        if record._time is not None:
            point.time(record._time, write_precision=record._write_precision)
        point._field_types = dict(record._field_types)

        return self._build_result(source=record, point=point,
                                  explicit_tags=explicit_tags,
                                  effective_tags=effective_tags,
                                  omitted_keys=omitted_keys)

    def _merge_dict(self, record: dict, write_precision: WritePrecision, **kwargs) -> MergedRecord:
        record_tag_keys = kwargs.get("record_tag_keys", None)
        if record_tag_keys is not None:
            explicit_tags = {key: record[key] for key in record_tag_keys if key in record}
        else:
            explicit_tags = dict(record.get("tags", {}) or {})

        effective_tags, omitted_keys = self._partition_tags(explicit_tags)

        sanitized = dict(record)
        if record_tag_keys is not None:
            for key in omitted_keys:
                sanitized.pop(key, None)
        else:
            sanitized_tags = dict(record.get("tags", {}) or {})
            for key in omitted_keys:
                sanitized_tags.pop(key, None)
            sanitized["tags"] = sanitized_tags

        point = Point.from_dict(sanitized, write_precision=write_precision, **kwargs)
        return self._build_result(source=record, point=point,
                                  explicit_tags=explicit_tags,
                                  effective_tags=effective_tags,
                                  omitted_keys=omitted_keys)

    def _partition_tags(self, explicit_tags: Mapping[str, Any]):
        omitted_keys = set()
        effective_tags = {}
        omit_set = set(self._options.omit_tag_keys)
        for key, value in explicit_tags.items():
            if value is OMIT_TAG or key in omit_set:
                omitted_keys.add(key)
            else:
                effective_tags[key] = value
        return effective_tags, omitted_keys

    def _build_result(self, source: Any, point: Point,
                      explicit_tags: Mapping[str, Any],
                      effective_tags: Mapping[str, Any],
                      omitted_keys: Iterable[str]) -> MergedRecord:
        applied_defaults = {}
        configured_omissions = set(self._options.omit_tag_keys)
        for key, value in self._options.default_tags.items():
            # An explicitly omitted key must never be repaired by a default.
            if key not in explicit_tags and key not in omitted_keys \
                    and key not in configured_omissions:
                point.tag(key, value)
                applied_defaults[key] = value

        all_omissions = set(omitted_keys) | configured_omissions

        provenance = []
        for key in sorted(effective_tags):
            provenance.append(TagProvenance(key=key, value=effective_tags[key],
                                            origin=TagOrigin.RECORD))
        for key in sorted(applied_defaults):
            provenance.append(TagProvenance(key=key, value=applied_defaults[key],
                                            origin=TagOrigin.DEFAULT))
        for key in sorted(all_omissions):
            provenance.append(TagProvenance(key=key, value=explicit_tags.get(key),
                                            origin=TagOrigin.OMITTED))

        return MergedRecord(
            source=source,
            point=point,
            record_tags=dict(effective_tags),
            default_tags=applied_defaults,
            omitted_tags=sorted(all_omissions),
            tags=provenance,
            line_protocol=point.to_line_protocol(),
        )
