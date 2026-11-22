# Same-second POST replay: minimal offline case and review pack

All commands below run from the repository root and need no network or
listening socket. The WarcServer is mounted on `webtest` in-process.

```
.venv/bin/python evidence/incident/build.py        # rebuild collection/
.venv/bin/python evidence/incident/run_checks.py  # matrix -> results.json
.venv/bin/python evidence/incident/diagnose.py    # per-query trace -> diagnosis.json
.venv/bin/python evidence/incident/forensic.py    # WARC/index cross-check
.venv/bin/python evidence/incident/minimal_search.py
```

`build.py` is deterministic: WARC-Date is fixed at `2026-01-01T12:00:00Z`,
record ids are explicit, and rebuilding produces identical WARC and index
bytes.

## The two submissions

Both failures are in the same collection and use the same timestamp; the
read-only legacy archive is what masks the second one.

| | request A | request B |
|---|---|---|
| raw body | `a%26a` (5 bytes) | `a%26a=` (6 bytes) |
| hex | `6125323661` | `61253236613d` |
| decoded form | `a&a` (two `a` fields) | `a&a=` (two `a` fields) |
| stored HTTP Content-Length | `5` | `5` (payload is 6 bytes) |
| true index key | `example,shop)/checkout.html?__wb_method=post&a&a` | `example,shop)/checkout.html?__wb_method=post&a&a=` |
| indexed key | same | same as A |

Both requests are at the same URL and second. The percent-encoded `%26`
delimiter and the duplicate `a` field are what make A's key look like B's
truncated key.

## The wrong 200s

Both entry points were exercised: the direct form endpoint
`/incident/resource` and the wrapped-HTTP endpoint
`/incident/resource/postreq`.

- Query A: exact key has two rows, `orders-b.warc` (digest `DY5Z...`) and
  `orders-a.warc` (digest `LRKC...`). Same second, so the closest sort ties;
  the earlier merged row wins. A receives **`ORDER-B-002`**, not A.
- Query B: exact key has no row (B was filed under A's key). Fuzzy fallback
  matches the read-only legacy row and two POST rows; the legacy row is
  first. B receives **`LEGACY-OLD-BODY`** at the same `Memento-Datetime`.
- A third replay of query B uses the **archived** request header
  (Content-Length 5). The wrapper then reads only `a%26a`, so B lands on A's
  key and picks B's own row first: the body is `ORDER-B-002` again, but this
  time for the wrong reason. This is the false pass that masks the failure:
  the same query returns two different bodies (legacy via the direct form,
  response B via the archived header) and both are HTTP 200 with the same
  second. `results.json` records all three entry variants.

## Where identity is still recoverable

`forensic.json` shows, per request record:

- full raw bytes, hex, SHA-256: recoverable from the WARC payload at the
  absolute body offset in `warc_offsets.json` (`orders-a.warc` body@962
  length 5; `orders-b.warc` body@962 length 6);
- `WARC-Concurrent-To` pairing to `resp-a` / `resp-b`: intact;
- `WARC-Payload-Digest`: distinct;
- HTTP `Content-Length`: identical (5) for both; for B it hides the sixth
  byte, so **the header layer can no longer tell the two apart**.

The index key and `requestBody` (both `a&a`) are downstream of the header
and collapse. The candidate selection never consults `requestBody` or method.

## Reproduction matrix (see `results.json`)

| scenario | A | B |
|---|---|---|
| base (direct form, or `postreq` with the real 6-byte length) | `ORDER-B-002` | `LEGACY-OLD-BODY` |
| base, query B via `postreq` with the archived 5-byte header | `ORDER-B-002` | `ORDER-B-002` (B's row on A's key: false pass) |
| re-index only `orders-b.warc` | `ORDER-B-002` | `LEGACY-OLD-BODY` |
| fix B's request header, keep old index | `ORDER-B-002` | `LEGACY-OLD-BODY` (byte-identical to base) |
| re-index only `orders-a.warc` | `ORDER-B-002` | `LEGACY-OLD-BODY` |
| rebuild every index from same WARCs | `ORDER-B-002` | `LEGACY-OLD-BODY` |
| fix B's request header **and** re-index B | `ORDER-A-000` | `ORDER-B-002` (archived-header path: `ORDER-A-000`) |
| positive: only A (+ legacy) | `ORDER-A-000` | legacy (B absent) |
| positive: only B, correct header | `ORDER-B-002` | `ORDER-B-002` |

Every row is HTTP 200 with the same `Memento-Datetime`. The table shows the
direct form and `postreq` with the real body length. Only rewriting the
mismatched request *and* regenerating that WARC's index changes the outcome.
Replaying B through `postreq` with the archived 5-byte header always folds B
back onto A's key; after the repair that path selects A's row, which is the
same key collapse seen from the other side.

## Why rebuilding all indexes does not always fix it

The collision is stored in the WARC request header. A full rebuild reuses
the same `Content-Length: 5` and recomputes the same truncated key, so the
false alias is reproduced faithfully. Index rebuild fixes stale/index-level
damage only; it cannot recover a byte that the header declares out of scope.
The request bytes are still in the WARC payload, so a WARC-aware repair can
regenerate the correct key, but the stock `cdx-indexer -p` path cannot.

## Files

- `collection/warcs/legacy-ro.warc` (read-only) -- response only, plain key
- `collection/warcs/orders-a.warc` -- response A + request A
- `collection/warcs/orders-b.warc` -- response B + request B (short header)
- `collection/indexes/index.cdxj` -- merged index as served
- `results.json` -- full replay matrix, both entry points, pass/fail checks
- `diagnosis.json` -- per-query key, candidates, selected row, boundary notes
- `forensic.json` -- WARC/index cross-check, false-alias detection
- `warc_offsets.json` -- record and request-body byte offsets for every WARC
- `minimal_search.out` -- exhaustive search: 5 bytes is the shortest body
  that keeps both an encoded delimiter and the duplicate `a` field
- `SOURCES.md` -- exact code locations of each failure stage
- `verify.sh` -- rebuilds the collection and regenerates every evidence file
