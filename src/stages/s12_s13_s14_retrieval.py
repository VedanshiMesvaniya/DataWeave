"""Stages 12–14 — Retrieval, Reranking, and Generation.

Stage 12: True hybrid dense+sparse retrieval with RRF fusion (top 20-50)
          BM25-lite heuristic REMOVED — replaced by real Qdrant sparse vectors.
Stage 13: Jina Reranker v3 → top 5-8
Stage 14: Task-routed LLM generation with citations
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
from pathlib import Path
from typing import Any

import httpx

from src.core.config import settings
from src.core.provider_client import ProviderRouter
from src.core.rate_limiter import RateLimiter, get_shared_rate_limiter
from src.models.schemas import Citation, Chunk, ChunkType, QueryResult, RetrievedChunk
from src.stages.s10_embeddings import EmbeddingService
from src.stages.s11_vector_store import QdrantStore

logger = logging.getLogger(__name__)

_JINA_RERANK_URL = "https://api.jina.ai/v1/rerank"


# ---------------------------------------------------------------------------
# Stage 12 — Retrieval
# ---------------------------------------------------------------------------

class Retriever:
    """True hybrid retriever: Jina dense + Jina sparse vectors, RRF-fused in Qdrant.

    Supports optional metadata filters to constrain retrieval to specific
    document types, source files, page ranges, etc.
    """

    def __init__(
        self,
        store: QdrantStore,
        embedding_service: EmbeddingService,
    ) -> None:
        self._store = store
        self._embeddings = embedding_service

    async def retrieve(
        self,
        query: str,
        top_k: int = 30,
        filters: dict[str, Any] | None = None,
        exhaustive: bool = False,
    ) -> list[RetrievedChunk]:
        """Retrieve the top-k most relevant chunks for a query.

        Args:
            query: Natural language question.
            top_k: Number of results to retrieve.
            filters: Optional metadata filter dict. Supported keys:
                     document_type, source_file, page_number, document_id, chunk_type.
            exhaustive: When True, boosts top_k and disables per-document caps to
                        maximise recall for "list all X" style queries.

        Returns:
            List of RetrievedChunk sorted by descending relevance score.
        """
        effective_top_k = min(top_k * 2, 120) if exhaustive else top_k

        # Get dense + sparse query embeddings in one API call
        dense_vector, sparse_vector = await self._embeddings.embed_query(query)

        # Hybrid RRF search (degrades gracefully to dense-only if sparse is empty)
        results = await self._store.search_hybrid(
            query_vector=dense_vector,
            sparse_vector=sparse_vector,
            query_text=query,
            top_k=effective_top_k,
            filters=filters,
        )

        # Collapse boilerplate shared across documents (same intro/disclaimer
        # stored once per file) so repeated passages don't crowd the reranker or
        # the final context. Results are already score-sorted, so the first copy
        # — the best-scored one — is the representative we keep.
        return _suppress_near_duplicates(
            results, similarity_threshold=settings.dedup_near_duplicate_threshold
        )


# ---------------------------------------------------------------------------
# Stage 13 — Reranking
# ---------------------------------------------------------------------------

class Reranker:
    """Reranks retrieved chunks using Jina Reranker v3."""

    def __init__(self, rate_limiter: RateLimiter | None = None) -> None:
        self._rate_limiter = rate_limiter or get_shared_rate_limiter()
        self._http: httpx.AsyncClient | None = None

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    async def rerank(
        self, query: str, chunks: list[RetrievedChunk], top_k: int = 6
    ) -> list[RetrievedChunk]:
        """Rerank chunks and return the top-k."""
        if not chunks:
            return []

        if settings.jina_api_key:
            try:
                return await self._rerank_jina(query, chunks, top_k)
            except Exception as e:
                logger.warning("Jina reranking failed: %s — using lexical fallback", e)

        # Fallback (no Jina / Jina down): rank by lexical relevance to the query,
        # NOT raw retrieval score. Retrieval scores aren't comparable across
        # sources — a SQL result is hard-coded to score 1.0, so a plain
        # score-sort would always float an off-topic SQL row above the truly
        # relevant document chunks (e.g. GPU rows dominating a "cheapest iPhone"
        # answer). Query-term overlap demotes chunks that don't match the words.
        return _lexical_rerank(query, chunks, top_k)

    async def _rerank_jina(
        self, query: str, chunks: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        """Rerank via Jina Reranker API."""
        await self._rate_limiter.acquire("jina")
        http = self._get_http()

        documents = [c.chunk.content for c in chunks]

        response = await http.post(
            _JINA_RERANK_URL,
            headers={
                "Authorization": f"Bearer {settings.jina_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "jina-reranker-v2-base-multilingual",
                "query": query,
                "documents": documents,
                "top_n": top_k,
            },
        )
        response.raise_for_status()
        data = response.json()

        reranked: list[RetrievedChunk] = []
        for result in data.get("results", []):
            idx = result["index"]
            score = result["relevance_score"]
            reranked_chunk = chunks[idx]
            reranked_chunk.score = score
            reranked_chunk.retrieval_method = "reranked"
            reranked.append(reranked_chunk)

        return reranked


_LEXICAL_STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "was", "were", "be", "what", "which", "who", "how", "many", "much", "do",
    "does", "did", "with", "by", "at", "as", "from", "that", "this", "it",
    "its", "compare", "make", "table", "chart", "show", "me", "give",
})


def _lexical_terms(text: str) -> set[str]:
    """Lowercase content words (length ≥ 3, non-stopword) for overlap scoring."""
    import re
    return {
        w for w in re.findall(r"[a-z0-9]+", text.lower())
        if len(w) >= 3 and w not in _LEXICAL_STOPWORDS
    }


def _lexical_rerank(
    query: str, chunks: list[RetrievedChunk], top_k: int
) -> list[RetrievedChunk]:
    """Relevance-rank chunks by query-term overlap (Jina-less fallback).

    Score = fraction of the query's content words that appear in the chunk,
    with the original retrieval score as a tiny tiebreaker. A chunk that shares
    no query terms (e.g. GPU-sales rows for an iPhone question) sinks to the
    bottom regardless of its raw retrieval score.
    """
    q_terms = _lexical_terms(query)
    if not q_terms:
        # Nothing to match on — preserve retrieval order.
        return sorted(chunks, key=lambda r: r.score, reverse=True)[:top_k]

    def _relevance(r: RetrievedChunk) -> tuple[float, float]:
        overlap = len(q_terms & _lexical_terms(r.chunk.content)) / len(q_terms)
        return (overlap, r.score)

    return sorted(chunks, key=_relevance, reverse=True)[:top_k]


# ---------------------------------------------------------------------------
# Stage 14 — Generation
# ---------------------------------------------------------------------------

class Generator:
    """Task-routed LLM generation with citation support."""

    def __init__(self, router: ProviderRouter) -> None:
        self._router = router

    async def generate(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        *,
        task: str | None = None,
        history: list[dict] | None = None,
        context_limit: int | None = None,
        source_mode: str = "doc_only",
    ) -> QueryResult:
        """Generate an answer from retrieved chunks.

        Automatically routes to the best model for the inferred task type.

        ``context_limit`` caps how many of the (already reranked) chunks are
        actually placed in the prompt; ``None`` uses them all. The top few
        carry the answer, so trimming cuts input tokens and latency without
        hurting quality — see _limit_context_chunks.
        """
        if not chunks:
            return QueryResult(
                query=query,
                answer="I couldn't find any relevant information to answer your question.",
                model_used="none",
                reasoning_task="no_chunks",
            )

        # Determine task type if not provided
        if task is None:
            task = _classify_query_task(query)

        # Only the best chunks go into the prompt; citations are drawn from the
        # same trimmed set so we never cite a source the model didn't see.
        context_chunks = _limit_context_chunks(chunks, context_limit)
        sql_table_md = _extract_sql_table(context_chunks)
        has_other_chunks = any(c.chunk.chunk_type != ChunkType.SQL_RESULT for c in context_chunks)
        if sql_table_md and not has_other_chunks:
            return QueryResult(
                query=query,
                answer=_pick_greeting() + sql_table_md,
                citations=[],
                model_used="sql/direct",
                reasoning_task=task,
                chunks_retrieved=len(chunks),
                chunks_after_rerank=len(chunks),
                usage=self._router.usage.model_copy(),
            )
        context = _build_context(context_chunks)

        # Build the prompt. The fixed answer rules live in the system prompt
        # (cacheable prefix), so the user message carries only the volatile
        # context + question.
        system_prompt = _build_system_prompt(task, source_mode)
        user_prompt = f"""Context (retrieved document chunks):
