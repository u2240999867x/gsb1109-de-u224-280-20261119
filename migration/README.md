# Legacy BSON UUID migration

This package migrates customer keys and order customer references from legacy
BSON UUID binaries (binary subtype 3) to the standard BSON UUID binary
(subtype 4, RFC-4122 byte order). It is built on the repository's own
`bson.binary.Binary` decoders for the Python, Java and C# legacy
representations.

## Safety rules

- A legacy binary is migrated only when the business supplies the source
  representation for that exact field. A binary without a declared source is
  never interpreted as a UUID, so it is never guessed or rewritten.
- Provenance is itemized per field locator
  (`collection/document-id/field-path`). A repeated record for the same
  locator is a resend with no identity change and does not revoke an existing
  applicable decision.
- Before any decision is issued, every decodable claim is collected. A UUID
  claimed by more than one distinct customer is a conflict; all fields that
  resolve to it, including previously unique ones, are blocked rather than
  migrated.
- Already-standard binaries (subtype 4) are recorded as `already_standard`
  and never rewritten. Reapplying a credential over migrated files skips them
  and leaves the BSON bytes identical.
- Every other binary (plain subtype 0, MD5 subtype 5, non-managed fields,
  wrong-length payloads) is preserved byte-for-byte. `audit_preservation`
  fails the run if any untouched binary changes.

## Decisions

Each binary field produces one decision carrying the original BSON subtype and
payload, the owning customer, the source used, and the normalized target UUID
when applicable:

- `applicable` - sourced, unique legacy binary safe to migrate.
- `already_standard` - already a standard UUID binary.
- `unknown_source` - legacy UUID binary with no declared source.
- `ambiguous` - provenance does not name a legacy representation.
- `conflict` - normalized UUID is claimed by multiple distinct customers.
- `not_uuid` - managed field whose binary is not a 16-byte UUID value.
- `out_of_scope` - binary outside the managed `customer_key`/`customer`
  fields, preserved as-is.

## Credential workflow

A preview turns the current collections and the business source file into
decisions and can emit a credential containing only the applicable entries.
Each credential entry pins the exact original BSON value, so applying it hours
later re-validates the stored value and recomputes a fresh preview:

- unchanged and still applicable - the field is migrated (`applied`);
- already migrated to the pinned target - skipped without rewriting
  (`skipped_already_migrated`);
- now conflicting or missing provenance - blocked (`blocked`), while unrelated
  entries still apply;
- stored value changed or field removed - refused as `stale`.

## Usage

```bash
# Preview and, optionally, issue an applicable-only credential.
python -m migration.cli preview \
    --collections path/to/collections_dir \
    --sources path/to/sources.json \
    --out preview.json --credential credential.json

# Apply the credential against the current snapshot.
python -m migration.cli apply \
    --collections path/to/collections_dir \
    --sources path/to/sources.json \
    --credential credential.json \
    --out-dir path/to/migrated_dir --report report.json
```

Collection directories hold one `<name>.bson` file per collection as
concatenated BSON documents. The provenance file is a JSON array of
`{"customer_id", "source", "locator"}` records, where `source` is one of
`python_legacy`, `java_legacy`, `csharp_legacy` or `standard`.

## Deterministic scenario

`python -m migration.scenarios.run_demo` rebuilds the full review workflow
under `migration_demo/`: the first batch with a sourced and an unsourced order
plus the Java/C# shared-UUID group, a no-op resend, a late genuine conflict
with an unaffected control order, and a final idempotent rerun. The package
tests live in `migration/tests/`.
