"""Parse SQL queries and extract structural metadata.

The ``sql-metadata`` package analyses SQL statements and returns the
tables, columns, aliases, CTE definitions, subqueries, values, comments,
and query type they contain.  The primary entry point is :class:`Parser`::

    from sql_metadata import Parser

    parser = Parser("SELECT id, name FROM users WHERE active = 1")
    print(parser.tables)   # ['users']
    print(parser.columns)  # ['id', 'name', 'active']

Under the hood the library delegates to `sqlglot <https://github.com/tobymao/sqlglot>`_
for AST construction and tokenization, with custom dialect handling for
MSSQL, MySQL, Hive/Spark, and TSQL bracket notation.
"""

from sql_metadata.exceptions import InvalidQueryDefinition
from sql_metadata.keywords_lists import QueryType
from sql_metadata.merge_dry_run import (
    ACTION_DELETE,
    ACTION_DO_NOTHING,
    ACTION_INSERT,
    ACTION_NO_BRANCH,
    ACTION_UPDATE,
    MergeConflict,
    MergeConflictError,
    MergeDryRun,
    MergeEvaluationError,
    MergePlan,
    MergeSpec,
    MergeStructure,
    analyze_merge,
)
from sql_metadata.parser import Parser

__all__ = [
    "ACTION_DELETE",
    "ACTION_DO_NOTHING",
    "ACTION_INSERT",
    "ACTION_NO_BRANCH",
    "ACTION_UPDATE",
    "InvalidQueryDefinition",
    "MergeConflict",
    "MergeConflictError",
    "MergeDryRun",
    "MergeEvaluationError",
    "MergePlan",
    "MergeSpec",
    "MergeStructure",
    "Parser",
    "QueryType",
    "analyze_merge",
]
