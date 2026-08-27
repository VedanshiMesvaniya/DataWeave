"""Stage 12b — Text-to-SQL Retrieval.

Dynamically translates natural language into SQL against the live database,
executes it, and returns the results formatted as a context chunk.
"""

from __future__ import annotations

from collections import OrderedDict
import functools
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from src.core.config import settings
from src.core.db_client import run_readonly_query
from src.core.pipeline_metrics import log_event as _log_pipeline_event
from src.core.provider_client import ProviderRouter
from src.core.sql_column_registry import ColumnRegistry
from src.core.sql_dialects import SQLDialectProfile, get_dialect_profile
from src.core.value_resolver import EntityValueResolver
from src.models.schemas import Chunk, ChunkType, RetrievedChunk, DocumentType
from src.stages.s10_embeddings import EmbeddingService
from src.stages.s11_vector_store import QdrantStore

logger = logging.getLogger(__name__)


def format_schema_rows(profile: SQLDialectProfile, rows: list[dict[str, Any]]) -> str:
    """Turn an engine's raw introspection rows into schema text for the NL2SQL prompt.

    Pure function of (dialect profile, rows) — no instance state, no global 
    settings — so it can be unit tested directly for each engine.

    SQLite's sqlite_master query already returns one full CREATE TABLE
    statement per row. MySQL's information_schema.columns query returns
    one row per column, so those need grouping by table first.
    """
    if profile.key == "sqlite":
        return "\n\n".join(
            row["sql"] for row in rows if row["name"] != "sqlite_sequence"
        )

    if profile.key == "mysql":
        tables: dict[str, list[str]] = {}
        for row in rows:
            comment = row.get("column_comment") or ""
            suffix = f"  -- {comment}" if comment else ""
            tables.setdefault(row["table_name"], []).append(
                f"  {row['column_name']} {row['data_type']}{suffix}"
            )
        # One line per table header + one line per column, no wrapping
        # parens/commas/blank-line separators. Purely a formatting change —
        # every table, column, type, and comment is still present in full —
        # that trims the fixed per-call token cost of a large schema (~10%
        # measured on a 68-table schema) without hiding anything from the
        # model the way a smaller top_k selection would.
        return "\n".join(
            f"TABLE {name}\n" + "\n".join(cols)
            for name, cols in tables.items()
        )

    raise ValueError(f"Unsupported dialect key {profile.key!r}")


def format_fk_rows(rows: list[dict]) -> str:
    if not rows:
        return ""

    lines = [
        f"  {r['table_name']}.{r['column_name']} -> {r['referenced_table_name']}.{r['referenced_column_name']}"
        for r in rows
    ]
    return "Foreign Keys:\n" + "\n".join(lines)


class UnsafeQueryError(Exception):
    """Raised when sqlglot rejects a query (e.g. not a SELECT). Never retried."""
    pass


