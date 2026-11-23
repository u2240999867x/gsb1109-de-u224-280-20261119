# GSB1109-20261119-U224 archive-time diagnostic

This package answers one question: *from what an archive can observe online
(header, per-block raw signed-int64 min/max, and one caller window), which
archive decisions are justified, and which need more evidence?* It builds
real files, opens them through xarray's CF/index machinery with byte-accurate
read accounting, and compares that with a complete offline decode.

Everything here is local and replayable:

```bash
.venv/bin/python diagnostics/GSB1109/run_all.py
```

## 1. Inputs and conventions

* Reference instant: `2100-01-01T00:00:00`.
* Time values: signed 64-bit integer nanoseconds relative to the reference,
  stored as `NC_INT64` in **CDF-5** (netCDF classic model, version 5).
  CDF-1/2 cannot hold int64, and this host has no `netCDF4`/`h5py`/NetCDF-C
  stack, so CDF-5 is the only faithful container available. The reader parses
  headers without touching data and records every data access.
* A "block" is a contiguous logical record range with archived raw min/max.
  Three blocks of 8 records for the 24-record pairs, two blocks of 4 for the
  360_day control.
* The caller window is records `[4, 12)` (pair1/pair2) or `[2, 5)`
  (360_day). "Budget" means: any read whose logical span is not contained in
  this window has exceeded the acquisition budget and its values may not be
  used as evidence.

Files are deterministic; `expected/manifest.json` pins each sha256.

## 2. Two pairs that look identical online

Both pairs are emitted in two layouts with identical conclusions:

* `dim`: `time` is the dimension coordinate of dimension `time`.
* `aux`: `time` is an auxiliary coordinate on dimension `record`.

### Pair 1 — hidden 1 ns residual

`pair1_tail_1ns` / `pair1_whole_second` share header (units, calendar), all
three block extrema, and the raw values in the caller window. They differ only
at logical record 17, inside block 2, which is entirely outside the window:

| member | record 17 | block 2 min / max |
|---|---|---|
| `tail_1ns` | `2100-01-18T00:00:00.000000001` | `1382400000000000` / `6311347200000000000` |
| `whole_second` | `2100-01-18T00:00:00` | identical |

The block maximum touches 2300-01-01; both endpoints of block 2 are whole
seconds. The 1 ns residual is strictly interior, so the raw extrema do not
move.

### Pair 2 — fill code masking a real far record

`pair2_fill_only` / `pair2_fill_plus_real_far` both set `_FillValue` to
`4985971200000000000` (= 2258-01-01 in ns) and both have that code as the last
record, so block 2's raw maximum equals the fill code in both. They differ at
record 22:

| member | record 22 (non-fill) | block 2 max (raw) |
|---|---|---|
| `fill_only` | `2100-01-23T00:00:00` | fill code |
| `fill_plus_real_far` | `2257-12-31T00:00:00` | fill code |

A reader that treats the raw maximum as a date, or that assumes the block
contains only near data plus one fill, is wrong for the second file.

### Control — 360_day

`cal360_feb30` stores day 59, which is a legal `2100-02-30` in the 360_day
calendar. Because cftime rejects `nanoseconds since …`, this control honestly
uses `microseconds since 2100-01-01 00:00:00` (the finest CF-legal unit) so the
file itself is valid and the calendar, not the container, is under test.

## 3. Online observations (no block interiors)

All figures below come from `reports/diagnostic.json`; read `byte` values are
physical file offsets.

### 3.1 Header-only inference

`decode_times=CFDatetimeCoder(infer_dtype="metadata")` with
`create_default_indexes=False` performs **zero data reads** in all ten cases:
the standard pairs infer `datetime64[ns]` from the synthetic `[0, 1]`; the
360_day control is uncertain from metadata alone and therefore reports object
without probing, which is still read-free here because its metadata path does
not force a sample. It reports the same dtype for both members of each pair.
This is the only mode that is wholly within budget by construction.

### 3.2 Default index creation — the budget violation

