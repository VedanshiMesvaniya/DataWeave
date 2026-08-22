"""Tests for src.core.query_cache — the per-chat retrieval cache that lets a
repeated question within the same chat skip re-embedding/reranking/SQL-gen."""

import time

from src.core.query_cache import RetrievalCache, _normalize


def test_normalize_collapses_trivial_differences():
    assert _normalize("  Hey  What's the Revenue?  ") == "hey what's the revenue"
    assert _normalize("hey whats the revenue") == "hey whats the revenue"
    assert _normalize("What is EMO?") == _normalize("what is emo")


def test_miss_on_empty_cache():
    cache = RetrievalCache()
    assert cache.get("chat-1", "hello") is None


def test_hit_on_repeated_question_same_chat():
    cache = RetrievalCache()
    cache.set("chat-1", "What is EMO?", vector_chunks=["v1"], sql_chunks=[], sql_infra_error=False)

    hit = cache.get("chat-1", "what is emo?")  # different case/punctuation

    assert hit is not None
    assert hit.vector_chunks == ["v1"]
    assert hit.sql_chunks == []
    assert hit.sql_infra_error is False


def test_miss_on_different_chat():
    cache = RetrievalCache()
    cache.set("chat-1", "What is EMO?", vector_chunks=["v1"], sql_chunks=[], sql_infra_error=False)

    assert cache.get("chat-2", "What is EMO?") is None


def test_miss_on_different_question():
    cache = RetrievalCache()
    cache.set("chat-1", "What is EMO?", vector_chunks=["v1"], sql_chunks=[], sql_infra_error=False)

    assert cache.get("chat-1", "What is a robot?") is None


def test_no_chat_id_never_caches():
    cache = RetrievalCache()
    cache.set(None, "What is EMO?", vector_chunks=["v1"], sql_chunks=[], sql_infra_error=False)
    assert cache.get(None, "What is EMO?") is None
    assert cache.get("", "What is EMO?") is None


def test_invalidate_all_clears_every_chat():
    cache = RetrievalCache()
    cache.set("chat-1", "q1", vector_chunks=["v1"], sql_chunks=[], sql_infra_error=False)
    cache.set("chat-2", "q2", vector_chunks=["v2"], sql_chunks=[], sql_infra_error=False)

    cache.invalidate_all()

    assert cache.get("chat-1", "q1") is None
    assert cache.get("chat-2", "q2") is None


def test_invalidate_chat_only_clears_that_chat():
    cache = RetrievalCache()
    cache.set("chat-1", "q1", vector_chunks=["v1"], sql_chunks=[], sql_infra_error=False)
    cache.set("chat-2", "q2", vector_chunks=["v2"], sql_chunks=[], sql_infra_error=False)

    cache.invalidate_chat("chat-1")

    assert cache.get("chat-1", "q1") is None
    assert cache.get("chat-2", "q2") is not None


def test_entry_expires_after_ttl(monkeypatch):
    import src.core.query_cache as qc

    monkeypatch.setattr(qc, "TTL_SECONDS", 0.05)
    cache = RetrievalCache()
    cache.set("chat-1", "q1", vector_chunks=["v1"], sql_chunks=[], sql_infra_error=False)

    assert cache.get("chat-1", "q1") is not None
    time.sleep(0.1)
    assert cache.get("chat-1", "q1") is None
