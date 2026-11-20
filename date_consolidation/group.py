"""Per (source, version) evidence state.

A legacy store promises a single non-ISO day/month order for a given data
submission version.  Rows of the two stores may be interleaved in one file but
the promise, and therefore the verdict, is always scoped to a single source
and version.  A finding on one group never changes another group: a store that
contradicts itself cannot be marked self-consistent, while HQ keeps producing
confirmed ISO values independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Set

from .config import ISO_ORDER, SourceConfig
from .interp import Reading


@dataclass(frozen=True)
class Contradiction:
    source: str
    version: str
    observed_order: str
    expected_orders: Set[str]
    raw_text: str
    physical_line: int
    file_path: str


@dataclass
class GroupState:
    source: str
    version: str
    source_config: SourceConfig

    # Orders actually exercised by unambiguous evidence in this group.
    confirmed_orders: Set[str] = field(default_factory=set)
    # Format names seen for each confirmed order.
    order_formats: Dict[str, Set[str]] = field(default_factory=dict)
    # Every observation that broke the single-order promise, with its raw text
    # and physical line so the report can quote the original evidence.
    contradictions: List[Contradiction] = field(default_factory=list)

    @property
    def is_iso_group(self) -> bool:
        return self.source_config.iso_format is not None and not self.source_config.legacy_formats

    @property
    def consistent(self) -> bool:
        """A group is self-consistent only with no contradictory evidence."""

        return not self.contradictions

    @property
    def pinned_orders(self) -> Set[str]:
        """Orders proven in use by unambiguous rows (legacy groups only)."""

        return set(self.confirmed_orders)

    def observe(
        self,
        readings: List[Reading],
        *,
        raw_text: str,
        physical_line: int,
        file_path: str,
    ) -> None:
        """Fold one row's legal readings into the group's evidence."""

        if self.is_iso_group:
            return
        orders = {reading.order for reading in readings}
        # A row legal under exactly one order pins that order.  A row legal
        # under both orders (same or different day) adds no order evidence.
        if len(orders) != 1:
            return

        (only_order,) = orders
        for reading in readings:
            self.order_formats.setdefault(only_order, set()).add(reading.format_name)
        if self.confirmed_orders and only_order not in self.confirmed_orders:
            # Earlier rows already proved a different order for this same
            # source and version: the single-order promise is broken.
            self.contradictions.append(
                Contradiction(
                    source=self.source,
                    version=self.version,
                    observed_order=only_order,
                    expected_orders=set(self.confirmed_orders),
                    raw_text=raw_text,
                    physical_line=physical_line,
                    file_path=file_path,
                )
            )
        self.confirmed_orders.add(only_order)


@dataclass(frozen=True)
class Verdict:
    """Outcome for one raw cell."""

    kind: str  # "confirmed" | "same_day_unknown_format" | "ambiguous" | "unparseable"
    standardized: Optional[date]
    readings: List[Reading]
    reason: str

    @property
    def values(self) -> List[date]:
        return sorted({reading.value for reading in self.readings})


def classify(readings: List[Reading], group: GroupState) -> Verdict:
    """Decide what may enter the standardized column for one cell."""

    if not readings:
        return Verdict(
            kind="unparseable",
            standardized=None,
            readings=[],
            reason="no configured format accepts the raw value",
        )

    iso_readings = [reading for reading in readings if reading.order == ISO_ORDER]
    if iso_readings and group.is_iso_group:
        reading = iso_readings[0]
        return Verdict(
            kind="confirmed",
            standardized=reading.value,
            readings=readings,
            reason=(
                f"ISO source; value and format {reading.format_name!r} "
                f"({reading.format.pattern}) are fixed"
            ),
        )

    orders = {reading.order for reading in readings}
    values = sorted({reading.value for reading in readings})

    # A cell legal under exactly one order fixes its own value and format no
    # matter what the rest of the group does.  Group-level contradictions are
    # reported separately and only cost the group its self-consistent label.
    if len(orders) == 1:
        reading = readings[0]
        formats_used = sorted({item.format_name for item in readings})
        return Verdict(
            kind="confirmed",
            standardized=reading.value,
            readings=readings,
            reason=(
                f"only order {reading.order} yields a legal date; "
                f"format(s): {', '.join(formats_used)}"
            ),
        )

    pinned = group.pinned_orders
    # The group's evidence rules out every order but one and this cell is
    # legal under the survivor: the pin fixes the calendar day.
    surviving = [reading for reading in readings if reading.order in pinned]
    if group.consistent and len(pinned) == 1 and surviving:
        (only_order,) = pinned
        order_values = {reading.value for reading in surviving}
        if len(order_values) == 1:
            formats_used = sorted({reading.format_name for reading in surviving})
            return Verdict(
                kind="confirmed",
                standardized=next(iter(order_values)),
                readings=readings,
                reason=(
                    f"order {only_order} proven for this source/version; "
                    f"legal format(s) here: {', '.join(formats_used)}"
                ),
            )

    # Both orders apply but resolve to the same calendar day: the value is
    # certain while the written order/format stays unknown.
    if len(values) == 1:
        return Verdict(
            kind="same_day_unknown_format",
            standardized=values[0],
            readings=readings,
            reason="all legal orders resolve to the same day; order/format is not proven",
        )

    # Both orders apply and yield different days; no trustworthy pin exists,
    # so this cell genuinely could be two dates.
    return Verdict(
        kind="ambiguous",
        standardized=None,
        readings=readings,
        reason="distinct calendar days are legal under different day/month orders",
    )
