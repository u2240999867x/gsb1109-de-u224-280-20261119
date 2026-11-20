# exportBytes re-rooting: offline regression notes

Scope: `MSGFile.exportBytes` / `OleWriter.fromMsg` when the source is an
embedded message that downstream archiving wants to store as a standalone
file. The regression tests live in
`extract_msg_tests/export_bytes_tests.py` and build every fixture fully
offline with `OleWriter` (no example files, no network).

## Fixture: two-level embedded original

`root (parent) -> middle.msg -> inner.msg`, built so that mix-ups are
detectable:

- Parent and innermost message define the *same* properties (class type,
  body `1000`, subject `0037`, string-named property `ArchiveTag` at stream
  `0x8000`) with *different* values.
- Both carry a local attachment named `shared.txt` with different contents.
- Parent additionally defines a property (`0C1A` sender name) that the
  innermost message does not.
- The named property catalog (`__nameid_version1.0`) exists only at the
  file root, as the standard requires; embedded messages resolve named
  properties through it.

## Expected current behavior (specimen input)

Exporting the innermost message and re-opening the bytes as a new root:

- Body, subject, named property value, and the local `shared.txt`
  attachment all reconcile 1:1 with the innermost message as read from the
  original.
- The embedded properties stream (24-byte header) gains 8 reserved bytes to
  become a top level stream (32-byte header). This container layout change
  is required for re-rooting and is *not* damage; property data must still
  parse (`340D0003` readable).
- The root's named property catalog is copied into the copy, so
  `ArchiveTag` still resolves.
- Nothing from the parent or middle message leaks in: parent-only
  properties stay absent and parent/middle content bytes do not appear in
  the container.

## Boundary input 1: embedded properties stream uses the top level layout

The embedded `__properties_version1.0` has a 32-byte header
(`len % 16 == 0`), which is only legal at the top level.

- Default (strict) mode: earliest visible rejection is
  `StandardViolationError` ("...top level properties stream.") raised from
  `OleWriter.fromMsg` while copying the properties stream. No copy is
  produced.
- `allowBadEmbed=True`: the stream is kept as-is (it already has the top
  level layout) and, because the store support mask `340D0003` is missing
  and strings are unicode, a `340D0003` entry is appended. The copy opens;
  body and named property still reconcile with the original embedded
  message, and the appended mask is observable evidence of the repair.

## Boundary input 2: embedded message carries its own named property catalog

The embedded message contains its own `__nameid_version1.0` directory whose
definition disagrees with the root catalog (it maps a different name,
`ForgedTag`, to the same stream ID `0x8000`).

- Default (strict) mode: earliest visible rejection is
  `StandardViolationError` ("...contains it's own named streams.") raised
  when the writer tries to add the root catalog over the already-copied
  foreign one. No copy is produced.
- `allowBadEmbed=True`: `fromMsg` returns early and never copies the root
  catalog. The copy keeps the *foreign* catalog. Consequences, asserted
  independently in the test:
  - the body survives (`B2_BODY` reconciles);
  - the named property is *lost* under its proper identity
    (`ArchiveTag` resolves to `None`);
  - the value is still recoverable, but only under the foreign identity
    (`ForgedTag`) or as the raw `__substg1.0_8000001F` stream.

  So a lenient export that "only" preserves the body while losing or
  rebinding named properties is called out by the test as exactly that
  difference, not hidden behind a generic "file opens" check.

## Validation status

- The fixture builders were validated offline with a stdlib-only check of
  the byte layouts (header sizes, the 24 -> 32 byte transform, property
  entry packing, named property entry/GUID/string stream layout) against
  the parsing rules in `PropertiesStore`, `Named`, and `OleWriter.fromMsg`.
- The test suite itself has **not** been executed in this environment: the
  machine is offline and the runtime dependencies (`olefile`,
  `red-black-tree-mod`, `compressed-rtf`, ...) are not installed. Run with:

  ```
  pip install -r requirements.txt
  python tests.py
  ```
