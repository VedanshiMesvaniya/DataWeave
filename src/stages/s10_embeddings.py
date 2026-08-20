"""Stage 10 — Embeddings.

Jina v3 as primary (genuinely permanent free tier), Gemini text-embedding-004
as overflow when Jina's RPM is exhausted.

ARCH-3: Jina and Gemini are concrete EmbeddingAdapter implementations.
        Swap or add adapters without touching EmbeddingService.

ARCH-4: EmbeddingService refuses to fall back to an adapter whose vector_dim
        differs from the primary's. A mismatch would silently write wrong-sized
        vectors into the Qdrant collection, corrupting search. We raise
        DimensionMismatchError instead.

Sparse vector support: JinaEmbeddingAdapter requests return_sparse=true, giving
both dense (1024-d) and sparse vectors in one API call. GeminiEmbeddingAdapter
returns empty sparse vectors; the retrieval layer degrades gracefully to
dense-only search.
"""

from __future__ import annotations

import logging

import httpx

from src.core.config import settings
from src.core.embeddings import (
    DimensionMismatchError,
    EmbeddingAdapter,
    QueryEmbeddingCache,
    SparseVector,
)
from src.core.rate_limiter import RateLimiter, get_shared_rate_limiter
from src.models.schemas import Chunk

logger = logging.getLogger(__name__)

_JINA_EMBED_URL = "https://api.jina.ai/v1/embeddings"
_GEMINI_EMBED_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Re-export so existing callers that do
#   from src.stages.s10_embeddings import SparseVector
# keep working without changes.
__all__ = [
    "DimensionMismatchError",
    "EmbeddingAdapter",
    "EmbeddingService",
    "GeminiEmbeddingAdapter",
    "JinaEmbeddingAdapter",
    "QueryEmbeddingCache",
    "SparseVector",
    "get_query_embedding_cache",
]

# ---------------------------------------------------------------------------
# Process-scoped query embedding cache (ARCH-10)
# ---------------------------------------------------------------------------

_query_cache: QueryEmbeddingCache | None = None


def get_query_embedding_cache() -> QueryEmbeddingCache:
    """Return the process-scoped singleton query embedding cache.

    Lazily initialised on first call so settings are fully loaded first.
    """
    global _query_cache
    if _query_cache is None:
        _query_cache = QueryEmbeddingCache(max_size=settings.embedding_cache_size)
    return _query_cache


def _empty_sparse(n: int) -> list[SparseVector]:
    return [SparseVector() for _ in range(n)]


# ---------------------------------------------------------------------------
# Concrete adapters
# ---------------------------------------------------------------------------

class JinaEmbeddingAdapter:
    """Jina Embeddings v3 — 1024-dimensional dense + sparse."""

    model_id = "jina-embeddings-v3"
    vector_dim = 1024
    supports_sparse = True

    _BATCH_SIZE = 64

    def __init__(self, api_key: str, rate_limiter: RateLimiter) -> None:
        self._api_key = api_key
        self._rate_limiter = rate_limiter
        self._http: httpx.AsyncClient | None = None

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=60.0)
        return self._http

    async def embed(
        self,
        texts: list[str],
        task: str = "retrieval.passage",
    ) -> tuple[list[list[float]], list[SparseVector]]:
        http = self._get_http()
        all_dense: list[list[float]] = []
        all_sparse: list[SparseVector] = []

        for i in range(0, len(texts), self._BATCH_SIZE):
            batch = texts[i : i + self._BATCH_SIZE]
            await self._rate_limiter.acquire("jina")

            response = await http.post(
                _JINA_EMBED_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model_id,
                    "input": batch,
                    "task": task,
                    "return_sparse": True,
                },
            )
            response.raise_for_status()
            data = response.json()

            for emb in sorted(data["data"], key=lambda x: x["index"]):
                all_dense.append(emb["embedding"])
                sparse_raw = emb.get("sparse_embedding")
                if sparse_raw:
                    all_sparse.append(SparseVector(
                        indices=[int(k) for k in sparse_raw.keys()],
                        values=[float(v) for v in sparse_raw.values()],
                    ))
                else:
                    all_sparse.append(SparseVector())

        return all_dense, all_sparse


class GeminiEmbeddingAdapter:
    """Gemini text-embedding-004 — 768-dimensional dense only.

    Note: 768 ≠ 1024 (Jina). This adapter cannot safely substitute for Jina
    once the Qdrant collection has been created with 1024-d vectors.
    EmbeddingService enforces this at runtime via DimensionMismatchError.
    """

    model_id = "text-embedding-004"
    vector_dim = 768
    supports_sparse = False

    _BATCH_SIZE = 100

    def __init__(self, api_key: str, rate_limiter: RateLimiter) -> None:
        self._api_key = api_key
        self._rate_limiter = rate_limiter
        self._http: httpx.AsyncClient | None = None

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=60.0)
        return self._http

    async def embed(
        self,
        texts: list[str],
        task: str = "retrieval.passage",
    ) -> tuple[list[list[float]], list[SparseVector]]:
        http = self._get_http()
        all_vectors: list[list[float]] = []

        for i in range(0, len(texts), self._BATCH_SIZE):
            batch = texts[i : i + self._BATCH_SIZE]
            await self._rate_limiter.acquire("gemini")

            url = f"{_GEMINI_EMBED_URL}/text-embedding-004:batchEmbedContents?key={self._api_key}"
            body = [
                {"model": "models/text-embedding-004", "content": {"parts": [{"text": t}]}}
                for t in batch
            ]
            response = await http.post(url, json={"requests": body})
            response.raise_for_status()
            data = response.json()

            for emb in data.get("embeddings", []):
                all_vectors.append(emb["values"])

        return all_vectors, _empty_sparse(len(all_vectors))