---
{context}
---

Question: {query}"""

        # Generate — recent conversation turns go between the system prompt and
        # the grounded question so follow-ups stay coherent.
        response = await self._router.chat(
            task,
            messages=[
                {"role": "system", "content": system_prompt},
                *_history_messages(history),
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=2048,
        )

        # Extract citations and format the answer text
        citations, clean_answer = _extract_and_format_citations(response, context_chunks)
        # Server-side varying opener — see _pick_greeting for why this isn't
        # left to the LLM.
        clean_answer = _pick_greeting() + clean_answer
        if sql_table_md:
            clean_answer = f"{clean_answer}\n\n{sql_table_md}"

        return QueryResult(
            query=query,
            answer=clean_answer,
            citations=citations,
            # The actual provider/model that answered (after any fallback), not
            # the task label — so provider selection and the "answered using"
            # trace show real information.
            model_used=self._router.last_used or task,
            reasoning_task=task,
            chunks_retrieved=len(chunks),
            chunks_after_rerank=len(chunks),
            # Token cost of the whole query (all LLM calls routed so far).
            usage=self._router.usage.model_copy(),
        )

    async def generate_stream(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        *,
        task: str | None = None,
        history: list[dict] | None = None,
        context_limit: int | None = None,
        source_mode: str = "doc_only",
    ):
        """Stage 14: Answer generation via LLM stream.

        Yields string chunks during generation, and a final QueryResult object
        when the stream completes. ``context_limit`` caps how many reranked
        chunks are placed in the prompt (see generate()).
        """
        if not chunks:
            yield "I don't have any information about that in my current documents."
            yield QueryResult(
                query=query,
                answer="I don't have any information about that in my current documents.",
                citations=[],
                model_used="none",
                reasoning_task="none",
                chunks_retrieved=0,
                chunks_after_rerank=0,
            )
            return

        if task is None:
            task = _classify_query_task(query)

        # Only the best chunks go into the prompt; citations come from the same
        # trimmed set. The fixed answer rules are in the system prompt, so the
        # user message carries only the volatile context + question.
        context_chunks = _limit_context_chunks(chunks, context_limit)
        sql_table_md = _extract_sql_table(context_chunks)
        has_other_chunks = any(c.chunk.chunk_type != ChunkType.SQL_RESULT for c in context_chunks)
        if sql_table_md and not has_other_chunks:
            greeting = _pick_greeting()
            yield greeting
            yield sql_table_md
            yield QueryResult(
                query=query,
                answer=greeting + sql_table_md,
                citations=[],
                model_used="sql/direct",
                reasoning_task=task,
                chunks_retrieved=len(chunks),
                chunks_after_rerank=len(chunks),
                usage=self._router.usage.model_copy(),
            )
            return
        context = _build_context(context_chunks)
        system_prompt = _build_system_prompt(task, source_mode)
        user_prompt = f"""Context (retrieved document chunks):
