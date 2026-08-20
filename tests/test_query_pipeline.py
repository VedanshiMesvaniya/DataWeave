"""Integration tests for the Query Pipeline."""

import pytest
from unittest.mock import AsyncMock

from src.models.schemas import QueryResult, Chunk, RetrievedChunk, ChunkType, DocumentType, TokenUsage
from src.pipeline.query import QueryPipeline, _is_document_listing_query
from src.stages.s10_embeddings import SparseVector


@pytest.mark.parametrize(
    "question",
    [
        "what files do you have",
        "list all documents",
        "how many documents do you have",
        "what's in your knowledge base",
        "list docs",
    ],
)
def test_document_listing_true(question):
    assert _is_document_listing_query(question) is True


@pytest.mark.parametrize(
    "question",
    [
        # Business-entity scoped → live-data question, NOT a corpus listing.
        "how many files did customer Acme upload",
        "list all orders by revenue",
        "how many invoices does each customer have",
        "show documents attached to order 42",
        # No listing phrase at all.
        "what is the total revenue in 2025",
    ],
)
def test_document_listing_false_for_data_questions(question):
    assert _is_document_listing_query(question) is False


@pytest.fixture
def mock_router():
    router = AsyncMock()
    
    async def mock_chat(*args, **kwargs):
        return "This is a mock answer based on the context."

    async def mock_chat_stream(*args, **kwargs):
        yield "This "
        yield "is a mock "
        yield "stream answer."

    router.chat = mock_chat
    router.chat_stream = mock_chat_stream
    # The generator reads router.last_used for QueryResult.model_used; the real
    # router sets this to a "provider/model" string once a call completes.
    router.last_used = "mock/model"
    # The generator also reads router.usage (a real TokenUsage the router
    # accumulates per request) and copies it onto the QueryResult.
    router.usage = TokenUsage(input_tokens=120, output_tokens=45, calls=1, provider="mock", model="model")
    return router


@pytest.fixture
def mock_store():
    store = AsyncMock()
    return store


@pytest.fixture
def mock_embeddings():
    service = AsyncMock()
    
    async def mock_embed_queries(queries):
        return [[0.1] * 384 for _ in queries]
        
    async def mock_embed_query(query):
        return ([0.1] * 384, SparseVector())
        
    service.embed_queries = mock_embed_queries
    service.embed_query = mock_embed_query
    return service


@pytest.mark.asyncio
async def test_query_pipeline_empty_retrieval(mock_router, mock_store, mock_embeddings):
    # Setup mock to return no chunks
    mock_store.search_hybrid = AsyncMock(return_value=[])
    
    pipeline = QueryPipeline(
        router=mock_router,
        vector_store=mock_store,
        embedding_service=mock_embeddings
    )
    
    result = await pipeline.query("What is the capital of France?")
    
    assert isinstance(result, QueryResult)
    assert result.query == "What is the capital of France?"
    assert "No relevant documents found" in result.answer
    assert result.chunks_retrieved == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["", "   ", "\n\t"])
async def test_query_pipeline_empty_or_whitespace_input(
    question, mock_router, mock_store, mock_embeddings
):
    """Empty / whitespace-only input must not crash — no source matches, so the
    pipeline returns the graceful no-results message."""
    mock_store.search_hybrid = AsyncMock(return_value=[])
    pipeline = QueryPipeline(
        router=mock_router, vector_store=mock_store, embedding_service=mock_embeddings
    )
    # SQL stage abstains (no DB in this unit test).
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[])

    result = await pipeline.query(question)

    assert isinstance(result, QueryResult)
    assert "No relevant documents found" in result.answer


