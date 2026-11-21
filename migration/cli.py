"""Command line interface for the legacy UUID migration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING

from migration.engine import (
    APPLIED,
    DEFAULT_FIELDS,
    SKIPPED_ALREADY_MIGRATED,
    apply_credential,
    audit_preservation,
    build_credential,
    build_registry,
    fingerprint_inputs,
    load_credential,
    plan_all,
    read_bson,
    read_sources,
    save_credential,
    write_bson,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from migration.engine import Decision


def _load_collections(directory: Path) -> dict[str, list[dict[str, object]]]:
    collections: dict[str, list[dict[str, object]]] = {}
    for path in sorted(directory.glob("*.bson")):
        collections[path.stem] = read_bson(str(path))
    if not collections:
        raise SystemExit(f"no .bson collection files found in {directory}")
    return collections


def _write_collections(directory: Path, collections: dict[str, list[dict[str, object]]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, documents in collections.items():
        write_bson(str(directory / f"{name}.bson"), documents)


def _print_decisions(decisions: list[Decision]) -> None:
    header = (
        f"{'locator':<34} {'subtype':>7}  {'original BSON (hex)':<34} "
        f"{'customer':<10} {'source':<14} decision"
    )
    print(header)
    print("-" * len(header))
    for decision in decisions:
        print(
            f"{decision.locator:<34} {decision.subtype:>7}  {decision.original_hex:<34} "
            f"{decision.customer_id!s:<10} {decision.source!s:<14} {decision.reason}"
            + (f" -> {decision.target_uuid}" if decision.target_uuid else "")
        )


def _counts(decisions: list[Decision]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision.reason] = counts.get(decision.reason, 0) + 1
    return dict(sorted(counts.items()))


def _preview(args: argparse.Namespace) -> None:
    collections_dir = Path(args.collections)
    collections = _load_collections(collections_dir)
    sources = read_sources(args.sources)
    registry = build_registry(sources)
    fields = tuple(args.fields)
    decisions = plan_all(collections, registry, fields)
    _print_decisions(decisions)
    payload = {
        "decisions": [decision.to_dict() for decision in decisions],
        "counts": _counts(decisions),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    print(f"\nwrote preview {out_path}")
    if args.credential:
        input_paths = [str(path) for path in sorted(collections_dir.glob("*.bson"))]
        input_paths.append(args.sources)
        credential = build_credential(decisions, fingerprint_inputs(input_paths))
        save_credential(args.credential, credential)
        print(
            f"wrote credential {args.credential} "
            f"({len(credential.entries)} applicable entries, id {credential.credential_id})"
        )


def _apply(args: argparse.Namespace) -> None:
    collections = _load_collections(Path(args.collections))
    registry = build_registry(read_sources(args.sources))
    credential = load_credential(args.credential)
    result = apply_credential(credential, collections, registry, tuple(args.fields))
    print("credential application results:")
    for locator, outcome in sorted(result.outcomes.items()):
        print(f"  {locator:<34} {outcome}")
    applied = sum(1 for outcome in result.outcomes.values() if outcome == APPLIED)
    skipped = sum(1 for outcome in result.outcomes.values() if outcome == SKIPPED_ALREADY_MIGRATED)
    blocked = sum(1 for outcome in result.outcomes.values() if outcome == "blocked")
    print(f"\napplied={applied} already_migrated={skipped} blocked={blocked}")
    preserved = audit_preservation(collections, result.collections, tuple(args.fields))
    print(f"preserved untouched binaries: {len(preserved)}")
    out_dir = Path(args.out_dir)
    _write_collections(out_dir, result.collections)
    print(f"wrote migrated collections to {out_dir}")
    report = {
        "credential_id": credential.credential_id,
        "outcomes": result.outcomes,
        "fresh_preview": [decision.to_dict() for decision in result.fresh_decisions],
        "fresh_counts": _counts(result.fresh_decisions),
        "preserved": preserved,
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(f"wrote apply report {report_path}")


def build_parser() -> argparse.ArgumentParser:
    """Build the migration CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview = subparsers.add_parser("preview", help="preview migration decisions")
    preview.add_argument("--collections", required=True, help="directory of <name>.bson files")
    preview.add_argument("--sources", required=True, help="JSON file of customer sources")
    preview.add_argument("--out", required=True, help="output preview JSON path")
    preview.add_argument("--credential", help="optional path to write an applicable credential")
    preview.add_argument("--fields", nargs="+", default=list(DEFAULT_FIELDS))
    preview.set_defaults(func=_preview)

    apply = subparsers.add_parser("apply", help="apply a credential to current data")
    apply.add_argument("--collections", required=True, help="directory of <name>.bson files")
    apply.add_argument("--sources", required=True, help="JSON file of current customer sources")
    apply.add_argument("--credential", required=True, help="credential issued by a preview")
    apply.add_argument("--out-dir", required=True, help="directory for migrated .bson files")
    apply.add_argument("--report", required=True, help="output apply report JSON path")
    apply.add_argument("--fields", nargs="+", default=list(DEFAULT_FIELDS))
    apply.set_defaults(func=_apply)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
