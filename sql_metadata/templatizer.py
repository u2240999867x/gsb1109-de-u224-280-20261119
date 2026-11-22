"""Structural query templates for grouping log entries by shape.

Complements :class:`~sql_metadata.generalizator.Generalizator` (which
anonymises individual literals) with a *structural* template that can be
used as a grouping key for query logs:

* Comparison values and bind values never appear in the template —
  literals become ``?`` and bindings become canonical ``:p1``, ``:p2``, …
  names that only preserve the *equality structure* of the bindings
  (``:x`` used twice differs from ``:lo`` + ``:hi``).
* Structural literals stay in the template: ``date_trunc`` granularity,
  JSON extraction paths and ``ORDER BY`` / ``GROUP BY`` output positions.
* Relation aliases are alpha-renamed (``t1``, ``t2``, …) in a
  scope-aware way, so shadowing subqueries keep the distinction between
  references to the inner and the outer binding, and top-level ``AND``
  conjuncts are sorted — reports that only differ in alias/parameter
  names or ``AND`` item order share one template.
* Bindings used as JSON paths are only reliable structure when the
  caller declares them via ``declared_paths``.  An undeclared path
  binding is marked ``[private-path]`` and reported through
  :attr:`Templatizer.issues` so it never silently mixes into a plain
  comparison group.
"""

import itertools
import math
from collections.abc import Container, Iterable

from sqlglot import exp

from sql_metadata.ast_parser import ASTParser
from sql_metadata.generalizator import Generalizator

#: Expression positions relevant for template extraction.
_VALUE = "value"
_PATH = "path"
_ORDINAL = "ordinal"

#: JSON extractor node types whose ``expression`` argument is a path.
_JSON_EXTRACTORS = (
    exp.JSONExtract,
    exp.JSONExtractScalar,
    exp.JSONBExtract,
    exp.JSONBExtractScalar,
)

#: Function forms of JSON path extraction (parsed as anonymous
#: functions); all arguments after the document are structural paths.
_JSON_PATH_FUNCTIONS = frozenset(
    {"json_extract_path", "jsonb_extract_path", "jsonb_extract_path_text"}
)

#: Statement nodes that open a new alias scope.
_SCOPE_NODES = (exp.Select, exp.Update, exp.Delete)

#: Date/time truncation nodes whose ``unit`` argument is structural.
_TRUNCATION_NODES = (
    exp.DateTrunc,
    exp.TimestampTrunc,
    exp.TimeTrunc,
    exp.DatetimeTrunc,
)

#: Token prefixes used to mark symbols inside the prepared tree.  The
#: NUL prefix cannot appear in real identifiers, so tokens never collide
#: with names coming from the query itself.
_ALIAS_TOKEN = "\x00a"
_PARAM_TOKEN = "\x00p"

#: Suffix appended to canonical names of undeclared JSON path bindings.
_PRIVATE_PATH_SUFFIX = "[private-path]"

#: Cap on the number of renaming permutations explored when
#: canonicalising alias/parameter names.  Beyond it the template falls
#: back to first-occurrence naming (still deterministic).
_MAX_RENAMINGS = 5040

_ParamKey = tuple[str, str]  # ("named", name) or ("anon", 1-based index)
_AliasKey = tuple[int, str]  # (id of the scope node, alias name)
_Frame = dict[str, _AliasKey]