@pytest.mark.asyncio
async def test_sql_provider_outage_reports_unavailable_not_missing(
    mock_router, mock_store, mock_embeddings
):
    """When the SQL path found nothing only because its models were unreachable
    (not because the data is missing), the answer must say so — not the
    misleading 'no documents' message."""
    from src.pipeline.query import _SQL_UNAVAILABLE_MSG

    mock_store.search_hybrid = AsyncMock(return_value=[])  # no documents either
    pipeline = QueryPipeline(
        router=mock_router, vector_store=mock_store, embedding_service=mock_embeddings
    )
    pipeline._sql_retriever = AsyncMock()
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[])
    pipeline._sql_retriever.last_infra_error = "All providers exhausted for task 'reasoning'"

    result = await pipeline.query("total revenue in 2025")

    assert result.answer == _SQL_UNAVAILABLE_MSG
    assert result.reasoning_task == "sql_unavailable"


@pytest.mark.asyncio
async def test_no_infra_error_keeps_normal_no_results_message(
    mock_router, mock_store, mock_embeddings
):
    """A clean 'nothing found' (SQL abstained, no docs) keeps the ordinary
    message — the outage wording only appears on a real provider outage."""
    mock_store.search_hybrid = AsyncMock(return_value=[])
    pipeline = QueryPipeline(
        router=mock_router, vector_store=mock_store, embedding_service=mock_embeddings
    )
    pipeline._sql_retriever = AsyncMock()
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[])
    pipeline._sql_retriever.last_infra_error = None

    result = await pipeline.query("something with no answer anywhere")

    assert "No relevant documents found" in result.answer
    assert result.reasoning_task == "no_results"


@pytest.mark.asyncio
async def test_sql_outage_streaming_reports_unavailable(
    mock_router, mock_store, mock_embeddings
):
    from src.pipeline.query import _SQL_UNAVAILABLE_MSG

    mock_store.search_hybrid = AsyncMock(return_value=[])
    pipeline = QueryPipeline(
        router=mock_router, vector_store=mock_store, embedding_service=mock_embeddings
    )
    pipeline._sql_retriever = AsyncMock()
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[])
    pipeline._sql_retriever.last_infra_error = "All providers exhausted for task 'reasoning'"

    chunks = [c async for c in pipeline.query_stream("total revenue in 2025")]
    final = chunks[-1]
    assert final.reasoning_task == "sql_unavailable"
    assert final.answer == _SQL_UNAVAILABLE_MSG


@pytest.mark.asyncio
async def test_query_pipeline_success(mock_router, mock_store, mock_embeddings):
    # Setup mock to return some chunks
    mock_chunk = Chunk(
        chunk_id="chunk-1",
        document_id="doc-1",
        content="Paris is the capital of France.",
        chunk_type=ChunkType.PROSE,
        page_number=1,
        document_type=DocumentType.GENERAL,
        source_file="test.txt"
    )
    retrieved_chunk = RetrievedChunk(chunk=mock_chunk, score=0.9)
    mock_store.search_hybrid = AsyncMock(return_value=[retrieved_chunk])
    
    pipeline = QueryPipeline(
        router=mock_router,
        vector_store=mock_store,
        embedding_service=mock_embeddings
    )
    
    result = await pipeline.query("What is the capital of France?")
    
    assert isinstance(result, QueryResult)
    assert result.query == "What is the capital of France?"
    assert "This is a mock answer" in result.answer
    assert result.chunks_retrieved == 1
    assert result.chunks_after_rerank == 1


@pytest.mark.asyncio
async def test_query_pipeline_stream_success(mock_router, mock_store, mock_embeddings):
    # Setup mock to return some chunks
    mock_chunk = Chunk(
        chunk_id="chunk-1",
        document_id="doc-1",
        content="Paris is the capital of France.",
        chunk_type=ChunkType.PROSE,
        page_number=1,
        document_type=DocumentType.GENERAL,
        source_file="test.txt"
    )
    retrieved_chunk = RetrievedChunk(chunk=mock_chunk, score=0.9)
    mock_store.search_hybrid = AsyncMock(return_value=[retrieved_chunk])
    
    pipeline = QueryPipeline(
        router=mock_router,
        vector_store=mock_store,
        embedding_service=mock_embeddings
    )
    
    chunks = []
    async for chunk in pipeline.query_stream("What is the capital of France?"):
        chunks.append(chunk)
        
    assert len(chunks) > 0
    final_result = chunks[-1]
    
    assert isinstance(final_result, QueryResult)
    assert final_result.query == "What is the capital of France?"
    assert "stream answer" in final_result.answer
    assert final_result.chunks_retrieved == 1
    # Token usage from the router is copied onto the result and serializes with
    # a computed total, so the UI can show a breakdown per answer.
    assert final_result.usage.total_tokens == 165
    assert final_result.usage.model_dump()["total_tokens"] == 165