# ---------------------------------------------------------------------------
# Service — adapter orchestration + ARCH-4 guard
# ---------------------------------------------------------------------------

class EmbeddingService:
    """Orchestrates embedding adapters: primary first, fallback only when safe.

    ARCH-4: The fallback adapter is never used when its vector_dim differs from
    the primary's. Mixing dimensions would silently write incompatible vectors
    into the Qdrant collection, breaking cosine similarity. We raise
    DimensionMismatchError instead, surfacing the misconfiguration immediately.

    Public API is unchanged from the previous monolithic implementation so all
    callers (IngestionPipeline, QueryPipeline, Retriever) work without change.
    """

    def __init__(
        self,
        rate_limiter: RateLimiter | None = None,
        primary: EmbeddingAdapter | None = None,
        fallback: EmbeddingAdapter | None = None,
    ) -> None:
        rl = rate_limiter or get_shared_rate_limiter()
        self._primary: EmbeddingAdapter = primary or self._default_primary(rl)
        self._fallback: EmbeddingAdapter | None = fallback if fallback is not None else self._default_fallback(rl)

    # ------------------------------------------------------------------
    # Adapter factories
    # ------------------------------------------------------------------

    @staticmethod
    def _default_primary(rl: RateLimiter) -> EmbeddingAdapter:
        if settings.jina_api_key:
            return JinaEmbeddingAdapter(settings.jina_api_key, rl)
        if settings.gemini_api_key:
            return GeminiEmbeddingAdapter(settings.gemini_api_key, rl)
        raise RuntimeError(
            "No embedding provider available — set JINA_API_KEY or GEMINI_API_KEY"
        )

    @staticmethod
    def _default_fallback(rl: RateLimiter) -> EmbeddingAdapter | None:
        if settings.jina_api_key and settings.gemini_api_key:
            return GeminiEmbeddingAdapter(settings.gemini_api_key, rl)
        return None

    # ------------------------------------------------------------------
    # Properties (ARCH-4 — collection creation reads these)
    # ------------------------------------------------------------------

    @property
    def vector_dim(self) -> int:
        """Dimensionality of vectors produced by the primary adapter."""
        return self._primary.vector_dim

    @property
    def model_id(self) -> str:
        """Model identifier of the primary adapter."""
        return self._primary.model_id

    # ------------------------------------------------------------------
    # Public embed interface
    # ------------------------------------------------------------------

    async def embed_chunks(
        self, chunks: list[Chunk]
    ) -> tuple[list[list[float]], list[SparseVector]]:
        return await self.embed_texts([c.content for c in chunks])

    async def embed_texts(
        self, texts: list[str]
    ) -> tuple[list[list[float]], list[SparseVector]]:
        """Embed texts. Returns (dense_vectors, sparse_vectors).

        Falls back to the secondary adapter only when its vector_dim matches
        the primary's. Raises DimensionMismatchError otherwise.
        """
        if not texts:
            return [], []

        try:
            return await self._primary.embed(texts)
        except Exception as exc:
            logger.warning(
                "Primary adapter %s failed: %s", self._primary.model_id, exc
            )

        if self._fallback is None:
            raise RuntimeError(
                f"Embedding failed and no fallback configured "
                f"(primary: {self._primary.model_id})"
            )

        if self._fallback.vector_dim != self._primary.vector_dim:
            raise DimensionMismatchError(
                f"Cannot fall back from {self._primary.model_id} "
                f"({self._primary.vector_dim}d) to {self._fallback.model_id} "
                f"({self._fallback.vector_dim}d) — dimension mismatch would "
                f"corrupt the Qdrant collection. Either fix the primary adapter "
                f"or configure a same-dimension fallback."
            )

        logger.warning("Falling back to %s", self._fallback.model_id)
        return await self._fallback.embed(texts)

    async def embed_query(self, query: str) -> tuple[list[float], SparseVector]:
        """Embed a single query string. Returns (dense_vector, sparse_vector).

        ARCH-10: checks the process-scoped QueryEmbeddingCache before calling
        the embedding API. A cache hit skips the network round-trip entirely —
        especially valuable for SQL queries, where the same analytical question
        is asked repeatedly and the SQL result is already cached by
        SQLRetriever._result_cache.
        """
        cache = get_query_embedding_cache()
        cached = cache.get(query)
        if cached is not None:
            logger.debug("Embedding cache hit (len=%d chars)", len(query))
            return cached

        dense_list, sparse_list = await self.embed_texts([query])
        result = dense_list[0], sparse_list[0]
        cache.put(query, result[0], result[1])
        return result

    async def embed_queries(self, queries: list[str]) -> list[list[float]]:
        """Embed multiple queries (dense only). Used for batch retrieval tests."""
        dense_list, _ = await self.embed_texts(queries)
        return dense_list
