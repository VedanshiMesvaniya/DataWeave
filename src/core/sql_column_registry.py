"""Schema-Aware Column Registry — validates generated SQL against the real schema.

Built from the same schema text that `_get_schema()` already fetches, so no
extra DB round-trips.  Two validation passes:

1. **Column validation** — every column reference in the SQL is checked against
   the actual columns on the referenced table.  Hallucinated columns are caught
   before they hit the DB, producing a clear retry message ("Column 'x' does
   not exist on table 'y'. Available columns: a, b, c").

2. **Alias validation** — flags misleading aliases where the LLM copies a word
   from the user's question as a column alias even though the underlying
   expression resolves to something semantically different.  This prevents e.g.
   `product_type_id AS technology_used` when the user asked about "technology".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of validating a generated SQL query against the schema."""

    is_valid: bool
    errors: list[str] = field(default_factory=list)
    hallucinated_columns: list[str] = field(default_factory=list)


class ColumnRegistry:
    """Auto-built index of ``table → {columns}`` from the live schema text.

    Supports both SQLite (``CREATE TABLE`` statements) and MySQL
    (``information_schema.columns`` row format produced by
    ``format_schema_rows``).
    """

    def __init__(self, schema_text: str, dialect: str) -> None:
        self._dialect = dialect
        self._tables: dict[str, set[str]] = {}  # table_name → {col_name (lower)}
        self._tables_original: dict[str, list[str]] = {}  # for display
        self._parse_schema(schema_text)

    # ------------------------------------------------------------------
    # Schema parsing
    # ------------------------------------------------------------------

    def _parse_schema(self, text: str) -> None:
        """Extract table → column mappings from the schema text."""
        if self._dialect == "sqlite":
            self._parse_sqlite(text)
        elif self._dialect == "mysql":
            self._parse_mysql(text)
        else:
            logger.warning("ColumnRegistry: unsupported dialect %r", self._dialect)

    _CREATE_RE = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"[`\"']?(\w+)[`\"']?\s*\((.*?)\)(?:\s*;)?",
        re.IGNORECASE | re.DOTALL,
    )
    _COL_RE = re.compile(
        r"^\s*[`\"']?(\w+)[`\"']?\s+\w+",
        re.MULTILINE,
    )

    def _parse_sqlite(self, text: str) -> None:
        """Parse CREATE TABLE statements (SQLite's sqlite_master format)."""
        for match in self._CREATE_RE.finditer(text):
            table = match.group(1)
            body = match.group(2)
            cols: list[str] = []
            for line in body.split(","):
                line = line.strip()
                # Skip constraints (PRIMARY KEY, FOREIGN KEY, UNIQUE, CHECK)
                if re.match(
                    r"^\s*(PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|CHECK|CONSTRAINT)\b",
                    line,
                    re.IGNORECASE,
                ):
                    continue
                col_match = self._COL_RE.match(line)
                if col_match:
                    cols.append(col_match.group(1))
            if cols:
                self._tables[table.lower()] = {c.lower() for c in cols}
                self._tables_original[table.lower()] = cols

    _MYSQL_TABLE_RE = re.compile(
        r"^TABLE\s+(\w+)\s*$",
        re.MULTILINE,
    )
    _MYSQL_COL_RE = re.compile(r"^\s+(\w+)\s+\w+", re.MULTILINE)

    def _parse_mysql(self, text: str) -> None:
        """Parse the "TABLE name\\n  col type\\n..." format from format_schema_rows.

        Matches the compact (no parens/commas/blank-line-separated) format
        introduced alongside this — see the comment in format_schema_rows for
        why. Column lines are unchanged from the previous format, so
        _MYSQL_COL_RE didn't need to change, only the table-header pattern.
        """
        blocks = re.split(r"\n(?=TABLE\s)", text)
        for block in blocks:
            table_match = self._MYSQL_TABLE_RE.match(block)
            if not table_match:
                continue
            table = table_match.group(1)
            cols: list[str] = []
            for col_match in self._MYSQL_COL_RE.finditer(block):
                cols.append(col_match.group(1))
            if cols:
                self._tables[table.lower()] = {c.lower() for c in cols}
                self._tables_original[table.lower()] = cols

    # ------------------------------------------------------------------
    # Column validation
    # ------------------------------------------------------------------

    def tables_original(self) -> dict[str, list[str]]:
        """Read-only view of ``table (lower) -> [column names, original case]``.

        Used by EntityValueResolver (src/core/value_resolver.py) to pick
        which columns are worth sampling real distinct values from, without
        that module needing its own copy of the schema-parsing logic.
        """
        return self._tables_original

    def has_column(self, table: str, column: str) -> bool:
        """True if `column` is a known column of `table` in the live schema.

        Returns True (assume valid, don't block) when `table` itself isn't
        in the registry — this method exists to filter out *provably* stale
        external data (e.g. a glossary entry referencing a renamed/dropped
        column), not to validate arbitrary SQL, so an unrecognized table
        must never produce a false "stale" verdict.
        """
        known_cols = self._tables.get(table.lower())
        if known_cols is None:
            return True
        return column.lower() in known_cols

    def validate_columns(self, sql: str) -> ValidationResult:
        """Check every column reference in the SQL against the schema.

        Returns a ValidationResult with specific error messages for any
        hallucinated columns, including the list of real columns available.
        """
        if not self._tables:
            # No schema loaded — skip validation rather than blocking everything.
            return ValidationResult(is_valid=True)

        try:
            ast = sqlglot.parse_one(sql, read=self._dialect)
        except Exception:
            # If it doesn't parse, _is_safe_read_query will catch it anyway.
            return ValidationResult(is_valid=True)

        # Build a map of alias → real table name from the query's FROM/JOIN.
        table_aliases = self._resolve_table_aliases(ast)
        # Column aliases declared in any SELECT list (e.g. `SUM(x) AS total`).
        # Standard SQL allows referencing these, unqualified, from ORDER BY,
        # GROUP BY, and HAVING in the same query — e.g.
        # `SELECT SUM(qty) AS total ... ORDER BY total DESC`. Without this,
        # every query that ranks/sorts by an aggregate alias (an extremely
        # common, perfectly valid pattern) gets rejected as a "hallucinated
        # column" even though no real database would reject it.
        select_aliases = self._collect_select_aliases(ast)

        errors: list[str] = []
        hallucinated: list[str] = []

        for col_node in ast.find_all(exp.Column):
            col_name = (col_node.name or "").strip()
            if not col_name:
                continue

            # Resolve the table for this column.
            table_ref = col_node.table
            if table_ref:
                # Qualified: table.column or alias.column
                real_table = table_aliases.get(table_ref.lower(), table_ref.lower())
                known_cols = self._tables.get(real_table)
                if known_cols is not None and col_name.lower() not in known_cols:
                    display_cols = self._tables_original.get(real_table, sorted(known_cols))
                    # Show a useful subset, not hundreds of columns.
                    shown = display_cols[:20]
                    suffix = f" (+{len(display_cols) - 20} more)" if len(display_cols) > 20 else ""
                    errors.append(
                        f"Column '{col_name}' does not exist on table '{real_table}'. "
                        f"Available columns: {', '.join(shown)}{suffix}"
                    )
                    hallucinated.append(f"{real_table}.{col_name}")
            else:
                # Unqualified: check against all tables in the query's FROM/JOIN,
                # unless it's actually a reference to a SELECT-list alias (never
                # qualified with a table, so only relevant to this branch).
                if col_name.lower() in select_aliases:
                    continue
                from_tables = set(table_aliases.values())
                if from_tables:
                    found_in_any = any(
                        col_name.lower() in (self._tables.get(t) or set())
                        for t in from_tables
                    )
                    if not found_in_any:
                        # In SQLite, a double-quoted token (e.g. WHERE status = "completed")
                        # is evaluated as a string literal by SQLite's legacy
                        # double-quote fallback whenever it's compared against a
                        # genuine, known column — sqlglot still parses it as an
                        # exp.Column, so without this check it's flagged as a
                        # hallucinated column even though it's a valid literal.
                        if self._dialect == "sqlite" and self._is_sqlite_literal_fallback(
                            col_node, from_tables, table_aliases
                        ):
                            continue
                        table_list = ", ".join(sorted(from_tables))
                        errors.append(
                            f"Column '{col_name}' not found in any of the query's tables "
                            f"({table_list}). Check spelling or qualify with table name."
                        )
                        hallucinated.append(col_name)

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            hallucinated_columns=hallucinated,
        )

    def _is_sqlite_literal_fallback(
        self,
        col_node: exp.Column,
        from_tables: set[str],
        table_aliases: dict[str, str],
    ) -> bool:
        """True only if col_node is a double-quoted token being compared
        against a real, known column — SQLite's legacy double-quote-as-string
        fallback applies in exactly that shape, and nowhere else (a bare
        projection or a comparison against a non-column stays flagged)."""
        if not getattr(col_node.this, "quoted", False):
            return False
        if col_node.table:
            return False

        def _is_known_col(node) -> bool:
            if not isinstance(node, exp.Column):
                return False
            t_ref = node.table
            c_name = (node.name or "").lower()
            if t_ref:
                real_t = table_aliases.get(t_ref.lower(), t_ref.lower())
                return c_name in (self._tables.get(real_t) or set())
            return any(c_name in (self._tables.get(t) or set()) for t in from_tables)

        parent = col_node.parent
        if isinstance(parent, (exp.Binary, exp.Predicate, exp.Like, exp.ILike)):
            other = parent.expression if col_node is parent.this else getattr(parent, "this", None)
            return _is_known_col(other)
        elif isinstance(parent, exp.In):
            if col_node is not parent.this:
                return _is_known_col(parent.this)
        return False

    def _collect_select_aliases(self, ast: exp.Expression) -> set[str]:
        """Collect every SELECT-list alias name declared anywhere in the query.

        Covers all SELECT scopes (subqueries, CTEs) rather than trying to match
        aliases to their exact scope — over-permissive in the rare case a
        WHERE clause misuses an alias from an unrelated scope (invalid SQL
        that would still be caught at DB-execution time and fed back on
        retry), in exchange for never false-flagging the common, valid
        ORDER BY / GROUP BY / HAVING alias reference this exists to fix.
        """
        aliases: set[str] = set()
        for select_node in ast.find_all(exp.Select):
            for projection in select_node.expressions:
                if isinstance(projection, exp.Alias) and projection.alias:
                    aliases.add(projection.alias.lower())
        return aliases

    def _resolve_table_aliases(self, ast: exp.Expression) -> dict[str, str]:
        """Build alias → real_table_name mapping from the query's FROM/JOIN."""
        aliases: dict[str, str] = {}
        for table_node in ast.find_all(exp.Table):
            real_name = (table_node.name or "").lower()
            alias = table_node.alias
            if alias:
                aliases[alias.lower()] = real_name
            if real_name:
                aliases[real_name] = real_name
        return aliases

    # ------------------------------------------------------------------
    # Alias validation
    # ------------------------------------------------------------------

    # Words too common to flag as "copied from the question".
    _STOP_WORDS = frozenset({
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "shall",
        "should", "may", "might", "must", "can", "could", "of", "in", "to",
        "for", "with", "on", "at", "from", "by", "as", "into", "through",
        "and", "or", "but", "not", "no", "all", "each", "every", "both",
        "how", "what", "which", "who", "whom", "this", "that", "these",
        "those", "my", "our", "your", "its", "their", "total", "count",
        "sum", "average", "max", "min", "many", "much", "most", "least",
        "top", "bottom", "first", "last", "per", "by", "show", "list",
        "get", "find", "give", "me", "us", "we", "i",
    })

    # Words that describe the same financial/volume metric family regardless
    # of which underlying column they're computed from — an alias and its
    # underlying column both drawing from this set is a legitimate pairing
    # (e.g. "total_revenue" aliasing an "amount" column), not a sign the
    # alias was invented from the question.
    _METRIC_WORDS = frozenset({
        "revenue", "sales", "amount", "total", "spent", "cost", "value",
        "turnover", "price", "count", "quantity", "qty", "sum", "avg",
        "quoted", "billed", "invoiced",
    })

    def validate_aliases(self, sql: str, question: str) -> list[str]:
        """Flag aliases that appear to be copied from the question rather than
        derived from what the column actually contains.

        Only flags when ALL of:
        1. The alias text is NOT a real column name anywhere in the schema.
        2. A significant word in the alias appears in the question.
        3. The underlying column's real name is semantically different.
        """
        try:
            ast = sqlglot.parse_one(sql, read=self._dialect)
        except Exception:
            return []

        # All known column names across the entire schema (lower).
        all_columns = set()
        for cols in self._tables.values():
            all_columns.update(cols)

        question_words = {
            w.lower()
            for w in re.findall(r"\w+", question)
            if w.lower() not in self._STOP_WORDS and len(w) > 2
        }

        warnings: list[str] = []
        for alias_node in ast.find_all(exp.Alias):
            alias_name = alias_node.alias
            if not alias_name or not isinstance(alias_name, str):
                continue

            alias_lower = alias_name.lower()

            # Skip if the alias IS a real column name — it's descriptive by definition.
            if alias_lower in all_columns:
                continue

            # Check if the alias overlaps with question words.
            # Split on non-alpha (incl. underscores) so compound aliases like
            # "technology_used" match question words "technology" and "used".
            alias_words = {
                w.lower()
                for w in re.split(r"[^a-zA-Z]+", alias_name)
                if w.lower() not in self._STOP_WORDS and len(w) > 2
            }
            overlap = alias_words & question_words
            if not overlap:
                continue

            # Get the underlying expression's column name.
            child = alias_node.this
            underlying = ""
            if isinstance(child, exp.Column):
                underlying = child.name or ""
            elif isinstance(child, (exp.Sum, exp.Count, exp.Max, exp.Min, exp.Avg)):
                # Aggregate — underlying is the aggregated column.
                inner = child.this
                if isinstance(inner, exp.Column):
                    underlying = inner.name or ""

            if not underlying:
                continue

            # If the underlying column name is very different from the alias,
            # and the alias looks like it was lifted from the question — flag it.
            underlying_words = set(re.findall(r"\w+", underlying.lower()))
            if alias_words & underlying_words:
                continue

            # A financial/volume metric alias (e.g. "total_revenue" on an
            # "amount" column) legitimately describes the aggregated value
            # without sharing any literal word with the raw column name —
            # that's not a hallucination signal, it's exactly what a good
            # alias should do. Only suppress the warning when BOTH sides
            # reference the same metric family, so an unrelated relabeling
            # (e.g. calling a customer_id column "total_revenue") still gets
            # flagged.
            if (alias_words & self._METRIC_WORDS) and (underlying_words & self._METRIC_WORDS):
                continue

            warnings.append(
                f"Alias '{alias_name}' on column '{underlying}' appears derived "
                f"from the question, not the data. Use a descriptive alias based "
                f"on the actual column (e.g. '{underlying}' or "
                f"'{underlying.replace('_id', '_name')}')."
            )

        return warnings
