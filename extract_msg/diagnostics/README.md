# Body-path diagnostics

This directory contains a read-only diagnostic for archived `.msg` files whose
HTML export is empty or contains replacement characters. It uses the existing
`extract_msg.openMsg` entry point and the normal `body`, `htmlBody`, and
`htmlBodyPrepared` properties. It does not rewrite the archive, change the
conversion strategy, or alter any extraction option.

## Reproduce locally

```bash
# Write the controlled samples and manifest.
python -m extract_msg.diagnostics --generate-fixtures tmp/body-diagnostics

# Diagnose them and write both machine- and human-readable reports.
python -m extract_msg.diagnostics \
  --diagnose tmp/body-diagnostics \
  --json tmp/body-diagnostics.json \
  --markdown tmp/body-diagnostics.md
```

`--diagnose` accepts a directory of `.msg` files. Names such as
`regular/<scenario>.msg` and `signed/<scenario>.msg` are paired automatically,
so the report includes a direct regular-vs-signed fork table. It can also accept
a single `.msg` path.

## Controlled sample matrix

| Sample | Native HTML | RTF | Purpose |
| --- | --- | --- | --- |
| `missing-native__rtf-no-html` | stream absent | present, no HTML | Prove the RTF branch runs, yields nothing, then plain text is considered. |
| `empty-native__rtf-no-html` | stream present, zero bytes | present, no HTML | Contrast an existing empty stream with an absent stream. |
| `missing-native__no-rtf` | stream absent | absent | Show the plain-text fallback with no RTF stage. |
| `missing-native__rtf-html` | stream absent | present, contains HTML | Show a successful RTF-to-HTML result. |
| `native-html-mojibake` | bytes, declares windows-1252 but is UTF-8 | present, no HTML | Show a wrong encoding choice where the bytes are valid UTF-8. |
| `native-html-replacement` | bytes, declares UTF-8 but contains invalid bytes | present, no HTML | Show replacement characters produced from bytes that were actually obtained. |
| `native-html-ok` | valid bytes | present, no HTML | Control sample. |

Every fixture has a regular and a signed variant. The signed MIME part carries
the plain body; the native HTML stream and RTF stream are configured
independently. One extra signed sample, `missing-native__rtf-no-html__mime-html`,
puts HTML in the signed MIME entity itself.

The generated `manifest.json` records each fixture's path, size, SHA-256, and
intended state. Regenerating is deterministic and does not modify the original
archive.

## Observed results

These are the results from the current parser (`extract_msg 0.58.2`) on the
controlled matrix. They are the expected values for the tests, not assumptions
about arbitrary archived messages.

| Scenario | Regular path | Signed path | First divergence |
| --- | --- | --- | --- |
| missing native, RTF without HTML | RTF attempted, no HTML; plain fallback used | RTF attempted, no HTML; plain fallback **not** used; empty HTML | `plain_fallback_attempted` |
| empty native | Empty stream skips RTF; plain fallback used | Empty stream skips RTF **and** plain fallback; empty HTML | `plain_fallback_attempted` |
| missing native, no RTF | Plain fallback used (`</body></html>`) | Plain fallback used (`</body></head>`) | `html_length` |
| missing native, RTF HTML | RTF HTML used | RTF HTML used | none |
| native HTML present | Native bytes used | Native bytes used | none |

The first two rows are the important archive cases. In the signed path an
existing zero-byte native stream is accepted as the HTML value and prevents all
fallback; an absent stream still runs RTF but does not continue to the signed
MIME plain body. In the regular path both missing and empty streams end at the
same generated plain-text HTML, but the trace distinguishes them. When the final
HTML values are equal, `first_anomaly` and `rtf_html_attempted` still tell the
two causes apart.

The `missing native, no RTF` row is also a real fork: both paths use plain text,
but their generated tags differ, and the report records that as the first
divergence at `html_length` rather than hiding it behind a byte count.

## Triage codes

