"""Unit tests for the migration preview and credential engine."""

from __future__ import annotations

import unittest
from uuid import UUID

from bson import decode_all, encode
from bson.binary import Binary, UuidRepresentation
from bson.codec_options import CodecOptions
from migration.engine import (
    ALREADY_STANDARD,
    AMBIGUOUS,
    APPLICABLE,
    APPLIED,
    BLOCKED,
    CONFLICT,
    NOT_UUID,
    OUT_OF_SCOPE,
    SKIPPED_ALREADY_MIGRATED,
    STALE,
    UNKNOWN_SOURCE,
    Collections,
    CustomerSource,
    Decision,
    Document,
    apply_credential,
    audit_preservation,
    build_credential,
    build_registry,
    plan_all,
)

UNSPECIFIED: CodecOptions[Document] = CodecOptions(
    uuid_representation=UuidRepresentation.UNSPECIFIED
)
UUID_A = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
UUID_B = UUID("11111111-2222-4333-8444-555555555555")


def legacy(uuid_value: UUID, representation: int) -> Binary:
    return Binary.from_uuid(uuid_value, representation)


def standard(uuid_value: UUID) -> Binary:
    return Binary.from_uuid(uuid_value, UuidRepresentation.STANDARD)


def decisions_by_locator(decisions: list[Decision]) -> dict[str, Decision]:
    return {decision.locator: decision for decision in decisions}


class PreviewTests(unittest.TestCase):
    def test_standard_customer_key_is_never_a_migration_target(self) -> None:
        collections = {
            "customers": [{"_id": "c1", "customer_key": standard(UUID_A)}],
            "orders": [],
        }
        decisions = plan_all(collections, build_registry([]))
        decision = decisions_by_locator(decisions)["customers/c1/customer_key"]
        self.assertEqual(decision.reason, ALREADY_STANDARD)
        self.assertEqual(decision.source, "standard")
        self.assertEqual(decision.target_uuid, None)

    def test_sourced_legacy_reference_is_applicable_with_evidence(self) -> None:
        binary = legacy(UUID_A, UuidRepresentation.PYTHON_LEGACY)
        collections = {"orders": [{"_id": "o1", "customer_id": "c1", "customer": binary}]}
        sources = [CustomerSource("c1", "python_legacy", "orders/o1/customer")]
        decisions = plan_all(collections, build_registry(sources))
        decision = decisions_by_locator(decisions)["orders/o1/customer"]
        self.assertEqual(decision.reason, APPLICABLE)
        self.assertEqual(decision.original_hex, bytes(binary).hex())
        self.assertEqual(decision.subtype, 3)
        self.assertEqual(decision.source, "python_legacy")
        self.assertEqual(decision.customer_id, "c1")
        self.assertEqual(decision.target_uuid, str(UUID_A))
        self.assertEqual(decision.target_hex, UUID_A.hex)

    def test_unsourced_legacy_binary_is_not_guessed_as_uuid(self) -> None:
        binary = legacy(UUID_A, UuidRepresentation.PYTHON_LEGACY)
        collections = {"orders": [{"_id": "o1", "customer_id": "c1", "customer": binary}]}
        decisions = plan_all(collections, build_registry([]))
        decision = decisions_by_locator(decisions)["orders/o1/customer"]
        self.assertEqual(decision.reason, UNKNOWN_SOURCE)
        self.assertIsNone(decision.target_uuid)
        self.assertEqual(decision.original_hex, bytes(binary).hex())

    def test_java_and_csharp_legacy_bytes_normalize_to_same_conflicting_uuid(self) -> None:
        java_bytes = legacy(UUID_B, UuidRepresentation.JAVA_LEGACY)
        csharp_bytes = legacy(UUID_B, UuidRepresentation.CSHARP_LEGACY)
        self.assertNotEqual(bytes(java_bytes), bytes(csharp_bytes))
        collections = {
            "customers": [
                {"_id": "c2", "customer_key": java_bytes},
                {"_id": "c3", "customer_key": csharp_bytes},
            ],
            "orders": [
                {"_id": "o2", "customer_id": "c2", "customer": java_bytes},
                {"_id": "o3", "customer_id": "c3", "customer": csharp_bytes},
            ],
        }
        sources = [
            CustomerSource("c2", "java_legacy", "customers/c2/customer_key"),
            CustomerSource("c3", "csharp_legacy", "customers/c3/customer_key"),
            CustomerSource("c2", "java_legacy", "orders/o2/customer"),
            CustomerSource("c3", "csharp_legacy", "orders/o3/customer"),
        ]
        decisions = decisions_by_locator(plan_all(collections, build_registry(sources)))
        for locator in (
            "customers/c2/customer_key",
            "customers/c3/customer_key",
            "orders/o2/customer",
            "orders/o3/customer",
        ):
            self.assertEqual(decisions[locator].reason, CONFLICT)
            self.assertEqual(decisions[locator].target_uuid, str(UUID_B))

    def test_non_managed_and_non_uuid_binaries_are_preserved_and_labelled(self) -> None:
        plain = Binary(b"plain-binary-payload", 0)
        md5 = Binary(b"md5-binary-value", 5)
        other = legacy(UUID_A, UuidRepresentation.PYTHON_LEGACY)
        collections = {
            "orders": [
                {
                    "_id": "o1",
                    "customer_id": "c1",
                    "attachments": [plain],
                    "checksum": md5,
                    "other_ref": other,
                }
            ]
        }
        decisions = decisions_by_locator(plan_all(collections, build_registry([])))
        self.assertEqual(decisions["orders/o1/attachments/0"].reason, OUT_OF_SCOPE)
        self.assertEqual(decisions["orders/o1/checksum"].reason, OUT_OF_SCOPE)
        self.assertEqual(decisions["orders/o1/other_ref"].reason, OUT_OF_SCOPE)

    def test_non_sixteen_byte_legacy_subtype_is_not_uuid(self) -> None:
        short_binary = Binary(b"\x01\x02\x03", 3)
        collections = {"orders": [{"_id": "o1", "customer_id": "c1", "customer": short_binary}]}
        sources = [CustomerSource("c1", "python_legacy", "orders/o1/customer")]
        decisions = decisions_by_locator(plan_all(collections, build_registry(sources)))
        self.assertEqual(decisions["orders/o1/customer"].reason, NOT_UUID)

    def test_standard_provenance_on_legacy_binary_is_ambiguous_not_rewritten(self) -> None:
        binary = legacy(UUID_A, UuidRepresentation.PYTHON_LEGACY)
        collections = {"orders": [{"_id": "o1", "customer_id": "c1", "customer": binary}]}
        sources = [CustomerSource("c1", "standard", "orders/o1/customer")]
        decisions = decisions_by_locator(plan_all(collections, build_registry(sources)))
        self.assertEqual(decisions["orders/o1/customer"].reason, AMBIGUOUS)


