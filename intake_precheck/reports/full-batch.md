# Intake precheck report

- Source: `/Users/guo/Documents/GSB/1109/batches/de-u224-280-20261119/control/questions/GSB1109-20261119-U224-DE-PY-100/workspace/intake_precheck/data/batch-arrival-2026-11-21.csv`
- Saved contract schema: `contract/inbound-contract.schema.json` (read-only)
- Scan scope: **full**
- Confirmed range: **data rows 1..8 (of the scanned input)**
- The complete input was scanned in a single pass.
- Verdict under the saved contract: **4 cell rejection(s) across 3 row(s)**

## Entry: legacy date-format sample

First cells matching the contract's old date format (`%Y-%m-%d`), shown before any verdict:

| Row | Field | Raw cell |
| --- | --- | --- |
| 1 | arrival_date | `2026-11-14` |
| 2 | arrival_date | `2026-11-15` |
| 3 | arrival_date | `2026-11-16` |

## Column profiles against the saved contract

| Field | Contract type | Non-blank | Blank | Entirely blank | Rejections |
| --- | --- | --- | --- | --- | --- |
| arrival_date | date | 7 | 1 | no | 2 |
| note | string | 3 | 5 | no | 0 |
| quantity | number | 8 | 0 | no | 2 |
| remark | string | 0 | 8 | yes | 0 |
| tail_note | string | 4 | 4 | no | 0 |

- `remark` is entirely blank in the scanned range. Blank cells are missing values, NOT a type change: no rejection is raised and no new type is asserted for this column.

## Rejections under the saved contract

Earliest offending row and the exact type / constraint / missing reason are listed; the original cells are preserved verbatim.

| Earliest row | Field | Reason | Frictionless note | Raw cell |
| --- | --- | --- | --- | --- |
| 5 | arrival_date | type cast failure | type is "date/default" | `2026-11-18T08:30:00+09:00` |
| 5 | quantity | constraint violation | constraint "maximum" is "1000" | `1250.75` |

All rejected rows (full raw evidence):

| Row | Field | Reason | Note | Raw cell |
| --- | --- | --- | --- | --- |
| 5 | arrival_date | type cast failure | type is "date/default" | `2026-11-18T08:30:00+09:00` |
| 5 | quantity | constraint violation | constraint "maximum" is "1000" | `1250.75` |
| 7 | quantity | constraint violation | constraint "maximum" is "1000" | `1500` |
| 8 | arrival_date | missing value | constraint "required" is "True" | `` |

## Candidate inference (hint only)

The descriptor below is inferred from the scanned fragment with Frictionless `Detector`. It is strictly separated from the saved-contract verdict above: it never accepts or rejects any record and must not replace the saved contract without a contract change.

```json
{
  "fields": [
    {
      "name": "arrival_date",
      "type": "string"
    },
    {
      "name": "note",
      "type": "string"
    },
    {
      "name": "quantity",
      "type": "number"
    },
    {
      "name": "remark",
      "type": "any"
    },
    {
      "name": "tail_note",
      "type": "string"
    }
  ]
}
```

- Column "remark" is entirely blank in the scanned range: there is no value to infer a type from. The detector's fallback entry is not evidence of a type change; the blank cells remain missing values under the saved contract.
- Column "arrival_date" has saved-contract rejections (missing, type); review the candidate type only as a renegotiation hint, never as grounds to accept this batch.
- Column "quantity" has saved-contract rejections (constraint); review the candidate type only as a renegotiation hint, never as grounds to accept this batch.

## Preserved evidence beyond the contract

- Rejected rows, original values: `/Users/guo/Documents/GSB/1109/batches/de-u224-280-20261119/control/questions/GSB1109-20261119-U224-DE-PY-100/workspace/intake_precheck/reports/evidence/full-batch-rejections.csv`
- Cell-level reasons with raw input: `/Users/guo/Documents/GSB/1109/batches/de-u224-280-20261119/control/questions/GSB1109-20261119-U224-DE-PY-100/workspace/intake_precheck/reports/evidence/full-batch-rejections.jsonl`
