"""In-memory, per-chat cache of retrieval results.

If the same question (or a trivially-reworded one) is asked again within the
same chat session, retrieval — vector search, reranking, and SQL generation —
is the expensive part: embedding calls, reranker API calls, and an LLM call
just to build the SQL query. Re-running all of that for an identical question
burns tokens and quota for no new information. This cache lets the query
pipeline skip straight to generation and reuse what was already fetched.

Scope and invalidation:
- Keyed by (chat_id, normalized question) — a repeat only hits within the
  SAME chat session; a different chat (or a fresh session) always fetches
  live, so this never leaks context across conversations.
- Entries expire after TTL_SECONDS so a long-running chat doesn't serve
  arbitrarily stale results if the person never explicitly changes topic.
- invalidate_all() must be called whenever documents are ingested, replaced,
  or deleted, or the live DB schema changes — the underlying data just
  changed, so every cached retrieval could now be wrong or incomplete.
- Process-local, in-memory only. Restarting the server clears it, which is
  fine — this is a cost-saving optimization, not a correctness store.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

from src.models.schemas import RetrievedChunk

TTL_SECONDS = 30 * 60  # 30 minutes
_MAX_ENTRIES_PER_CHAT = 50


def _normalize(question: str) -> str:
    """Collapse whitespace/case/trailing punctuation so trivially different
    phrasings of the same question ('Hey  what's the revenue?' vs
    'hey whats the revenue') still hit the same cache entry."""
    q = question.strip().lower()
    q = re.sub(r"\s+", " ", q)
    q = re.sub(r"[?!.]+$", "", q)
    return q


@dataclass
class RetrievalCacheEntry:
    vector_chunks: list[RetrievedChunk]
    sql_chunks: list[RetrievedChunk]
    sql_infra_error: bool
    created_at: float = field(default_factory=time.monotonic)


class RetrievalCache:
    """Thread-safe, per-chat cache of retrieval (not generation) results."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, dict[str, RetrievalCacheEntry]] = {}

    def get(self, chat_id: str | None, question: str) -> RetrievalCacheEntry | None:
        if not chat_id:
            return None
        key = _normalize(question)
        with self._lock:
            chat_cache = self._store.get(chat_id)
            if not chat_cache:
                return None
            entry = chat_cache.get(key)
            if entry is None:
                return None
            if time.monotonic() - entry.created_at > TTL_SECONDS:
                del chat_cache[key]
                return None
            return entry

    def set(
        self,
        chat_id: str | None,
        question: str,
        *,
        vector_chunks: list[RetrievedChunk],
        sql_chunks: list[RetrievedChunk],
        sql_infra_error: bool,
    ) -> None:
        if not chat_id:
            return
        key = _normalize(question)
        with self._lock:
            chat_cache = self._store.setdefault(chat_id, {})
            chat_cache[key] = RetrievalCacheEntry(
                vector_chunks=vector_chunks,
                sql_chunks=sql_chunks,
                sql_infra_error=sql_infra_error,
            )
            # Cap growth per chat — evict the single oldest entry once over
            # the limit, rather than tracking a full LRU for a rare edge case.
            if len(chat_cache) > _MAX_ENTRIES_PER_CHAT:
                oldest_key = min(chat_cache, key=lambda k: chat_cache[k].created_at)
                if oldest_key != key:
                    del chat_cache[oldest_key]

    def invalidate_chat(self, chat_id: str) -> None:
        """Drop cached retrievals for one chat (e.g. it was deleted)."""
        with self._lock:
            self._store.pop(chat_id, None)

    def invalidate_all(self) -> None:
        """Drop every cached retrieval across all chats.

        Call this whenever the underlying documents or live database change
        — ingestion, replacement, deletion — since a cache hit would
        otherwise serve retrieval results from before the change.
        """
        with self._lock:
            self._store.clear()


_shared_cache: RetrievalCache | None = None


def get_shared_retrieval_cache() -> RetrievalCache:
    """Process-wide cache instance, mirroring get_shared_rate_limiter()."""
    global _shared_cache
    if _shared_cache is None:
        _shared_cache = RetrievalCache()
    return _shared_cache
