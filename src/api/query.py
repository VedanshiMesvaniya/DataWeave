"""Query API — RAG query endpoint."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.pipeline.query import QueryPipeline

logger = logging.getLogger(__name__)
router = APIRouter()


class QueryRequest(BaseModel):
    """Request body for the query endpoint."""

    question: str
    top_k: int = 30
    rerank_top_k: int = 6
    filters: dict[str, Any] | None = None
    """Optional metadata filters to constrain retrieval.

    Supported keys:
    - ``document_type`` (str): exact match on document type enum value.
    - ``source_file`` (str): substring match on the source filename.
    - ``page_number`` (int): retrieve only chunks at or after this page.
    - ``document_id`` (str): restrict to a specific ingested document.
    - ``chunk_type`` (str): exact match on chunk type (prose, table, etc.).

    Example::

        {"source_file": "q3_report.pdf", "page_number": 10}
    """


@router.post("/query")
async def query_documents(request: QueryRequest) -> dict:
    """Query ingested documents using the RAG pipeline.

    Supports optional metadata filtering via the ``filters`` field to
    constrain retrieval to specific document types, files, or page ranges.
    """
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    try:
        pipeline = QueryPipeline()
        result = await pipeline.query(request.question, filters=request.filters)
        return {
            "status": "success",
            "query": result.query,
            "answer": result.answer,
            "citations": [c.model_dump() for c in result.citations],
            "model_used": result.model_used,
            "chunks_retrieved": result.chunks_retrieved,
            "chunks_after_rerank": result.chunks_after_rerank,
            "filters_applied": request.filters,
        }
    except Exception:
        # Log the full error server-side; return a generic message so internal
        # details (paths, provider errors, keys in messages) don't leak.
        logger.exception("Query failed")
        raise HTTPException(status_code=500, detail="Query failed. Please try again.")
