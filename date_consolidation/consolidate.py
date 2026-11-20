"""Read the batch files, gather evidence and classify every date cell."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import Config, FileSpec
from .group import GroupState, Verdict, classify
from .interp import Reading, interpret_cell


@dataclass(frozen=True)
class RawRow:
    file_path: str
    physical_line: int
    source: str
    version: str
    raw_text: str


@dataclass
class Record:
    file_path: str
    physical_line: int
    source: str
    version: str
    raw_text: str
    readings: List[Reading]
    verdict: Optional[Verdict] = None

    @property
    def group_key(self) -> Tuple[str, str]:
        return (self.source, self.version)

    @property
    def standardized(self):
        return self.verdict.standardized if self.verdict else None


@dataclass
class ConsolidationResult:
    records: List[Record]
    groups: Dict[Tuple[str, str], GroupState]
    stable_format: str

    def by_kind(self, kind: str) -> List[Record]:
        return [record for record in self.records if record.verdict and record.verdict.kind == kind]

    @property
    def confirmed(self) -> List[Record]:
        return self.by_kind("confirmed")

    @property
    def same_day_unknown_format(self) -> List[Record]:
        return self.by_kind("same_day_unknown_format")

    @property
    def ambiguous(self) -> List[Record]:
        return self.by_kind("ambiguous")

    @property
    def unparseable(self) -> List[Record]:
        return self.by_kind("unparseable")


def _read_file(spec: FileSpec, base_dir: Path) -> List[RawRow]:
    path = base_dir / spec.path
    rows: List[RawRow] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        if spec.has_header:
            header = next(reader)
            index = {name: pos for pos, name in enumerate(header)}
            required = (spec.source_column, spec.version_column, spec.date_column)
            missing = [name for name in required if name not in index]
            if missing:
                raise KeyError(f"{spec.path}: missing columns {missing}")
        else:
            index = {
                spec.source_column: 0,
                spec.version_column: 1,
                spec.date_column: 2,
            }

        for values in reader:
            if not values or all(not value.strip() for value in values):
                continue
            # reader.line_num is the physical line of the last row read,
            # including quoted newlines and the header.
            line = reader.line_num
            source = values[index[spec.source_column]].strip()
            version = values[index[spec.version_column]].strip()
            raw_text = values[index[spec.date_column]].strip()
            if source not in spec.sources:
                raise ValueError(
                    f"{spec.path}:{line}: source {source!r} is not declared for this file"
                )
            rows.append(
                RawRow(
                    file_path=spec.path,
                    physical_line=line,
                    source=source,
                    version=version,
                    raw_text=raw_text,
                )
            )
    return rows


def consolidate(config: Config, base_dir: str | Path = ".") -> ConsolidationResult:
    base_dir = Path(base_dir)

    raw_rows: List[RawRow] = []
    for spec in config.files:
        raw_rows.extend(_read_file(spec, base_dir))

    # First pass: interpret every cell and feed group evidence in physical
    # reading order so contradictions surface in batch order.
    groups: Dict[Tuple[str, str], GroupState] = {}
    interpreted: List[Tuple[RawRow, List[Reading]]] = []
    for row in raw_rows:
        source_config = config.source(row.source)
        key = (row.source, row.version)
        group = groups.get(key)
        if group is None:
            group = GroupState(
                source=row.source, version=row.version, source_config=source_config
            )
            groups[key] = group
        readings = interpret_cell(row.raw_text, source_config.formats)
        group.observe(
            readings,
            raw_text=row.raw_text,
            physical_line=row.physical_line,
            file_path=row.file_path,
        )
        interpreted.append((row, readings))

    # Second pass: all evidence is in, so each verdict is final.
    records: List[Record] = []
    for row, readings in interpreted:
        group = groups[(row.source, row.version)]
        verdict = classify(readings, group)
        records.append(
            Record(
                file_path=row.file_path,
                physical_line=row.physical_line,
                source=row.source,
                version=row.version,
                raw_text=row.raw_text,
                readings=readings,
                verdict=verdict,
            )
        )

    return ConsolidationResult(
        records=records, groups=groups, stable_format=config.stable_format
    )
