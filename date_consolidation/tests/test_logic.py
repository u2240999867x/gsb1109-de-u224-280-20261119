from datetime import date

import pytest

from date_consolidation.config import (
    DAY_FIRST,
    ISO_ORDER,
    MONTH_FIRST,
    FormatSpec,
    SourceConfig,
)
from date_consolidation.group import GroupState, classify
from date_consolidation.interp import interpret_cell

DMY = FormatSpec("dmy", "%d/%m/%Y", DAY_FIRST)
MDY = FormatSpec("mdy", "%m/%d/%Y", MONTH_FIRST)
ISO = FormatSpec("iso", "%Y-%m-%d", ISO_ORDER)

LEGACY = SourceConfig("store", [DMY, MDY])
HQ = SourceConfig("hq", [ISO])


def test_interpreter_keeps_both_legal_readings_in_order():
    readings = interpret_cell("05/03/2024", LEGACY.formats)
    assert {(r.value, r.order) for r in readings} == {
        (date(2024, 3, 5), DAY_FIRST),
        (date(2024, 5, 3), MONTH_FIRST),
    }


def test_interpreter_same_day_two_formats():
    readings = interpret_cell("09/09/2024", LEGACY.formats)
    assert len(readings) == 2
    assert {r.value for r in readings} == {date(2024, 9, 9)}


def test_interpreter_single_order_row():
    readings = interpret_cell("13/02/2024", LEGACY.formats)
    assert len(readings) == 1
    assert readings[0].order == DAY_FIRST
    assert readings[0].value == date(2024, 2, 13)


def test_interpreter_rejects_unknown_form_without_defaulting():
    assert interpret_cell("2024.02.13", LEGACY.formats) == []
    assert interpret_cell("not-a-date", LEGACY.formats) == []
    assert interpret_cell("", LEGACY.formats) == []


def test_verdicts_do_not_depend_on_configured_format_order():
    reversed_legacy = SourceConfig("store", [MDY, DMY])
    forward = interpret_cell("05/03/2024", LEGACY.formats)
    reversed_ = interpret_cell("05/03/2024", reversed_legacy.formats)
    assert {(r.value, r.order) for r in forward} == {(r.value, r.order) for r in reversed_}

    group_forward = GroupState("store", "v7", LEGACY)
    group_reversed = GroupState("store", "v7", reversed_legacy)
    pin_text = "13/02/2024"
    group_forward.observe(interpret_cell(pin_text, LEGACY.formats), raw_text=pin_text, physical_line=2, file_path="f.csv")
    group_reversed.observe(interpret_cell(pin_text, reversed_legacy.formats), raw_text=pin_text, physical_line=2, file_path="f.csv")
    assert group_forward.pinned_orders == group_reversed.pinned_orders == {DAY_FIRST}
    assert classify(forward, group_forward).standardized == classify(reversed_, group_reversed).standardized


def test_group_pin_resolves_otherwise_ambiguous_cell():
    group = GroupState("store", "v7", LEGACY)
    pin = interpret_cell("13/02/2024", LEGACY.formats)
    group.observe(pin, raw_text="13/02/2024", physical_line=2, file_path="f.csv")
    assert group.pinned_orders == {DAY_FIRST}
    assert group.consistent

    verdict = classify(interpret_cell("05/03/2024", LEGACY.formats), group)
    assert verdict.kind == "confirmed"
    assert verdict.standardized == date(2024, 3, 5)


def test_contradictory_tail_marks_group_inconsistent_but_keeps_per_row_verdicts():
    group = GroupState("store", "v7", LEGACY)
    group.observe(
        interpret_cell("13/02/2024", LEGACY.formats),
        raw_text="13/02/2024",
        physical_line=2,
        file_path="f.csv",
    )
    tail = interpret_cell("02/25/2024", LEGACY.formats)
    group.observe(tail, raw_text="02/25/2024", physical_line=7, file_path="f.csv")

    assert not group.consistent
    assert group.confirmed_orders == {DAY_FIRST, MONTH_FIRST}
    contradiction = group.contradictions[0]
    assert contradiction.physical_line == 7
    assert contradiction.raw_text == "02/25/2024"
    assert contradiction.observed_order == MONTH_FIRST

    # The tail row itself is still confirmed on its own single-order evidence.
    verdict = classify(tail, group)
    assert verdict.kind == "confirmed"
    assert verdict.standardized == date(2024, 2, 25)

    # Ambiguous cell can no longer rely on a trustworthy group pin.
    ambiguous = classify(interpret_cell("05/03/2024", LEGACY.formats), group)
    assert ambiguous.kind == "ambiguous"
    assert ambiguous.standardized is None
    assert ambiguous.values == [date(2024, 3, 5), date(2024, 5, 3)]


def test_same_day_cell_exports_value_but_keeps_format_unknown():
    group = GroupState("store", "v7", LEGACY)
    verdict = classify(interpret_cell("09/09/2024", LEGACY.formats), group)
    assert verdict.kind == "same_day_unknown_format"
    assert verdict.standardized == date(2024, 9, 9)
    assert len(verdict.readings) == 2


def test_hq_iso_confirmed_independently_of_legacy_contradiction():
    hq_group = GroupState("hq", "v7", HQ)
    verdict = classify(interpret_cell("2024-02-15", HQ.formats), hq_group)
    assert verdict.kind == "confirmed"
    assert verdict.standardized == date(2024, 2, 15)
    assert verdict.readings[0].format.name == "iso"


def test_hq_rejects_non_iso_cell():
    hq_group = GroupState("hq", "v7", HQ)
    verdict = classify(interpret_cell("15/02/2024", HQ.formats), hq_group)
    assert verdict.kind == "unparseable"
    assert verdict.standardized is None


def test_different_version_starts_fresh_group():
    v7 = GroupState("store", "v7", LEGACY)
    v8 = GroupState("store", "v8", LEGACY)
    v7.observe(
        interpret_cell("13/02/2024", LEGACY.formats),
        raw_text="13/02/2024",
        physical_line=2,
        file_path="f.csv",
    )
    v8.observe(
        interpret_cell("02/25/2024", LEGACY.formats),
        raw_text="02/25/2024",
        physical_line=3,
        file_path="f.csv",
    )
    assert v7.consistent and v8.consistent
    assert v7.pinned_orders == {DAY_FIRST}
    assert v8.pinned_orders == {MONTH_FIRST}
