"""End-to-end demonstration of the atomic, streaming package conversion.

Run with the repository virtualenv:

    .venv/bin/python demo/run_demo.py

The scenario mirrors the operational requirements:

* two conversions start from the same on-disk source package with different
  region filters and are consumed concurrently (interleaved);
* the strict conversion fails late, inside the dependent ``items`` resource,
  while the tolerant one keeps streaming and publishes a complete package;
* the original package is reopened several times throughout and always
  describes the old data (old paths, old schema, old row counts);
* an abandoned consumer is never marked successful;
* after the failure a fresh recovery run is started from the same source;
* every landed package is reopened independently to verify join keys, row
  counts and referential integrity.
"""

from __future__ import annotations

import os
import shutil
from contextlib import ExitStack
from typing import Any, Dict

from frictionless import Package

from conversion_engine import ConversionError, PackageConverter, open_source_package

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.join(HERE, "data", "source")
SOURCE_DESCRIPTOR = os.path.join(SOURCE_DIR, "datapackage.json")


def read_legacy_report(descriptor_path: str) -> Dict[str, Dict[str, Any]]:
    """Downstream report: reads the package by path, schema and row count."""
    package = open_source_package(descriptor_path)
    snapshot: Dict[str, Dict[str, Any]] = {}
    for resource in package.resources:
        with resource:
            rows = resource.read_rows()
        snapshot[resource.name] = {
            "path": resource.path,
            "fields": resource.schema.field_names,
            "row_count": len(rows),
        }
    return snapshot


def _print_header(title: str) -> None:
    print(f"\n=== {title} ===")


