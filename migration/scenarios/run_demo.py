"""End-to-end scenario for the legacy UUID migration review workflow.

The scenario is fully deterministic and exercises:

1. The first batch: a customer already on the standard UUID subtype whose two
   orders carry a sourced and an unsourced legacy reference, plus two distinct
   verified customers whose Java and C# legacy keys normalize to the same UUID.
2. A resend that only repeats provenance for an already verified customer.
3. A late and genuine conflict: a different customer whose distinct legacy
   bytes normalize to the previously unique customer UUID.
4. A final rerun on already-migrated files proving standard records are not
   rewritten.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import UUID

from bson.binary import Binary, UuidRepresentation
from migration.engine import (
    ALREADY_STANDARD,
    APPLICABLE,
    APPLIED,
    BLOCKED,
    CONFLICT,
    OUT_OF_SCOPE,
    SKIPPED_ALREADY_MIGRATED,
    UNKNOWN_SOURCE,
    Collections,
    CustomerSource,
    Decision,
    apply_credential,
    audit_preservation,
    build_credential,
    build_registry,
    fingerprint_inputs,
    load_credential,
    plan_all,
    save_credential,
    write_bson,
    write_sources,
)

U1 = UUID("00112233-4455-4677-8899-aabbccddeeff")
U2 = UUID("10203040-5060-4070-8090-a0b0c0d0e0f0")
U5 = UUID("55555555-6666-4777-8888-9999aaaabbbb")
U_UNSOURCED = UUID("77777777-8888-4999-aaaa-bbbbccccdddd")

DEMO_ROOT = Path(__file__).resolve().parents[2] / "migration_demo"


def _legacy(uuid_value: UUID, representation: int) -> Binary:
    return Binary.from_uuid(uuid_value, representation)


def _standard(uuid_value: UUID) -> Binary:
    return Binary.from_uuid(uuid_value, UuidRepresentation.STANDARD)


def build_batch_one() -> tuple[Collections, list[CustomerSource]]:
    """Build the first batch collections and its itemized provenance."""
    customers = [
        {
            "_id": "c1",
            "customer_key": _standard(U1),
            "blob": Binary(b"\x00" * 16, 0),
        },
        {
            "_id": "c2",
            "customer_key": _legacy(U2, UuidRepresentation.JAVA_LEGACY),
        },
        {
            "_id": "c3",
            "customer_key": _legacy(U2, UuidRepresentation.CSHARP_LEGACY),
            "other_ref": _legacy(
                UUID("33333333-4444-4555-8666-777788889999"),
                UuidRepresentation.PYTHON_LEGACY,
            ),
        },
        {
            "_id": "c5",
            "customer_key": _standard(U5),
        },
    ]
    orders = [
        {
            "_id": "o1",
            "customer_id": "c1",
            "customer": _legacy(U1, UuidRepresentation.PYTHON_LEGACY),
            "note": "sourced legacy reference to the already-standard customer",
        },
        {
            "_id": "o2",
            "customer_id": "c1",
            "customer": _legacy(U_UNSOURCED, UuidRepresentation.PYTHON_LEGACY),
            "attachments": [Binary(b"plain-binary-payload", 0)],
            "checksum": Binary(b"md5-binary-value", 5),
            "note": "legacy reference with no declared source",
        },
        {
            "_id": "o3",
            "customer_id": "c2",
            "customer": _legacy(U2, UuidRepresentation.JAVA_LEGACY),
            "note": "isolated order pointing at the shared UUID group",
        },
        {
            "_id": "o4",
            "customer_id": "c3",
            "customer": _legacy(U2, UuidRepresentation.CSHARP_LEGACY),
            "note": "isolated order pointing at the shared UUID group",
        },
        {
            "_id": "o5",
            "customer_id": "c5",
            "customer": _legacy(U5, UuidRepresentation.PYTHON_LEGACY),
            "note": "control order unrelated to any conflict",
        },
    ]
    sources = [
        CustomerSource("c1", "python_legacy", "orders/o1/customer"),
        CustomerSource("c2", "java_legacy", "customers/c2/customer_key"),
        CustomerSource("c3", "csharp_legacy", "customers/c3/customer_key"),
        CustomerSource("c2", "java_legacy", "orders/o3/customer"),
        CustomerSource("c3", "csharp_legacy", "orders/o4/customer"),
        CustomerSource("c5", "python_legacy", "orders/o5/customer"),
    ]
    return {"customers": customers, "orders": orders}, sources


def _write_dataset(directory: Path, collections: dict[str, list[dict[str, object]]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, documents in collections.items():
        write_bson(str(directory / f"{name}.bson"), documents)


def _decisions_by_locator(decisions: list[Decision]) -> dict[str, Decision]:
    return {decision.locator: decision for decision in decisions}


def _print_header(title: str) -> None:
    print("\n" + "=" * 88)
    print(title)
    print("=" * 88)


def _print_decisions(decisions: list[Decision]) -> None:
    for decision in decisions:
        suffix = f" -> {decision.target_uuid}" if decision.target_uuid else ""
        print(
            f"  {decision.locator:<32} subtype={decision.subtype} "
            f"original={decision.original_hex} customer={decision.customer_id} "
            f"source={decision.source} decision={decision.reason}{suffix}"
        )


def _assert_outcomes(actual: dict[str, str], expected: dict[str, str]) -> None:
    for locator, outcome in expected.items():
        if actual.get(locator) != outcome:
            raise AssertionError(f"{locator}: expected {outcome}, got {actual.get(locator)}")


def main() -> None:
    """Run every phase of the scenario."""
    if DEMO_ROOT.exists():
        shutil.rmtree(DEMO_ROOT)
    batch_one, batch_sources = build_batch_one()

    _print_header("Phase 1 - first batch preview and applicable credential")
    batch_dir = DEMO_ROOT / "batch1"
    _write_dataset(batch_dir, batch_one)
    write_sources(str(batch_dir / "sources.json"), batch_sources)
    registry = build_registry(batch_sources)
    decisions = plan_all(batch_one, registry)
    _print_decisions(decisions)
    by_locator = _decisions_by_locator(decisions)
    assert by_locator["customers/c1/customer_key"].reason == ALREADY_STANDARD
    assert by_locator["customers/c2/customer_key"].reason == CONFLICT
    assert by_locator["customers/c3/customer_key"].reason == CONFLICT
    assert by_locator["orders/o1/customer"].reason == APPLICABLE
    assert by_locator["orders/o1/customer"].target_uuid == str(U1)
    assert by_locator["orders/o2/customer"].reason == UNKNOWN_SOURCE
    assert by_locator["orders/o2/customer"].target_uuid is None
    assert by_locator["orders/o3/customer"].reason == CONFLICT
    assert by_locator["orders/o4/customer"].reason == CONFLICT
    assert by_locator["orders/o5/customer"].reason == APPLICABLE
    assert by_locator["orders/o5/customer"].target_uuid == str(U5)
    assert by_locator["orders/o2/attachments/0"].reason == OUT_OF_SCOPE
    assert by_locator["orders/o2/checksum"].reason == OUT_OF_SCOPE
    assert by_locator["customers/c3/other_ref"].reason == OUT_OF_SCOPE
    input_paths = [str(path) for path in sorted(batch_dir.iterdir())]
    credential = build_credential(decisions, fingerprint_inputs(input_paths))
    credential_path = DEMO_ROOT / "credential1.json"
    save_credential(str(credential_path), credential)
    assert [entry.doc_id for entry in credential.entries] == ["o1", "o5"]
    print(
        f"\ncredential {credential.credential_id} applicable order entries: "
        f"{[entry.doc_id for entry in credential.entries]}"
    )
    preview_dir = DEMO_ROOT / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    with (preview_dir / "preview1.json").open("w", encoding="utf-8") as handle:
        json.dump([decision.to_dict() for decision in decisions], handle, indent=2)

    _print_header("Phase 2a - resend without identity change, original credential still applies")
    resend_collections_dir = DEMO_ROOT / "rerun_resend" / "collections"
    _write_dataset(resend_collections_dir, batch_one)
    resend_sources = [
        *batch_sources,
        CustomerSource("c1", "python_legacy", "orders/o1/customer"),
    ]
    write_sources(str(resend_collections_dir / "sources.json"), resend_sources)
    resend_decisions = plan_all(batch_one, build_registry(resend_sources))
    _print_decisions(resend_decisions)
    assert [decision.to_dict() for decision in resend_decisions] == [
        decision.to_dict() for decision in decisions
    ]
    resend_result = apply_credential(
        load_credential(str(credential_path)), batch_one, build_registry(resend_sources)
    )
    _assert_outcomes(
        resend_result.outcomes,
        {"orders/o1/customer": APPLIED, "orders/o5/customer": APPLIED},
    )
    preserved = audit_preservation(batch_one, resend_result.collections)
    _write_dataset(DEMO_ROOT / "rerun_resend" / "applied", resend_result.collections)
    print(f"  preserved untouched binaries: {len(preserved)}")
    print(f"  outcomes: {resend_result.outcomes}")

    _print_header("Phase 2b - late genuine conflict on the previously unique UUID")
    conflict_collections = {
        "customers": [
            *batch_one["customers"],
            {
                "_id": "c4",
                "customer_key": _legacy(U1, UuidRepresentation.JAVA_LEGACY),
            },
        ],
        "orders": batch_one["orders"],
    }
    conflict_sources = [
        *batch_sources,
        CustomerSource("c4", "java_legacy", "customers/c4/customer_key"),
    ]
    conflict_dir = DEMO_ROOT / "rerun_conflict" / "collections"
    _write_dataset(conflict_dir, conflict_collections)
    write_sources(str(conflict_dir / "sources.json"), conflict_sources)
    conflict_decisions = plan_all(conflict_collections, build_registry(conflict_sources))
    _print_decisions(conflict_decisions)
    conflict_by_locator = _decisions_by_locator(conflict_decisions)
    assert conflict_by_locator["customers/c4/customer_key"].reason == CONFLICT
    assert conflict_by_locator["orders/o1/customer"].reason == CONFLICT
    assert conflict_by_locator["orders/o5/customer"].reason == APPLICABLE
    assert conflict_by_locator["orders/o2/customer"].reason == UNKNOWN_SOURCE
    conflict_result = apply_credential(
        load_credential(str(credential_path)),
        conflict_collections,
        build_registry(conflict_sources),
    )
    _assert_outcomes(
        conflict_result.outcomes,
        {"orders/o1/customer": BLOCKED, "orders/o5/customer": APPLIED},
    )
    _write_dataset(DEMO_ROOT / "rerun_conflict" / "applied", conflict_result.collections)
    audit_preservation(conflict_collections, conflict_result.collections)
    o1_after = _decisions_by_locator(
        plan_all(conflict_result.collections, build_registry(conflict_sources))
    )
    assert o1_after["orders/o1/customer"].reason == CONFLICT
    print(f"  outcomes: {conflict_result.outcomes}")

    _print_header("Phase 3 - rerun on migrated files with late customer and isolated records")
    canonical = apply_credential(
        load_credential(str(credential_path)), batch_one, build_registry(batch_sources)
    )
    final_collections = {
        "customers": [
            *canonical.collections["customers"],
            {"_id": "c4", "customer_key": _legacy(U1, UuidRepresentation.JAVA_LEGACY)},
        ],
        "orders": canonical.collections["orders"],
    }
    final_dir = DEMO_ROOT / "final" / "collections"
    _write_dataset(final_dir, final_collections)
    write_sources(str(final_dir / "sources.json"), conflict_sources)
    final_decisions = plan_all(final_collections, build_registry(conflict_sources))
    _print_decisions(final_decisions)
    final_by_locator = _decisions_by_locator(final_decisions)
    assert final_by_locator["orders/o1/customer"].reason == ALREADY_STANDARD
    assert final_by_locator["orders/o5/customer"].reason == ALREADY_STANDARD
    assert final_by_locator["customers/c4/customer_key"].reason == CONFLICT
    final_result = apply_credential(
        load_credential(str(credential_path)),
        final_collections,
        build_registry(conflict_sources),
    )
    _assert_outcomes(
        final_result.outcomes,
        {
            "orders/o1/customer": SKIPPED_ALREADY_MIGRATED,
            "orders/o5/customer": SKIPPED_ALREADY_MIGRATED,
        },
    )
    final_out = DEMO_ROOT / "final" / "applied"
    _write_dataset(final_out, final_result.collections)
    for name in ("customers", "orders"):
        before = (final_dir / f"{name}.bson").read_bytes()
        after = (final_out / f"{name}.bson").read_bytes()
        assert before == after, f"{name}.bson was rewritten during the idempotent rerun"
    print(f"  outcomes: {final_result.outcomes}")
    print("  migrated .bson files are byte-identical: standard records were not rewritten")

    _print_header("Scenario summary")
    print(f"  demo artifacts written under {DEMO_ROOT}")
    print("  unsourced and conflicting binaries kept their original BSON subtype and bytes")
    print("  all other binaries preserved byte-for-byte (see audit_preservation calls)")


if __name__ == "__main__":
    main()
