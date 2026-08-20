"""Tests for the token-saving generation optimizations:

  1. Context trimming — only the top reranked chunks reach the prompt.
  2. Cacheable system prompt — the fixed answer rules live in the system prompt
     (a stable prefix), not the per-call user message.
"""

from src.models.schemas import Chunk, ChunkType, DocumentType, RetrievedChunk
from src.stages.s12_s13_s14_retrieval import (
    _ANSWER_RULES,
    _build_system_prompt,
    _build_context,
    _extract_sql_table,
    _limit_context_chunks,
)


def _chunks(n: int) -> list[RetrievedChunk]:
    out = []
    for i in range(n):
        c = Chunk(
            chunk_id=f"c{i}",
            document_id="doc",
            content=f"content {i}",
            chunk_type=ChunkType.PROSE,
            page_number=1,
            document_type=DocumentType.GENERAL,
            source_file="f.txt",
        )
        out.append(RetrievedChunk(chunk=c, score=1.0 - i * 0.01))
    return out


# ---------------------------------------------------------------------------
# _limit_context_chunks
# ---------------------------------------------------------------------------

def test_none_limit_keeps_everything():
    chunks = _chunks(25)
    assert _limit_context_chunks(chunks, None) == chunks


def test_limit_keeps_top_n_in_order():
    chunks = _chunks(25)
    trimmed = _limit_context_chunks(chunks, 5)
    assert len(trimmed) == 5
    assert [c.chunk.chunk_id for c in trimmed] == ["c0", "c1", "c2", "c3", "c4"]


def test_limit_is_floored_at_two():
    # A misconfigured/tiny limit must never starve the model below 2 chunks.
    chunks = _chunks(10)
    assert len(_limit_context_chunks(chunks, 1)) == 2
    assert len(_limit_context_chunks(chunks, 0)) == 2


def test_limit_larger_than_available_returns_all():
    chunks = _chunks(3)
    assert len(_limit_context_chunks(chunks, 5)) == 3


def test_sql_result_is_hidden_from_prompt_but_preserved_exactly():
    sql_table = (
        "SQL Query Executed: `SELECT * FROM gpu_sales`\n\n"
        "| model | units |\n"
        "| --- | --- |\n"
        "| A100 | 12 |"
    )
    chunks = [
        RetrievedChunk(
            chunk=Chunk(
                chunk_id="live_sql_001",
                document_id="live_db",
                chunk_type=ChunkType.SQL_RESULT,
                content=sql_table,
                page_number=0,
                document_type=DocumentType.GENERAL,
                source_file="live_database (gpu_sales table)",
            ),
            score=1.0,
        )
    ]

    context = _build_context(chunks)

    assert sql_table not in context
    assert "The exact table will be appended automatically" in context
    assert _extract_sql_table(chunks) == sql_table


# ---------------------------------------------------------------------------
# Cacheable system prompt
# ---------------------------------------------------------------------------

def test_answer_rules_live_in_system_prompt():
    # The citation/formatting rules must be part of the (stable) system prompt
    # so they form a cacheable prefix instead of being re-sent per call.
    prompt = _build_system_prompt("general_qa")
    assert _ANSWER_RULES.strip() in prompt
    assert "bracketed source marker" in prompt


def test_system_prompt_is_stable_across_calls():
    # A cacheable prefix only helps if it's byte-identical every time.
    assert _build_system_prompt("general_qa") == _build_system_prompt("general_qa")
    assert _build_system_prompt("reasoning") == _build_system_prompt("reasoning")