With defaults, `open_dataset` does two things before returning:

1. `_decode_cf_datetime_dtype` probes the first and last record to choose a
   dtype. For a window of `[4, 12)` these probes at logical 0 and 23 are
   already outside the budget.
2. If `time` is a dimension coordinate, `_maybe_create_default_indexes`
   materialises a pandas index, which reads **every block**.

Measured for `pair1_tail_1ns_dim` (header ends at byte 904):

| seq | phase | logical span | physical bytes |
|---|---|---|---|
| 0 | dtype_inference | 0 | 904–912 |
| 1 | dtype_inference | 23 | 1088–1096 |
| 2 | index_creation | 0–7 | 904–968 |
| 3 | index_creation | 8–15 | 968–1032 |
| 4 | index_creation | 16–23 | 1032–1096 |

The earliest actual read outside the window is seq 0, byte 904: the first
record. The earliest **index-creation** read outside is seq 2, byte 904. Both
are reported; neither value is used below as archive evidence.

In the `aux` layout there is no dimension coordinate, so no default index is
created. The only reads are the same first/last dtype probes (bytes 944 and
1128), still outside the `[4, 12)` budget. The 360_day control likewise reads
0 and 7, then all blocks for its dimension index.

### 3.3 Within-budget behaviour that is common to both members

With `create_default_indexes=False` (and either metadata inference or
`decode_times=False`), re-selecting the caller window reads only logical
`[4, 7]` and `[8, 11]` — exactly records 4–11. Across every mode the returned
window values, their dtype, and their NaT/fill status are identical between
pair members:

* `datetime64[ns]`, zero numeric deviation from the exact int64, no NaT.
* `raw_int64` mode returns exact `int64` with zero deviation.
* Pair 2's window contains no fill code in either member.

Re-selecting the same window twice on one lazy handle returns identical values
and dtype; the second selection re-reads the two window blocks (two new reads)
but never leaves the window. So **online, the two files in each pair are
indistinguishable**. The library also cannot warn you: pair partners differ
only in an unread block interior, and even the fill code is a legal, in-range
value.

### 3.4 Library induced vs archive math

These are separated by `library_probes` in `reports/diagnostic.json`:

* **Math (independent of xarray).** The relative offset
  `6311347200000000000 ns` fits int64 and `timedelta64[ns]`; the absolute
  instant `2100-01-01 + that offset = 10413792000000000000 ns` exceeds
  int64 max (`9223372036854775807`). Therefore no single `datetime64[ns]`
  absolute representation covers 2100→2300, while `datetime64[s/ms/us]` and
  cftime object arrays can.
* **Header unit.** With `nanoseconds since …`, cftime cannot decode at all
  (it accepts only microseconds and coarser). With an honest
  `microseconds since …` header, cftime returns 2300-01-01 correctly. So the
  2300 failure is *not* intrinsic to the instant; it is intrinsic to a
  nanosecond-unit absolute array plus a library that requires CF-legal units
  for its out-of-range fallback.
* **Library hazard (plain xarray, no container).** An `IndexVariable` that
  declares `datetime64[ns]` while its lazy data yields cftime objects (the
  normal fallback for out-of-range dates) is silently cast during index
  materialisation: the 2300 value becomes `1715-06-13T00:25:26.290448384`
  with **no warning**. This is reproduced with only `xarray` objects. It means
  a default-index open of a nanosecond file can corrupt an unread far block
  rather than merely fail.

That last point matters for the archive: the observed failure/success of an
open is not a reliable statement about the stored dates.

## 4. Offline full decode (ground truth)

Computed by reading every record, then classifying values by exact int64
divisibility and absolute-range fit:

