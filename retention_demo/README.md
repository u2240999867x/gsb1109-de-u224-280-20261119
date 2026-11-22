# Retention safe revisit recording demo

Offline, shared-directory demonstration that a recorded **WARC revisit** can
never be left dangling when old retention volumes are rotated away, while the
ordinary revisit reading path keeps working.

## Run

```bash
python -m retention_demo.run_demo
```

Everything runs on local files (no network, no redis). The equivalent
assertions are also packaged as the pytest suite
`tests/test_record_retention.py`.

## What is on disk

`retention_demo/archivelab.py` builds, in a temporary shared directory:

* `warcs/volume-a.warc.gz` and `warcs/volume-b.warc.gz` — two **real** response
  records carrying the same URI, WARC-Date and payload digest, but stored in
  different retention volumes;
* `indexes/source-a.cdxj`, `indexes/source-b.cdxj` — healthy per-volume CDX;
* `indexes/moved-a.cdxj` — a CDX advertising a volume that has been moved away
  (the file name no longer resolves);
* `indexes/bad-offset-a.cdxj` — a CDX whose offset does not point at the record;
* `recorded/` — new captures and their CDX evidence;
* `retention.json` / `retention.lock` — the cross-process retention ledger.

A candidate is only accepted after the volume is **active** in the ledger,
the record loads at the CDX offset/length, and its payload digest matches.
The stale CDX entries therefore can never produce a revisit.

## Two interleavings, two terminal states

The recorder and rotation are real subprocesses coordinated through signal
files (`retention_demo/barrier.py`). The barrier fires exactly after the
recorder verified the preferred old volume and opened its retention claim,
but before the new capture is committed.

1. **Recorder wins (`coop` withdraw).** Rotation asks to withdraw the volume
   while the claim is open. The ledger refuses (`VolumeRetained`), the
   recorder commits a revisit whose original stays readable, and replay loads
   the payload from that exact volume even when the other identical-payload
   volume is listed first.
2. **Rotation wins (`force` withdraw).** The volume is marked withdrawn and
   its bytes removed irreversibly first. The recorder re-validates after the
   barrier, detects the loss and either re-selects the other qualified volume
   or, when none remains, writes the full response so the capture is
   self-contained.

## Crash and restart

With `PYWB_CRASH_BEFORE_COMMIT=1` the recorder exits after the retention
evidence and new WARC are on disk but before the reference is committed.
`retention_demo/restartproc.py` reaps only the stale, uncommitted claim whose
process is gone. Committed claims are never reaped: a volume still backing a
committed capture remains non-withdrawable, while the interrupted attempt
pins nothing permanently.

## Replay provenance

Swapping the index source order changes which identical-payload volume is
chosen at record time, and the written `orig.filename` / `orig.offset` /
`orig.length` fields pin replay to that concrete record — the loader never
silently substitutes the other WARC with equal bytes. A second replay mode
(`provenance=False`) drops those fields and exercises the ordinary
`WARC-Refers-To` resolution used by existing archives.
