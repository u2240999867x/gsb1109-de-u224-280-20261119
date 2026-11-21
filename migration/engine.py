"""Preview, credential and apply logic for the legacy UUID migration.

The migration turns BSON binaries that carry a legacy UUID representation
(BSON binary subtype 3) into standard UUID binaries (BSON binary subtype 4).
A binary is only migrated when the business has supplied the source
representation for that exact field. Binaries without a declared source are
never interpreted as UUIDs and every other BSON binary value is preserved
unchanged.

Provenance is itemized: each business record names the field locator
(``collection/document/field-path``), the customer that the field identifies
and the source representation. A resend that repeats the same record carries
no new identity.

Decisions are made in two phases. The first scan collects every UUID claim
(which normalized UUID a field resolves to and which customer it identifies)
so that a UUID claimed by more than one distinct customer can be marked as
conflicting before any individual migration decision is issued. The second
scan turns each binary field into a :class:`Decision`.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from bson import decode_all, encode
from bson.binary import (
    OLD_UUID_SUBTYPE,
    UUID_SUBTYPE,
    Binary,
    UuidRepresentation,
)
from bson.codec_options import CodecOptions

# A BSON document is dynamically typed, so its values are represented as Any.
Document = dict[str, Any]
Collections = dict[str, list[Document]]
JsonDict = dict[str, Any]

UNSPECIFIED_CODEC: CodecOptions[Document] = CodecOptions(
    uuid_representation=UuidRepresentation.UNSPECIFIED
)

STANDARD_SOURCE = "standard"
LEGACY_SOURCES = {
    "python_legacy": UuidRepresentation.PYTHON_LEGACY,
    "java_legacy": UuidRepresentation.JAVA_LEGACY,
    "csharp_legacy": UuidRepresentation.CSHARP_LEGACY,
}
VALID_SOURCES = (STANDARD_SOURCE, *LEGACY_SOURCES)

APPLICABLE = "applicable"
ALREADY_STANDARD = "already_standard"
APPLIED = "applied"
AMBIGUOUS = "ambiguous"
BLOCKED = "blocked"
CONFLICT = "conflict"
NOT_UUID = "not_uuid"
OUT_OF_SCOPE = "out_of_scope"
SKIPPED_ALREADY_MIGRATED = "skipped_already_migrated"
STALE = "stale"
UNKNOWN_SOURCE = "unknown_source"

DEFAULT_FIELDS = ("customer_key", "customer")


@dataclass(frozen=True)
class CustomerSource:
    """One itemized provenance record supplied by the business.

    :param customer_id: Customer that the field identifies.
    :param source: One of :data:`VALID_SOURCES`.
    :param locator: Field locator ``collection/document/field-path`` the
        provenance applies to. A repeated record for the same locator is a
        resend without identity change.
    """

    customer_id: str
    source: str
    locator: str


@dataclass
class Decision:
    """The migration decision for a single BSON binary field.

    :param collection: Name of the collection holding the document.
    :param doc_id: Value of the document ``_id`` field.
    :param path: Field path from the document root to the binary.
    :param subtype: Original BSON binary subtype.
    :param original_hex: Hex dump of the original binary payload.
    :param customer_id: Customer that owns the document, or ``None`` when the
        document does not declare a customer.
    :param source: Source representation used to interpret the binary, or
        ``None`` when no usable source was supplied.
    :param reason: One of the module-level decision constants.
    :param target_uuid: Normalized UUID string for applicable decisions.
    :param target_hex: RFC-4122 payload of the standard UUID binary.
    """

    collection: str
    doc_id: str
    path: tuple[str | int, ...]
    subtype: int
    original_hex: str
    customer_id: str | None
    source: str | None
    reason: str
    target_uuid: str | None = None
    target_hex: str | None = None

    @property
    def locator(self) -> str:
        """Stable string identifying the document field."""
        return f"{self.collection}/{self.doc_id}/{'/'.join(str(part) for part in self.path)}"

    def to_dict(self) -> JsonDict:
        """Serialize the decision to a JSON-compatible dictionary."""
        return {
            "locator": self.locator,
            "collection": self.collection,
            "doc_id": self.doc_id,
            "path": list(self.path),
            "original": {"subtype": self.subtype, "hex": self.original_hex},
            "customer_id": self.customer_id,
            "source": self.source,
            "decision": self.reason,
            "target_uuid": self.target_uuid,
            "target_hex": self.target_hex,
        }


@dataclass
class CredentialEntry:
    """One applicable migration decision captured inside a credential.

    The entry pins the exact original BSON value so an application run can
    refuse to touch a field whose stored value changed after the preview.
    """

    collection: str
    doc_id: str
    path: tuple[str | int, ...]
    subtype: int
    original_hex: str
    source: str
    customer_id: str
    target_uuid: str
    target_hex: str

    def to_dict(self) -> JsonDict:
        """Serialize the entry to a JSON-compatible dictionary."""
        return {
            "collection": self.collection,
            "doc_id": self.doc_id,
            "path": list(self.path),
            "subtype": self.subtype,
            "original_hex": self.original_hex,
            "source": self.source,
            "customer_id": self.customer_id,
            "target_uuid": self.target_uuid,
            "target_hex": self.target_hex,
        }

    @classmethod
    def from_dict(cls, data: JsonDict) -> CredentialEntry:
        """Build an entry from a JSON-compatible dictionary."""
        path = tuple(str(part) if isinstance(part, str) else int(part) for part in data["path"])
        return cls(
            collection=str(data["collection"]),
            doc_id=str(data["doc_id"]),
            path=path,
            subtype=int(data["subtype"]),
            original_hex=str(data["original_hex"]),
            source=str(data["source"]),
            customer_id=str(data["customer_id"]),
            target_uuid=str(data["target_uuid"]),
            target_hex=str(data["target_hex"]),
        )


@dataclass
class MigrationCredential:
    """The applicable subset of a preview, safe to hand to reviewers.

    :param credential_id: Stable identifier derived from the entries.
    :param created_at: ISO-8601 creation timestamp.
    :param input_fingerprint: SHA-256 of the preview inputs, for auditing.
    :param entries: Pinned applicable migration entries.
    """

    credential_id: str
    created_at: str
    input_fingerprint: str
    entries: list[CredentialEntry] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        """Serialize the credential to a JSON-compatible dictionary."""
        return {
            "credential_id": self.credential_id,
            "created_at": self.created_at,
            "input_fingerprint": self.input_fingerprint,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, data: JsonDict) -> MigrationCredential:
        """Build a credential from a JSON-compatible dictionary."""
        entries = [CredentialEntry.from_dict(row) for row in data["entries"]]
        return cls(
            credential_id=str(data["credential_id"]),
            created_at=str(data["created_at"]),
            input_fingerprint=str(data["input_fingerprint"]),
            entries=entries,
        )


class Registry:
    """Itemized customer provenance assembled from the business sources."""

    def __init__(self, sources: list[CustomerSource]):
        by_locator: dict[str, list[CustomerSource]] = defaultdict(list)
        for record in sources:
            if record.source not in VALID_SOURCES:
                raise ValueError(f"unknown source {record.source!r} at {record.locator}")
            by_locator[record.locator].append(record)
        self._by_locator = dict(by_locator)

    def source_for_locator(self, locator: str) -> str | None:
        """Return the agreed source for a field, or ``None`` when undeclared.

        Raises :class:`ValueError` when resends disagree on the source.
        """
        records = self._by_locator.get(locator)
        if not records:
            return None
        sources = {record.source for record in records}
        if len(sources) > 1:
            raise ValueError(f"conflicting provenance sources for {locator}: {sorted(sources)}")
        return records[0].source

    def records_for(self, locator: str) -> tuple[CustomerSource, ...]:
        """Return all provenance records for a field, resends included."""
        return tuple(self._by_locator.get(locator, ()))


def read_bson(path: str) -> list[Document]:
    """Read a file of concatenated BSON documents."""
    with open(path, "rb") as handle:
        return decode_all(handle.read(), codec_options=UNSPECIFIED_CODEC)


def write_bson(path: str, documents: list[Document]) -> None:
    """Write documents as concatenated BSON, preserving plain binaries."""
    with open(path, "wb") as handle:
        for document in documents:
            handle.write(encode(document, codec_options=UNSPECIFIED_CODEC))


def read_sources(path: str) -> list[CustomerSource]:
    """Read the business-supplied provenance file."""
    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle)
    return [
        CustomerSource(
            customer_id=str(row["customer_id"]),
            source=str(row["source"]),
            locator=str(row["locator"]),
        )
        for row in rows
    ]


def write_sources(path: str, sources: list[CustomerSource]) -> None:
    """Write provenance records as JSON."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            [
                {
                    "customer_id": record.customer_id,
                    "source": record.source,
                    "locator": record.locator,
                }
                for record in sources
            ],
            handle,
            indent=2,
        )
        handle.write("\n")


