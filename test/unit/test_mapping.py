"""Tests for column mapping helpers used by the mapped ``load`` path."""
import pytest

from etlhelper.exceptions import ETLHelperMappingConflictError
from etlhelper.mapping import (
    project_row,
    resolve_source_field,
    validate_column_mapping,
)

VERSION_FIELD = 'collector_version'

MAPPING = {
    'ReportCode': {'v1': 'code', 'v2': 'report_code'},
    'Station': 'station',
    'Quality': 'quality',
}


def test_validate_column_mapping_accepts_plain_source_fields():
    validate_column_mapping({
        'ReportCode': 'code',
        'Station': 'station',
        'Quality': 'quality',
    })


def test_validate_column_mapping_accepts_version_groups_with_version_field():
    validate_column_mapping(MAPPING, version_field=VERSION_FIELD)


def test_validate_column_mapping_rejects_multiple_fields_without_version_field():
    with pytest.raises(ETLHelperMappingConflictError, match="ReportCode"):
        validate_column_mapping(MAPPING)


def test_validate_column_mapping_rejects_ambiguous_version_group():
    mapping = {
        'ReportCode': {'v1': ['code', 'report_code']},
        'Station': 'station',
    }
    with pytest.raises(ETLHelperMappingConflictError, match="version 'v1'"):
        validate_column_mapping(mapping, version_field=VERSION_FIELD)


def test_validate_column_mapping_rejects_conflict_with_collection_no_version_field():
    mapping = {
        'ReportCode': {'v1': ('code', 'report_code')},
        'Station': 'station',
    }
    with pytest.raises(ETLHelperMappingConflictError, match="multiple source"):
        validate_column_mapping(mapping)


def test_validate_column_mapping_accepts_single_element_version_group():
    mapping = {
        'ReportCode': {'v1': ['code'], 'v2': ('report_code',)},
        'Station': 'station',
    }
    validate_column_mapping(mapping, version_field=VERSION_FIELD)


def test_project_row_unwraps_single_element_version_group():
    mapping = {
        'ReportCode': {'v1': ['code'], 'v2': ('report_code',)},
        'Station': 'station',
    }
    row = {VERSION_FIELD: 'v1', 'code': 'RC-1', 'station': 'ST-A'}

    values = project_row(row, mapping, ('ReportCode', 'Station'),
                         VERSION_FIELD)

    assert values == ('RC-1', 'ST-A')


def test_resolve_source_field_plain_mapping():
    assert resolve_source_field('code', {}, VERSION_FIELD) == 'code'


def test_resolve_source_field_selects_declared_version_field():
    old_row = {VERSION_FIELD: 'v1', 'code': 'RC-1', 'report_code': 'DIAG'}
    new_row = {VERSION_FIELD: 'v2', 'report_code': 'RC-2'}

    assert resolve_source_field(MAPPING['ReportCode'], old_row,
                                VERSION_FIELD) == 'code'
    assert resolve_source_field(MAPPING['ReportCode'], new_row,
                                VERSION_FIELD) == 'report_code'


def test_resolve_source_field_missing_version_returns_none():
    row = {'code': 'RC-1', 'report_code': 'DIAG'}
    assert resolve_source_field(MAPPING['ReportCode'], row,
                                VERSION_FIELD) is None


def test_resolve_source_field_unknown_version_returns_none():
    row = {VERSION_FIELD: 'v3', 'code': 'RC-1'}
    assert resolve_source_field(MAPPING['ReportCode'], row,
                                VERSION_FIELD) is None


def test_project_row_uses_target_order_not_dictionary_key_order():
    # Keys are deliberately declared in the reverse of the target order
    row = {'quality': 'good', 'station': 'ST-A', 'code': 'RC-1',
           'report_code': 'DIAG', VERSION_FIELD: 'v1'}

    values = project_row(row, MAPPING,
                         ('ReportCode', 'Station', 'Quality'), VERSION_FIELD)

    assert values == ('RC-1', 'ST-A', 'good')


def test_project_row_old_version_ignores_diagnostic_report_code():
    row = {VERSION_FIELD: 'v1', 'code': 'RC-OLD', 'report_code': 'DIAG',
           'station': 'ST-A', 'quality': 'good'}

    values = project_row(row, MAPPING,
                         ('ReportCode', 'Station', 'Quality'), VERSION_FIELD)

    assert values[0] == 'RC-OLD'


def test_project_row_new_version_uses_report_code():
    row = {VERSION_FIELD: 'v2', 'report_code': 'RC-NEW',
           'station': 'ST-A', 'quality': 'good'}

    values = project_row(row, MAPPING,
                         ('ReportCode', 'Station', 'Quality'), VERSION_FIELD)

    assert values == ('RC-NEW', 'ST-A', 'good')


def test_project_row_missing_source_field_is_none():
    row = {VERSION_FIELD: 'v2', 'report_code': 'RC-NEW', 'quality': 'good'}

    values = project_row(row, MAPPING,
                         ('ReportCode', 'Station', 'Quality'), VERSION_FIELD)

    assert values == ('RC-NEW', None, 'good')


def test_project_row_explicit_none_is_kept_as_none():
    row = {VERSION_FIELD: 'v1', 'code': 'RC-OLD', 'station': 'ST-A',
           'quality': None}

    values = project_row(row, MAPPING,
                         ('ReportCode', 'Station', 'Quality'), VERSION_FIELD)

    assert values == ('RC-OLD', 'ST-A', None)


def test_project_row_unmapped_and_version_fields_are_not_bound():
    row = {VERSION_FIELD: 'v1', 'code': 'RC-OLD', 'station': 'ST-A',
           'quality': 'good', 'diagnostic': 'noise'}
    mapping = {'ReportCode': 'code', 'Station': 'station'}

    values = project_row(row, mapping, ('ReportCode', 'Station'),
                         VERSION_FIELD)

    assert values == ('RC-OLD', 'ST-A')
