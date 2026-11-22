#!/usr/bin/env python3
"""
Migrate the supplier order rules to the renamed order columns.

The migration is a single lexical pass over the rule text. Only symbol tokens
that denote input fields (or comprehension variables that hold one of the new
field names) are changed; string literals, the trailing comment, attribute
names such as ``partner.rate`` and explicit mapping keys stay exactly as they
were written.

Column policy
-------------
* the old top-level ``rate`` column is now called ``total``
* the old top-level ``total`` column is now called ``rate``
* the free field ``fee`` is now called ``entry``

Comprehension policy
--------------------
The input field ``entry`` collides with the existing outer comprehension
variable ``entry``, and the swapped column names collide with an inner
comprehension variable. A local whose name would capture a migrated field is
renamed (``entry`` -> ``entry_local``, ``rate`` -> ``rate_local``) together
with every reference to it. This keeps "the value read from the outer input"
separate from "the value assigned by the comprehension" without changing the
rule's meaning.

Selector policy
---------------
The business declares a finite selector domain and, for each value, the *new*
column that value selects. Explicit-key lookups written as
``{ "standard": total, "promo": rate }[selector]`` are rewritten through that
map. A key that is not declared has no unambiguous target; the lookup is
reported and left untouched instead of guessed.

Running this module regenerates the artifacts next to it.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys

import rule_engine
from rule_engine.parser import Parser

OLD_RATE = 'rate'
OLD_TOTAL = 'total'
OLD_FEE = 'fee'
NEW_FREE = 'entry'
NEW_RATE = 'rate'
NEW_TOTAL = 'total'
SELECTOR = 'selector'

# Business-declared selector domain: selector value -> the old column that
# value used to select and the new column it selects after the rename. The two
# entries are the whole declared range; the sample records exercise both of
# them ("standard" and "promo").
SELECTOR_DECLARATION = {
    'standard': {'old': OLD_RATE, 'new': NEW_TOTAL},
    'promo': {'old': OLD_TOTAL, 'new': NEW_RATE},
}
SELECTOR_MAP = {
    key: value['new']
    for key, value in SELECTOR_DECLARATION.items()
}
assert set(SELECTOR_MAP.values()) == {NEW_RATE, NEW_TOTAL}

FIELD_RENAMES = {
    OLD_FEE: NEW_FREE,
    OLD_RATE: NEW_TOTAL,
    OLD_TOTAL: NEW_RATE,
}
NEW_FIELD_NAMES = frozenset((NEW_FREE, NEW_RATE, NEW_TOTAL))

MIGRATION_DIR = pathlib.Path(__file__).resolve().parent
SOURCE_DIR = MIGRATION_DIR / 'source'
MIGRATED_DIR = MIGRATION_DIR / 'migrated'
BLOCKED_DIR = MIGRATION_DIR / 'blocked'
MIGRATED_JSON = MIGRATION_DIR / 'migrated_rules.json'
CHANGES_JSON = MIGRATION_DIR / 'changes.json'
CHANGES_MD = MIGRATION_DIR / 'changes.md'
DIAGNOSTICS_JSON = MIGRATION_DIR / 'diagnostics.json'


@dataclasses.dataclass(frozen=True)
class Token:
    type: str
    value: str
    lexpos: int
    lineno: int

    @property
    def end(self) -> int:
        return self.lexpos + len(self.value)


@dataclasses.dataclass(frozen=True)
class Comprehension:
    open_index: int
    for_index: int
    variable_index: int
    in_index: int
    if_index: int | None
    close_index: int


@dataclasses.dataclass(frozen=True)
class SymbolOccurrence:
    index: int
    token: Token
    role: str  # field, local, declaration, attribute, builtin


@dataclasses.dataclass(frozen=True)
class SelectorMember:
    key: str
    key_index: int
    value_start: int
    value_end: int
    value_symbol_index: int | None


@dataclasses.dataclass(frozen=True)
class SelectorLookup:
    open_index: int
    close_index: int
    selector_index: int
    members: tuple[SelectorMember, ...]


def tokenize(text: str) -> list[Token]:
    lexer = Parser()._lexer
    lexer.input(text)
    tokens: list[Token] = []
    while True:
        token = lexer.token()
        if token is None:
            return tokens
        tokens.append(Token(token.type, token.value, token.lexpos, token.lineno))


def _pair_brackets(tokens: list[Token], open_type: str, close_type: str) -> dict[int, int]:
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for index, token in enumerate(tokens):
        if token.type == open_type:
            stack.append(index)
        elif token.type == close_type and stack:
            pairs[stack.pop()] = index
    return pairs


def comprehensions(tokens: list[Token]) -> list[Comprehension]:
    pairs = _pair_brackets(tokens, 'LBRACKET', 'RBRACKET')
    found: list[Comprehension] = []
    for open_index, close_index in sorted(pairs.items()):
        depth = 0
        for_index = None
        for index in range(open_index + 1, close_index):
            token = tokens[index]
            if token.type == 'LBRACKET':
                depth += 1
            elif token.type == 'RBRACKET':
                depth -= 1
            elif token.type == 'FOR' and depth == 0:
                for_index = index
                break
        if for_index is None:
            continue
        if tokens[for_index + 1].type != 'SYMBOL' or tokens[for_index + 2].type != 'IN':
            continue
        depth = 0
        if_index = None
        for index in range(for_index + 3, close_index):
            token = tokens[index]
            if token.type == 'LBRACKET':
                depth += 1
            elif token.type == 'RBRACKET':
                depth -= 1
            elif token.type == 'IF' and depth == 0:
                if_index = index
                break
        found.append(Comprehension(
                open_index=open_index,
                for_index=for_index,
                variable_index=for_index + 1,
                in_index=for_index + 2,
                if_index=if_index,
                close_index=close_index,
        ))
    return found


def _in_comprehension_body(comprehension: Comprehension, index: int) -> bool:
    """
    Whether *index* is in the scope of the comprehension variable.

    The result expression precedes ``for`` and the condition follows ``if``;
    the iterable between ``in`` and ``if`` (or the closing bracket) is
    evaluated before the assignment and is outside the variable's scope.
    """
    if comprehension.open_index < index < comprehension.for_index:
        return True
    if comprehension.if_index is None:
        return False
    return comprehension.if_index < index < comprehension.close_index


def symbol_occurrences(text: str) -> tuple[list[Token], list[SymbolOccurrence]]:
    tokens = tokenize(text)
    scopes = comprehensions(tokens)
    declarations = {scope.variable_index: scope for scope in scopes}
    occurrences: list[SymbolOccurrence] = []
    for index, token in enumerate(tokens):
        if token.type != 'SYMBOL':
            continue
        if token.value.startswith('$'):
            occurrences.append(SymbolOccurrence(index, token, 'builtin'))
            continue
        if index and tokens[index - 1].type in ('ATTR', 'ATTR_SAFE'):
            # `object.name`: the name is an attribute of another object, not
            # a top-level input field
            occurrences.append(SymbolOccurrence(index, token, 'attribute'))
            continue
        if index in declarations:
            occurrences.append(SymbolOccurrence(index, token, 'declaration'))
            continue
        active = {
                tokens[scope.variable_index].value
                for scope in scopes
                if _in_comprehension_body(scope, index)
        }
        role = 'local' if token.value in active else 'field'
        occurrences.append(SymbolOccurrence(index, token, role))
    return tokens, occurrences


def _string_literal_value(spelling: str) -> str:
    value = spelling
    if value[:1] in ('r', 's'):
        value = value[1:]
    return value[1:-1]


def selector_lookups(text: str) -> list[SelectorLookup]:
    """
    Find ``{ "key": expression, ... }[selector]`` lookups in *text*.
    """
    tokens = tokenize(text)
    brace_pairs = _pair_brackets(tokens, 'LBRACE', 'RBRACE')
    lookups: list[SelectorLookup] = []
    for open_index, close_index in sorted(brace_pairs.items()):
        if close_index + 3 >= len(tokens):
            continue
        if tokens[close_index + 1].type != 'LBRACKET':
            continue
        if tokens[close_index + 2].type != 'SYMBOL' or tokens[close_index + 2].value != SELECTOR:
            continue
        if tokens[close_index + 3].type != 'RBRACKET':
            continue
        members: list[SelectorMember] = []
        segment_start = open_index + 1
        depth = 0
        for index in range(open_index + 1, close_index):
            token = tokens[index]
            if token.type in ('LBRACE', 'LBRACKET'):
                depth += 1
            elif token.type in ('RBRACE', 'RBRACKET'):
                depth -= 1
            elif token.type == 'COMMA' and depth == 0:
                member = _selector_member(tokens, segment_start, index)
                if member is not None:
                    members.append(member)
                segment_start = index + 1
        member = _selector_member(tokens, segment_start, close_index)
        if member is not None:
            members.append(member)
        lookups.append(SelectorLookup(open_index, close_index, close_index + 2, tuple(members)))
    return lookups


def _selector_member(tokens: list[Token], start: int, end: int) -> SelectorMember | None:
    if end - start < 3:
        return None
    key_token = tokens[start]
    if key_token.type != 'STRING' or tokens[start + 1].type != 'COLON':
        return None
    value_start = start + 2
    value_end = end
    symbol_index = None
    if value_end - value_start == 1 and tokens[value_start].type == 'SYMBOL':
        symbol_index = value_start
    return SelectorMember(
            key=_string_literal_value(key_token.value),
            key_index=start,
            value_start=value_start,
            value_end=value_end,
            value_symbol_index=symbol_index,
    )


def _line_column(text: str, offset: int) -> tuple[int, int]:
    line = text.count('\n', 0, offset) + 1
    column = offset - text.rfind('\n', 0, offset)
    return line, column


def _local_renames(
        tokens: list[Token],
        occurrences: list[SymbolOccurrence],
        *,
        field_targets: dict[int, str],
) -> dict[str, str]:
    """
    Rename comprehension variables that would capture a field after migration.

    A local is renamed only when a *field* occurrence that is in that local's
    scope will have the local's name once the fields are renamed. The
    generated name is checked against every symbol spelling already used by
    the rule so it cannot collide.
    """
    scopes = comprehensions(tokens)
    occurrence_by_index = {occurrence.index: occurrence for occurrence in occurrences}
    captured: set[str] = set()
    for scope in scopes:
        local_name = tokens[scope.variable_index].value
        for occurrence in occurrences:
            if occurrence.role != 'field':
                continue
            if not _in_comprehension_body(scope, occurrence.index):
                continue
            if field_targets.get(occurrence.index) == local_name:
                captured.add(local_name)
                break
    taken = {occurrence.token.value for occurrence in occurrences}
    renamed: dict[str, str] = {}
    for name in sorted(captured):
        candidate = name + '_local'
        while candidate in taken:
            candidate += '_'
        renamed[name] = candidate
        taken.add(candidate)
    return renamed


def _local_reason(name: str) -> str:
    if name == NEW_FREE:
        return (
            "outer comprehension local renamed so the new free field 'entry' "
            "keeps the outer-input value instead of the inner assignment"
        )
    return (
        "inner comprehension local renamed so the swapped column '{0}' is not "
        "captured by the inner assignment".format(name)
    )


def migrate(text: str, *, rule_name: str = '') -> tuple[str, list[dict], dict]:
    """
    Return ``(migrated_text, changes, diagnostics)`` for *text*.
    """
    tokens, occurrences = symbol_occurrences(text)
    lookups = selector_lookups(text)

    overrides: dict[int, tuple[str, str]] = {}
    selector_skip: set[int] = set()
    unresolved: list[dict] = []
    lookup_reports: list[dict] = []
    for lookup in lookups:
        lookup_line, lookup_column = _line_column(text, tokens[lookup.selector_index].lexpos)
        report = dict(
                line=lookup_line,
                column=lookup_column,
                keys=[],
                declared=[],
                mapping={},
                values={},
                unresolved=[],
        )
        for member in lookup.members:
            key_line, key_column = _line_column(text, tokens[member.key_index].lexpos)
            report['keys'].append(member.key)
            report.setdefault('values', {})
            if member.value_symbol_index is not None:
                report['values'][member.key] = tokens[member.value_symbol_index].value
            if member.key in SELECTOR_MAP:
                report['declared'].append(member.key)
                report['mapping'][member.key] = SELECTOR_MAP[member.key]
                if member.value_symbol_index is None:
                    unresolved.append(dict(
                            kind='selector-value-expression',
                            key=member.key,
                            line=key_line,
                            column=key_column,
                            lookup_line=lookup_line,
                            lookup_column=lookup_column,
                            reason=(
                                "selector value '{0}' is declared, but its explicit key value is "
                                "not a single column reference and cannot be rewritten mechanically".format(member.key)
                            ),
                    ))
                    for index in range(member.value_start, member.value_end):
                        selector_skip.add(index)
                else:
                    occurrence = next(
                            occurrence for occurrence in occurrences
                            if occurrence.index == member.value_symbol_index
                    )
                    if occurrence.role != 'field':
                        # the declared key points at something that is not an
                        # input column (a comprehension local or an attribute)
                        unresolved.append(dict(
                                kind='selector-value-not-field',
                                key=member.key,
                                line=key_line,
                                column=key_column,
                                lookup_line=lookup_line,
                                lookup_column=lookup_column,
                                reason=(
                                    "selector value '{0}' is declared, but its explicit key value "
                                    "'{1}' is not a top-level input field and cannot be treated as "
                                    "an order column".format(member.key, tokens[member.value_symbol_index].value)
                                ),
                                old_rule=rule_name,
                                old_rule_text=text,
                        ))
                        for index in range(member.value_start, member.value_end):
                            selector_skip.add(index)
                    else:
                        declaration = SELECTOR_DECLARATION[member.key]
                        actual = tokens[member.value_symbol_index].value
                        if actual != declaration['old']:
                            unresolved.append(dict(
                                    kind='selector-value-mismatch',
                                    key=member.key,
                                    line=key_line,
                                    column=key_column,
                                    lookup_line=lookup_line,
                                    lookup_column=lookup_column,
                                    reason=(
                                        "selector value '{0}' is declared to select the old column "
                                        "'{1}', but the explicit key maps to '{2}'; the reference "
                                        "has no unambiguous target".format(member.key, declaration['old'], actual)
                                    ),
                                    old_rule=rule_name,
                                    old_rule_text=text,
                            ))
                            for index in range(member.value_start, member.value_end):
                                selector_skip.add(index)
                        else:
                            overrides[member.value_symbol_index] = (
                                    declaration['new'],
                                    "selector key '{0}' selects the new field '{1}'".format(member.key, declaration['new']),
                            )
            else:
                report['unresolved'].append(member.key)
                unresolved.append(dict(
                        kind='selector-key',
                        key=member.key,
                        line=key_line,
                        column=key_column,
                        lookup_line=lookup_line,
                        lookup_column=lookup_column,
                        reason=(
                            "selector value '{0}' is not in the business-declared map; "
                            "the reference at line {1}, column {2} has no unambiguous target "
                            "and was left unchanged".format(member.key, lookup_line, lookup_column)
                        ),
                        old_rule=rule_name,
                        old_rule_text=text,
                        old_reference=text[tokens[member.key_index].lexpos:tokens[lookup.close_index + 3].end],
                ))
                # do not guess: leave this member's value exactly as written
                for index in range(member.value_start, member.value_end):
                    selector_skip.add(index)
        lookup_reports.append(report)

    # work out what every field occurrence will be called, so a local is only
    # renamed when a field of that spelling is actually in its scope
    field_targets: dict[int, str] = {}
    for occurrence in occurrences:
        if occurrence.role != 'field' or occurrence.index in selector_skip:
            continue
        field_targets[occurrence.index] = overrides.get(
                occurrence.index,
                (FIELD_RENAMES.get(occurrence.token.value, occurrence.token.value), None),
        )[0]
    renames = _local_renames(tokens, occurrences, field_targets=field_targets)
    replacements: dict[int, str] = {}
    changes: list[dict] = []
    for occurrence in occurrences:
        new_value: str | None = None
        reason: str | None = None
        if occurrence.index in overrides:
            new_value, reason = overrides[occurrence.index]
        elif occurrence.index in selector_skip:
            continue
        elif occurrence.role == 'field':
            new_value = FIELD_RENAMES.get(occurrence.token.value)
            if new_value is not None:
                reason = "top-level order column swapped" if occurrence.token.value in (OLD_RATE, OLD_TOTAL) else "free-field input renamed"
        elif occurrence.role in ('local', 'declaration') and occurrence.token.value in renames:
            new_value = renames[occurrence.token.value]
            reason = _local_reason(occurrence.token.value)
        if new_value is None or new_value == occurrence.token.value:
            continue
        line, column = _line_column(text, occurrence.token.lexpos)
        line_start = text.rfind('\n', 0, occurrence.token.lexpos) + 1
        line_end = text.find('\n', occurrence.token.lexpos)
        if line_end == -1:
            line_end = len(text)
        replacements[occurrence.index] = new_value
        changes.append(dict(
                rule=rule_name,
                token_index=occurrence.index,
                offset=occurrence.token.lexpos,
                line=line,
                column=column,
                before=occurrence.token.value,
                after=new_value,
                role=occurrence.role,
                reason=reason,
                original_line=text[line_start:line_end],
        ))

    pieces: list[str] = []
    cursor = 0
    for index in sorted(replacements):
        token = tokens[index]
        pieces.append(text[cursor:token.lexpos])
        pieces.append(replacements[index])
        cursor = token.end
    pieces.append(text[cursor:])
    migrated_text = ''.join(pieces)

    diagnostics = dict(
            rule=rule_name,
            selector_map=SELECTOR_MAP,
            selector_declaration=SELECTOR_DECLARATION,
            selector_lookups=lookup_reports,
            unresolved=unresolved,
            local_renames=renames,
    )
    return migrated_text, changes, diagnostics


def expected_symbols(text: str) -> set[str]:
    """The symbols a correctly migrated text must expose."""
    tokens, occurrences = symbol_occurrences(text)
    overrides: dict[int, str] = {}
    for lookup in selector_lookups(text):
        for member in lookup.members:
            if member.key in SELECTOR_MAP and member.value_symbol_index is not None:
                overrides[member.value_symbol_index] = SELECTOR_MAP[member.key]
    field_targets: dict[int, str] = {}
    for occurrence in occurrences:
        if occurrence.role != 'field':
            continue
        field_targets[occurrence.index] = overrides.get(
                occurrence.index,
                (FIELD_RENAMES.get(occurrence.token.value, occurrence.token.value), None),
        )[0]
    renames = _local_renames(tokens, occurrences, field_targets=field_targets)
    expected: set[str] = set()
    for occurrence in occurrences:
        if occurrence.index in overrides:
            expected.add(overrides[occurrence.index])
        elif occurrence.role == 'field':
            expected.add(field_targets[occurrence.index])
        elif occurrence.role in ('local', 'declaration'):
            expected.add(renames.get(occurrence.token.value, occurrence.token.value))
    return expected


def render_changes(all_changes: dict[str, list[dict]]) -> str:
    lines = ['# Migration changes', '']
    lines.append('One row per changed symbol occurrence. `line:column` and `offset` are 1-based line/column and')
    lines.append('0-based character offsets into the original rule text; `original line` is the full source line.')
    lines.append('')
    for name, records in all_changes.items():
        lines.append('## {0}'.format(name))
        lines.append('')
        lines.append('| line:column | offset | before | after | role | reason | original line |')
        lines.append('| --- | --- | --- | --- | --- | --- | --- |')
        for record in records:
            original = record['original_line'].replace('|', '\\|')
            lines.append('| {line}:{column} | {offset} | `{before}` | `{after}` | {role} | {reason} | `{original}` |'.format(
                    original=original,
                    **record
            ))
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def main() -> int:
    MIGRATED_DIR.mkdir(exist_ok=True)
    BLOCKED_DIR.mkdir(exist_ok=True)
    rules: dict[str, dict] = {}
    all_changes: dict[str, list[dict]] = {}
    all_diagnostics: dict[str, dict] = {}
    for source_path in sorted(SOURCE_DIR.glob('*.rule')):
        name = source_path.stem
        text = source_path.read_text(encoding='utf-8')
        migrated_text, changes, diagnostics = migrate(text, rule_name=name)
        blocked = bool(diagnostics['unresolved'])
        if not blocked:
            rule = rule_engine.Rule(migrated_text)
            missing = expected_symbols(text) - rule.context.symbols
            if missing:
                raise SystemExit('{0}: migrated rule is missing symbols {1}'.format(name, sorted(missing)))
            (MIGRATED_DIR / source_path.name).write_text(migrated_text, encoding='utf-8')
        else:
            (BLOCKED_DIR / source_path.name).write_text(migrated_text, encoding='utf-8')
        rules[name] = dict(
                publishable=not blocked,
                source=text,
                migrated=migrated_text,
                changes=changes,
                diagnostics=diagnostics,
                selector_map=SELECTOR_MAP,
        )
        all_changes[name] = changes
        all_diagnostics[name] = diagnostics
    MIGRATED_JSON.write_text(json.dumps(rules, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    CHANGES_JSON.write_text(json.dumps(all_changes, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    CHANGES_MD.write_text(render_changes(all_changes), encoding='utf-8')
    DIAGNOSTICS_JSON.write_text(json.dumps(all_diagnostics, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
