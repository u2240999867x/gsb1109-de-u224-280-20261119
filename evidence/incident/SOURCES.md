# Source map for the replay incident

Line numbers are for this snapshot (`pywb 2.11.1`, commit `a6c821a`).

## 1. Request bytes to index key

`pywb/warcserver/inputrequest.py:243-246` -- form bodies are percent-decoded
with `unquote_plus` before the query string is built. `%26` becomes a literal
`&`, so encoded and unencoded delimiters collapse into the same query syntax.
`:291-292` then truncates that decoded query to 4096 chars without any
re-encoding, so the loss happens in both replay and indexing.

`pywb/warcserver/inputrequest.py:349-362` -- `append_query` adds the POST body
query to the URL and prefixes `__wb_method=POST`.

`pywb/utils/canonicalize.py:25` and
`surt/GoogleURLCanonicalizer.py:159-183` -- `canonicalize` goes through
`minimalEscape`, which calls `unescapeRepeatedly`. `%26` decodes to `&`, then
`&` is re-escaped as `&` and the query is alpha-sorted. `%3D` similarly
decodes to `=`. This is why distinct raw bodies share one SURT key.

## 2. Indexer trusts the request header's Content-Length

`pywb/indexer/archiveindexer.py:183-192` -- the response entry is paired to
the request, and `MethodQueryCanonicalizer` is built with
`record.http_headers.get_header('Content-Length')`. Bytes past that length in
the WARC request payload are never read for the key.

`pywb/indexer/archiveindexer.py:58-78` -- `merge_request_data` writes the
canonicalized key, `method`, and `requestBody` onto the response row. There is
no check against the stored payload length and no digest check tying
`requestBody` to the actual bytes. `requestBody` comes from the same
`MethodQueryCanonicalizer` instance, so it is also truncated at the header
length and cannot be used to tell the two requests apart.

`pywb/indexer/archiveindexer.py:211-239` -- pairing requires consecutive
records, equal `WARC-Target-URI`, and reciprocal/paired `WARC-Concurrent-To`.
This pairing is intact for both requests; the damage is later, at key
computation, not at pairing.

## 3. Candidate merge and closest tie-break

`pywb/warcserver/index/aggregator.py:100-117` -- exact queries with `closest`
are merged by `(timestamp, cdx)`. The second tuple element is the CDX object,
ordered by its JSON serialization, so same-second rows from different files
get a deterministic but semantically arbitrary order.

`pywb/warcserver/index/cdxops.py:327-380` -- `cdx_sort_closest` uses distance
only, and for equal distance keeps the earlier `pos`. Thus the first row in
the merged iterator wins; `requestBody` is not consulted.

## 4. Missing exact key falls back to fuzzy, then to the legacy row

`pywb/warcserver/handlers.py:56-66` -- `_load_index_source` augments the URL
with the POST query before querying the index.

`pywb/warcserver/index/fuzzymatcher.py:167-198` -- if the exact key produced
no rows, `get_fuzzy_iter` runs the default prefix rule with the plain URL.
`match_general_fuzzy_query` (`:200-240`) accepts the plain-URL legacy row and
any POST rows that share the base URL. The legacy row is returned first by
the merged order.

`pywb/warcserver/handlers.py:158-168` -- the resource loader returns the first
candidate that loads successfully, so the first (legacy) row is replayed
without checking method or body.

## Where each layer stops being able to distinguish the two requests

| Layer | Request A | Request B | Distinguishable? |
|---|---|---|---|
| WARC request payload | `a%26a` (5 bytes) | `a%26a=` (6 bytes) | yes, raw bytes and SHA-256 differ |
| WARC pairing | `WARC-Concurrent-To` -> resp-a | `WARC-Concurrent-To` -> resp-b | yes, record ids differ |
| HTTP `Content-Length` inside the request record | 5 | 5 (stored body is 6) | no, identical headers; B's 6th byte is invisible to a header reader |
| Canonical index key | `...?__wb_method=post&a&a` | same key | no, one key for both rows |
| Candidate selection | B's row first (digest/JSON order) | legacy row first | no, chosen by row order, not identity |

So the original fields are recoverable from the WARC payload at
`(filename, offset, length)`, while from the HTTP header layer down the two
requests are not distinguishable.