def build_registry(sources: list[CustomerSource]) -> Registry:
    """Build the provenance registry used by a preview."""
    return Registry(sources)


def _walk_binary_fields(
    value: Any, path: tuple[str | int, ...]
) -> list[tuple[tuple[str | int, ...], Binary]]:
    found: list[tuple[tuple[str | int, ...], Binary]] = []
    if isinstance(value, Binary):
        found.append((path, value))
    elif isinstance(value, dict):
        for key, nested in value.items():
            found.extend(_walk_binary_fields(nested, (*path, key)))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(_walk_binary_fields(nested, (*path, index)))
    return found


def _replace_at(value: Any, path: tuple[str | int, ...], replacement: Binary) -> Any:
    if not path:
        return replacement
    head, rest = path[0], path[1:]
    if isinstance(head, int):
        sequence = list(value)
        sequence[head] = _replace_at(sequence[head], rest, replacement)
        return sequence
    document = dict(value)
    document[head] = _replace_at(document[head], rest, replacement)
    return document


def _customer_of(document: Document) -> str | None:
    customer = document.get("customer_id")
    if customer is not None:
        return str(customer)
    return str(document["_id"]) if "customer_key" in document or "customer" in document else None


def _is_legacy_uuid_binary(binary: Binary) -> bool:
    return binary.subtype == OLD_UUID_SUBTYPE and len(binary) == 16