class CredentialTests(unittest.TestCase):
    def _base_dataset(self) -> tuple[Collections, list[CustomerSource]]:
        collections = {
            "customers": [{"_id": "c1", "customer_key": standard(UUID_A)}],
            "orders": [
                {
                    "_id": "o1",
                    "customer_id": "c1",
                    "customer": legacy(UUID_A, UuidRepresentation.PYTHON_LEGACY),
                },
                {
                    "_id": "o2",
                    "customer_id": "c1",
                    "customer": legacy(UUID_B, UuidRepresentation.PYTHON_LEGACY),
                },
                {
                    "_id": "o5",
                    "customer_id": "c5",
                    "customer": legacy(UUID_B, UuidRepresentation.JAVA_LEGACY),
                },
            ],
        }
        sources = [
            CustomerSource("c1", "python_legacy", "orders/o1/customer"),
            CustomerSource("c5", "java_legacy", "orders/o5/customer"),
        ]
        return collections, sources

    def test_credential_only_contains_applicable_entries(self) -> None:
        collections, sources = self._base_dataset()
        decisions = plan_all(collections, build_registry(sources))
        credential = build_credential(decisions, "fingerprint")
        self.assertEqual([entry.doc_id for entry in credential.entries], ["o1", "o5"])
        for entry in credential.entries:
            self.assertEqual(entry.subtype, 3)

    def test_resend_without_identity_change_keeps_credential_applicable(self) -> None:
        collections, sources = self._base_dataset()
        registry = build_registry(
            [*sources, CustomerSource("c1", "python_legacy", "orders/o1/customer")]
        )
        credential = build_credential(plan_all(collections, build_registry(sources)), "fp")
        result = apply_credential(credential, collections, registry)
        self.assertEqual(
            result.outcomes,
            {"orders/o1/customer": APPLIED, "orders/o5/customer": APPLIED},
        )
        migrated = result.collections["orders"][0]["customer"]
        self.assertEqual(migrated.subtype, 4)
        self.assertEqual(bytes(migrated), UUID_A.bytes)

    def test_late_conflict_blocks_only_affected_decision(self) -> None:
        collections, sources = self._base_dataset()
        conflicting_collections = {
            "customers": [
                *collections["customers"],
                {"_id": "c4", "customer_key": legacy(UUID_A, UuidRepresentation.JAVA_LEGACY)},
            ],
            "orders": collections["orders"],
        }
        conflicting_sources = [
            *sources,
            CustomerSource("c4", "java_legacy", "customers/c4/customer_key"),
        ]
        credential = build_credential(plan_all(collections, build_registry(sources)), "fp")
        result = apply_credential(
            credential, conflicting_collections, build_registry(conflicting_sources)
        )
        self.assertEqual(result.outcomes["orders/o1/customer"], BLOCKED)
        self.assertEqual(result.outcomes["orders/o5/customer"], APPLIED)
        blocked_binary = result.collections["orders"][0]["customer"]
        self.assertEqual(blocked_binary.subtype, 3)
        self.assertEqual(bytes(blocked_binary), bytes(collections["orders"][0]["customer"]))

    def test_changed_original_value_makes_entry_stale(self) -> None:
        collections, sources = self._base_dataset()
        credential = build_credential(plan_all(collections, build_registry(sources)), "fp")
        changed = {
            "customers": collections["customers"],
            "orders": [
                {
                    "_id": "o1",
                    "customer_id": "c1",
                    "customer": legacy(UUID_A, UuidRepresentation.JAVA_LEGACY),
                },
                *collections["orders"][1:],
            ],
        }
        result = apply_credential(credential, changed, build_registry(sources))
        self.assertEqual(result.outcomes["orders/o1/customer"], STALE)

    def test_rerun_on_migrated_files_skips_and_keeps_bytes_identical(self) -> None:
        collections, sources = self._base_dataset()
        registry = build_registry(sources)
        credential = build_credential(plan_all(collections, registry), "fp")
        first = apply_credential(credential, collections, registry)
        second = apply_credential(credential, first.collections, registry)
        self.assertEqual(set(second.outcomes.values()), {SKIPPED_ALREADY_MIGRATED})
        raw_before = b"".join(
            encode(doc, codec_options=UNSPECIFIED) for doc in first.collections["orders"]
        )
        raw_after = b"".join(
            encode(doc, codec_options=UNSPECIFIED) for doc in second.collections["orders"]
        )
        self.assertEqual(raw_before, raw_after)

    def test_audit_preservation_reports_untouched_binaries(self) -> None:
        plain = Binary(b"plain-binary-payload", 0)
        collections = {
            "orders": [
                {
                    "_id": "o1",
                    "customer_id": "c1",
                    "customer": legacy(UUID_A, UuidRepresentation.PYTHON_LEGACY),
                    "attachments": [plain],
                }
            ]
        }
        sources = [CustomerSource("c1", "python_legacy", "orders/o1/customer")]
        credential = build_credential(plan_all(collections, build_registry(sources)), "fp")
        result = apply_credential(credential, collections, build_registry(sources))
        preserved = audit_preservation(collections, result.collections)
        self.assertEqual(
            preserved,
            [
                {
                    "locator": "orders/o1/attachments/0",
                    "subtype": 0,
                    "hex": bytes(plain).hex(),
                }
            ],
        )

    def test_bson_round_trip_keeps_unsourced_binary_exact(self) -> None:
        unsourced = legacy(UUID_B, UuidRepresentation.PYTHON_LEGACY)
        document = {"_id": "o2", "customer_id": "c1", "customer": unsourced}
        raw = encode(document, codec_options=UNSPECIFIED)
        decoded = decode_all(raw, codec_options=UNSPECIFIED)[0]
        re_encoded = encode(decoded, codec_options=UNSPECIFIED)
        self.assertEqual(raw, re_encoded)
        self.assertEqual(decoded["customer"].subtype, 3)
        self.assertEqual(bytes(decoded["customer"]), bytes(unsourced))


if __name__ == "__main__":
    unittest.main()
