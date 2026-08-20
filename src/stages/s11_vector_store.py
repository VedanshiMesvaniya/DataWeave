"""Stage 11 — Vector Store.

Qdrant Cloud free cluster as primary (1GB RAM, no query metering).
Falls back to a local in-memory store for development/testing.

Hybrid search: dense cosine similarity + Jina sparse vectors fused via
Reciprocal Rank Fusion (RRF), replacing the previous BM25-lite heuristic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from src.core.config import settings
from src.core.embeddings import DimensionMismatchError, SparseVector
from src.models.schemas import Chunk, RetrievedChunk

if TYPE_CHECKING:
    from src.stages.s10_embeddings import EmbeddingService

logger = logging.getLogger(__name__)

# Module-level singletons — shared across all QdrantStore instances in the
# same process so _ensure_collection() only runs once and never wipes data.
_global_client: Any = None
_global_has_sparse: bool = False


@runtime_checkable
class VectorStore(Protocol):
    """Protocol for vector store implementations — swappable."""

    async def upsert(
        self,
        chunks: list[Chunk],
        vectors: list[list[float]],
        sparse_vectors: list[SparseVector] | None = None,
    ) -> None: ...

    async def search_dense(
        self,
        query_vector: list[float],
        top_k: int = 30,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]: ...

    async def search_hybrid(
        self,
        query_vector: list[float],
        sparse_vector: SparseVector | None = None,
        query_text: str = "",
        top_k: int = 30,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]: ...

    async def delete_document(self, document_id: str) -> None: ...
    async def set_document_active(self, document_id: str, active: bool) -> None: ...
    async def get_stats(self) -> dict[str, Any]: ...


class QdrantStore:
    """Qdrant Cloud vector store with true hybrid dense+sparse search.

    ARCH-4: Pass an EmbeddingService so the collection is created with exactly
    the right vector_size for the active embedding model. If no service is
    provided the legacy default (1024) is used, which matches Jina v3.
    """

    def __init__(
        self,
        collection_name: str = "globle_mind",
        vector_size: int = 1024,
        embedding_service: EmbeddingService | None = None,
    ) -> None:
        self._collection_name = collection_name
        if embedding_service is not None:
            self._vector_size = embedding_service.vector_dim
            self._embedding_model = embedding_service.model_id
        else:
            self._vector_size = vector_size
            self._embedding_model = "unknown"
        self._has_sparse: bool = False  # Set True once sparse collection is confirmed

    async def _get_client(self) -> Any:
        global _global_client, _global_has_sparse
        if _global_client is None:
            from qdrant_client import AsyncQdrantClient

            if settings.qdrant_url and settings.qdrant_api_key:
                _global_client = AsyncQdrantClient(
                    url=settings.qdrant_url,
                    api_key=settings.qdrant_api_key,
                )
            else:
                # Local in-memory for dev/testing
                _global_client = AsyncQdrantClient(location=":memory:")
                logger.info("Using in-memory Qdrant (no QDRANT_URL configured)")

            await self._ensure_collection(_global_client)

        self._has_sparse = _global_has_sparse
        return _global_client

    async def _ensure_collection(self, client: Any) -> None:
        """Create collection with both dense and sparse vector support.

        If the collection already exists, check whether it already has
        the sparse vector config. If not (legacy collection), recreate it.

        NOTE: sparse_vectors_config from qdrant_client is a Pydantic model,
        not a plain dict — use hasattr() to check for keys safely.
        """
        global _global_has_sparse
        from qdrant_client.models import (
            Distance,
            SparseVectorParams,
            VectorParams,
        )

        collections = await client.get_collections()
        existing_names = [c.name for c in collections.collections]

        if self._collection_name in existing_names:
            # Check if existing collection has sparse vector support.
            # In qdrant_client, sparse vectors live at:
            #   info.config.params.sparse_vectors  (a plain dict, may be None)
            # NOT at info.config.sparse_vectors_config (that field is always None).
            info = await client.get_collection(self._collection_name)
            sparse_vectors = getattr(info.config.params, "sparse_vectors", None)

            has_sparse_field = bool(
                sparse_vectors and "text_sparse" in sparse_vectors
            )

            if has_sparse_field:
                _global_has_sparse = True
                self._has_sparse = True
                logger.info(
                    "Collection '%s' already exists with sparse support",
                    self._collection_name,
                )
                await self._ensure_payload_indexes(client)
                return
            else:
                # Legacy collection without sparse — recreate it
                logger.warning(
                    "Collection '%s' exists but lacks sparse vectors — recreating with sparse support",
                    self._collection_name,
                )
                await client.delete_collection(self._collection_name)

        # Create fresh collection with dense + sparse support
        await client.create_collection(
            collection_name=self._collection_name,
            vectors_config=VectorParams(
                size=self._vector_size,
                distance=Distance.COSINE,
            ),
            sparse_vectors_config={
                "text_sparse": SparseVectorParams(),
            },
        )
        _global_has_sparse = True
        self._has_sparse = True
        logger.info(
            "Created Qdrant collection '%s' (model=%s, vector_size=%d, sparse=True)",
            self._collection_name,
            self._embedding_model,
            self._vector_size,
        )
        await self._ensure_payload_indexes(client)

    async def _ensure_payload_indexes(self, client: Any) -> None:
        """Create payload indexes required for filtered search.

        Qdrant requires an explicit payload index for any field used in a
        Filter when the collection is large enough to need one. Without this,
        queries raise a 400 "Index required" error. The calls are idempotent —
        safe to run on every startup even if the indexes already exist.
        """
        from qdrant_client.models import PayloadSchemaType

        indexes = [
            ("active", PayloadSchemaType.BOOL),
            ("document_id", PayloadSchemaType.KEYWORD),
        ]
        for field_name, field_schema in indexes:
            try:
                await client.create_payload_index(
                    collection_name=self._collection_name,
                    field_name=field_name,
                    field_schema=field_schema,
                )
                logger.info(
                    "Ensured payload index on '%s' for collection '%s'",
                    field_name,
                    self._collection_name,
                )
            except Exception as e:
                logger.warning("Could not create payload index on '%s': %s", field_name, e)

    async def upsert(
        self,
        chunks: list[Chunk],
        vectors: list[list[float]],
        sparse_vectors: list[SparseVector] | None = None,
    ) -> None:
        """Store chunks with their dense (and optionally sparse) embeddings."""
        import hashlib

        from qdrant_client.models import PointStruct

        # ARCH-4: validate every vector matches the collection's configured size
        # before touching the network. Catches a dim-mismatch from a wrong
        # fallback adapter before it corrupts the collection.
        for idx, vec in enumerate(vectors):
            if len(vec) != self._vector_size:
                raise DimensionMismatchError(
                    f"Vector at index {idx} has {len(vec)} dimensions but "
                    f"collection '{self._collection_name}' expects "
                    f"{self._vector_size} (model: {self._embedding_model}). "
                    f"Re-create the collection or align your embedding model."
                )

        client = await self._get_client()

        # Pad sparse_vectors if not provided or mismatched length
        if sparse_vectors is None or len(sparse_vectors) != len(chunks):
            sparse_vectors = [SparseVector() for _ in chunks]

        def _stable_id(chunk_id: str) -> int:
            """Stable positive integer ID from chunk_id string (63-bit to avoid Qdrant signed overflow)."""
            return int(hashlib.sha256(chunk_id.encode()).hexdigest(), 16) % (2**63)

        points = []
        for chunk, vector, sparse in zip(chunks, vectors, sparse_vectors):
            payload = {
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "content": chunk.content,
                "chunk_type": chunk.chunk_type.value,
                "page_number": chunk.page_number,
                "section_hierarchy": chunk.section_hierarchy,
                "parent_chunk_id": chunk.parent_chunk_id,
                "document_type": chunk.document_type.value,
                "source_file": chunk.source_file,
                "confidence": chunk.confidence,
                "token_count": chunk.token_count,
                # Version lifecycle: chunks are born active. A later replacement
                # flips the superseded version's chunks to active=False (see
                # set_document_active), and retrieval excludes those.
                "active": True,
            }

            # Build the vectors dict — always include dense; add sparse if available
            vectors_dict: dict[str, Any] = {"": vector}  # unnamed = dense
            if self._has_sparse and not sparse.is_empty():
                from qdrant_client.models import SparseVector as QdrantSparseVector
                vectors_dict["text_sparse"] = QdrantSparseVector(
                    indices=sparse.indices,
                    values=sparse.values,
                )

            points.append(
                PointStruct(
                    id=_stable_id(chunk.chunk_id),
                    vector=vectors_dict,
                    payload=payload,
                )
            )

        # Batch upsert
        batch_size = 100
        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            await client.upsert(
                collection_name=self._collection_name,
                points=batch,
            )

        logger.info("Upserted %d chunks to Qdrant", len(points))

    async def search_dense(
        self,
        query_vector: list[float],
        top_k: int = 30,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """Dense (semantic) vector search with optional metadata filtering."""
        client = await self._get_client()
        qdrant_filter = self._build_filter(filters)

        response = await client.query_points(
            collection_name=self._collection_name,
            query=query_vector,
            limit=top_k,
            query_filter=qdrant_filter,
        )

        return [self._point_to_retrieved_chunk(r, "dense") for r in response.points]

    async def search_hybrid(
        self,
        query_vector: list[float],
        sparse_vector: SparseVector | None = None,
        query_text: str = "",
        top_k: int = 30,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """True hybrid search: dense + sparse fused via Reciprocal Rank Fusion (RRF).

        Falls back to dense-only if sparse vector is empty (e.g., Gemini fallback).
        """
        qdrant_filter = self._build_filter(filters)

        # If no sparse vector available, degrade gracefully to dense-only
        if sparse_vector is None or sparse_vector.is_empty() or not self._has_sparse:
            return await self.search_dense(query_vector, top_k=top_k, filters=filters)

        client = await self._get_client()

        try:
            from qdrant_client.models import (
                Fusion,
                FusionQuery,
                Prefetch,
                SparseVector as QdrantSparseVector,
            )

            response = await client.query_points(
                collection_name=self._collection_name,
                prefetch=[
                    # Dense search leg
                    Prefetch(
                        query=query_vector,
                        using="",  # default (unnamed) dense vector
                        limit=top_k * 2,
                        filter=qdrant_filter,
                    ),
                    # Sparse search leg
                    Prefetch(
                        query=QdrantSparseVector(
                            indices=sparse_vector.indices,
                            values=sparse_vector.values,
                        ),
                        using="text_sparse",
                        limit=top_k * 2,
                        filter=qdrant_filter,
                    ),
                ],
                query=FusionQuery(fusion=Fusion.RRF),
                limit=top_k,
            )

            return [self._point_to_retrieved_chunk(r, "hybrid_rrf") for r in response.points]

        except Exception as e:
            logger.warning("Hybrid RRF search failed: %s — falling back to dense", e)
            return await self.search_dense(query_vector, top_k=top_k, filters=filters)

    async def delete_document(self, document_id: str) -> None:
        """Delete all chunks belonging to a document."""
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        client = await self._get_client()

        await client.delete(
            collection_name=self._collection_name,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="document_id",
                        match=MatchValue(value=document_id),
                    )
                ]
            ),
        )
        logger.info("Deleted document %s from Qdrant", document_id)

    async def set_document_active(self, document_id: str, active: bool) -> None:
        """Flip the ``active`` flag on every chunk of a document.

        Used by the replace flow: after a new version is fully indexed, the
        superseded version's chunks are marked ``active=False`` so they stop
        surfacing in retrieval without being deleted (history is preserved).
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        client = await self._get_client()
        await client.set_payload(
            collection_name=self._collection_name,
            payload={"active": active},
            points=Filter(
                must=[
                    FieldCondition(
                        key="document_id",
                        match=MatchValue(value=document_id),
                    )
                ]
            ),
        )
        logger.info("Set active=%s for document %s in Qdrant", active, document_id)

    async def get_stats(self) -> dict[str, Any]:
        """Return collection statistics."""
        client = await self._get_client()
        info = await client.get_collection(self._collection_name)
        return {
            "collection": self._collection_name,
            "points_count": info.points_count,
            "vectors_count": getattr(info, "vectors_count", getattr(info, "indexed_vectors_count", 0)),
            "has_sparse": self._has_sparse,
            "status": info.status.value,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_filter(filters: dict[str, Any] | None) -> Any:
        """Build a Qdrant Filter from a plain dict.

        Every search excludes superseded chunks: a ``must_not active == False``
        clause is always applied. It is expressed as *exclude the inactive*
        (rather than *require active*) so chunks written before the ``active``
        field existed — which have no ``active`` key — still surface. Only a
        chunk explicitly flipped to ``active=False`` by a replacement is hidden.

        Supported filter keys:
          document_type (str)  — exact match
          source_file (str)    — substring match
          page_number (int)    — minimum page number
          document_id (str)    — exact document match
          chunk_type (str)     — exact chunk type match
        """
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            MatchText,
            MatchValue,
            Range,
        )

        filters = filters or {}
        conditions = []

        if "document_type" in filters:
            conditions.append(
                FieldCondition(key="document_type", match=MatchValue(value=filters["document_type"]))
            )
        if "document_id" in filters:
            conditions.append(
                FieldCondition(key="document_id", match=MatchValue(value=filters["document_id"]))
            )
        if "chunk_type" in filters:
            conditions.append(
                FieldCondition(key="chunk_type", match=MatchValue(value=filters["chunk_type"]))
            )
        if "source_file" in filters:
            conditions.append(
                FieldCondition(key="source_file", match=MatchText(text=filters["source_file"]))
            )
        if "page_number" in filters:
            conditions.append(
                FieldCondition(key="page_number", range=Range(gte=filters["page_number"]))
            )

        # Always hide superseded chunks.
        exclude_inactive = [FieldCondition(key="active", match=MatchValue(value=False))]
        
        # Hide SQL_SCHEMA chunks from regular document search unless explicitly requested.
        # This prevents schema dumps from polluting the context window of standard QA queries.
        from src.models.schemas import ChunkType
        if not filters or "chunk_type" not in filters or filters["chunk_type"] != ChunkType.SQL_SCHEMA.value:
            exclude_inactive.append(
                FieldCondition(key="chunk_type", match=MatchValue(value=ChunkType.SQL_SCHEMA.value))
            )

        return Filter(must=conditions or None, must_not=exclude_inactive)

    @staticmethod
    def _point_to_retrieved_chunk(point: Any, method: str) -> RetrievedChunk:
        """Convert a Qdrant search result point to a RetrievedChunk."""
        payload = point.payload
        chunk = Chunk(
            chunk_id=payload.get("chunk_id", ""),
            document_id=payload.get("document_id", ""),
            chunk_type=payload.get("chunk_type", "prose"),
            content=payload.get("content", ""),
            token_count=payload.get("token_count", 0),
            page_number=payload.get("page_number", 0),
            section_hierarchy=payload.get("section_hierarchy", []),
            parent_chunk_id=payload.get("parent_chunk_id"),
            document_type=payload.get("document_type", "general"),
            source_file=payload.get("source_file", ""),
            confidence=payload.get("confidence", 1.0),
        )
        return RetrievedChunk(
            chunk=chunk,
            score=point.score,
            retrieval_method=method,
        )