| Code | Meaning | Archive action |
| --- | --- | --- |
| `NATIVE_HTML_MISSING` | `__substg1.0_10130102` does not exist. | Treat as “no native HTML”; check whether RTF or signed MIME supplied the body. |
| `NATIVE_HTML_EMPTY` | The stream exists and is zero bytes. Do not merge with “missing”. | Likely an upstream export defect. Keep the stream state in the work record. |
| `ENCODING_GUESS_MISMATCH` | The bytes are valid UTF-8, but the parser chose another encoding and the displayed text differs. | Re-decode from the recorded original bytes; do not report “no body”. |
| `ENCODING_GUESS_ON_INVALID_UTF8` | UTF-8 decoding failed, and the parser guessed a fallback encoding. | Preserve original bytes; compare with the strict-decode failure offset. |
| `REPLACEMENT_CHARACTERS` | U+FFFD is visible after parsing the bytes obtained. | Body is present but damaged; keep bytes and record the first replacement index. |
| `PARSER_REPLACEMENT_WARNING` | BeautifulSoup explicitly warned that it replaced undecodable characters. | Confirms the U+FFFD came from parsing, not from a missing body. |
| `HTML_STRICT_DECODE_FAILED` | Strict UTF-8 decoding of the obtained bytes raised. | Record byte offset and raw bytes; re-encode or transcode as a separate repair. |
| `RTF_MISSING` | No RTF stream exists. | Do not send to an RTF repair queue. |
| `RTF_HTML_UNAVAILABLE` | RTF exists but yields no encapsulated HTML. | RTF repair will not recover HTML. |
| `RTF_HTML_USED` | HTML came from the RTF body. | Mark the source as RTF-recovered. |
| `SIGNED_MIME_HTML_USED` | HTML came from the signed MIME entity. | Mark the source as S/MIME-recovered; do not expect RTF to be consulted next time. |
| `PLAIN_TEXT_FALLBACK` | HTML was generated from plain text. | Body is recovered, not absent. Record that formatting is lost. |
| `PLAIN_TEXT_NOT_USED` | Plain text is available but the HTML path stopped empty. | Signed-path suspicion; inspect the signed MIME entity and empty native stream. |
| `NO_HTML_OUTPUT` | The HTML value is empty and the prepared export is zero bytes. | Route by the preceding cause, not as a generic empty message. |
| `PREPARE_CHANGED_TEXT` | The prepared HTML text differs from the text parsed from obtained bytes. | Preserve the preparse bytes; the prepare step changes visible text. |
| `EXPORT_REENCODED_TEXT` | The saved HTML body differs from the prepared body text. | Fix the final write/encoding step; the archive bytes are not necessarily at fault. |
| `HTML_SAVE_FAILED` | Preparing or saving HTML raised. | Capture the exception in the report; do not classify as empty content. |
| `DIAGNOSTIC_FAILED` | The diagnostic itself could not complete. | Do not use the sample's result; inspect the reported exception. |

## Reading a report

Each per-fixture record contains:

- `native_html`: `missing`, `empty`, or `bytes`, with size and SHA-256.
- `rtf`: stream presence/size, deencapsulation content type, HTML/text size.
- `path`: whether RTF was attempted and its result, whether plain fallback was
  attempted and used, the winning source, and whether an empty native stream
  short-circuited the rest.
- `body` / `html_body` / `html_body_prepared` / `save_html_prepared`: values and
  errors at each public stage.
- `decoding`: raw size/SHA/hex sample, chosen vs declared vs sniffed encoding,
  strict UTF-8 outcome, first decode anomaly, replacement character index, and
  parser warnings.
- `rtf_deencapsulation_calls`: the recorded calls to the existing
  `deencapsulateBody` method, including a `no-html` result versus “not
  attempted”.
- `log_markers`: the body-path log messages, which name the source that was
  tried.
- `triage_codes` and `first_anomaly`: the ordered cause labels and the earliest
  observable problem.

The JSON report keeps the full raw-byte evidence (hashes and hex samples) while
the Markdown report is intended for review and routing. A checked-in example
report is available in `OBSERVED.md`.

## Important distinction

`NATIVE_HTML_MISSING` and `NATIVE_HTML_EMPTY` are deliberately separate codes,
even when both produce the same generated HTML. `REPLACEMENT_CHARACTERS` and
`ENCODING_GUESS_MISMATCH` are also separate: one means the parser emitted U+FFFD
for bytes actually obtained, the other means the parser chose the wrong
encoding while the bytes themselves were valid. Neither is `NATIVE_HTML_MISSING`
and neither should be routed as “source contains no body”.
