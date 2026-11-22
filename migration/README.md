# Supplier order column migration

The supplier renamed the order columns. This bundle migrates the two in-use
Rule Engine rules once, with the old texts kept for review.

## Column policy

| old | new | scope |
| --- | --- | --- |
| top-level `rate` | `total` | input field |
| top-level `total` | `rate` | input field |
| free field `fee` | `entry` | input field |

`rate` and `total` are **swapped**, so they cannot be rewritten one after the
other in place: replacing `rate`->`total` first and then `total`->`rate` makes
every occurrence point at a single column. The migrator resolves each symbol
in one pass, using both directions of the map.

## Scope rules

A symbol is only renamed when it is a top-level input field. These are left
byte-for-byte unchanged:

- string literals, e.g. `note == "rate total fee"`;
- the trailing `#` comment;
- attribute names of another object, e.g. `partner.rate`, `partner.total`,
  `partner.fee`;
- explicit mapping keys, e.g. `"standard"`, `"promo"`.

Comprehension variables are scoped:

- the result expression before `for` and the condition after `if` see the
  variable; the iterable between `in` and `if` is evaluated first and does not;
- the new free field `entry` collides with the existing outer variable
  `entry`; every reference to that local is renamed `entry_local`, so
  `entry.amount * entry + total` keeps `entry.amount` as the row and the
  second `entry` as the outer supplier field;
- the swapped names `rate`/`total` collide with the inner variable `rate`;
  that local and its references are renamed `rate_local`, so
  `rate_local - rate` compares the slice value against the outer column.

## Selector policy

The business declared a finite selector domain, with these two values as the
whole range:

| selector value | old column | new column |
| --- | --- | --- |
| `standard` | `rate` | `total` |
| `promo` | `total` | `rate` |

`{ "standard": total, "promo": rate }[selector]` is rewritten through this
map. Any other selector value is **not** guessed: `migration/diagnostics.json`
lists the value, the original line/column, the original lookup text and the
complete old rule text, and the rule is written to `migration/blocked/`
instead of `migration/migrated/` so it cannot be published by accident.

## Files

- `source/order_rules_a.rule`, `source/order_rules_b.rule` – the two legacy
  rules, identical except for the inner local name;
- `source/order_rules_undeclared.rule` – a rule with an undeclared selector
  value, kept to demonstrate the blocked path;
- `migrated/order_rules_a.rule`, `migrated/order_rules_b.rule` – publishable
  migrated texts;
- `blocked/order_rules_undeclared.rule` – the blocked draft; it must not be
  published;
- `records.json` – old/new record pairs, including the swap-distinct pair
  whose old values are exactly the new values exchanged;
- `changes.md`, `changes.json` – every change with its 1-based line/column,
  0-based offset, original line and reason;
- `diagnostics.json` – selector declaration, lookup reports, unresolved
  references and the local renames;
- `migrated_rules.json` – the complete machine-readable bundle (source,
  migrated text, changes, diagnostics, publishability);
- `migrate_rules.py` – regenerates everything above;
- `verify_migration.py` – behavioural verification with the real engine.

## Running

```sh
PYTHONPATH=lib .venv/bin/python migration/migrate_rules.py
PYTHONPATH=lib:./migration .venv/bin/python -m unittest migration.verify_migration -v
```

The verification covers:

- old rule on old record == migrated rule on the renamed new record, for every
  fixture;
- the swap-distinct fixture, plus proof that a naive sequential replace
  evaluates differently;
- the `entry` field/local collision and the inner `rate` local collision are
  observable, i.e. dropping either rename changes the result;
- the two equivalent source rules still agree on every fixture;
- strings, comment and `partner.*` attributes are unchanged;
- an undeclared selector value is reported and its old rule text is preserved.