| case | whole-file lossless units | segmentation | fill | correct action |
|---|---|---|---|---|
| `pair1_whole_second` | `s, ms, us` (not `ns`: 2300 overflows absolute ns) | one segment | none | one unit covers the file; still preserve the 2300 extent |
| `pair1_tail_1ns` | none | `[0,22)` in `ns`, `[22,24)` in `s` | none | no single unit; keep lossless segments or keep int64 ns |
| `pair2_fill_only` | `s, ms, us, ns` | one segment | 1 (last record) | fill code is missing, not a date |
| `pair2_fill_plus_real_far` | `s, ms, us, ns` | one segment | 1 (last record) | a real far record exists below the fill maximum |
| `cal360_feb30` | CF-legal `s, ms, us`; 360_day date | one segment | none | keep the original 360_day calendar |

"None" in the segmentation column for `pair1_tail_1ns` is deliberate: the
interior 1 ns residual makes the block-2 gcd 1, so no single unit fits even
though the *observable* values would. `pair1_whole_second` is the opposite:
every value is a whole second, but absolute `datetime64[ns]` still overflows
at 2300, so `ns` is not a valid whole-file absolute unit.

These are exactly the categories the archive asked for: whole-file lossless in
one unit, lossless segmentation without one unit, and original-calendar
preservation. The 360_day case is the non-standard-calendar control; its
`feb30` decodes losslessly only as cftime/360_day, never as a proleptic
Gregorian date.

## 5. Two concrete archive decisions from identical online evidence

`reports/decisions.txt` / `.json` records this; `reports/invariance.json`
proves it is not position-specific.

### Pair 1

* Both open identically online, return identical windows, and have identical
  header + block extrema.
* `tail_1ns` must be **kept as int64 ns or split into lossless segments**; a
  single `datetime64[s]` conversion would silently drop 1 ns.
* `whole_second` may be **stored losslessly in `s` (or `ms`/`us`)**; `ns` is
  unavailable as an absolute unit because of 2300, but that is a range fact,
  not a precision fact.

The observable difference is one nanosecond in an unread block. Without
block-interior evidence, an archivist choosing `s` for both would lose the
residual in the first file and cannot know it.

### Pair 2

* Both open identically online (same dtype, same window, same extrema, same
  single fill at the far extreme).
* `fill_only` must be recorded as **fill code = missing**, with data ending
  near 2100.
* `fill_plus_real_far` must record a **real far record at 2257-12-31**, plus
  the fill policy; treating the block maximum as a date would wrongly date the
  file at the fill instant, and treating the whole block as "near + one fill"
  would drop a real record.

The observable difference is a non-fill value strictly below the fill code, so
the raw maximum hides it. No header-only or window-only rule can see it.

## 6. Minimal additional archive evidence

The proposal is a **per-block supplement**, added to the existing block table
but obtained without reading the caller window and without depending on
physical block placement:

1. `fill_count` for each block (exact count of the declared fill code).
2. exact non-fill `min` and `max` (signed int64) for each block.
3. `gcd` of the non-fill values in each block (signed int64).
4. the block's logical `[start, stop)` range and its declared fill code(s).

Why this is enough, and minimal:

* For **pair 1**, both files have identical raw min/max, but block 2's non-fill
  gcd is `86400000000000` in `whole_second` and `1` in `tail_1ns`. The gcd
  alone forces the different unit decision. Nothing smaller suffices: min/max
  are equal, and the count of records is equal. (A subsecond count would also
  work, but gcd is the exact, unit-agnostic form.)
* For **pair 2**, block 2 has one fill in both, but the non-fill maximum is
  `1900800000000000` in `fill_only` and `4985884800000000000` in
  `fill_plus_real_far`. The exact non-fill max (with fill count) distinguishes
  "nothing but near data plus fill" from "a real far record hidden under the
  fill maximum". The raw max cannot, and the fill count alone cannot.

`reports/decisions.json` re-derives every action from this supplement alone
and shows it matches the full offline action. The supplement is invariant
under block permutation and window shift (§7).

What remains unknown without more evidence: if the archive cannot add
per-block non-fill extrema/gcd/fill counts, then for these pairs the correct
action is genuinely undetermined from the original summary. The report keeps
those as unknown rather than guessing.

## 7. Position independence

