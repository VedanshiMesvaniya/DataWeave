"""Tests for ARCH-3 (EmbeddingAdapter Protocol) and ARCH-4 (dimension guard).

ARCH-3: concrete adapters satisfy the EmbeddingAdapter Protocol; EmbeddingService
        delegates to whichever primary adapter is injected.
ARCH-4: EmbeddingService refuses to fall back when vector dims differ; QdrantStore
        rejects upserts whose vector dimension doesn't match the collection's.
"""

from __future__ import annotations

import pytest

from src.core.embeddings import DimensionMismatchError, EmbeddingAdapter, SparseVector
from src.stages.s10_embeddings import (
    EmbeddingService,
    GeminiEmbeddingAdapter,
    JinaEmbeddingAdapter,
)
from src.stages.s11_vector_store import QdrantStore


# ---------------------------------------------------------------------------
# Lightweight fake adapters for unit tests
# ---------------------------------------------------------------------------

class _FakeAdapter:
    """Configurable fake that satisfies EmbeddingAdapter Protocol."""

    def __init__(
        self,
        *,
        model_id: str = "fake-model",
        vector_dim: int = 1024,
        supports_sparse: bool = False,
        fail: bool = False,
    ) -> None:
        self.model_id = model_id
        self.vector_dim = vector_dim
        self.supports_sparse = supports_sparse
        self._fail = fail
        self.calls = 0

    async def embed(
        self, texts: list[str], task: str = "retrieval.passage"
    ) -> tuple[list[list[float]], list[SparseVector]]:
        if self._fail:
            raise RuntimeError("adapter error")
        self.calls += 1
        dense = [[0.1] * self.vector_dim for _ in texts]
        sparse = [SparseVector() for _ in texts]
        return dense, sparse


# ---------------------------------------------------------------------------
# ARCH-3 — Protocol conformance
# ---------------------------------------------------------------------------

def test_jina_adapter_satisfies_protocol():
    """JinaEmbeddingAdapter must be an instance of EmbeddingAdapter at runtime."""
    from unittest.mock import MagicMock
    rl = MagicMock()
    adapter = JinaEmbeddingAdapter(api_key="key", rate_limiter=rl)
    assert isinstance(adapter, EmbeddingAdapter)


def test_gemini_adapter_satisfies_protocol():
    from unittest.mock import MagicMock
    rl = MagicMock()
    adapter = GeminiEmbeddingAdapter(api_key="key", rate_limiter=rl)
    assert isinstance(adapter, EmbeddingAdapter)


def test_fake_adapter_satisfies_protocol():
    """Third-party / test adapters satisfy the protocol without inheriting anything."""
    assert isinstance(_FakeAdapter(), EmbeddingAdapter)


@pytest.mark.asyncio
async def test_service_uses_primary_adapter():
    primary = _FakeAdapter(vector_dim=1024)
    svc = EmbeddingService(primary=primary, fallback=None)
    dense, sparse = await svc.embed_texts(["hello", "world"])
    assert primary.calls == 1
    assert len(dense) == 2
    assert all(len(v) == 1024 for v in dense)


@pytest.mark.asyncio
async def test_service_exposes_primary_metadata():
    primary = _FakeAdapter(model_id="my-model", vector_dim=512)
    svc = EmbeddingService(primary=primary, fallback=None)
    assert svc.vector_dim == 512
    assert svc.model_id == "my-model"


@pytest.mark.asyncio
async def test_service_empty_texts_returns_empty():
    svc = EmbeddingService(primary=_FakeAdapter(), fallback=None)
    dense, sparse = await svc.embed_texts([])
    assert dense == []
    assert sparse == []


# ---------------------------------------------------------------------------
# ARCH-4 — dimension mismatch guard in EmbeddingService
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fallback_used_when_dims_match():
    """Fallback is allowed when both adapters produce the same dimensionality."""
    primary = _FakeAdapter(vector_dim=1024, fail=True)
    fallback = _FakeAdapter(model_id="fallback-1024", vector_dim=1024)
    svc = EmbeddingService(primary=primary, fallback=fallback)

    dense, _ = await svc.embed_texts(["hi"])
    assert fallback.calls == 1
    assert len(dense[0]) == 1024


@pytest.mark.asyncio
async def test_fallback_blocked_when_dims_differ():
    """Fallback is refused when its vector_dim differs from the primary's."""
    primary = _FakeAdapter(model_id="jina-1024", vector_dim=1024, fail=True)
    fallback = _FakeAdapter(model_id="gemini-768", vector_dim=768)
    svc = EmbeddingService(primary=primary, fallback=fallback)

    with pytest.raises(DimensionMismatchError, match="1024d.*768d|768d.*1024d"):
        await svc.embed_texts(["hi"])

    assert fallback.calls == 0  # never called


@pytest.mark.asyncio
async def test_no_fallback_raises_runtime_error():
    primary = _FakeAdapter(fail=True)
    svc = EmbeddingService(primary=primary, fallback=None)

    with pytest.raises(RuntimeError, match="no fallback configured"):
        await svc.embed_texts(["hi"])


# ---------------------------------------------------------------------------
# ARCH-4 — dimension guard in QdrantStore.upsert()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_qdrant_store_derives_size_from_service():
    svc = EmbeddingService(primary=_FakeAdapter(vector_dim=512), fallback=None)
    store = QdrantStore(embedding_service=svc)
    assert store._vector_size == 512


@pytest.mark.asyncio
async def test_qdrant_store_derives_model_from_service():
    svc = EmbeddingService(primary=_FakeAdapter(model_id="my-embed", vector_dim=256), fallback=None)
    store = QdrantStore(embedding_service=svc)
    assert store._embedding_model == "my-embed"


@pytest.mark.asyncio
async def test_qdrant_upsert_rejects_wrong_dimension():
    """upsert() raises DimensionMismatchError before touching the network."""
    from src.models.schemas import Chunk

    store = QdrantStore(vector_size=1024)

    chunk = Chunk(
        chunk_id="c1",
        document_id="d1",
        chunk_type="prose",
        content="hello",
        token_count=1,
    )
    wrong_dim_vector = [0.1] * 768  # 768 ≠ 1024

    with pytest.raises(DimensionMismatchError, match="768.*1024|1024.*768"):
        await store.upsert([chunk], [wrong_dim_vector])


@pytest.mark.asyncio
async def test_qdrant_upsert_rejects_first_bad_vector_in_batch():
    """The guard catches dimension errors anywhere in the batch, not just index 0."""
    from src.models.schemas import Chunk

    store = QdrantStore(vector_size=1024)
    store._embedding_model = "test-model"

    def _chunk(i: int) -> Chunk:
        return Chunk(
            chunk_id=f"c{i}", document_id="d1", chunk_type="prose",
            content="x", token_count=1,
        )

    chunks = [_chunk(i) for i in range(3)]
    vectors = [
        [0.1] * 1024,  # ok
        [0.1] * 1024,  # ok
        [0.1] * 512,   # bad — index 2
    ]

    with pytest.raises(DimensionMismatchError, match="index 2"):
        await store.upsert(chunks, vectors)
