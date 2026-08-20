"""Core embeddings abstractions — Protocol, shared types, errors, and cache.

Kept in src/core so both the embedding adapters (src/stages/s10_embeddings.py)
and the vector store (src/stages/s11_vector_store.py) can import from here
without creating a circular dependency.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class DimensionMismatchError(ValueError):
    """Raised when a vector's dimensionality doesn't match the collection's configured size."""


@dataclass
class SparseVector:
    """Sparse embedding represented as parallel index/value arrays (Qdrant format)."""

    indices: list[int] = field(default_factory=list)
    values: list[float] = field(default_factory=list)

    def is_empty(self) -> bool:
        return len(self.indices) == 0


class QueryEmbeddingCache:
    """Process-scoped LRU cache for query embeddings.

    ARCH-10: query-time embeds are called on every request for the same
    question text. Caching them eliminates the Jina API round-trip for
    repeated questions — especially impactful for SQL queries, where the
    same analytical questions are asked frequently and the SQL result is
    already cached by SQLRetriever._result_cache.

    Keys are query strings (exact, case-sensitive). The model/adapter is
    fixed per process (ARCH-4 enforces this), so no model tag is needed in
    the key. Statistics (hits, misses, hit_rate) are exposed via stats() so
    the /api/providers/usage endpoint can surface cache efficiency.
    """

    def __init__(self, max_size: int = 256) -> None:
        self._cache: OrderedDict[str, tuple[list[float], SparseVector]] = OrderedDict()
        self._max_size = max_size
        self._hits = 0
        self._misses = 0

    def get(self, text: str) -> tuple[list[float], SparseVector] | None:
        if text not in self._cache:
            self._misses += 1
            return None
        self._cache.move_to_end(text)
        self._hits += 1
        return self._cache[text]

    def put(self, text: str, dense: list[float], sparse: SparseVector) -> None:
        self._cache[text] = (dense, sparse)
        self._cache.move_to_end(text)
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def stats(self) -> dict[str, object]:
        total = self._hits + self._misses
        return {
            "size": len(self._cache),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total else 0.0,
        }

    def clear(self) -> None:
        self._cache.clear()
        self._hits = 0
        self._misses = 0


@runtime_checkable
class EmbeddingAdapter(Protocol):
    """Protocol for embedding backend adapters — implement to add a new provider.

    Each adapter declares the model it wraps and the output dimensionality.
    EmbeddingService uses these to enforce the ARCH-4 invariant: a fallback
    adapter whose vector_dim differs from the primary is never used.
    """

    model_id: str
    vector_dim: int
    supports_sparse: bool

    async def embed(
        self,
        texts: list[str],
        task: str = "retrieval.passage",
    ) -> tuple[list[list[float]], list[SparseVector]]:
        """Embed a batch of texts. Returns (dense_vectors, sparse_vectors).

        sparse_vectors must always have len == len(texts). Adapters that
        don't support sparse must return a list of empty SparseVectors.
        """
        ...