class Templatizer:
    """Produce a structural grouping template for a SQL query.

    :param sql: Raw SQL query string to templatize.
    :type sql: str
    :param declared_paths: Names of bindings (without the leading
        colon) whose values are JSON paths that the caller declares as
        public structure, e.g. ``{"path"}`` for ``data -> :path``.
        Private parameter values are never accepted here.
    :type declared_paths: Iterable[str] | None
    :param ignore_in_list_size: When ``True``, scalar ``IN (…)`` lists
        and ``VALUES (…)`` row counts collapse to a single item so list
        size does not split groups.  Tuple arity, subquery ``IN`` and
        quoted identifiers always stay distinguishable.
    :type ignore_in_list_size: bool
    """

    def __init__(
        self,
        sql: str = "",
        declared_paths: Iterable[str] | None = None,
        ignore_in_list_size: bool = False,
    ) -> None:
        self._raw_query = sql
        self._declared_paths = frozenset(declared_paths or ())
        self._ignore_in_list_size = ignore_in_list_size

        self._template: str | None = None
        self._reliable = True
        self._issues: list[str] = []

        # State used while computing the template.
        self._tree: exp.Expression | None = None
        self._param_symbols: list[_ParamKey] = []
        self._param_roles: dict[_ParamKey, set[str]] = {}
        self._anon_params = 0
        self._alias_symbols: list[_AliasKey] = []
        self._alias_index: dict[_AliasKey, int] = {}
        self._private_path_tokens: set[str] = set()

    @property
    def template(self) -> str:
        """Return the structural template used as the grouping key.

        Contains no comparison values and no bind values — only the
        canonical query shape.  ``""`` for empty input.

        :rtype: str
        """
        if self._template is None:
            self._compute()
        assert self._template is not None
        return self._template

    @property
    def reliable(self) -> bool:
        """Whether the template can reliably separate different structures.

        ``False`` when an undeclared binding provides a JSON path (the
        group may mix queries with different paths) or when the query
        could not be parsed structurally.

        :rtype: bool
        """
        _ = self.template
        return self._reliable

    @property
    def issues(self) -> tuple[str, ...]:
        """Return human-readable reasons for an unreliable template.

        :rtype: tuple[str, ...]
        """
        _ = self.template
        return tuple(self._issues)

    # -- computation ---------------------------------------------------

    def _compute(self) -> None:
        """Compute the template, reliability flag and issues."""
        if not self._raw_query or not self._raw_query.strip():
            self._template = ""
            return
        try:
            ast = ASTParser(self._raw_query).ast
        except ValueError:
            self._template = Generalizator(self._raw_query).generalize
            self._reliable = False
            self._issues.append(
                "query could not be parsed structurally; "
                "fell back to literal generalisation"
            )
            return
        if ast is None:
            self._template = ""
            return
        self._tree = self._prepare(ast.copy(), [], _VALUE)
        self._analyse_bindings()
        self._template = self._canonical_sql()

    # -- structural preparation (pass A) -------------------------------

    def _prepare(
        self, node: exp.Expression, scopes: list[_Frame], position: str
    ) -> exp.Expression:
        """Normalise values and tokenise symbols in *node* (in place).

        Literals in value position become anonymous ``?`` placeholders,
        bindings become ``\\x00p<N>`` tokens and alias definitions plus
        their column references become ``\\x00a<N>`` tokens.  Literals in
        structural positions (JSON paths, ``ORDER BY``/``GROUP BY``
        ordinals) are kept as-is.
        """
        if isinstance(node, exp.Literal):
            return exp.Placeholder() if position == _VALUE else node
        if (
            isinstance(node, exp.Neg)
            and isinstance(node.this, exp.Literal)
            and node.this.is_number
        ):
            return exp.Placeholder() if position == _VALUE else node
        if isinstance(node, exp.Placeholder):
            self._register_param(node, position)
            return node
        if isinstance(node, _SCOPE_NODES):
            scopes = [*scopes, self._build_frame(node)]
            self._prepare_children(node, scopes)
            return node
        if isinstance(node, exp.Column):
            self._tokenize_qualifier(node, scopes)
            return node
        if isinstance(node, (exp.Table, exp.Subquery)):
            self._tokenize_alias_def(node, scopes)
            self._prepare_children(node, scopes)
            return node
        return self._prepare_special(node, scopes, position)

    def _prepare_special(
        self, node: exp.Expression, scopes: list[_Frame], position: str
    ) -> exp.Expression:
        """Handle node types with positional or list arguments."""
        del position  # only generic children below this point
        if isinstance(node, _TRUNCATION_NODES):
            return self._prepare_truncation(node, scopes)
        if self._is_json_path_function(node):
            return self._prepare_json_function(node, scopes)
        if isinstance(node, _JSON_EXTRACTORS):
            return self._prepare_json_extractor(node, scopes)
        if isinstance(node, exp.Ordered):
            node.set("this", self._prepare(node.this, scopes, _ORDINAL))
            self._prepare_children(node, scopes, skip={"this"})
            return node
        if isinstance(node, exp.Group):
            node.set(
                "expressions",
                [self._prepare(item, scopes, _ORDINAL) for item in node.expressions],
            )
            self._prepare_children(node, scopes, skip={"expressions"})
            return node
        if isinstance(node, exp.In):
            return self._prepare_in(node, scopes)
        if isinstance(node, exp.Values):
            return self._prepare_values(node, scopes)
        self._prepare_children(node, scopes)
        return node

    @staticmethod
    def _is_json_path_function(node: exp.Expression) -> bool:
        """Return whether *node* is a function-form JSON path extraction."""
        return (
            isinstance(node, exp.Anonymous)
            and node.this.lower() in _JSON_PATH_FUNCTIONS
        )

    def _prepare_truncation(
        self, node: exp.Expression, scopes: list[_Frame]
    ) -> exp.Expression:
        """Keep the structural ``unit`` of a date/time truncation."""
        unit = node.args.get("unit")
        if isinstance(unit, exp.Expression):
            node.set("unit", self._prepare(unit, scopes, _ORDINAL))
        self._prepare_children(node, scopes, skip={"unit"})
        return node

    def _prepare_json_function(
        self, node: exp.Expression, scopes: list[_Frame]
    ) -> exp.Expression:
        """Keep the path arguments of a function-form JSON extraction."""
        node.set(
            "expressions",
            [
                self._prepare(arg, scopes, _VALUE if index == 0 else _PATH)
                for index, arg in enumerate(node.expressions)
            ],
        )
        self._prepare_children(node, scopes, skip={"expressions"})
        return node

    def _prepare_json_extractor(
        self, node: exp.Expression, scopes: list[_Frame]
    ) -> exp.Expression:
        """Keep the structural path of a JSON extractor node."""
        node.set("this", self._prepare(node.this, scopes, _VALUE))
        path = node.args.get("expression")
        if path is not None:
            node.set("expression", self._prepare(path, scopes, _PATH))
        self._prepare_children(node, scopes, skip={"this", "expression"})
        return node

    def _prepare_children(
        self,
        node: exp.Expression,
        scopes: list[_Frame],
        skip: Container[str] = frozenset(),
    ) -> None:
        """Recursively prepare every expression child of *node*."""
        for key, value in list(node.args.items()):
            if key in skip:
                continue
            if isinstance(value, exp.Expression):
                node.set(key, self._prepare(value, scopes, _VALUE))
            elif isinstance(value, list):
                node.set(
                    key,
                    [
                        self._prepare(item, scopes, _VALUE)
                        if isinstance(item, exp.Expression)
                        else item
                        for item in value
                    ],
                )

    def _prepare_in(self, node: exp.In, scopes: list[_Frame]) -> exp.In:
        """Normalise an ``IN`` predicate, honouring the list-size option."""
        this = node.args.get("this")
        if isinstance(this, exp.Expression):
            node.set("this", self._prepare(this, scopes, _VALUE))
        query = node.args.get("query")
        if isinstance(query, exp.Expression):
            node.set("query", self._prepare(query, scopes, _VALUE))
        items = node.args.get("expressions") or []
        if items:
            prepared = [self._prepare(item, scopes, _VALUE) for item in items]
            if self._ignore_in_list_size:
                if all(isinstance(item, exp.Tuple) for item in prepared):
                    # Tuple arity stays visible, row count collapses.
                    prepared = prepared[:1]
                elif not any(isinstance(item, exp.Tuple) for item in prepared):
                    prepared = [exp.Placeholder()]
            node.set("expressions", prepared)
        return node

    def _prepare_values(self, node: exp.Values, scopes: list[_Frame]) -> exp.Values:
        """Normalise a ``VALUES`` list, honouring the list-size option."""
        rows = [self._prepare(row, scopes, _VALUE) for row in node.expressions]
        if self._ignore_in_list_size and len(rows) > 1:
            rows = rows[:1]
        node.set("expressions", rows)
        return node

    # -- symbol registration -------------------------------------------

    def _register_param(self, node: exp.Placeholder, position: str) -> None:
        """Register a binding symbol and replace its name with a token."""
        name = node.args.get("this")
        if isinstance(name, str) and name:
            key = ("named", name)
        else:
            self._anon_params += 1
            key = ("anon", str(self._anon_params))
        if key not in self._param_roles:
            self._param_roles[key] = set()
            self._param_symbols.append(key)
        role = "path" if position == _PATH else "value"
        self._param_roles[key].add(role)
        index = self._param_symbols.index(key)
        node.set("this", f"{_PARAM_TOKEN}{index}")

    def _build_frame(self, node: exp.Expression) -> _Frame:
        """Register alias symbols introduced by a scope node."""
        frame: _Frame = {}
        for item in self._scope_items(node):
            alias = item.alias if isinstance(item, (exp.Table, exp.Subquery)) else ""
            if alias:
                symbol = (id(node), alias)
                frame[alias] = symbol
                if symbol not in self._alias_index:
                    self._alias_index[symbol] = len(self._alias_symbols)
                    self._alias_symbols.append(symbol)
        return frame

    @classmethod
    def _scope_items(cls, node: exp.Expression) -> list[exp.Expression]:
        """Collect table-like items that may introduce aliases."""
        items: list[exp.Expression] = []
        for item in cls._raw_scope_items(node):
            items.extend(cls._unwrap_table_expressions(item))
        return items

    @staticmethod
    def _raw_scope_items(node: exp.Expression) -> list[exp.Expression]:
        """Collect direct FROM/JOIN/USING items of a scope node."""
        raw: list[exp.Expression] = []
        from_arg = node.args.get("from_") or node.args.get("from")
        if from_arg is not None:
            if isinstance(from_arg.this, exp.Expression):
                raw.append(from_arg.this)
            raw.extend(
                item
                for item in from_arg.expressions
                if isinstance(item, exp.Expression)
            )
        for join in node.args.get("joins") or []:
            if isinstance(join.this, exp.Expression):
                raw.append(join.this)
        for using in node.args.get("using") or []:
            if isinstance(using, exp.Expression):
                raw.append(using)
        this = node.args.get("this")
        if isinstance(this, (exp.Table, exp.Subquery)):
            raw.append(this)
        return raw

    @classmethod
    def _unwrap_table_expressions(cls, item: exp.Expression) -> list[exp.Expression]:
        """Unwrap parenthesised table expressions within a FROM item.

        A parenthesised join such as ``(t2 a JOIN t3 b ON …)`` parses as
        a ``Subquery`` wrapping a ``Table``; its inner aliases belong to
        the enclosing scope.  Real subqueries (wrapping a ``Select``)
        keep their own scope and only contribute their outer alias.
        """
        if isinstance(item, exp.Subquery) and not isinstance(item.this, exp.Table):
            return [item]
        items = [item]
        for child in item.iter_expressions():
            items.extend(cls._unwrap_table_expressions(child))
        return items

    def _alias_token(self, symbol: _AliasKey) -> str:
        """Return the token string for an alias symbol."""
        return f"{_ALIAS_TOKEN}{self._alias_index[symbol]}"

    def _tokenize_qualifier(self, node: exp.Column, scopes: list[_Frame]) -> None:
        """Replace a column's table qualifier with its alias token."""
        qualifier = node.args.get("table")
        if not isinstance(qualifier, exp.Identifier):
            return
        for frame in reversed(scopes):
            symbol = frame.get(qualifier.this)
            if symbol is not None:
                qualifier.set("this", self._alias_token(symbol))
                qualifier.set("quoted", False)
                return

    def _tokenize_alias_def(self, node: exp.Expression, scopes: list[_Frame]) -> None:
        """Replace a table/subquery alias definition with its token."""
        alias = node.args.get("alias")
        if not isinstance(alias, exp.TableAlias) or not scopes:
            return
        symbol = scopes[-1].get(node.alias)
        ident = alias.this
        if symbol is not None and isinstance(ident, exp.Identifier):
            ident.set("this", self._alias_token(symbol))
            ident.set("quoted", False)

    # -- reliability analysis ------------------------------------------

    def _analyse_bindings(self) -> None:
        """Flag undeclared JSON path bindings as unreliable."""
        for index, key in enumerate(self._param_symbols):
            roles = self._param_roles[key]
            declared = key[0] == "named" and key[1] in self._declared_paths
            if "path" not in roles or declared:
                continue
            self._private_path_tokens.add(f"{_PARAM_TOKEN}{index}")
            self._reliable = False
            label = f":{key[1]}" if key[0] == "named" else "?"
            if "value" in roles:
                self._issues.append(
                    f"binding {label} is used both as a comparison value and "
                    "as a JSON path but is not declared as public structure; "
                    "the template cannot reliably separate queries with "
                    "different paths"
                )
            else:
                self._issues.append(
                    f"binding {label} provides a JSON path but is not "
                    "declared as public structure; the template cannot "
                    "reliably separate queries with different paths"
                )

    # -- canonical rendering (pass B) ----------------------------------

    def _canonical_sql(self) -> str:
        """Render the minimal template over all canonical renamings."""
        assert self._tree is not None
        param_count = len(self._param_symbols)
        alias_count = len(self._alias_symbols)
        if math.factorial(param_count) * math.factorial(alias_count) > _MAX_RENAMINGS:
            param_orders = [tuple(range(param_count))]
            alias_orders = [tuple(range(alias_count))]
        else:
            param_orders = list(itertools.permutations(range(param_count)))
            alias_orders = list(itertools.permutations(range(alias_count)))
        best: str | None = None
        for param_order in param_orders:
            param_names = {
                f"{_PARAM_TOKEN}{old}": f"p{new + 1}"
                for new, old in enumerate(param_order)
            }
            for alias_order in alias_orders:
                alias_names = {
                    f"{_ALIAS_TOKEN}{old}": f"t{new + 1}"
                    for new, old in enumerate(alias_order)
                }
                work = self._render(self._tree.copy(), _VALUE, param_names, alias_names)
                sql = work.sql()
                if best is None or sql < best:
                    best = sql
        assert best is not None
        return best

    def _render(
        self,
        node: exp.Expression,
        position: str,
        param_names: dict[str, str],
        alias_names: dict[str, str],
    ) -> exp.Expression:
        """Replace tokens with canonical names and sort ``AND`` items."""
        if isinstance(node, exp.Placeholder):
            self._render_placeholder(node, position, param_names)
            return node
        if isinstance(node, exp.Column):
            self._render_qualifier(node, alias_names)
            return node
        if isinstance(node, (exp.Table, exp.Subquery)):
            self._render_alias_def(node, alias_names)
            self._render_children(node, param_names, alias_names)
            return node
        if isinstance(node, exp.And):
            self._render_children(node, param_names, alias_names)
            return self._sort_and(node)
        if self._is_json_path_function(node):
            return self._render_json_function(node, param_names, alias_names)
        if isinstance(node, _JSON_EXTRACTORS):
            return self._render_json_extractor(node, param_names, alias_names)
        self._render_children(node, param_names, alias_names)
        return node

    def _render_placeholder(
        self, node: exp.Placeholder, position: str, param_names: dict[str, str]
    ) -> None:
        """Replace a binding token with its canonical name."""
        token = node.args.get("this")
        name = param_names.get(token) if isinstance(token, str) else None
        if name is not None:
            if position == _PATH and token in self._private_path_tokens:
                name += _PRIVATE_PATH_SUFFIX
            node.set("this", name)

    def _render_json_function(
        self,
        node: exp.Expression,
        param_names: dict[str, str],
        alias_names: dict[str, str],
    ) -> exp.Expression:
        """Render a function-form JSON extraction with path positions."""
        node.set(
            "expressions",
            [
                self._render(
                    arg,
                    _VALUE if index == 0 else _PATH,
                    param_names,
                    alias_names,
                )
                for index, arg in enumerate(node.expressions)
            ],
        )
        self._render_children(node, param_names, alias_names, skip={"expressions"})
        return node

    def _render_json_extractor(
        self,
        node: exp.Expression,
        param_names: dict[str, str],
        alias_names: dict[str, str],
    ) -> exp.Expression:
        """Render a JSON extractor node with its path position."""
        node.set("this", self._render(node.this, _VALUE, param_names, alias_names))
        path = node.args.get("expression")
        if path is not None:
            node.set(
                "expression",
                self._render(path, _PATH, param_names, alias_names),
            )
        self._render_children(
            node, param_names, alias_names, skip={"this", "expression"}
        )
        return node

    def _render_children(
        self,
        node: exp.Expression,
        param_names: dict[str, str],
        alias_names: dict[str, str],
        skip: Container[str] = frozenset(),
    ) -> None:
        """Recursively render every expression child of *node*."""
        for key, value in list(node.args.items()):
            if key in skip:
                continue
            if isinstance(value, exp.Expression):
                node.set(key, self._render(value, _VALUE, param_names, alias_names))
            elif isinstance(value, list):
                node.set(
                    key,
                    [
                        self._render(item, _VALUE, param_names, alias_names)
                        if isinstance(item, exp.Expression)
                        else item
                        for item in value
                    ],
                )

    @staticmethod
    def _render_qualifier(node: exp.Column, alias_names: dict[str, str]) -> None:
        """Replace a column's alias token with its canonical name."""
        qualifier = node.args.get("table")
        if isinstance(qualifier, exp.Identifier):
            canonical = alias_names.get(qualifier.this)
            if canonical is not None:
                qualifier.set("this", canonical)

    @staticmethod
    def _render_alias_def(node: exp.Expression, alias_names: dict[str, str]) -> None:
        """Replace an alias definition token with its canonical name."""
        alias = node.args.get("alias")
        if isinstance(alias, exp.TableAlias) and isinstance(alias.this, exp.Identifier):
            canonical = alias_names.get(alias.this.this)
            if canonical is not None:
                alias.this.set("this", canonical)

    @staticmethod
    def _sort_and(node: exp.And) -> exp.Expression:
        """Flatten nested ``AND`` chains and sort conjuncts canonically."""
        conjuncts: list[exp.Expression] = []

        def collect(part: exp.Expression) -> None:
            if isinstance(part, exp.And):
                collect(part.this)
                collect(part.expression)
            else:
                conjuncts.append(part)

        collect(node)
        conjuncts.sort(key=lambda conjunct: conjunct.sql())
        combined = conjuncts[0]
        for conjunct in conjuncts[1:]:
            combined = exp.And(this=combined, expression=conjunct)
        return combined