def _is_standard_uuid_binary(binary: Binary) -> bool:
    return binary.subtype == UUID_SUBTYPE and len(binary) == 16


def _locator(collection_name: str, document: Document, path: tuple[str | int, ...]) -> str:
    return f"{collection_name}/{document['_id']}/{'/'.join(str(part) for part in path)}"


def _collect_claims(
    collections: Collections, registry: Registry, fields: tuple[str, ...]
) -> dict[str, set[str]]:
    claims: dict[str, set[str]] = defaultdict(set)
    for collection_name, documents in collections.items():
        for document in documents:
            customer_id = _customer_of(document)
            if customer_id is None:
                continue
            for path, binary in _walk_binary_fields(document, ()):
                if path and path[0] not in fields:
                    continue
                normalized = _decode_claim(
                    binary,
                    registry,
                    _locator(collection_name, document, path),
                )
                if normalized is not None:
                    claims[normalized].add(customer_id)
    return claims


def _decode_claim(binary: Binary, registry: Registry, locator: str) -> str | None:
    if _is_standard_uuid_binary(binary):
        # Standard binaries self-describe their UUID and need no provenance.
        return str(binary.as_uuid(UuidRepresentation.STANDARD))
    if _is_legacy_uuid_binary(binary):
        source = registry.source_for_locator(locator)
        if source in LEGACY_SOURCES:
            return str(binary.as_uuid(LEGACY_SOURCES[source]))
    return None


def plan_all(
    collections: Collections,
    registry: Registry,
    fields: tuple[str, ...] = DEFAULT_FIELDS,
) -> list[Decision]:
    """Preview every binary field and return the ordered decisions.

    :param collections: Mapping of collection name to its documents.
    :param registry: Provenance registry built from business sources.
    :param fields: Top-level field names that may hold customer references.
    """
    claims = _collect_claims(collections, registry, fields)
    conflicts = {claimed for claimed, customers in claims.items() if len(customers) > 1}
    decisions: list[Decision] = []
    for collection_name, documents in collections.items():
        for document in documents:
            customer_id = _customer_of(document)
            for path, binary in _walk_binary_fields(document, ()):
                locator = _locator(collection_name, document, path)
                if path and path[0] not in fields:
                    decisions.append(
                        _decision(
                            collection_name,
                            document,
                            path,
                            binary,
                            customer_id,
                            None,
                            OUT_OF_SCOPE,
                        )
                    )
                    continue
                decisions.append(
                    _decide_field(
                        collection_name,
                        document,
                        path,
                        binary,
                        customer_id,
                        registry,
                        conflicts,
                        locator,
                    )
                )
    return decisions