---
{context}
---

Question: {query}"""

        # Varying opener streamed first so the UI shows it immediately, before
        # the model's own tokens start arriving.
        greeting = _pick_greeting()
        yield greeting

        full_answer_parts = []
        async for chunk_text in self._router.chat_stream(
            task,
            messages=[
                {"role": "system", "content": system_prompt},
                *_history_messages(history),
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=2048,
        ):
            full_answer_parts.append(chunk_text)
            yield chunk_text

        full_answer = "".join(full_answer_parts)
        citations, clean_answer = _extract_and_format_citations(full_answer, context_chunks)
        clean_answer = greeting + clean_answer
        if sql_table_md:
            clean_answer = f"{clean_answer}\n\n{sql_table_md}"

        yield QueryResult(
            query=query,
            answer=clean_answer,
            citations=citations,
            # Real provider/model that streamed the answer (set by the router
            # when the stream completed), not the task label.
            model_used=self._router.last_used or task,
            reasoning_task=task,
            chunks_retrieved=len(chunks),
            chunks_after_rerank=len(chunks),
            # Token cost of the whole query (all LLM calls routed so far).
            usage=self._router.usage.model_copy(),
        )


def _classify_query_task(query: str) -> str:
    """Infer the best LLM task type from the query."""
    query_lower = query.lower()

    if any(kw in query_lower for kw in ["summarize", "summary", "overview", "brief"]):
        return "summarization"
    if any(kw in query_lower for kw in ["why", "how", "explain", "reason", "analyze", "compare"]):
        return "reasoning"
    if any(kw in query_lower for kw in ["extract", "list", "find all", "what are the"]):
        return "extraction"

    return "general_qa"


_EXHAUSTIVE_KEYWORDS = [
    "list every", "list all", "all companies", "every company", "all products",
    "every product", "all entities", "every entity", "all mentions", "all models",
    "every model", "all documents", "enumerate", "comprehensive list",
    "complete list", "full list", "all names", "every name", "all people",
    "every person", "all locations", "every location", "across all", "across documents",
]


def _is_exhaustive_query(query: str) -> bool:
    """Return True if the query asks for an exhaustive enumeration."""
    q = query.lower()
    return any(kw in q for kw in _EXHAUSTIVE_KEYWORDS)


_VISUALIZATION_KEYWORDS = [
    "chart", "graph", "plot", "diagram", "visualize", "visualise",
    "visualization", "visualisation", "bar chart", "line chart", "pie chart",
    "scatter", "histogram", "flowchart", "timeline", "trend", "breakdown",
    "distribution", "draw", "sketch",
]


def _is_visualization_query(query: str) -> bool:
    """Return True if the query asks for a chart / graph / diagram.

    Used to keep document retrieval in play even when a metric keyword like
    "total" would otherwise route the request to the SQL table alone — a
    chart may need to draw on values that live in the uploaded documents.
    """
    q = query.lower()
    return any(kw in q for kw in _VISUALIZATION_KEYWORDS)


_DEDUP_WORD_RE = re.compile(r"[a-z0-9]+")

# Below this many distinct words a chunk is too short for token-overlap to be a
# reliable duplicate signal (a lone number, a one-line heading, a table cell can
# collide with an unrelated chunk by coincidence), so we only ever collapse it
# on an *exact* normalized-text match, never a fuzzy one.
_DEDUP_MIN_TOKENS = 12


def _dedup_tokens(text: str) -> list[str]:
    """Lowercased alphanumeric words — the normalization both dedup paths share."""
    return _DEDUP_WORD_RE.findall(text.lower())


def _suppress_near_duplicates(
    chunks: list[RetrievedChunk],
    *,
    similarity_threshold: float = 0.9,
) -> list[RetrievedChunk]:
    """Drop chunks whose text duplicates a higher-ranked chunk already kept.

    Boilerplate shared across documents — a company motto, a repeated project
    preamble, a standard disclaimer — is embedded and stored once per file, so a
    query that matches it retrieves the same passage many times. Left alone,
    those near-identical copies dominate the reranker and the final context,
    starving unique content. This collapses them, keeping a single representative.

    Two shapes are handled:
      * **Exact repeats** — identical text after whitespace/case normalization.
        Always collapsed; there is no downside to keeping one copy of identical
        text (the only loss is a redundant citation to a second file).
      * **Near repeats** — same passage with a trivial edit (a date, a version
        number). Collapsed only when the token-set Jaccard overlap meets
        ``similarity_threshold`` AND both chunks are long enough
        (``_DEDUP_MIN_TOKENS``) that high overlap is meaningful — so genuinely
        distinct short chunks are never merged. Pass a threshold > 1.0 to disable
        the fuzzy path and collapse exact repeats only.

    Input is assumed to be in descending relevance order (as retrieval returns
    it), so the first occurrence is the best-scored copy and is the one kept.
    """
    if len(chunks) <= 1:
        return chunks

    kept: list[RetrievedChunk] = []
    kept_token_sets: list[set[str]] = []
    seen_exact: set[str] = set()
    dropped = 0

    for c in chunks:
        tokens = _dedup_tokens(c.chunk.content or "")

        # Exact-duplicate (normalized) — cheap, and always safe to collapse.
        fingerprint = hashlib.sha1(" ".join(tokens).encode("utf-8")).hexdigest()
        if fingerprint in seen_exact:
            dropped += 1
            continue

        # Near-duplicate — only meaningful for chunks with enough distinct words.
        token_set = set(tokens)
        if len(token_set) >= _DEDUP_MIN_TOKENS:
            is_near_dup = any(
                prior
                and len(token_set & prior) / len(token_set | prior) >= similarity_threshold
                for prior in kept_token_sets
            )
            if is_near_dup:
                dropped += 1
                continue

        seen_exact.add(fingerprint)
        kept.append(c)
        kept_token_sets.append(token_set)

    if dropped:
        logger.info(
            "Near-duplicate suppression: dropped %d/%d chunk(s) sharing text "
            "with a higher-ranked passage",
            dropped,
            len(chunks),
        )
    return kept


def _enforce_document_diversity(
    chunks: list[RetrievedChunk],
    target_k: int,
) -> list[RetrievedChunk]:
    """Guarantee proportional document representation in the final context.

    Algorithm:
    1. Group chunks by document_id.
    2. Compute per-document quota = ceil(target_k / num_unique_docs), min 2.
    3. Fill slots round-robin by score, respecting per-document quota.
    4. If slots remain after all quotas are filled, top up from leftovers.

    This prevents a single high-volume or high-similarity document from
    crowding out all chunks from other documents.
    """
    import math
    from collections import defaultdict

    if not chunks:
        return chunks

    # Group by document preserving score order (chunks are already sorted desc)
    doc_buckets: dict[str, list[RetrievedChunk]] = defaultdict(list)
    for c in chunks:
        doc_buckets[c.chunk.document_id].append(c)

    num_docs = len(doc_buckets)
    if num_docs <= 1:
        # Single document — no diversity enforcement needed
        return chunks[:target_k]

    # Per-document slot quota
    quota = max(2, math.ceil(target_k / num_docs))
    logger.debug(
        "Document diversity: %d docs, target_k=%d, quota=%d per doc",
        num_docs, target_k, quota,
    )

    # Fill slots: take up to `quota` from each document in round-robin passes
    selected: list[RetrievedChunk] = []
    pointers = {doc_id: 0 for doc_id in doc_buckets}
    leftover: list[RetrievedChunk] = []

    # Pass 1: take quota from each doc (preserving intra-doc score order)
    for doc_id, bucket in doc_buckets.items():
        taken = bucket[:quota]
        selected.extend(taken)
        leftover.extend(bucket[quota:])

    # Trim to target_k
    selected = selected[:target_k]

    # Pass 2: if we still have room, fill from leftover sorted by score
    if len(selected) < target_k:
        leftover_sorted = sorted(leftover, key=lambda r: r.score, reverse=True)
        selected.extend(leftover_sorted[: target_k - len(selected)])

    # Re-sort the final selection by score so the LLM sees best chunks first
    selected.sort(key=lambda r: r.score, reverse=True)

    doc_counts = defaultdict(int)
    for c in selected:
        doc_counts[c.chunk.document_id] += 1
    logger.info(
        "Post-diversity chunks: %d total from %d docs — %s",
        len(selected),
        len(doc_counts),
        dict(doc_counts),
    )

    return selected


_VISUALIZATION_GUIDANCE = """

