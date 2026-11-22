# Migration changes

One row per changed symbol occurrence. `line:column` and `offset` are 1-based line/column and
0-based character offsets into the original rule text; `original line` is the full source line.

## order_rules_a

| line:column | offset | before | after | role | reason | original line |
| --- | --- | --- | --- | --- | --- | --- |
| 1:1 | 0 | `rate` | `total` | field | top-level order column swapped | `rate < total` |
| 1:8 | 7 | `total` | `rate` | field | top-level order column swapped | `rate < total` |
| 2:5 | 17 | `fee` | `entry` | field | free-field input renamed | `and fee >= 0` |
| 5:20 | 107 | `fee` | `entry` | field | free-field input renamed | `and partner.fee == fee` |
| 6:19 | 129 | `rate` | `total` | field | selector key 'standard' selects the new field 'total' | `and { "standard": rate, "promo": total }[selector] >= fee` |
| 6:34 | 144 | `total` | `rate` | field | selector key 'promo' selects the new field 'rate' | `and { "standard": rate, "promo": total }[selector] >= fee` |
| 6:55 | 165 | `fee` | `entry` | field | free-field input renamed | `and { "standard": rate, "promo": total }[selector] >= fee` |
| 8:7 | 186 | `entry` | `entry_local` | local | outer comprehension local renamed so the new free field 'entry' keeps the outer-input value instead of the inner assignment | `      entry.amount * fee + rate` |
| 8:22 | 201 | `fee` | `entry` | field | free-field input renamed | `      entry.amount * fee + rate` |
| 8:28 | 207 | `rate` | `total` | field | top-level order column swapped | `      entry.amount * fee + rate` |
| 9:11 | 222 | `entry` | `entry_local` | declaration | outer comprehension local renamed so the new free field 'entry' keeps the outer-input value instead of the inner assignment | `      for entry in orders` |
| 10:10 | 247 | `entry` | `entry_local` | local | outer comprehension local renamed so the new free field 'entry' keeps the outer-input value instead of the inner assignment | `      if entry.qty > fee` |
| 10:22 | 259 | `fee` | `entry` | field | free-field input renamed | `      if entry.qty > fee` |
| 11:21 | 283 | `rate` | `rate_local` | local | inner comprehension local renamed so the swapped column 'rate' is not captured by the inner assignment | `         and $sum([ rate - total for rate in entry.slices if rate > 0 ]) < 0` |
| 11:28 | 290 | `total` | `rate` | field | top-level order column swapped | `         and $sum([ rate - total for rate in entry.slices if rate > 0 ]) < 0` |
| 11:38 | 300 | `rate` | `rate_local` | declaration | inner comprehension local renamed so the swapped column 'rate' is not captured by the inner assignment | `         and $sum([ rate - total for rate in entry.slices if rate > 0 ]) < 0` |
| 11:46 | 308 | `entry` | `entry_local` | local | outer comprehension local renamed so the new free field 'entry' keeps the outer-input value instead of the inner assignment | `         and $sum([ rate - total for rate in entry.slices if rate > 0 ]) < 0` |
| 11:62 | 324 | `rate` | `rate_local` | local | inner comprehension local renamed so the swapped column 'rate' is not captured by the inner assignment | `         and $sum([ rate - total for rate in entry.slices if rate > 0 ]) < 0` |

## order_rules_b

| line:column | offset | before | after | role | reason | original line |
| --- | --- | --- | --- | --- | --- | --- |
| 1:1 | 0 | `rate` | `total` | field | top-level order column swapped | `rate < total` |
| 1:8 | 7 | `total` | `rate` | field | top-level order column swapped | `rate < total` |
| 2:5 | 17 | `fee` | `entry` | field | free-field input renamed | `and fee >= 0` |
| 5:20 | 107 | `fee` | `entry` | field | free-field input renamed | `and partner.fee == fee` |
| 6:19 | 129 | `rate` | `total` | field | selector key 'standard' selects the new field 'total' | `and { "standard": rate, "promo": total }[selector] >= fee` |
| 6:34 | 144 | `total` | `rate` | field | selector key 'promo' selects the new field 'rate' | `and { "standard": rate, "promo": total }[selector] >= fee` |
| 6:55 | 165 | `fee` | `entry` | field | free-field input renamed | `and { "standard": rate, "promo": total }[selector] >= fee` |
| 8:7 | 186 | `entry` | `entry_local` | local | outer comprehension local renamed so the new free field 'entry' keeps the outer-input value instead of the inner assignment | `      entry.amount * fee + rate` |
| 8:22 | 201 | `fee` | `entry` | field | free-field input renamed | `      entry.amount * fee + rate` |
| 8:28 | 207 | `rate` | `total` | field | top-level order column swapped | `      entry.amount * fee + rate` |
| 9:11 | 222 | `entry` | `entry_local` | declaration | outer comprehension local renamed so the new free field 'entry' keeps the outer-input value instead of the inner assignment | `      for entry in orders` |
| 10:10 | 247 | `entry` | `entry_local` | local | outer comprehension local renamed so the new free field 'entry' keeps the outer-input value instead of the inner assignment | `      if entry.qty > fee` |
| 10:22 | 259 | `fee` | `entry` | field | free-field input renamed | `      if entry.qty > fee` |
| 11:28 | 290 | `total` | `rate` | field | top-level order column swapped | `         and $sum([ unit - total for unit in entry.slices if unit > 0 ]) < 0` |
| 11:46 | 308 | `entry` | `entry_local` | local | outer comprehension local renamed so the new free field 'entry' keeps the outer-input value instead of the inner assignment | `         and $sum([ unit - total for unit in entry.slices if unit > 0 ]) < 0` |

## order_rules_undeclared

| line:column | offset | before | after | role | reason | original line |
| --- | --- | --- | --- | --- | --- | --- |
| 1:1 | 0 | `rate` | `total` | field | top-level order column swapped | `rate < total` |
| 1:8 | 7 | `total` | `rate` | field | top-level order column swapped | `rate < total` |
| 2:5 | 17 | `fee` | `entry` | field | free-field input renamed | `and fee >= 0` |
| 3:19 | 44 | `rate` | `total` | field | selector key 'standard' selects the new field 'total' | `and { "standard": rate, "promo": total, "clearance": rate }[selector] >= fee` |
| 3:34 | 59 | `total` | `rate` | field | selector key 'promo' selects the new field 'rate' | `and { "standard": rate, "promo": total, "clearance": rate }[selector] >= fee` |
| 3:74 | 99 | `fee` | `entry` | field | free-field input renamed | `and { "standard": rate, "promo": total, "clearance": rate }[selector] >= fee` |