def _decision(
    collection_name: str,
    document: Document,
    path: tuple[str | int, ...],
    binary: Binary,
    customer_id: str | None,
    source: str | None,
    reason: str,
    target: UUID | None = None,
) -> Decision:
    return Decision(
        collection=collection_name,
        doc_id=str(document["_id"]),
        path=path,
        subtype=binary.subtype,
        original_hex=bytes(binary).hex(),
        customer_id=customer_id,
        source=source,
        reason=reason,
        target_uuid=str(target) if target is not None else None,
        target_hex=target.hex if target is not None else None,
    )


def _decide_field(
    collection_name: str,
    document: Document,
    path: tuple[str | int, ...],
    binary: Binary,
    customer_id: str | None,
    registry: Registry,
    conflicts: set[str],
    locator: str,
) -> Decision:
    if _is_standard_uuid_binary(binary):
        return _decision(
            collection_name, document, path, binary, customer_id, STANDARD_SOURCE, ALREADY_STANDARD
        )
    if not _is_legacy_uuid_binary(binary):
        return _decision(collection_name, document, path, binary, customer_id, None, NOT_UUID)
    source = registry.source_for_locator(locator)
    if source is None:
        return _decision(collection_name, document, path, binary, customer_id, None, UNKNOWN_SOURCE)
    if source not in LEGACY_SOURCES:
        return _decision(collection_name, document, path, binary, customer_id, source, AMBIGUOUS)
    normalized = binary.as_uuid(LEGACY_SOURCES[source])
    if str(normalized) in conflicts:
        return _decision(
            collection_name, document, path, binary, customer_id, source, CONFLICT, normalized
        )
    return _decision(
        collection_name, document, path, binary, customer_id, source, APPLICABLE, normalized
    )


def fingerprint_inputs(paths: list[str]) -> str:
    """Hash the preview input files so credential runs remain auditable."""
    digest = hashlib.sha256()
    for path in paths:
        with open(path, "rb") as handle:
            digest.update(path.encode("utf-8"))
            digest.update(handle.read())
    return digest.hexdigest()