Formatting and visualization:
- Present tabular or comparison data as a GitHub-flavored Markdown table.
- When the user asks for a chart, graph, or diagram — or when one would communicate the answer more clearly — render it as a fenced ```mermaid code block. The app renders Mermaid natively, so DO NOT claim you lack tools to create visualizations.
- Only chart numbers that actually appear in the context. If the exact values needed for a chart are missing, say which values are missing and chart whatever related data IS available rather than refusing outright.

You MUST use valid Mermaid syntax. Copy the structure of these exact templates — do not invent syntax from other charting tools (no `type`, no `timeUnit`, no `range [...]`, no `plot`, no `{ }` blocks, no `[["date", value]]` pairs — those are NOT Mermaid and will fail to render):

Bar or line chart (comparing values across categories, or a trend). The x-axis is a plain list of category labels; each series is a flat list of numbers, one per label, in the same order:
```mermaid
xychart-beta
    title "Total Units Sold by Month"
    x-axis [Jan, Feb, Mar, Apr, May]
    y-axis "Units Sold" 0 --> 120
    line [8, 34, 30, 45, 90]
```
(Use `bar [ ... ]` instead of `line [ ... ]` for a bar chart. If dates are involved, bucket them into a handful of labelled categories like months or quarters — Mermaid has no time axis.)

Proportions / share of a whole:
```mermaid
pie showData
    title Market Share
    "NVIDIA" : 80
    "AMD" : 15
    "Intel" : 5