def _sql_table_md() -> str:
    return (
        "SQL Query Executed: `SELECT model, units FROM gpu_sales`\n\n"
        "| model | units |\n"
        "| --- | --- |\n"
        "| A100 | 12 |"
    )


def _sql_retrieved() -> RetrievedChunk:
    return RetrievedChunk(
        chunk=Chunk(
            chunk_id="live_sql_001",
            document_id="live_db",
            content=_sql_table_md(),
            chunk_type=ChunkType.SQL_RESULT,
            page_number=0,
            document_type=DocumentType.GENERAL,
            source_file="live_database (gpu_sales table)",
        ),
        score=1.0,
        retrieval_method="text-to-sql",
    )


def _doc_retrieved() -> RetrievedChunk:
    return RetrievedChunk(
        chunk=Chunk(
            chunk_id="chunk-1",
            document_id="doc-1",
            content="Paris is the capital of France.",
            chunk_type=ChunkType.PROSE,
            page_number=1,
            document_type=DocumentType.GENERAL,
            source_file="test.txt",
        ),
        score=0.9,
    )


@pytest.mark.asyncio
async def test_sql_plus_document_blends_prose_and_appends_exact_table(
    mock_router, mock_store, mock_embeddings
):
    """When BOTH a SQL result and a document chunk are retrieved, the answer is
    synthesized by the LLM (document context) AND the exact SQL table is appended
    verbatim — the SQL result must never silently drop to a document-only answer,
    nor short-circuit the documents. (Regression: the pipeline previously
    returned the SQL table alone via a dead intent classifier.)"""
    sql_table = _sql_table_md()
    sql_retrieved = _sql_retrieved()
    vector_retrieved = _doc_retrieved()

    mock_router.chat = AsyncMock(return_value="This is a mock answer based on the context.")
    mock_store.search_hybrid = AsyncMock(return_value=[vector_retrieved])

    pipeline = QueryPipeline(
        router=mock_router,
        vector_store=mock_store,
        embedding_service=mock_embeddings,
    )
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[sql_retrieved])
    pipeline._reranker.rerank = AsyncMock(return_value=[vector_retrieved])

    result = await pipeline.query("Show me the live database results")

    assert isinstance(result, QueryResult)
    # Both sources present: LLM prose from the document, exact SQL table appended.
    assert "This is a mock answer" in result.answer
    assert sql_table in result.answer
    # The blend path runs the LLM (not the raw sql/direct short-circuit).
    assert result.model_used == "mock/model"
    assert mock_router.chat.await_count >= 1


@pytest.mark.asyncio
async def test_sql_only_no_documents_returns_table_direct(
    mock_router, mock_store, mock_embeddings
):
    """SQL result with NO document context short-circuits to the exact table,
    with no LLM synthesis call — the solo-SQL path."""
    sql_retrieved = _sql_retrieved()

    mock_router.chat = AsyncMock(return_value="unused")
    mock_store.search_hybrid = AsyncMock(return_value=[])  # no documents match

    pipeline = QueryPipeline(
        router=mock_router,
        vector_store=mock_store,
        embedding_service=mock_embeddings,
    )
    pipeline._sql_retriever.retrieve = AsyncMock(return_value=[sql_retrieved])

    result = await pipeline.query("Show me the live database results")

    assert _sql_table_md() in result.answer
    assert result.model_used == "sql/direct"
    assert mock_router.chat.await_count == 0
