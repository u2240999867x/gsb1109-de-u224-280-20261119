"""Human-readable evidence report.

Every judgment quotes the original raw text and its physical source line;
nothing here asks the reader to trust a value without its provenance.
"""

from __future__ import annotations

from typing import List

from .consolidate import ConsolidationResult, Record
from .group import GroupState

KIND_TITLES = {
    "confirmed": "Confirmed: value and format both supported by evidence",
    "same_day_unknown_format": "Same calendar day, format/order still unknown",
    "ambiguous": "Genuinely two possible dates",
    "unparseable": "No configured format accepts the raw value",
}
KIND_ORDER = ["confirmed", "same_day_unknown_format", "ambiguous", "unparseable"]


def _format_reading(reading) -> str:
    return f"{reading.value.isoformat()} via {reading.format_name} ({reading.format.pattern}, {reading.order})"


def _render_record(record: Record) -> List[str]:
    lines = [
        f"  - {record.file_path}:{record.physical_line} "
        f"source={record.source!r} version={record.version!r} raw={record.raw_text!r}",
    ]
    if record.readings:
        for reading in record.readings:
            lines.append(f"      reading: {_format_reading(reading)}")
    else:
        lines.append("      reading: none")
    lines.append(f"      verdict: {record.verdict.kind} - {record.verdict.reason}")
    if record.standardized is not None:
        lines.append(f"      standardized: {record.standardized.isoformat()}")
    else:
        lines.append("      standardized: (left empty)")
    return lines


def _render_group(group: GroupState) -> List[str]:
    lines = [
        f"  source={group.source!r} version={group.version!r} "
        f"self_consistent={group.consistent}"
    ]
    if group.is_iso_group:
        lines.append("      ISO source: order evidence not applicable")
    else:
        if group.confirmed_orders:
            detail = ", ".join(sorted(group.confirmed_orders))
            lines.append(f"      order(s) proven by single-format rows: {detail}")
        else:
            lines.append("      no single-format row yet; no order proven")
        for order, names in sorted(group.order_formats.items()):
            lines.append(f"      formats observed for {order}: {', '.join(sorted(names))}")
        for contradiction in group.contradictions:
            lines.append(
                "      contradiction: "
                f"{contradiction.file_path}:{contradiction.physical_line} "
                f"raw={contradiction.raw_text!r} is legal only as "
                f"{contradiction.observed_order} but this source/version already "
                f"proved {', '.join(sorted(contradiction.expected_orders))}"
            )
    return lines


def build_report(result: ConsolidationResult) -> str:
    out: List[str] = ["Date consolidation evidence report", "=" * 36, ""]

    out.append("Per source/version consistency")
    out.append("-" * 30)
    for key in sorted(result.groups):
        out.extend(_render_group(result.groups[key]))
    out.append("")

    for kind in KIND_ORDER:
        records = result.by_kind(kind)
        out.append(KIND_TITLES[kind])
        out.append("-" * len(KIND_TITLES[kind]))
        if not records:
            out.append("  (none)")
        for record in records:
            out.extend(_render_record(record))
        out.append("")

    return "\n".join(out).rstrip() + "\n"
