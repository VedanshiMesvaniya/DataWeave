"""Entity/value resolver — grounds NL2SQL generation in the DB's actual data.

Problem this solves: the SQL-generation prompt (see s12b_sql_retrieval.py)
previously only ever saw table/column *names* and *types* — never what is
actually stored in them. When a person asks about a specific name, category,
or status ("sales for Reliance Digital", "orders in the Electronics
category", "customers in Gujarat"), the model had no way to know the exact
string stored in the database — casing, punctuation, abbreviation, or a
completely different label ("Electronics" vs "electronic_goods" vs "ELEC")
— so it guessed. A wrong guess produces a silent 0-row result or a WHERE
clause that matches nothing, which reads to the user as "the answer failed"
even though the data was there all along.

This module closes that gap without turning every question into a full data
dump:

1. Once per process (cached, same lifetime as SQLRetriever's schema cache),
   it samples the distinct values of columns that *look* like they hold
   names/categories/labels (heuristic on column name — see
   ``_is_entity_column``). Numeric, id, date, and amount-style columns are
   skipped, so this never tries to "sample" a price or a foreign key.
2. For each question, it matches the question's words against those sampled
   values in memory (no extra DB round trip after the first load) and
   returns only the values that actually matched something the person
   typed — capped in both column count and values-per-column so the added
   prompt cost stays small even on a wide schema.

The result is a short "Real data values matching this question" block that
_generate_sql injects into the system prompt, telling the model the exact
value(s) that exist in the data so it can write
``WHERE category = 'Electronics'`` instead of guessing at one.
"""

from __future__ import annotations

import logging
import re

from src.core.db_client import run_readonly_query
from src.core.sql_column_registry import ColumnRegistry
from src.core.sql_dialects import SQLDialectProfile

logger = logging.getLogger(__name__)

# Column-name fragments that usually hold human-readable text worth
# grounding: a name, a category/type label, a status, a place. Matched as a
# substring of the (lowercased) column name.
_ENTITY_COLUMN_HINTS = (
    "name", "title", "label", "category", "categories", "type", "status",
    "brand", "city", "state", "country", "region", "segment", "department",
    "industry", "tag", "class", "group", "code", "gender", "grade",
)

# Column-name fragments that rule a column out even if a hint above also
# matches — e.g. "type_id" contains "type" but is a foreign key, not free
# text — or that are clearly numeric/temporal/sensitive.
_EXCLUDE_COLUMN_HINTS = (
    "_id", "id_", "_at", "_date", "_time", "amount", "price", "rate",
    "qty", "quantity", "count", "total", "percent", "ratio", "number",
    "phone", "email", "url", "password", "token", "hash", "lat", "lng",
    "latitude", "longitude", "created", "updated", "deleted",
)

# Bounds that keep the initial sampling pass and the per-query prompt
# addition cheap even on a wide/many-table schema.
_MAX_COLUMNS_SAMPLED = 60
_MAX_VALUES_PER_COLUMN = 500
_MAX_MATCHED_LINES = 25
_MAX_VALUES_PER_MATCHED_LINE = 8

_STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is",
    "are", "was", "were", "be", "by", "as", "at", "no", "not", "with",
    "from", "how", "what", "which", "who", "show", "list", "give", "get",
    "find", "me", "us", "total", "count", "sum", "average", "top", "many",
    "much", "most", "least", "per", "this", "that", "all", "each", "my",
    "our", "your", "does", "did", "has", "have", "had",
})


def _is_entity_column(column_name: str) -> bool:
    name = column_name.lower()
    if any(bad in name for bad in _EXCLUDE_COLUMN_HINTS):
        return False
    return any(hint in name for hint in _ENTITY_COLUMN_HINTS)


