"""Interpret a raw date cell under every format its source allows.

All legal readings are retained.  A value such as ``05/03/2024`` can survive as
two different calendar dates (one per day/month order); the caller decides
what the evidence supports, this module never picks a preferred order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional, Sequence

from .config import FormatSpec

@dataclass(frozen=True)
class Reading:
    """One legal interpretation of a raw cell."""

    value: date
    format: FormatSpec

    @property
    def order(self) -> str:
        return self.format.order

    @property
    def format_name(self) -> str:
        return self.format.name


def _try_pattern(cell: str, pattern: str) -> Optional[date]:
    # Mirrors DateField: strptime itself rejects trailing junk; the caller has
    # already stripped surrounding whitespace.
    try:
        return datetime.strptime(cell, pattern).date()
    except (ValueError, TypeError):
        return None


def interpret_cell(cell: object, formats: Sequence[FormatSpec]) -> List[Reading]:
    """Return every distinct (date, format) reading allowed for ``cell``."""

    if not isinstance(cell, str):
        return []
    text = cell.strip()
    if not text:
        return []

    readings: List[Reading] = []
    seen = set()
    for spec in formats:
        value = _try_pattern(text, spec.pattern)
        if value is None:
            continue
        key = (value, spec.name)
        if key in seen:
            continue
        seen.add(key)
        readings.append(Reading(value=value, format=spec))

    readings.sort(key=lambda reading: (reading.order, reading.format_name, reading.value))
    return readings