def build_credential(decisions: list[Decision], input_fingerprint: str) -> MigrationCredential:
    """Capture the applicable decisions as a reviewable credential."""
    entries = [
        CredentialEntry(
            collection=decision.collection,
            doc_id=decision.doc_id,
            path=decision.path,
            subtype=decision.subtype,
            original_hex=decision.original_hex,
            source=decision.source or "",
            customer_id=decision.customer_id or "",
            target_uuid=decision.target_uuid or "",
            target_hex=decision.target_hex or "",
        )
        for decision in decisions
        if decision.reason == APPLICABLE
    ]
    material = json.dumps(
        [entry.to_dict() for entry in entries], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return MigrationCredential(
        credential_id=hashlib.sha256(material).hexdigest()[:16],
        created_at=datetime.now(timezone.utc).isoformat(),
        input_fingerprint=input_fingerprint,
        entries=entries,
    )


def save_credential(path: str, credential: MigrationCredential) -> None:
    """Persist a credential as JSON."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(credential.to_dict(), handle, indent=2)
        handle.write("\n")


def load_credential(path: str) -> MigrationCredential:
    """Load a previously issued credential."""
    with open(path, encoding="utf-8") as handle:
        return MigrationCredential.from_dict(json.load(handle))


@dataclass
class ApplyResult:
    """Outcome of applying a credential against current data.

    :param outcomes: Per-locator constants from :data:`APPLIED`,
        :data:`SKIPPED_ALREADY_MIGRATED`, :data:`BLOCKED` or :data:`STALE`.
    :param collections: Transformed collection documents.
    :param fresh_decisions: Decisions recomputed from the current snapshot.
    """

    outcomes: dict[str, str]
    collections: Collections
    fresh_decisions: list[Decision]


def _find_binary(document: Document, path: tuple[str | int, ...]) -> Binary | None:
    value: Any = document
    for part in path:
        if isinstance(part, int):
            if not isinstance(value, list) or part >= len(value):
                return None
            value = value[part]
        else:
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
    return value if isinstance(value, Binary) else None


def apply_credential(
    credential: MigrationCredential,
    collections: Collections,
    registry: Registry,
    fields: tuple[str, ...] = DEFAULT_FIELDS,
) -> ApplyResult:
    """Apply a credential, re-validating every entry against current data.

    A credential entry is applied only when its original BSON value is still
    stored unchanged and a fresh preview still considers it applicable. A late
    conflict blocks the affected entry while unrelated entries still apply.
    Already-standard fields are never rewritten.
    """
    fresh = plan_all(collections, registry, fields)
    fresh_by_locator = {decision.locator: decision for decision in fresh}
    working: Collections = {name: list(documents) for name, documents in collections.items()}
    doc_index = {
        (name, str(document["_id"])): (position, document)
        for name, documents in working.items()
        for position, document in enumerate(documents)
    }
    outcomes: dict[str, str] = {}
    for entry in credential.entries:
        locator = f"{entry.collection}/{entry.doc_id}/{'/'.join(str(part) for part in entry.path)}"
        positioned = doc_index.get((entry.collection, entry.doc_id))
        if positioned is None:
            outcomes[locator] = STALE
            continue
        position, document = positioned
        current = _find_binary(document, entry.path)
        if current is None:
            outcomes[locator] = STALE
            continue
        if _is_standard_uuid_binary(current) and bytes(current).hex() == entry.target_hex:
            outcomes[locator] = SKIPPED_ALREADY_MIGRATED
            continue
        if current.subtype != entry.subtype or bytes(current).hex() != entry.original_hex:
            outcomes[locator] = STALE
            continue
        fresh_decision = fresh_by_locator.get(locator)
        if fresh_decision is None or fresh_decision.reason != APPLICABLE:
            reason = fresh_decision.reason if fresh_decision is not None else STALE
            if reason in (CONFLICT, UNKNOWN_SOURCE, AMBIGUOUS):
                outcomes[locator] = BLOCKED
            else:
                outcomes[locator] = STALE
            continue
        replacement = Binary(UUID(entry.target_uuid).bytes, UUID_SUBTYPE)
        working[entry.collection][position] = _replace_at(document, entry.path, replacement)
        outcomes[locator] = APPLIED
    return ApplyResult(outcomes=outcomes, collections=working, fresh_decisions=fresh)


def audit_preservation(
    before: Collections,
    after: Collections,
    fields: tuple[str, ...] = DEFAULT_FIELDS,
) -> list[JsonDict]:
    """Verify that non-migrated binaries survive an apply byte-for-byte.

    Returns the list of preserved binary descriptors; raises :class:`ValueError`
    if any untouched binary changed.
    """
    preserved: list[JsonDict] = []
    for collection_name in before:
        before_docs = {str(doc["_id"]): doc for doc in before[collection_name]}
        after_docs = {str(doc["_id"]): doc for doc in after[collection_name]}
        if before_docs.keys() != after_docs.keys():
            raise ValueError(f"document set changed in collection {collection_name}")
        for doc_id, old_document in before_docs.items():
            old_fields = _walk_binary_fields(old_document, ())
            new_fields = _walk_binary_fields(after_docs[doc_id], ())
            if len(old_fields) != len(new_fields):
                raise ValueError(f"binary field count changed for {collection_name}/{doc_id}")
            for (old_path, old_binary), (new_path, new_binary) in zip(
                old_fields, new_fields, strict=True
            ):
                if old_path != new_path:
                    raise ValueError(f"binary path changed for {collection_name}/{doc_id}")
                descriptor = {
                    "locator": f"{collection_name}/{doc_id}/"
                    f"{'/'.join(str(part) for part in old_path)}",
                    "subtype": old_binary.subtype,
                    "hex": bytes(old_binary).hex(),
                }
                same_value = old_binary.subtype == new_binary.subtype and bytes(
                    old_binary
                ) == bytes(new_binary)
                migrated = (
                    old_path
                    and old_path[0] in fields
                    and old_binary.subtype == OLD_UUID_SUBTYPE
                    and new_binary.subtype == UUID_SUBTYPE
                )
                if not same_value and not migrated:
                    raise ValueError(f"unexpected binary rewrite at {descriptor['locator']}")
                if same_value:
                    preserved.append(descriptor)
    return preserved