_ABSTAIN_RE = re.compile(r"^\W*no_sql\b", re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_SQL_START_RE = re.compile(r"\b(SELECT|WITH)\b", re.IGNORECASE)
_SIMPLE_TABLE_COLUMN_RE = re.compile(r"^([A-Za-z_]\w*)\.([A-Za-z_]\w*)$")


def _unwrap_sql(text: str) -> str:
    """Extract the SQL from an LLM response that may wrap it in a markdown code
    fence or precede it with a prose line ("Here is the query: SELECT ...").

    Only the minimal, safe extractions are performed:
      * strip any <think>...</think> reasoning block a thinking-capable model
        may emit despite being told not to (belt-and-suspenders alongside the
        enable_thinking=false / max_tokens floor set in ProviderRouter);
      * a fenced ```sql ... ``` (or bare ``` ... ```) block anywhere in the reply;
      * otherwise, if a leading prose preamble sits before the first SELECT/WITH,
        drop the preamble so the query still parses instead of being rejected as
        unsafe and silently falling back to document search.

    Without this, a well-formed query decorated with a stray sentence would fail
    _is_safe_read_query and make a genuine SQL question answer document-only.
    """
    cleaned = re.sub(r"(?s)<think>.*?</think>", "", text).strip()
    if not cleaned and "</think>" in text:
        cleaned = text.split("</think>")[-1].strip()
    text = cleaned if cleaned else text.strip()

    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    km = _SQL_START_RE.search(text)
    if km and km.start() > 0:
        return text[km.start():].strip()
    return text.strip()


def _is_all_null(rows: list[dict[str, Any]]) -> bool:
    """True for a single row whose every column is NULL — the shape an aggregate
    (SUM/MAX/MIN/AVG) returns when its WHERE clause matches nothing.

    Multi-row results, or any row with at least one non-NULL value, are real
    data and return False. COUNT(*) over no matches yields 0 (not NULL), so this
    never collapses a legitimate "0 of X" answer.
    """
    return len(rows) == 1 and all(v is None for v in rows[0].values())


def _is_aggregate_over_zero_rows(sql: str, rows: list[dict[str, Any]], dialect: str) -> bool:
    """Stricter version of the all-NULL check above: also confirms the query
    actually contains a top-level aggregate (SUM/AVG/MIN/MAX/etc.) with no
    GROUP BY before treating an all-NULL row as "zero matches".

    _is_all_null alone over-collapses: a genuine non-aggregate row where the
    selected column happens to be NULL (e.g. `SELECT discount_code FROM
    orders WHERE id = 123` when discount_code is legitimately unset) has the
    same one-row-all-NULL shape as an aggregate-over-nothing, but is real
    data, not an empty result. Falls back to False (never collapses) if the
    SQL can't be parsed, which is the safe direction — worst case a real
    empty-aggregate result is treated as a normal row instead of triggering
    the retry/fallback path.
    """
    if not _is_all_null(rows):
        return False
    try:
        ast = sqlglot.parse_one(sql, read=dialect)
        has_agg = any(
            isinstance(n, (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.AggFunc))
            for n in ast.find_all(exp.Func)
        )
        has_group = ast.find(exp.Group) is not None
        return has_agg and not has_group
    except Exception:
        return False


def _extract_table_names(sql: str, dialect: str) -> list[str]:
    try:
        ast = sqlglot.parse_one(sql, read=dialect)
        return sorted({t.name for t in ast.find_all(exp.Table)})
    except Exception:
        return []


@functools.lru_cache(maxsize=1)
def _load_relationships() -> str:
    """Load the inferred join map from disk, cached for process lifetime.

    Databases without explicit FOREIGN KEY constraints give the SQL-generation
    model no way to know how tables join, so it guesses — producing errors like
    `Unknown column 'p.product_color_id' in 'on clause'` (the column lives on the
    line-item tables and joins to product_color, not on product). Feeding an
    explicit join map into the prompt removes that guesswork.

    Formatted one line per source table for compactness:
        - sales_order_products: sales_order_id->sales_order.id, product_id->product.id, ...

    Returns "" when no relationships file is present (e.g. a deployment whose DB
    has real FK constraints and needs no inferred map), so injection is opt-in.
    """
    path = Path(__file__).resolve().parents[2] / "config" / "sql_relationships.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rels = data.get("relationships") if isinstance(data, dict) else data
        if not rels:
            return ""
        grouped: dict[str, list[str]] = {}
        for r in rels:
            frm, fcol = r.get("from_table"), r.get("from_column")
            to, tcol = r.get("to_table"), r.get("to_column")
            if not all((frm, fcol, to, tcol)):
                continue
            grouped.setdefault(frm, []).append(f"{fcol}->{to}.{tcol}")
        return "\n".join(
            f"- {table}: {', '.join(edges)}" for table, edges in sorted(grouped.items())
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return ""


@functools.lru_cache(maxsize=1)
@functools.lru_cache(maxsize=1)
def _load_glossary_lines() -> tuple[str, ...]:
    """Load SQL glossary lines from disk, cached for process lifetime. ARCH-9.

    Returns one formatted line per concept ("- concept: syn1, syn2, ...") so
    callers can join them for the full glossary or filter them by query
    relevance (see _build_business_glossary_for_query) without re-parsing
    the JSON file each time.
    """
    path = Path(__file__).resolve().parents[2] / "config" / "sql_glossary.json"
    try:
        groups = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(groups, dict) or not groups:
            return ()

        lines: list[str] = []
        for concept, syns in groups.items():
            if isinstance(syns, str):
                synonym_text = syns
            elif isinstance(syns, list):
                synonym_text = ", ".join(str(item) for item in syns if str(item).strip())
            else:
                synonym_text = str(syns)

            synonym_text = synonym_text.strip()
            if synonym_text:
                lines.append(f"- {concept}: {synonym_text}")
        return tuple(lines)
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return ()


def _load_glossary() -> str:
    """Full business-term glossary as one text block (all concepts)."""
    return "\n".join(_load_glossary_lines())


def _build_business_glossary_for_query(query: str) -> str:
    """Filter the business-term glossary to concepts relevant to the query.

    This glossary (customer/client/buyer, product/item, ranking_top/most,
    etc.) was previously sent in full on every call regardless of the
    question — a large fixed token cost (~1,500 tokens measured) even though
    most queries only need a handful of its ~30+ concepts. This filters it
    the same deterministic way _build_column_glossary_for_query already
    filters the column glossary: keyword/synonym matching against the query,
    not embedding/vector similarity — so there's no silent-omission risk
    from a table or concept ranking below some fuzzy cutoff.

    Falls back to the FULL glossary whenever fewer than 2 concepts match.
    The glossary's whole purpose is covering informal/ambiguous phrasing the
    model wouldn't otherwise resolve, so a query that matches almost nothing
    is exactly the case where narrowing it down is least safe — that's a
    signal to show everything, not a reason to guess harder.
    """
    lines = _load_glossary_lines()
    if not lines:
        return ""

    query_lower = query.lower()
    query_words = _glossary_meaningful_words(query_lower)

    matched: list[str] = []
    for line in lines:
        # line format: "- concept: syn1, syn2, ..."
        body = line[2:]  # strip leading "- "
        concept, _, synonym_text = body.partition(":")
        candidates = [concept.strip()] + [s.strip() for s in synonym_text.split(",")]
        if any(
            cand and _term_matches_query(cand.lower(), query_lower, query_words)
            for cand in candidates
        ):
            matched.append(line)

    if len(matched) < 2:
        return "\n".join(lines)
    return "\n".join(matched)


@functools.lru_cache(maxsize=1)
def _get_raw_column_glossary() -> dict:
    """Load column-mapped glossary dict from disk."""
    path = Path(__file__).resolve().parents[2] / "config" / "sql_column_glossary.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


# Generic filler words excluded from glossary word-overlap matching. Without
# this, a compound term like "lead_interested_in" can match a query purely
# because both share the word "in" (as in "...in 2023"), producing a
# confident-looking but unrelated glossary hit. See _term_matches_query.
_GLOSSARY_STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "was", "were", "be", "by", "as", "at", "no", "not", "with", "from", "id",
})


