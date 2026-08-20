"""Tests for citation formatting and visualization-aware query routing.

Covers two regressions:
  1. SQL-stage chunk ids (e.g. "live_sql_001") leaking into the visible answer
     because the citation formatter only knew the hex "..._chunk_0043" shape.
  2. Chart requests being routed to SQL-only (tripped by a metric keyword like
     "total") so document data was never consulted.
"""

import pytest
from unittest.mock import AsyncMock

from src.models.schemas import Chunk, ChunkType, DocumentType, RetrievedChunk
from src.pipeline.query import QueryPipeline
from src.stages.s10_embeddings import SparseVector
from src.stages.s12_s13_s14_retrieval import (
    _extract_and_format_citations,
    _is_visualization_query,
)


def _chunk(chunk_id: str, source_file: str, page: int = 1, content: str = "x") -> RetrievedChunk:
    return RetrievedChunk(
        chunk=Chunk(
            chunk_id=chunk_id,
            document_id="doc",
            content=content,
            chunk_type=ChunkType.PROSE,
            page_number=page,
            document_type=DocumentType.GENERAL,
            source_file=source_file,
        ),
        score=1.0,
    )


# ---------------------------------------------------------------------------
# Citation formatting
# ---------------------------------------------------------------------------

def test_sql_chunk_id_does_not_leak():
    """A cited live_sql_* id becomes a clean footnote, never raw text."""
    chunks = [_chunk("live_sql_001", "live_database (gpu_sales table)", page=0)]
    answer = "Total units sold were highest for the RTX 4070 [live_sql_001]."

    _, clean = _extract_and_format_citations(answer, chunks)

    assert "live_sql_001" not in clean
    assert "[1]" in clean
    assert "**References**" in clean
    assert "live_database (gpu_sales table)" in clean


def test_hex_chunk_id_becomes_footnote():
    chunks = [_chunk("abcdef12_chunk_0003", "/home/user/docs/report.pdf", page=8)]
    answer = "The score was 86.8% [abcdef12_chunk_0003]."

    _, clean = _extract_and_format_citations(answer, chunks)

    assert "abcdef12_chunk_0003" not in clean
    assert "[1]" in clean
    # Only the clean filename, never the absolute path.
    assert "report.pdf (Page 8)" in clean
    assert "/home/user/docs" not in clean


def test_invented_id_is_stripped():
    """An id the model invented (not retrieved) is removed, not shown raw."""
    chunks = [_chunk("abcdef12_chunk_0003", "report.pdf")]
    answer = "A claim [deadbeef_chunk_9999] and a real one [abcdef12_chunk_0003]."

    _, clean = _extract_and_format_citations(answer, chunks)

    assert "deadbeef_chunk_9999" not in clean
    assert "abcdef12_chunk_0003" not in clean
    assert "[1]" in clean


def test_plain_number_brackets_are_left_untouched():
    """Prose like [1] is not a chunk id and must survive verbatim."""
    chunks = [_chunk("abcdef12_chunk_0003", "report.pdf")]
    answer = "See item [1] and [2] in the list [abcdef12_chunk_0003]."

    _, clean = _extract_and_format_citations(answer, chunks)

    assert "item [1]" in clean
    assert "[2]" in clean


def test_no_chunks_returns_answer_unchanged():
    _, clean = _extract_and_format_citations("Just prose, no cites.", [])
    assert clean == "Just prose, no cites."


# ---------------------------------------------------------------------------
# Visualization intent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "query",
    [
        "make a bar chart of total units sold",
        "plot the revenue trend",
        "draw a pie chart of market share",
        "visualize the distribution of sales",
        "show me a line graph",
    ],
)
def test_is_visualization_query_true(query):
    assert _is_visualization_query(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "what is the total revenue?",
        "summarize the report",
        "who is the CEO?",
    ],
)
def test_is_visualization_query_false(query):
    assert _is_visualization_query(query) is False


# ---------------------------------------------------------------------------
# Routing: a chart request with a metric keyword must still hit the documents
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_embeddings():
    service = AsyncMock()

    async def embed_query(query):
        return ([0.1] * 384, SparseVector())

    service.embed_query = embed_query
    return service


@pytest.mark.asyncio
async def test_chart_request_with_metric_keyword_upgrades_to_both(mock_embeddings):
    """"bar chart of total units" trips the SQL keyword "total" but must still
    run vector retrieval so the chart can draw on document data."""
    router = AsyncMock()
    store = AsyncMock()
    store.search_hybrid = AsyncMock(return_value=[])

    async def mock_chat(*args, **kwargs):
        return "answer"

    router.chat = mock_chat

    pipeline = QueryPipeline(router=router, vector_store=store, embedding_service=mock_embeddings)
    # Keep the SQL stage from touching a real database.
    pipeline._sql_retriever = AsyncMock()
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[])

    await pipeline.query("make a bar chart of total units sold")

    # BOTH path → vector retrieval ran despite the "total" keyword.
    assert store.search_hybrid.called
