"""Tests for the streaming, atomic package conversion engine.

Run with:

    .venv/bin/python -m pytest demo/test_conversion_engine.py -v
"""

from __future__ import annotations

import os
import shutil

import pytest
from frictionless import Package
from frictionless.resources.table import TableResource

from conversion_engine import (
    ConversionError,
    PackageConverter,
    build_target_schema,
    open_source_package,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_FIXTURE = os.path.join(HERE, "data", "source")


@pytest.fixture
def source_package_path(tmp_path):
    target = tmp_path / "source"
    shutil.copytree(SOURCE_FIXTURE, target)
    return str(target / "datapackage.json")


@pytest.fixture
def output_root(tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    return str(root)


def _legacy_snapshot(descriptor_path):
    package = open_source_package(descriptor_path)
    snapshot = {}
    for resource in package.resources:
        with resource:
            snapshot[resource.name] = (
                resource.path,
                tuple(resource.schema.field_names),
                len(resource.read_rows()),
            )
    return snapshot


def test_source_package_unchanged_and_still_readable(source_package_path, output_root):
    before = _legacy_snapshot(source_package_path)

    strict = PackageConverter(
        source_package_path,
        os.path.join(output_root, "east"),
        name="east",
        region="East",
        policy="strict",
    )
    with strict:
        strict.drain("orders")
        with pytest.raises(ConversionError) as exc_info:
            strict.drain("items")
        assert exc_info.value.resource_name == "items"
        assert exc_info.value.row_number == 19

    tolerant = PackageConverter(
        source_package_path,
        os.path.join(output_root, "west"),
        name="west",
        region="West",
        policy="tolerant",
    )
    with tolerant:
        tolerant.drain("orders")
        tolerant.drain("items")
        tolerant.commit()

    after = _legacy_snapshot(source_package_path)
    assert after == before
    assert after["orders"] == ("orders.csv", ("order_id", "customer", "region", "amount"), 8)
    assert after["items"] == ("items.csv", ("item_id", "order_id", "sku", "qty"), 20)


def test_strict_failure_leaves_no_partial_package(source_package_path, output_root):
    east_dir = os.path.join(output_root, "east")
    converter = PackageConverter(
        source_package_path, east_dir, name="east", region="East", policy="strict"
    )
    with converter:
        converter.drain("orders")
        with pytest.raises(ConversionError):
            converter.drain("items")

    assert converter.status == "failed"
    assert not os.path.exists(east_dir)
    assert not [
        name for name in os.listdir(output_root) if name.startswith(".staging-")
    ]
    with pytest.raises(Exception):
        Package(east_dir)


def test_tolerant_run_publishes_complete_standalone_package(
    source_package_path, output_root
):
    west_dir = os.path.join(output_root, "west")
    converter = PackageConverter(
        source_package_path, west_dir, name="west", region="West", policy="tolerant"
    )
    with converter:
        converter.drain("orders")
        converter.drain("items")
        descriptor_path = converter.commit()

    assert converter.status == "completed"
    assert os.path.exists(descriptor_path)

    # Reopened from disk alone: source generators are exhausted and the
    # source package objects are out of scope.
    reopened = Package(descriptor_path)
    report = reopened.validate()
    assert report.valid

    orders = reopened.get_resource("orders")
    items = reopened.get_resource("items")
    assert orders.schema.field_names == ["order_ref", "customer", "region", "amount"]
    assert items.schema.field_names == ["item_id", "order_ref", "sku", "qty"]
    assert orders.schema.primary_key == ["order_ref"]
    assert items.schema.primary_key == ["item_id"]
    assert items.schema.foreign_keys == [
        {"fields": ["order_ref"], "reference": {"resource": "orders", "fields": ["order_ref"]}}
    ]
    with orders, items:
        assert len(orders.read_rows()) == 4
        assert len(items.read_rows()) == 8

    assert converter.stats["orders"].rows_written == 4
    assert converter.stats["items"].rows_written == 8
    assert len(converter.stats["items"].rejected) == 1


def test_early_stop_is_never_marked_success(source_package_path, output_root):
    abandoned_dir = os.path.join(output_root, "abandoned")
    converter = PackageConverter(
        source_package_path,
        abandoned_dir,
        name="abandoned",
        region="East",
        policy="strict",
    )
    with converter:
        converter.pull("orders")
        converter.pull("items")
        converter.stop_early()

    assert converter.status == "aborted"
    assert not os.path.exists(abandoned_dir)


def test_recovery_starts_fresh_from_same_source(source_package_path, output_root):
    east_dir = os.path.join(output_root, "east")

    failed = PackageConverter(
        source_package_path, east_dir, name="east", region="East", policy="strict"
    )
    with failed:
        failed.drain("orders")
        with pytest.raises(ConversionError):
            failed.drain("items")
    assert failed.status == "failed"

    recovered = PackageConverter(
        source_package_path,
        east_dir,
        name="east-recovery",
        region="East",
        policy="tolerant",
    )
    with recovered:
        recovered.drain("orders")
        recovered.drain("items")
        descriptor_path = recovered.commit()

    assert recovered.status == "completed"
    reopened = Package(descriptor_path)
    assert reopened.validate().valid
    with reopened.get_resource("items") as items:
        assert len(items.read_rows()) == 11
    assert len(recovered.stats["items"].rejected) == 1


def test_parallel_conversions_keep_isolated_stats_and_schemas(
    source_package_path, output_root
):
    east = PackageConverter(
        source_package_path,
        os.path.join(output_root, "east"),
        name="east",
        region="East",
        policy="tolerant",
    )
    west = PackageConverter(
        source_package_path,
        os.path.join(output_root, "west"),
        name="west",
        region="West",
        policy="tolerant",
    )
    assert east.source_package is not west.source_package
    assert east.staging_dir != west.staging_dir

    with east, west:
        east.drain("orders")
        west.drain("orders")
        east.drain("items")
        west.drain("items")
        east_descriptor = east.commit()
        west_descriptor = west.commit()

    east_package = Package(east_descriptor)
    west_package = Package(west_descriptor)
    assert east.stats["items"].rows_written == 11
    assert west.stats["items"].rows_written == 8
    assert east_package is not west_package
    assert (
        east_package.get_resource("items").schema.field_names
        == west_package.get_resource("items").schema.field_names
    )


def test_conversion_never_eagerly_reads_all_rows(source_package_path, output_root, monkeypatch):
    def forbidden_read_rows(self, *, size=None):
        raise AssertionError("streaming conversion must not preload rows via read_rows")

    monkeypatch.setattr(TableResource, "read_rows", forbidden_read_rows)

    converter = PackageConverter(
        source_package_path,
        os.path.join(output_root, "west"),
        name="west",
        region="West",
        policy="tolerant",
    )
    with converter:
        converter.drain("orders")
        converter.drain("items")
        descriptor_path = converter.commit()

    assert Package(descriptor_path).validate().valid


def test_target_schema_renames_join_field_everywhere(source_package_path):
    package = Package(source_package_path)
    schema = build_target_schema(
        package.get_resource("items"), {"order_id": "order_ref"}
    )
    assert schema.primary_key == ["item_id"]
    assert schema.foreign_keys == [
        {"fields": ["order_ref"], "reference": {"resource": "orders", "fields": ["order_ref"]}}
    ]
    assert "order_id" not in schema.field_names


def test_commit_rolls_back_when_descriptor_write_fails(
    source_package_path, output_root, monkeypatch
):
    west_dir = os.path.join(output_root, "west")
    converter = PackageConverter(
        source_package_path, west_dir, name="west", region="West", policy="tolerant"
    )
    original_to_json = Package.to_json

    def failing_to_json(self, path=None, **kwargs):
        if path is not None:
            raise RuntimeError("simulated descriptor disk failure")
        return original_to_json(self, path=path, **kwargs)

    monkeypatch.setattr(Package, "to_json", failing_to_json)

    with converter:
        converter.drain("orders")
        converter.drain("items")
        with pytest.raises(RuntimeError, match="simulated descriptor disk failure"):
            converter.commit()

    assert converter.status == "failed"
    assert not os.path.exists(west_dir)