def _quote_identifier(dialect_key: str, identifier: str) -> str:
    """Quote a table/column name for the target engine.

    Identifiers here always come from schema introspection (never straight
    from user input), but they're still escaped defensively rather than
    trusted verbatim.
    """
    if dialect_key == "mysql":
        return "`" + identifier.replace("`", "``") + "`"
    return '"' + identifier.replace('"', '""') + '"'


class EntityValueResolver:
    """Samples and caches real column values, then grounds a question
    against them.

    One instance is created per SQLRetriever (see s12b_sql_retrieval.py);
    the sampled values are cached at the class level so the (comparatively
    expensive) initial DISTINCT scan only ever runs once per process —
    mirroring how SQLRetriever._full_schema_cache works.
    """

    # {(table, column): [distinct values, sorted]}, built once and reused.
    _value_cache: dict[tuple[str, str], list[str]] | None = None

    def __init__(self, registry: ColumnRegistry | None, dialect: SQLDialectProfile) -> None:
        self._registry = registry
        self._dialect = dialect

    @classmethod
    def clear_cache(cls) -> None:
        """Drop sampled values. Call this alongside a schema/data resync so
        stale values (a renamed category, a deleted customer) don't linger."""
        cls._value_cache = None

    def _candidate_columns(self) -> list[tuple[str, str]]:
        if self._registry is None:
            return []
        candidates: list[tuple[str, str]] = []
        for table, cols in self._registry.tables_original().items():
            for col in cols:
                if _is_entity_column(col):
                    candidates.append((table, col))
                if len(candidates) >= _MAX_COLUMNS_SAMPLED:
                    return candidates
        return candidates

    async def _ensure_values_loaded(self) -> None:
        if EntityValueResolver._value_cache is not None:
            return
        cache: dict[tuple[str, str], list[str]] = {}
        for table, col in self._candidate_columns():
            try:
                q_table = _quote_identifier(self._dialect.key, table)
                q_col = _quote_identifier(self._dialect.key, col)
                sql = (
                    f"SELECT DISTINCT {q_col} AS v FROM {q_table} "
                    f"WHERE {q_col} IS NOT NULL"
                )
                rows = await run_readonly_query(sql, max_rows=_MAX_VALUES_PER_COLUMN)
                values = sorted({
                    str(r["v"]).strip()
                    for r in rows
                    if r.get("v") is not None and str(r["v"]).strip()
                })
                if values:
                    cache[(table, col)] = values
            except Exception as e:
                # A single bad/renamed/locked column must never take down
                # sampling for every other column — skip it and move on.
                logger.debug("Skipping value sample for %s.%s: %s", table, col, e)
        EntityValueResolver._value_cache = cache
        logger.info(
            "EntityValueResolver sampled %d text column(s) for value grounding.",
            len(cache),
        )

    async def resolve(self, query: str) -> str:
        """Return a prompt-ready block of real DB values that match the question.

        Empty string when nothing was sampled or nothing matched — this is
        always optional grounding context, never a required section, so an
        empty result must never block SQL generation.
        """
        if self._registry is None:
            return ""
        await self._ensure_values_loaded()
        cache = EntityValueResolver._value_cache or {}
        if not cache:
            return ""

        query_lower = query.lower()
        tokens = [
            w for w in re.findall(r"[a-z0-9]+", query_lower)
            if len(w) >= 3 and w not in _STOPWORDS
        ]
        if not tokens:
            return ""

        lines: list[str] = []
        for (table, col), values in cache.items():
            matched = [v for v in values if any(tok in v.lower() for tok in tokens)]
            if not matched:
                continue
            shown = matched[:_MAX_VALUES_PER_MATCHED_LINE]
            quoted = ", ".join(f'"{v}"' for v in shown)
            more = (
                f" (+{len(matched) - len(shown)} more matching value(s))"
                if len(matched) > len(shown)
                else ""
            )
            lines.append(f"- {table}.{col}: {quoted}{more}")
            if len(lines) >= _MAX_MATCHED_LINES:
                break

        return "\n".join(lines)
