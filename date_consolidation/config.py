"""Configuration loading for date consolidation.

The allowed written forms come entirely from configuration.  Every format is a
``strptime``/``strftime`` pattern together with an explicit day/month order so
that a pattern can be classified without parsing its string.  HQ's ISO form is
configured the same way and is never offered to the legacy stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Orders a non-ISO numeric date may use.  "day_first" -> DD/MM, "month_first"
# -> MM/DD; "iso" -> YYYY-MM-DD and is not eligible as a legacy order.
DAY_FIRST = "day_first"
MONTH_FIRST = "month_first"
ISO_ORDER = "iso"

DEFAULT_STABLE_FORMAT = "%Y-%m-%d"


@dataclass(frozen=True)
class FormatSpec:
    """One legal written date form."""

    name: str
    pattern: str
    order: str

    @property
    def is_iso(self) -> bool:
        return self.order == ISO_ORDER


@dataclass(frozen=True)
class SourceConfig:
    """Allowed formats for one source (HQ or a store)."""

    source: str
    formats: List[FormatSpec]

    @property
    def iso_format(self) -> Optional[FormatSpec]:
        for spec in self.formats:
            if spec.is_iso:
                return spec
        return None

    @property
    def legacy_formats(self) -> List[FormatSpec]:
        return [spec for spec in self.formats if not spec.is_iso]

    def format_named(self, name: str) -> Optional[FormatSpec]:
        for spec in self.formats:
            if spec.name == name:
                return spec
        return None


@dataclass(frozen=True)
class FileSpec:
    """How to read one input file and which rows belong to which source."""

    path: str
    source_column: str
    version_column: str
    date_column: str
    # Sources whose rows may be interleaved in this file.
    sources: List[str]
    # One CSV line per data row; the header does not count.  Used only when a
    # caller does not supply physical lines itself.
    has_header: bool = True


@dataclass
class Config:
    sources: Dict[str, SourceConfig]
    files: List[FileSpec]
    stable_format: str = DEFAULT_STABLE_FORMAT

    def source(self, name: str) -> SourceConfig:
        try:
            return self.sources[name]
        except KeyError:
            raise KeyError(f"no configuration for source {name!r}") from None


def _parse_formats(raw: List[Dict[str, Any]]) -> List[FormatSpec]:
    formats: List[FormatSpec] = []
    for item in raw:
        order = item["order"]
        if order not in (DAY_FIRST, MONTH_FIRST, ISO_ORDER):
            raise ValueError(f"unknown order {order!r} in format {item.get('name')!r}")
        formats.append(
            FormatSpec(name=item["name"], pattern=item["pattern"], order=order)
        )
    if not formats:
        raise ValueError("each source must declare at least one format")
    return formats


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)

    sources: Dict[str, SourceConfig] = {}
    for item in data.get("sources", []):
        name = item["name"]
        sources[name] = SourceConfig(source=name, formats=_parse_formats(item["formats"]))

    files: List[FileSpec] = []
    for item in data.get("files", []):
        files.append(
            FileSpec(
                path=item["path"],
                source_column=item["source_column"],
                version_column=item["version_column"],
                date_column=item["date_column"],
                sources=list(item["sources"]),
                has_header=bool(item.get("has_header", True)),
            )
        )

    return Config(
        sources=sources,
        files=files,
        stable_format=data.get("stable_format", DEFAULT_STABLE_FORMAT),
    )
