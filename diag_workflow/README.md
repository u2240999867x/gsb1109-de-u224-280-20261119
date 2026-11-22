# Same-path missing-input diagnosis

This is an offline diagnostic fixture, not a workflow fix. It reproduces two
different dry-run failures that surface the *same* absolute path, then records
observations that show which stage must be repaired.

## Workflow

Two checkpoint layers:

1. `pick_regions` — reads `batch/<batch>.tsv` and writes the regions to process.
2. `expand_region` — reads `roster/<region>.tsv` and writes the real targets.

Downstream, `consume` asks for those real targets. `produce_data` is a similar
producer (`data/{region}/records.tsv`) whose `wildcard_constraints` allow only a
different region.

Both sites report:

```
Missing input files ... data/<region>/records.tsv
```

but for different reasons.

## Reproduce

From the repository root:

```bash
python3 diag_workflow/drive.py      # writes evidence/rounds/<case>/
python3 evidence/attribute.py       # recomputes attribution from trace events
python3 evidence/check.py           # asserts every requirement
```

The driver starts each case from an explicit snapshot (`state/`, `roster/`,
`previous/` are rebuilt), never from whatever happens to be on disk. Each case
changes exactly one config switch. The previous-roster audit opens the old file
inside the same run's input evaluation, but its content never feeds the returned
material; the trace records whether that read happened and its digest.

## Cases

| case | snapshot | checkpoint material | producer regions | result |
| --- | --- | --- | --- | --- |
| `1_siteA_layer2_pending` | layer 1 done, layer 2 not run | `data` | `south` | fails in `expand_region`, same path |
| `2_after_checkpoint_only` | layer 2 done, target absent | `roster` | `south` | fails in `consume`, same path |
| `3_constraint_only_pending` | layer 2 not run | `data` | `north\|south` | dry-run schedules the checkpoint; it is not treated as delivered |
| `4_siteB_both_done_constraint_rejects` | layer 2 done, target absent | `roster` | `south` | fails in `consume`; similar producer rejected by constraint |
| `5_both_fixed` | layer 2 done, target absent | `roster` | `north\|south` | completes |
| `6_previous_opens_but_not_basis` | layer 2 done, target absent | `roster` | `north\|south` | completes; old roster was actually read but is not the basis |
| `7_stale_targets_dry_run_skips` | stale target from old roster, result present | `roster` | `north\|south` | dry-run keeps the stale target |
| `8_stale_targets_recomputed` | same snapshot, real run | `roster` | `north\|south` | same round recomputes from the current roster |

Case 7 and 8 have the same log shape in the stale snapshot. The difference is
whether the round actually recomputes: the current roster's newer mtime forces
`expand_region` to run and overwrite the target, while the dry-run does not.

## What the trace answers

Enable with `SNAKEMAKE_DIAG_TRACE=<file.jsonl>` and optionally narrow file-open
auditing with `SNAKEMAKE_DIAG_WATCH=<workflow dir>`. With no environment
variable, tracing is a no-op.

- `checkpoint_get` — per checkpoint and wildcard set: `complete` or
  `incomplete`, plus the missing output and the created-output set. The final
  verdict is what governs the round.
- `producer_selection` — the prefix/suffix candidates from `OutputIndex` and
  which of them survive regex constraint validation. This distinguishes "the
  rule looked similar" from "the rule was a valid producer".
- `input_edge` — each input edge in resolution order; `first_missing` marks the
  first one that failed.
- `missing_input` / `exception` — the rule, wildcards, absolute files, and
  exception class actually raised.
- `read_source` / `file_open` / `artifact` — whether the previous roster was
  really opened, its digest, and what the recomputed target records as its
  `derived_from`.

`evidence/attribute.py` recomputes the first failed edge per case from these
events in sequence order, so the conclusion does not come from stdout wording
or from the current directory listing.

## Attribution

- Site A: layer 2 has not completed. Its material (`data/<region>/records.tsv`)
  is itself the missing input; fixing only the checkpoint material moves the
  failure to the downstream `consume` rule but keeps the same absolute path.
- Site B: both layers completed. The real roster derived the same path, but the
  only similar producer is a prefix/suffix candidate that is rejected by its
  wildcard constraint (an allow-list for a different region).
- Widening only the constraint does not make the pending checkpoint delivered;
  the checkpoint job is still scheduled in the DAG.
- A previous roster can be opened and still not be the basis for this round:
  cases 7 and 8 show the same stale snapshot, one skipped and one recomputed
  from the current roster.

## Scope

This fixture only diagnoses. It does not change Snakemake semantics; the source
changes add opt-in trace events and leave untraced runs on the original path.