def _glossary_meaningful_words(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9]+", text.lower())
        if len(w) >= 3 and w not in _GLOSSARY_STOPWORDS
    }


def _term_matches_query(term_lower: str, query_lower: str, query_words: set[str]) -> bool:
    """Whole-word phrase match, or majority word-overlap for multi-word terms.

    Uses a word-boundary regex (not plain substring) so a short term like
    "rate" can't match inside an unrelated word like "corporate", and strips
    stopwords before the overlap check so a single shared filler word (e.g.
    "in") can't trigger a match on an otherwise-unrelated compound term.
    """
    if re.search(rf"\b{re.escape(term_lower)}\b", query_lower):
        return True
    if " " in term_lower:
        term_words = _glossary_meaningful_words(term_lower)
        if not term_words:
            return False
        overlap = term_words & query_words
        return len(overlap) >= max(1, len(term_words) // 2)
    return False


def _build_column_glossary_for_query(query: str) -> str:
    """Filter the glossary to only include terms relevant to the query to save tokens.

    Matches against both the glossary key AND its listed synonyms — e.g. the
    query word "client" now matches the "customer" entry via its synonym
    list, instead of requiring the literal word "customer". Matching ignores
    stopwords/short filler words so a lone shared word like "in" can no
    longer trigger a false match on an unrelated compound column name.

    Every simple `table.column` mapping is also cross-checked against the
    live-schema ColumnRegistry (the same registry that validates generated
    SQL) before being included. The config file is static and can drift out
    of sync with the real database (a view's columns get renamed, a table
    gets dropped, etc.) — without this check a stale entry confidently tells
    the model to use a column that doesn't exist, the model complies, and
    the query is rejected by validation on every attempt. This makes the
    glossary self-correcting against whatever DB is actually connected,
    instead of requiring the config file to be hand-audited after every
    schema change.
    """
    data = _get_raw_column_glossary()
    if not data:
        return ""

    query_lower = query.lower()
    query_words = _glossary_meaningful_words(query_lower)
    registry = SQLRetriever._column_registry

    lines: list[str] = []
    for term, details in data.items():
        term_lower = term.lower().replace("_", " ")
        is_match = _term_matches_query(term_lower, query_lower, query_words)

        if not is_match:
            for syn in details.get("synonyms", []) or []:
                syn_lower = str(syn).lower().strip()
                if syn_lower and _term_matches_query(syn_lower, query_lower, query_words):
                    is_match = True
                    break

        if is_match:
            maps_to = details.get("maps_to")
            if maps_to:
                m = _SIMPLE_TABLE_COLUMN_RE.match(maps_to.strip())
                if m and registry is not None and not registry.has_column(m.group(1), m.group(2)):
                    logger.warning(
                        "Stale glossary entry dropped: '%s' -> '%s' does not "
                        "exist in the live schema.", term, maps_to,
                    )
                    _log_pipeline_event(
                        "stale_glossary_entry_dropped",
                        {"term": term, "maps_to": maps_to},
                        query=query,
                    )
                    continue
                note = details.get("note", "")
                note_str = f" ({note})" if note else ""
                lines.append(f'- "{term}" → {maps_to}{note_str}')

    return "\n".join(lines)


_MAX_RESULT_CACHE_ENTRIES = 256


class SQLRetriever:
    """Generates and executes SQL queries for analytical questions."""
    _full_schema_cache: str | None = None
    _column_registry: ColumnRegistry | None = None
    # Sent in full on every SQL-generation call (both retry attempts), so an
    # uncapped schema on a wide/many-table database inflates every single
    # query's input tokens, not just one answer. See _get_schema for why the
    # whole schema is sent instead of a smaller vector-selected subset.
    _MAX_SCHEMA_CHARS = 30000
    # Caches the full retrieve() result, keyed on the normalized question text.
    # Bounded to _MAX_RESULT_CACHE_ENTRIES with LRU eviction (oldest entry
    # dropped first) so a long-running process with many distinct questions
    # can't grow this dict without bound.
    _result_cache: "OrderedDict[str, tuple[float, list[RetrievedChunk]]]" = OrderedDict()

    def __init__(
        self,
        router: ProviderRouter,
        vector_store: QdrantStore | None = None,
        embedding_service: EmbeddingService | None = None,
    ) -> None:
        self._router = router
        self._vector_store = vector_store
        self._embeddings = embedding_service
        self._dialect = get_dialect_profile(settings.db_engine)
        self._glossary = _load_glossary()
        self._relationships = _load_relationships()
        # Set when a run returned no rows because the LLM providers needed to
        # GENERATE the SQL were all unreachable (rate-limited / dead models) —
        # NOT because the question had no DB answer. Lets the pipeline tell the
        # user "the model was unavailable" instead of the misleading "not in my
        # documents". One SQLRetriever exists per request (QueryPipeline is
        # per-request), so this is not shared across concurrent queries.
        self.last_infra_error: str | None = None
        # Informational status of the most recent retrieve() call: "success",
        # "empty_result", "not_applicable" (schema had nothing relevant / model
        # abstained), or "failed" (validation/execution error or blocked as
        # unsafe). Not used to change control flow today — kept for downstream
        # callers/telemetry that want to distinguish these cases.
        self.last_query_status: str | None = None

    @classmethod
    def clear_result_cache(cls) -> None:
        """Clear the cached query results and sampled real-data values
        (e.g. after a schema/data sync). Also drops the full-schema cache
        and the entity-value cache, so the next request re-reads the live
        DB instead of grounding against stale schema or stale sampled
        values."""
        cls._result_cache.clear()
        cls._full_schema_cache = None
        cls._column_registry = None
        EntityValueResolver.clear_cache()

    async def retrieve(self, query: str) -> list[RetrievedChunk]:
        """Convert NL to SQL, execute, and return formatted results (with 1 retry)."""
        self.last_infra_error = None
        self.last_query_status = None
        cache_key = query.strip().lower()
        cached = SQLRetriever._result_cache.get(cache_key)
        if cached is not None:
            cached_at, cached_chunks = cached
            if time.monotonic() - cached_at < settings.sql_result_cache_ttl_seconds:
                logger.info("SQL result cache hit for query: %s", query)
                SQLRetriever._result_cache.move_to_end(cache_key)
                self.last_query_status = "success"
                # Deep-copy so a caller mutating the returned chunk (e.g. the
                # pipeline attaching per-request metadata) can never poison
                # what's stored in the shared cache.
                return [c.model_copy(deep=True) for c in cached_chunks]

        result = await self._retrieve_uncached(query)
        # Never cache an empty result that was caused by a provider outage — the
        # next attempt (once a model is reachable again) must be free to retry
        # instead of being pinned to this transient failure for the cache TTL.
        if self.last_infra_error is None:
            while len(SQLRetriever._result_cache) >= _MAX_RESULT_CACHE_ENTRIES:
                SQLRetriever._result_cache.popitem(last=False)
            SQLRetriever._result_cache[cache_key] = (
                time.monotonic(),
                [c.model_copy(deep=True) for c in result],
            )
        return result

    async def _retrieve_uncached(self, query: str) -> list[RetrievedChunk]:
        schema = await self._get_schema(query)
        if not schema:
            self.last_query_status = "not_applicable"
            _log_pipeline_event("sql_schema_empty", {}, query=query)
            return []

        last_error = None
        for attempt in range(3):
            sql = await self._generate_sql(query, schema, last_error)
            if not sql:
                self.last_query_status = "not_applicable" if self.last_infra_error is None else "failed"
                return []

            try:
                # --- Column validation (catches hallucinated columns before DB) ---
                if SQLRetriever._column_registry:
                    validation = SQLRetriever._column_registry.validate_columns(sql)
                    if not validation.is_valid:
                        logger.warning("Column validation failed: %s", validation.errors)
                        _log_pipeline_event(
                            "column_hallucination_caught",
                            {"sql": sql, "hallucinated": validation.hallucinated_columns,
                             "errors": validation.errors},
                            query=query,
                        )
                        last_error = "Column validation failed:\n" + "\n".join(validation.errors)
                        continue  # retry with feedback

                    # Alias validation (first attempt only — don't loop forever)
                    if attempt == 0:
                        alias_warnings = self._column_registry.validate_aliases(sql, query)
                        if alias_warnings:
                            logger.warning("Alias validation: %s", alias_warnings)
                            _log_pipeline_event(
                                "alias_hallucination_caught",
                                {"sql": sql, "warnings": alias_warnings},
                                query=query,
                            )
                            last_error = "Alias quality issue:\n" + "\n".join(alias_warnings)
                            continue

                # --- Safety validation (AST parsing) ---
                if not self._is_safe_read_query(sql):
                    _log_pipeline_event(
                        "unsafe_sql_blocked",
                        {"sql": sql},
                        query=query,
                    )
                    raise UnsafeQueryError(f"Unsafe or unparseable SQL generated: {sql}")

                # Execute
                rows = await run_readonly_query(sql)

                # An aggregate over zero matching rows (SUM/MAX/MIN/AVG ... WHERE
                # <no match>) does NOT come back empty — SQL returns a single row
                # whose aggregate columns are all NULL (e.g. [{"total": None}]).
                # Treating that as a real result surfaces a meaningless
                # "total: None" answer AND suppresses the document fallback, so
                # it reads as a confident (wrong) "no data" instead of deferring
                # to the documents. Collapse it to the empty case so it takes the
                # same retry-then-fallback path as a genuine 0-row result.
                # COUNT(*) returns 0 (not NULL) for no matches, so a legitimate
                # "there are 0 of X" answer is preserved and never collapsed.
                if _is_aggregate_over_zero_rows(sql, rows, self._dialect.sqlglot_dialect):
                    rows = []

                # An empty result is ambiguous: it can mean the query is correct
                # and the true answer is "none", or that a wrong JOIN/WHERE
                # silently matched nothing (MySQL doesn't error on that, it just
                # returns 0 rows). Give the model one retry with that context on
                # the first attempt. See below for what happens if it's still
                # empty after the retry.
                if not rows and attempt == 0:
                    last_error = (
                        "Query executed successfully but returned 0 rows. If that's "
                        "surprising given the question, double-check your JOIN "
                        "conditions reference the correct foreign key columns."
                    )
                    continue


                # An empty result on the FINAL attempt is treated as "the SQL
                # path found nothing" and falls through to document search —
                # not returned as an answer chunk. A wrong JOIN/WHERE also
                # produces 0 rows (MySQL doesn't error on that), so trusting an
                # empty result as authoritative risks a confident "no data"
                # answer overriding a correct document-based one. Returning []
                # here mirrors the UnsafeQueryError and exhausted-retry paths
                # below — SQL only ever contributes a chunk when it found rows.
                if not rows:
                    logger.info(
                        "SQL query returned 0 rows after retry — falling back "
                        "to document search."
                    )
                    self.last_query_status = "empty_result"
                    return []

                tables = _extract_table_names(sql, self._dialect.sqlglot_dialect)
                label = f"live_database ({', '.join(tables)})" if tables else "live_database"

                formatted_table = self._format_rows_as_markdown(rows, sql)

                # Wrap in a RetrievedChunk
                chunk = Chunk(
                    chunk_id="live_sql_001",
                    document_id="live_db",
                    chunk_type=ChunkType.SQL_RESULT,
                    content=formatted_table,
                    document_type=DocumentType.GENERAL,
                    source_file=label,
                )

                _log_pipeline_event(
                    "sql_success",
                    {"sql": sql, "row_count": len(rows), "tables": tables,
                     "attempt": attempt + 1},
                    query=query,
                )
                self.last_query_status = "success"
                return [RetrievedChunk(chunk=chunk, score=1.0, retrieval_method="text-to-sql")]

            except UnsafeQueryError as e:
                # Security violations die instantly. No feedback loop.
                logger.warning(f"Blocked unsafe SQL query: {e}")
                self.last_query_status = "failed"
                return []
            except Exception as e:
                logger.error(f"SQL Execution failed on attempt {attempt + 1}: {e}")
                _log_pipeline_event(
                    "execution_error_caught",
                    {"sql": sql, "error": str(e), "attempt": attempt + 1},
                    query=query,
                )
                last_error = str(e)
                
        # If we exhausted retries, fail cleanly
        logger.warning("SQL generation failed after retry loop. Returning empty results.")
        self.last_query_status = "failed"
        _log_pipeline_event("retry_exhausted", {"last_error": last_error}, query=query)
        return []

    async def _fetch_full_schema(self) -> str:
        """Fetch the full, un-truncated DB schema to initialize the ColumnRegistry."""
        if SQLRetriever._full_schema_cache is not None:
            return SQLRetriever._full_schema_cache

        try:
            rows = await run_readonly_query(self._dialect.schema_query, max_rows=20000)
            schema = format_schema_rows(self._dialect, rows)

            if self._dialect.key == "mysql" and self._dialect.fk_query:
                fk_rows = await run_readonly_query(self._dialect.fk_query, max_rows=20000)
            elif self._dialect.key == "sqlite":
                fk_rows = await fetch_sqlite_foreign_keys()
            else:
                fk_rows = []

            fk_text = format_fk_rows(fk_rows)
            full_schema = schema + ("\n\n" + fk_text if fk_text else "")

            SQLRetriever._full_schema_cache = full_schema
            # Build the column registry from the complete schema text so it can
            # validate any generated SQL without truncation blindness.
            try:
                SQLRetriever._column_registry = ColumnRegistry(
                    full_schema, self._dialect.sqlglot_dialect
                )
            except Exception as reg_err:
                logger.warning("Failed to build column registry: %s", reg_err)

            return full_schema
        except Exception as e:
            logger.error("Failed to fetch full schema: %s", e)
            return ""

    async def _get_schema(self, query: str) -> str:
        """Fetch the full DB schema (cached, capped) for the SQL-generation prompt.

        Always sends the complete live schema rather than a vector-selected
        subset of it. An earlier version of this method used Schema RAG —
        embedding the question and searching pre-chunked schema text in
        Qdrant for the top_k=7 most relevant tables — to keep the prompt
        small. That trades away reliability for it: a table the question
        actually needs can rank below the cutoff (or never get indexed in
        the first place if the schema-chunk ingestion drifts out of sync
        with the live DB), and the model then has no way to know the table
        exists — it just answers NO_SQL or guesses wrong, with no visible
        signal of what was omitted. Querying the live schema directly avoids
        both failure modes and is also one fewer round-trip (no embed +
        vector search) per question. The tradeoff is a larger prompt, which
        is a far cheaper cost than a wrong or empty answer.
        """
        full_schema = await self._fetch_full_schema()
        if len(full_schema) > self._MAX_SCHEMA_CHARS:
            logger.warning(
                "Schema text (%d chars) exceeds cap - truncating to %d chars "
                "for the SQL-generation prompt.",
                len(full_schema), self._MAX_SCHEMA_CHARS,
            )
            return full_schema[: self._MAX_SCHEMA_CHARS] + "\n-- (schema truncated)"
        return full_schema

    async def _generate_sql(self, query: str, schema: str, last_error: str | None = None) -> str:
        """Prompt the reasoning LLM to generate SQL."""
        system_prompt = f"""You are a {self._dialect.name} expert. 
Given the following database schema, generate a highly optimized {self._dialect.name} SELECT statement to answer the user's question.

If nothing in this schema - no table or column - answers ANY part of the
question, respond with exactly the single word NO_SQL and nothing else.

If the question has multiple parts and only SOME relate to this schema,
IGNORE the unrelated parts and write a query for only the part(s) this
schema can answer. Do not try to combine unrelated concepts into one query,
and do not abstain just because part of the question is out of scope -
only respond NO_SQL if NONE of the parts are answerable here.

Return ONLY the raw SQL query, no markdown formatting, no explanations, no backticks.

Schema:
{schema}
"""
        if self._relationships:
            system_prompt += (
                "\n\nTable relationships (this database has NO foreign-key "
                "constraints — use ONLY these join paths, and never join on or "
                "select a column that a table's schema above does not list):\n"
                f"{self._relationships}\n"
            )
        system_prompt += self._OUTPUT_READABILITY_RULES

        column_glossary = _build_column_glossary_for_query(query)
        if column_glossary:
            system_prompt += (
                "\n\nColumn mapping (use these exact paths — do NOT invent columns):\n"
                f"{column_glossary}"
            )
        # Always include the business-term glossary too, not only when the
        # column-specific matcher above found nothing — it's the only place
        # broad synonym coverage lives (e.g. "client"/"item"/"most" ->
        # customer/product/ranking_top) and a narrow column-specific match
        # must never crowd it out of the prompt. Filtered by query relevance
        # (falls back to the full glossary when the match is weak — see
        # _build_business_glossary_for_query) to cut the ~1,500-token fixed
        # cost of sending all ~30+ concepts on every single call.
        business_glossary = _build_business_glossary_for_query(query)
        if business_glossary:
            system_prompt += (
                "\n\nBusiness term glossary (user may use these informal terms):\n"
                f"{business_glossary}"
            )
        if self._dialect.date_functions:
            system_prompt += (
                f"\n\nDate/time syntax for {self._dialect.name} "
                "(use these exact forms for relative dates like 'last month', 'this year'):\n"
                f"{self._dialect.date_functions}"
            )

        # Ground the query in the DB's actual data — not just column names.
        # Built against the current class-level ColumnRegistry (populated by
        # _get_schema()/_fetch_full_schema() above, so it's always in sync
        # with the schema just sent in this same prompt). Sampled values are
        # cached at the class level (see EntityValueResolver), so this is an
        # in-memory lookup on every call after the first.
        resolver = EntityValueResolver(SQLRetriever._column_registry, self._dialect)
        matched_values = await resolver.resolve(query)
        if matched_values:
            system_prompt += (
                "\n\nReal data values matching this question (these are the "
                "EXACT values that exist in the database right now for the "
                "name/category/status the user mentioned — match on these, "
                "do not invent or reformat your own version of them):\n"
                f"{matched_values}"
            )

        if last_error:
            system_prompt += f"\n\nWARNING: Your previous attempt failed with this error: {last_error}\nPlease fix the SQL query and try again."
        
        try:
            response = await self._router.chat(
                task="reasoning",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": query},
                ],
                max_tokens=2048
            )
            
            raw = (response or "").strip()

            # Abstention: the model may decorate the sentinel ("NO_SQL.",
            # "NO_SQL - the schema has no ...", or fenced). Any reply whose first
            # token is NO_SQL is an abstain, not a query to run.
            if _ABSTAIN_RE.match(raw):
                _log_pipeline_event("sql_abstain", {"raw_response": raw[:200]}, query=query)
                return ""

            # Strip code fences / prose preamble so a well-formed query isn't
            # discarded (and turned into a spurious document-only answer) just
            # because the model wrapped or introduced it.
            sql = _unwrap_sql(raw)
            if not sql or _ABSTAIN_RE.match(sql):
                _log_pipeline_event("sql_abstain", {"raw_response": raw[:200]}, query=query)
                return ""

            return sql
        except Exception as e:
            logger.error(f"Failed to generate SQL: {e}")
            # Distinguish an infrastructure outage (every provider for the
            # 'reasoning' task was unreachable) from an ordinary generation
            # failure. The former means the DB was never actually consulted, so
            # the pipeline should say the model was unavailable rather than
            # implying the data doesn't exist.
            if "All providers exhausted" in str(e):
                self.last_infra_error = str(e)
            return ""

    # Functions that read/write files, execute code, or cause DoS / lock
    # contention. Each is still a "SELECT" to sqlglot, so isinstance(ast,
    # exp.Select) alone would wave them through.
    _DANGEROUS_FUNCTIONS = frozenset({
        "load_file", "loadfile",              # MySQL: read an arbitrary file
        "sys_eval", "sys_exec", "sys_get",    # MySQL sys UDFs: shell execution
        "lo_import", "lo_export",             # Postgres large-object file I/O
        "benchmark",                          # MySQL: CPU exhaustion DoS
        "sleep",                              # MySQL/Postgres: thread sleep DoS
        "get_lock", "release_lock",           # MySQL: advisory lock contention DoS
        "release_all_locks",
        "is_free_lock", "is_used_lock",
    })

    _OUTPUT_READABILITY_RULES = """
Output readability rules:
- Never return a raw ID column (e.g. customer_id, product_id, order_id) by itself if a related table has a human-readable name, title, or label for it. JOIN to that table and return the readable value instead of, or alongside, the ID.
- Give every selected column a clear, descriptive alias using AS, so the result is understandable on its own without needing to see the query (e.g. SELECT c.name AS customer_name, SUM(o.amount) AS total_revenue - not SELECT c.name, SUM(o.amount)).
- Name each alias based on what the user actually asked for, ONLY when that wording accurately describes what the column holds (e.g. if the user asked "who spent the most", alias the result as top_customer or total_spent, not c1 or col2). Never invent a label that misrepresents the data - e.g. do not call a product_type_id column "technology_used" just because the word "technology" appeared in the question.
- Include any extra column that adds useful context to the answer (name, category, date, status) even if not strictly required to answer narrowly - the goal is a result a person can read and understand directly, not just the minimum data needed.
- "Most"/"highest"/"best" used in singular form (no number given) means exactly ONE result - apply LIMIT 1. "Top N" means LIMIT N. If the question asks to rank/list multiple items without a specific count, use a sensible default limit (e.g. LIMIT 20) rather than returning every row unbounded.
- Always filter out soft-deleted records (WHERE deleted_at IS NULL, or the table's equivalent) on any table that has a deleted_at column, unless the user explicitly asks to include deleted/inactive records.
- Financial Year handling: if a specific year is mentioned (e.g. '2024-2025' or '24-25'), join financial_year and filter on financial_year.fyear LIKE '%2024%'. For relative periods like 'this financial year' or 'current fiscal year', filter financial_year.current_year = 'Y'. If no year is specified for an all-time total, do not restrict by financial_year.
- Revenue vs. Invoiced/Tax: calculate standard sales revenue as product sales value SUM(rate * qty) from the sales order line items. If the user specifically asks for invoiced sales, tax-inclusive billing, or GST, use the proforma/invoice tables (grand_total, gst_amount) instead.
- When filtering by an entity or location name (customer name, state name, product name, etc.), match against the descriptive text column, not a numeric ID, unless the user gave you the ID directly.
- If the prompt includes a "Real data values matching this question" section, that is the exact spelling/casing/format the value has in the live database right now. Use it verbatim in your WHERE clause (an exact = match, not a guessed spelling) instead of reconstructing your own version of the name/category/status from the user's wording. If no such section is present, or the user's term isn't listed there, fall back to a case-insensitive LIKE/ILIKE match so an unlisted or partial value can still be found.
"""

    def _is_safe_read_query(self, sql: str) -> bool:
        """Parse the AST and confirm it's a single, side-effect-free read SELECT
        or UNION of SELECTs.

        ``isinstance(ast, (exp.Select, exp.Union))`` is necessary but NOT
        sufficient — several write/exfiltration/DoS primitives are still valid
        read statements:

          * ``SELECT ... INTO OUTFILE/DUMPFILE '/path'`` (MySQL) writes to disk;
          * ``SELECT LOAD_FILE('/etc/passwd')`` reads an arbitrary file;
          * ``SELECT SLEEP(10)`` / ``SELECT BENCHMARK(...)`` exhausts resources;
          * a stacked ``SELECT 1; DROP TABLE t`` smuggles a second statement.

        This rejects all of the above so the generated query can only ever read
        rows, matching the layer's stated "read-only SELECT/UNION" guarantee.
        UNION support (added alongside db_client's matching LIMIT-injection fix
        for exp.Union) lets a legitimate "combine two lists" question succeed
        instead of being silently blocked as unsafe.
        """
        try:
            # parse() (not parse_one) surfaces stacked statements so they can be
            # rejected rather than silently reduced to the first one.
            statements = [s for s in sqlglot.parse(sql, read=self._dialect.sqlglot_dialect) if s is not None]
        except Exception as e:
            logger.error(f"sqlglot rejected query '{sql}': {e}")
            return False

        if len(statements) != 1:
            logger.warning("Blocked multi-statement / stacked SQL: %s", sql)
            return False

        ast = statements[0]
        if not isinstance(ast, (exp.Select, exp.Union)):
            return False

        # SELECT ... INTO OUTFILE/DUMPFILE (or INTO @var) anywhere in the AST
        # (including inside a UNION branch) — a disk/variable write.
        for sel_node in ast.find_all(exp.Select):
            if sel_node.args.get("into") is not None:
                logger.warning("Blocked SELECT ... INTO (file/variable write): %s", sql)
                return False

        # File-read / code-exec / DoS / locking functions anywhere in the tree.
        # Checked two ways: exp.Anonymous covers functions sqlglot doesn't
        # recognize as a named builtin (e.g. sys_exec), and exp.Func covers
        # ones it does parse into a typed node (e.g. SLEEP is often typed).
        for anon in ast.find_all(exp.Anonymous):
            fname = (anon.this or "")
            if isinstance(fname, str) and fname.lower() in self._DANGEROUS_FUNCTIONS:
                logger.warning("Blocked dangerous function '%s' in SQL: %s", fname, sql)
                return False
        for func in ast.find_all(exp.Func):
            fname = func.sql_name() if hasattr(func, "sql_name") else getattr(func, "key", "")
            if isinstance(fname, str) and fname.lower() in self._DANGEROUS_FUNCTIONS:
                logger.warning("Blocked dangerous function '%s' in SQL: %s", fname, sql)
                return False

        return True

    # This table is returned as the answer VERBATIM (see _extract_sql_table in
    # s12_s13_s14_retrieval.py, which bypasses the LLM and _build_context's
    # token budget entirely). db_client.MAX_ROWS=500 protects the DB round-trip.
    # However, to prevent massive walls of text in the UI, we hard-cap the
    # display output to 10 rows per the user's preference.
    _MAX_DISPLAY_ROWS = 10

    def _format_rows_as_markdown(self, rows: list[dict[str, Any]], query: str) -> str:
        """Format dictionary rows into a markdown table, capped at 10 rows."""
        if not rows:
            return "No results."

        headers = list(rows[0].keys())
        header_row = "| " + " | ".join(headers) + " |"
        separator_row = "| " + " | ".join(["---"] * len(headers)) + " |"

        table_rows = [f"SQL Query Executed: `{query}`\n", header_row, separator_row]

        shown = 0
        for row in rows:
            if shown >= self._MAX_DISPLAY_ROWS:
                break
            values = [str(row[h]) for h in headers]
            line = "| " + " | ".join(values) + " |"
            table_rows.append(line)
            shown += 1

        result = "\n".join(table_rows)
        total = len(rows)
        if shown < total:
            result += (
                f"\n\n_Showing {shown} of {total} rows (result too large to "
                "display in full). Narrow your question (add a filter, date "
                "range, or LIMIT) to see a different slice._"
            )
        return result


async def fetch_sqlite_foreign_keys() -> list[dict]:
    tables = await run_readonly_query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence';"
    )
    fks = []
    for row in tables:
        table = row["name"]
        escaped_table = table.replace('"', '""')
        cols = await run_readonly_query(f'PRAGMA foreign_key_list("{escaped_table}");')
        for c in cols:
            fks.append({
                "table_name": table,
                "column_name": c["from"],
                "referenced_table_name": c["table"],
                "referenced_column_name": c["to"],
            })
    return fks