```

Process, flow, or hierarchy:
```mermaid
flowchart TD
    A[Start] --> B{Decision}
    B -->|Yes| C[Do this]
    B -->|No| D[Do that]
```

Keep every data point on its own line and make sure the number of y-values matches the number of x-axis labels."""


# ---------------------------------------------------------------------------
# Varying answer-opener (prefix greeting)
# ---------------------------------------------------------------------------
#
# A static opener ("Here's what I found:") on every answer reads as canned.
# This pool is picked from server-side (not left to the LLM, which tends to
# regress to one or two favourites) so it's cheap, deterministic to test, and
# never eats into the model's token budget. `_pick_greeting` avoids repeating
# the immediately previous pick so back-to-back answers in one session don't
# feel identical.
_GREETING_PREFIXES: list[str] = [
    "Here's what I found: ",
    "Based on the documents, ",
    "Looking at the retrieved context, ",
    "Good question — ",
    "Here's the answer: ",
    "Digging into the context, ",
    "From what's documented, ",
    "Checking the source material, ",
    "According to your knowledge base, ",
    "Walking through the context, ",
    "Here's what the documents show: ",
    "Pulling this from the retrieved chunks, ",
]

_last_greeting_idx: int | None = None


def _pick_greeting() -> str:
    """Return a random opener, never the same one twice in a row."""
    global _last_greeting_idx
    if len(_GREETING_PREFIXES) <= 1:
        return _GREETING_PREFIXES[0] if _GREETING_PREFIXES else ""
    idx = random.randrange(len(_GREETING_PREFIXES))
    while idx == _last_greeting_idx:
        idx = random.randrange(len(_GREETING_PREFIXES))
    _last_greeting_idx = idx
    return _GREETING_PREFIXES[idx]


# ---------------------------------------------------------------------------
# Reasoning discipline — force genuine reasoning, not fetch-and-paste
# ---------------------------------------------------------------------------
#
# Without this, the model tends to lift the closest-matching chunk almost
# verbatim instead of actually reasoning over it (see the EMO example: asked
# "how will EMO behave if we say 'you are cute'", the model just repeated
# what the catalog chunk described about the catalog, rather than working out
# what it implied for that specific phrase). This block makes the model name
# the kind of reasoning the question needs, apply it, and then disclose which
# logic path it used, so the "logic applied" is visible/auditable in the
# answer rather than hidden inside a single fetch-and-answer step.
_REASONING_DISCIPLINE = (
    "\n\nReasoning discipline: before answering, identify what the question "
    "actually requires — a direct fact lookup, a multi-step inference that "
    "combines several passages, a comparison/contrast, a calculation, or "
    "cause-and-effect reasoning about how something described in the context "
    "would behave in the specific situation asked about. Do not just restate "
    "or fetch the nearest-matching chunk verbatim — work through the logic "
    "needed to actually answer the question asked, using only what the "
    "context supports (no inventing specifics the context doesn't cover). "
    "End your answer with one line in exactly this format on its own line: "
    "'**Logic applied:** <Direct lookup | Multi-step inference | Comparison | "
    "Calculation | Cause-and-effect reasoning | Insufficient context> — "
    "<a few words on why>'."
)


# The fixed answer-formatting rules — byte-identical on every generation call.
# They live in the system prompt (not the per-call user message) so the whole
# instruction block forms a stable prompt *prefix*. Groq and Gemini 2.5
# automatically cache repeated prefixes, so keeping these constant and up front
# means they're billed/encoded once and reused, instead of being re-sent as
# fresh tokens after the (variable) context on every turn.
_ANSWER_RULES = (
    " Answer using ONLY the information in the provided context. If the context "
    "doesn't contain enough information to answer, say so explicitly. Each excerpt "
    "is tagged with a bracketed source marker like [a1b2c3d4_0007]. Cite your claims "
    'inline by copying the exact marker(s) in brackets — e.g. "Opus scored 86.8% '
    '[a1b2c3d4_0007]." These render as clean numbered references, so do NOT add a '
    'separate column or heading for them and do NOT refer to them as "chunks" in '
    "your prose. A chunk that merely shares a word with the question is not the same "
    "as actually answering it — if the retrieved context is only tangentially or "
    "coincidentally related, treat it as not containing the answer and say so rather "
    "than stretching it into a response."
)


def _source_mode(chunks: list[RetrievedChunk]) -> str:
    has_sql = any(c.chunk.chunk_type == ChunkType.SQL_RESULT for c in chunks)
    has_doc = any(c.chunk.chunk_type != ChunkType.SQL_RESULT for c in chunks)
    if has_sql and has_doc: return "both"
    if has_sql: return "sql_only"
    return "doc_only"

_MODE_INSTRUCTIONS = {
    "sql_only": (
        "The context contains ONLY live database results. These numbers are "
        "authoritative. Present them clearly with a brief natural-language "
        "introduction. Do not speculate beyond what the data shows."
    ),
    "both": (
        "The context contains BOTH live database results AND document passages. "
        "For numerical/factual claims, prefer the database results (computed from "
        "live data). For policies, explanations, or qualitative context, use the "
        "documents. Cite both sources. If they contradict, note the discrepancy."
    ),
    "doc_only": "",  # existing prompt works as-is
}


def _build_system_prompt(task: str, source_mode: str = "doc_only") -> str:
    """Build a task-appropriate system prompt.

    Everything here is stable per task (no query-specific content), so it acts
    as a cacheable prefix — see _ANSWER_RULES.
    """
    base = "You are a precise document analysis assistant. "

    prompts = {
        "general_qa": base + "Answer questions accurately based on the provided context. Always cite your sources using their bracketed source markers.",
        "reasoning": base + "Perform careful multi-step reasoning. Show your reasoning process. Cite all sources with their bracketed markers.",
        "extraction": base + "Extract structured information precisely. Use JSON format when appropriate. Cite sources with their bracketed markers.",
        "summarization": base + "Provide comprehensive summaries. Cover all key points from the context. Cite sources with their bracketed markers.",
    }

    mode_instruction = " " + _MODE_INSTRUCTIONS[source_mode] if _MODE_INSTRUCTIONS.get(source_mode) else ""
    return (
        prompts.get(task, prompts["general_qa"])
        + mode_instruction
        + _ANSWER_RULES
        + _REASONING_DISCIPLINE
        + _VISUALIZATION_GUIDANCE
    )


def _history_messages(history: list[dict] | None, *, max_turns: int = 6, max_chars: int = 1500) -> list[dict]:
    """Convert stored chat turns into chat-completion messages for continuity.

    Keeps only the last few user/assistant turns (skipping ingestion cards and
    in-flight placeholders) and truncates long answers so the prompt stays lean.
    """
    if not history:
        return []
    messages: list[dict] = []
    for turn in history[-max_turns:]:
        if turn.get("kind") == "ingestion" or turn.get("status") == "loading":
            continue
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            if role == "assistant" and "SQL Query Executed:" in content:
                content = content.split("SQL Query Executed:")[0].strip()
            messages.append({"role": role, "content": content[:max_chars]})
    return messages


def _limit_context_chunks(
    chunks: list[RetrievedChunk], limit: int | None
) -> list[RetrievedChunk]:
    """Keep only the top ``limit`` reranked chunks for the generation prompt.

    ``None`` (exhaustive queries) keeps everything. Otherwise the cap is floored
    at 2 so a short document with only a couple of relevant chunks — or a
    misconfigured limit — never starves the model of context.
    """
    if limit is None:
        return chunks
    return chunks[: max(limit, 2)]


def _extract_sql_table(chunks: list[RetrievedChunk]) -> str | None:
    """Return the exact SQL markdown table if one is present."""
    sql_tables = [
        c.chunk.content
        for c in chunks
        if c.chunk.chunk_type == ChunkType.SQL_RESULT and c.chunk.content
    ]
    if not sql_tables:
        return None
    return "\n\n".join(sql_tables)


def _build_context(chunks: list[RetrievedChunk]) -> str:
    """Format retrieved chunks into a context string, bounded by a token limit."""
    parts: list[str] = []
    current_tokens = 0
    max_context_tokens = settings.max_context_tokens

    for chunk in chunks:
        c = chunk.chunk
        header = f"[{c.chunk_id}] (page {c.page_number}, type: {c.chunk_type.value})"
        if c.chunk_type == ChunkType.SQL_RESULT:
            chunk_text = (
                f"{header}\nA live database query already ran and returned matching "
                "rows. Do not reproduce the table or invent new numbers. Write one "
                "short natural-language sentence introducing the result. The exact "
                "table will be appended automatically after your reply."
            )
        else:
            chunk_text = f"{header}\n{c.content}"

        # Rough token estimate. 3 chars/token (not the more common 4) —
        # deliberately conservative: this budget also has to cover markdown
        # tables and non-English text, which tokenize denser than plain
        # English prose, and this codebase routes across several providers
        # with different real tokenizers.
        est_tokens = len(chunk_text) // 3

        if current_tokens + est_tokens > max_context_tokens and parts:
            logger.info("Context length bounded to %d tokens (dropped %d lower-ranked chunks)", 
                       current_tokens, len(chunks) - len(parts))
            break
            
        parts.append(chunk_text)
        current_tokens += est_tokens

    return "\n\n---\n\n".join(parts)


def _extract_and_format_citations(answer: str, chunks: list[RetrievedChunk]) -> tuple[list[Citation], str]:
    """Extract chunk IDs, replace with readable footnotes, and append a source list.

    Handles two citation shapes the model produces:
      1. Single-ID brackets:  [<doc>_chunk_0043]
      2. Multi-ID brackets:   [<doc>_chunk_0043, <doc>_chunk_0027, ...]

    Every referenced chunk maps to a stable footnote number (deduped by the
    chunk's *source file + page*, so ten chunks from page 8 of one PDF collapse
    into a single [1] rather than [1][2]...[10]). Any chunk-ID bracket that
    can't be resolved to a retrieved chunk is stripped rather than left raw,
    so internal IDs never leak into the visible answer.
    """
    import re

    chunk_by_id = {c.chunk.chunk_id: c for c in chunks}

    # Build the citation-bracket matcher from the *actual* retrieved chunk ids,
    # so we catch every id shape the pipeline emits — hex "..._chunk_0043" from
    # documents AND synthetic ids like "live_sql_001" from the SQL stage —
    # without touching real prose brackets such as [1] or Markdown links.
    if chunk_by_id:
        _ID_ALT = "|".join(
            re.escape(cid) for cid in sorted(chunk_by_id, key=len, reverse=True)
        )
        _CITATION_BRACKET = re.compile(rf"\[\s*(?:{_ID_ALT})(?:\s*,\s*(?:{_ID_ALT}))*\s*\]")
    else:
        _CITATION_BRACKET = None

    # Shapes of internal ids, used only as a safety net to strip brackets for
    # chunks the model invented (cited but never retrieved), so raw ids never
    # reach the user regardless of source.
    _INTERNAL_ID = r"(?:[a-f0-9]+_chunk_\d+|live_[a-z0-9]+_\d+)"

    citations: list[Citation] = []
    sources_text: list[str] = []
    # Footnote numbers are assigned per unique (source_file, page) so repeated
    # chunks from the same page share one number.
    footnote_by_source: dict[tuple[str, int], int] = {}

    def _footnote_for(chunk: RetrievedChunk) -> int:
        key = (chunk.chunk.source_file, chunk.chunk.page_number)
        if key not in footnote_by_source:
            idx = len(footnote_by_source) + 1
            footnote_by_source[key] = idx
            citations.append(Citation(
                chunk_id=chunk.chunk.chunk_id,
                source_file=chunk.chunk.source_file,
                page_number=chunk.chunk.page_number,
                relevance_score=chunk.score,
            ))
            page_text = f" (Page {chunk.chunk.page_number})" if chunk.chunk.page_number else ""
            # Show the clean document name, never the absolute path or chunk id.
            doc_name = Path(chunk.chunk.source_file).name or chunk.chunk.source_file
            sources_text.append(f"{idx}. {doc_name}{page_text}")
        return footnote_by_source[key]

    def _replace_bracket(match: re.Match) -> str:
        raw_ids = [tok.strip() for tok in match.group(0).strip("[]").split(",")]
        footnotes: list[int] = []
        for cid in raw_ids:
            chunk = chunk_by_id.get(cid)
            if chunk is not None:
                fn = _footnote_for(chunk)
                if fn not in footnotes:
                    footnotes.append(fn)
        # If none resolved, drop the bracket entirely (no raw IDs leak through)
        return "".join(f"[{fn}]" for fn in footnotes)

    clean_answer = _CITATION_BRACKET.sub(_replace_bracket, answer) if _CITATION_BRACKET else answer

    # Safety net: strip any stray internal-id bracket the pattern above missed
    # (e.g. an id the model invented for a chunk that wasn't retrieved), so raw
    # ids are never shown to the user — hex chunk ids and live_sql_* alike.
    clean_answer = re.sub(
        rf"\[\s*{_INTERNAL_ID}(?:\s*,\s*{_INTERNAL_ID})*\s*\]", "", clean_answer
    )

    if sources_text:
        clean_answer += "\n\n**References**\n\n" + "\n".join(sources_text)

    return citations, clean_answer