def run_scenario(base_dir: str = HERE) -> Dict[str, Any]:
    source_descriptor = SOURCE_DESCRIPTOR
    output_dir = os.path.join(base_dir, "output")
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)

    results: Dict[str, Any] = {}

    _print_header("legacy report on the untouched source package")
    legacy_before = read_legacy_report(source_descriptor)
    for name, info in legacy_before.items():
        print(f"{name}: path={info['path']} fields={info['fields']} rows={info['row_count']}")
    assert legacy_before["orders"]["fields"] == ["order_id", "customer", "region", "amount"]
    assert legacy_before["items"]["fields"] == ["item_id", "order_id", "sku", "qty"]
    assert legacy_before["orders"]["row_count"] == 8
    assert legacy_before["items"]["row_count"] == 20
    results["legacy_before"] = legacy_before

    east_dir = os.path.join(output_dir, "east")
    west_dir = os.path.join(output_dir, "west")

    _print_header("start two conversions from the same source package")
    with ExitStack() as stack:
        east = stack.enter_context(
            PackageConverter(
                source_descriptor, east_dir, name="east",
                region="East", policy="strict",
            )
        )
        west = stack.enter_context(
            PackageConverter(
                source_descriptor, west_dir, name="west",
                region="West", policy="tolerant",
            )
        )
        print("each converter holds its own Package instance; no shared generators or stats")
        assert east.stats is not west.stats
        assert east.source_package is not west.source_package

        _print_header("read the first rows of each conversion (orders)")
        for label, converter in (("east", east), ("west", west)):
            first = [converter.pull("orders").row for _ in range(2)]
            print(f"{label} first orders: {first}")
            assert all(row is not None for row in first)

        _print_header("finish streaming orders for both conversions")
        east.drain("orders")
        west.drain("orders")
        print(f"east orders written: {east.stats['orders'].rows_written}")
        print(f"west orders written: {west.stats['orders'].rows_written}")
        assert east.stats["orders"].rows_written == 4
        assert west.stats["orders"].rows_written == 4

        _print_header("interleave items until the strict conversion hits the bad value")
        east_failed = False
        while not east.is_exhausted("items"):
            west_outcome = west.pull("items")
            try:
                east_outcome = east.pull("items")
            except ConversionError as exc:
                east_failed = True
                print(f"EAST FAILED: {exc}")
                print(f"east items scanned before failure: {east.stats['items'].rows_scanned}")
                east.abort()
                break
            if east_outcome.exhausted:
                break
            if not west_outcome.exhausted and west_outcome.rows_written:
                print(f"west accepted item {west_outcome.row}")
        assert east_failed
        assert east.status == "failed"

        _print_header("source package stays readable with old data during the failure")
        legacy_mid = read_legacy_report(source_descriptor)
        assert legacy_mid == legacy_before
        print("paths, schemas and row counts unchanged:", legacy_mid)

        _print_header("failed conversion leaves no readable partial package")
        assert not os.path.exists(east_dir)
        assert not any(
            name.startswith(".staging-east") for name in os.listdir(output_dir)
        )
        print(f"no package at {east_dir}; staging directory removed")

        _print_header("tolerant west conversion keeps streaming and publishes both resources")
        west.drain("items")
        west_descriptor = west.commit()
        assert west.status == "completed"
    print(f"west package published at {west_descriptor}")
    print(f"west stats: orders={west.stats['orders'].rows_written} "
          f"items={west.stats['items'].rows_written} "
          f"rejected={len(west.stats['items'].rejected)}")
    assert west.stats["items"].rows_written == 8
    assert len(west.stats["items"].rejected) == 1
    results["west_stats"] = {
        name: stats.rows_written for name, stats in west.stats.items()
    }

    _print_header("an early-stopped consumer must not look successful")
    abandoned_dir = os.path.join(output_dir, "abandoned")
    abandoned = PackageConverter(
        source_descriptor, abandoned_dir, name="abandoned",
        region="East", policy="strict",
    )
    with abandoned:
        abandoned.pull("orders")
        abandoned.pull("orders")
        abandoned.stop_early()
    assert abandoned.status == "aborted"
    assert not os.path.exists(abandoned_dir)
    print("abandoned conversion status: aborted; no output directory left behind")

    _print_header("recover east with a brand new run from the same source package")
    recovered = PackageConverter(
        source_descriptor, east_dir, name="east-recovery",
        region="East", policy="tolerant",
    )
    with recovered:
        recovered.drain("orders")
        recovered.drain("items")
        east_descriptor = recovered.commit()
    assert recovered.status == "completed"
    print(f"recovery package published at {east_descriptor}")
    print(f"recovery stats: orders={recovered.stats['orders'].rows_written} "
          f"items={recovered.stats['items'].rows_written} "
          f"rejected={len(recovered.stats['items'].rejected)}")
    assert recovered.stats["orders"].rows_written == 4
    assert recovered.stats["items"].rows_written == 11
    assert len(recovered.stats["items"].rejected) == 1
    results["east_stats"] = {
        name: stats.rows_written for name, stats in recovered.stats.items()
    }

    _print_header("reopen landed packages independently and verify the new join key")
    for label, descriptor_path in (("west", west_descriptor), ("east", east_descriptor)):
        package = Package(descriptor_path)
        report = package.validate()
        orders = package.get_resource("orders")
        items = package.get_resource("items")
        with orders, items:
            orders_rows = orders.read_rows()
            items_rows = items.read_rows()
        print(
            f"{label}: orders fields={orders.schema.field_names} "
            f"rows={len(orders_rows)} | items fields={items.schema.field_names} "
            f"rows={len(items_rows)}"
        )
        print(f"{label}: items foreign key = {items.schema.foreign_keys}")
        assert "order_ref" in orders.schema.field_names
        assert "order_id" not in orders.schema.field_names
        assert "order_ref" in items.schema.field_names
        assert "order_id" not in items.schema.field_names
        assert items.schema.foreign_keys[0]["fields"] == ["order_ref"]
        assert items.schema.foreign_keys[0]["reference"]["fields"] == ["order_ref"]
        assert report.valid
        results[f"{label}_rows"] = {
            "orders": len(orders_rows),
            "items": len(items_rows),
        }

    _print_header("conversion stats and schemas never bleed into each other")
    assert results["west_stats"] != results["east_stats"]
    assert results["west_rows"] == {"orders": 4, "items": 8}
    assert results["east_rows"] == {"orders": 4, "items": 11}
    legacy_after = read_legacy_report(source_descriptor)
    assert legacy_after == legacy_before
    print("source package still describes the old data:", legacy_after)

    _print_header("all checks passed")
    return results


def main() -> None:
    run_scenario()


if __name__ == "__main__":
    main()