`reports/invariance.json` rebuilds every pair with **reversed physical block
order** (offsets move from `[0, 64, 128]` to `[128, 64, 0]` while logical
values, block boundaries, and extrema are unchanged) and a **different window**
`[8, 16)`. Results:

* logical values are unchanged and both members' online summaries remain
  equal to each other;
* every whole-file/segmentation verdict and every supplement verdict is
  unchanged;
* budget violations are still detected and attributed to the earliest actual
  physical byte;
* each pair still forces the same two distinct archive actions.

The checks are expressed as properties (block order, physical offsets, window
position), not fixed record numbers. The only fixed numbers are the case
definitions themselves, which are inputs, not conclusions.

## 8. What the current summary can and cannot guarantee

**Confirmed now** (header + block extrema + budgeted window, plus the local
offline check as validation):

* The two members of each pair are indistinguishable online, including dtype,
  window values, fill status, and extrema.
* Default index creation reads outside the caller window (earliest actual
  read at byte 904/944, the record-0 probe; earliest index read at the same
  byte in the `dim` layout). Those reads are budget violations, not evidence.
* Pair 1 hides a 1 ns residual in an unread block; pair 2 hides a real far
  record beneath a fill-coded maximum.
* Pair 1 and pair 2 each require different archive actions per member.
* `pair1_whole_second` is whole-file lossless in `s/ms/us`;
  `pair1_tail_1ns` is segment-lossless but has no single unit;
  `pair2_*` are whole-file lossless; `cal360_feb30` must keep 360_day.
* The 2300 absolute range limit is mathematical; the nanosecond-unit cftime
  rejection and the silent object→ns index cast are library behaviours,
  reproducible with plain xarray.

**Unknown without adding the per-block supplement:**

* Which member of each pair a given file is, and therefore which action is
  correct. The original summary is consistent with both.
* Whether an unread block contains a 1 ns residual or a real far record; raw
  extrema cannot express either.

If a recommendation cannot be guaranteed from the original summary, this
report leaves it as an explicit unknown (§6) rather than converting a
budget-violating read into evidence.

## 9. Replay

```bash
# everything, from a clean checkout
.venv/bin/python diagnostics/GSB1109/run_all.py

# individual stages
.venv/bin/python -m diagnostics.GSB1109.build_cases   # writes data/, expected/manifest.json
.venv/bin/python -m diagnostics.GSB1109.analysis      # writes reports/diagnostic.json, summary.txt
.venv/bin/python -m diagnostics.GSB1109.invariance    # writes reports/invariance.json (permuted blocks + new window)
.venv/bin/python -m diagnostics.GSB1109.decisions     # writes reports/decisions.json, decisions.txt
.venv/bin/python -m pytest diagnostics/GSB1109/tests -q
```

Artifacts:

* `data/*.cdf5` — deterministic inputs; `expected/manifest.json` pins sha256.
* `reports/diagnostic.json` — all reads (phase, logical span, physical bytes),
  dtypes, window values, deviations, fill/NaT counts, offline verdicts,
  library probes.
* `reports/invariance.json` — permuted-block, shifted-window rerun.
* `reports/decisions.json` / `decisions.txt` — the two-action evidence and the
  supplement's sufficiency.
* `tests/test_diagnostic.py` — 20 regression tests covering exact int64
  round-trip, block extrema, hidden residual, identical summaries, budget
  flags, 360_day, unit verdicts, differing actions, invariance, and the
  library hazard.

## 10. Scope and caveats

* This host lacks netCDF4/HDF5, so the files are CDF-5 (classic model). The
  conclusions about xarray's decoders and index creation are exercised through
  a real `BackendEntrypoint` and real xarray store/CF code, not a mock.
* The silent object→ns cast was reproduced with plain xarray objects; it is
  not an artifact of the container. It is reported separately from archive
  math.
* Absolute `datetime64[ns]` cannot represent 2300 regardless of library; do
  not read a successful `datetime64[ns]` open that read the far block as proof
  that the date is correct — see §3.4.
