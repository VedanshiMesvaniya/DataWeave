"""Tests for near-duplicate chunk suppression at retrieval time.

Boilerplate shared across documents (a company motto, a repeated project
preamble, a standard disclaimer) is embedded once per file, so a query that
matches it can retrieve the same passage many times and crowd out unique
content. ``_suppress_near_duplicates`` collapses those copies, keeping the
best-scored representative, while never merging genuinely distinct passages.
"""

from src.models.schemas import Chunk, ChunkType, DocumentType, RetrievedChunk
from src.stages.s12_s13_s14_retrieval import _suppress_near_duplicates


def _chunk(chunk_id: str, content: str, document_id: str = "doc", score: float = 1.0) -> RetrievedChunk:
    c = Chunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        chunk_type=ChunkType.PROSE,
        page_number=1,
        document_type=DocumentType.GENERAL,
        source_file=f"{document_id}.txt",
    )
    return RetrievedChunk(chunk=c, score=score)


# A realistic shared preamble — long enough to trip the fuzzy path.
_BOILERPLATE = (
    "Acme Corporation is a global leader in advanced manufacturing solutions "
    "committed to sustainability innovation and operational excellence across "
    "every market we serve worldwide since our founding in nineteen eighty."
)


# ---------------------------------------------------------------------------
# Exact repeats
# ---------------------------------------------------------------------------

def test_exact_repeats_across_documents_collapse_to_one():
    # Same paragraph stored once per document → keep a single representative.
    chunks = [
        _chunk("a_intro", _BOILERPLATE, document_id="a", score=0.9),
        _chunk("b_intro", _BOILERPLATE, document_id="b", score=0.8),
        _chunk("c_intro", _BOILERPLATE, document_id="c", score=0.7),
    ]
    kept = _suppress_near_duplicates(chunks)
    assert len(kept) == 1
    # The highest-scored copy (first, since input is score-sorted) survives.
    assert kept[0].chunk.chunk_id == "a_intro"


def test_exact_match_is_whitespace_and_case_insensitive():
    a = _chunk("a", "The  QUICK   brown fox.", score=0.9)
    b = _chunk("b", "the quick brown fox", score=0.8)
    kept = _suppress_near_duplicates([a, b])
    assert len(kept) == 1
    assert kept[0].chunk.chunk_id == "a"


def test_short_exact_repeats_still_collapse():
    # Exact collapse is unconditional — it does not require the min-token length
    # that the fuzzy path does.
    a = _chunk("a", "Confidential", score=0.9)
    b = _chunk("b", "confidential", score=0.8)
    kept = _suppress_near_duplicates([a, b])
    assert len(kept) == 1


# ---------------------------------------------------------------------------
# Near repeats
# ---------------------------------------------------------------------------

def test_near_repeats_collapse_on_trivial_edit():
    # Same preamble differing only by a trailing year — a fuzzy duplicate.
    a = _chunk("a", _BOILERPLATE + " Updated 2024.", document_id="a", score=0.9)
    b = _chunk("b", _BOILERPLATE + " Updated 2025.", document_id="b", score=0.8)
    kept = _suppress_near_duplicates([a, b], similarity_threshold=0.9)
    assert len(kept) == 1
    assert kept[0].chunk.chunk_id == "a"


def test_threshold_above_one_disables_fuzzy_but_keeps_exact():
    # >1.0 threshold means "exact only": near-dups survive, exact-dups don't.
    near_a = _chunk("a", _BOILERPLATE + " Updated 2024.", score=0.9)
    near_b = _chunk("b", _BOILERPLATE + " Updated 2025.", score=0.8)
    exact_c = _chunk("c", _BOILERPLATE + " Updated 2024.", score=0.7)  # == near_a
    kept = _suppress_near_duplicates([near_a, near_b, exact_c], similarity_threshold=1.5)
    kept_ids = {c.chunk.chunk_id for c in kept}
    assert "a" in kept_ids and "b" in kept_ids  # fuzzy pair both survive
    assert "c" not in kept_ids                  # exact copy of "a" is dropped


# ---------------------------------------------------------------------------
# Distinct content is preserved
# ---------------------------------------------------------------------------

def test_distinct_chunks_all_survive():
    chunks = [
        _chunk("a", "Quarterly revenue rose twelve percent driven by cloud growth."),
        _chunk("b", "The board approved a new share buyback program on Tuesday."),
        _chunk("c", "Manufacturing defects fell after the new QA process launched."),
    ]
    kept = _suppress_near_duplicates(chunks)
    assert len(kept) == 3


def test_short_distinct_chunks_are_not_fuzzy_merged():
    # Two short chunks that share some words but are clearly different must not
    # be merged — the fuzzy path is length-gated exactly to avoid this.
    a = _chunk("a", "Revenue was 100 million dollars.")
    b = _chunk("b", "Revenue was 250 million dollars.")
    kept = _suppress_near_duplicates([a, b], similarity_threshold=0.5)
    assert len(kept) == 2


def test_order_and_representative_are_preserved():
    # Unique chunks keep their input (score) order; the kept boilerplate copy is
    # the first/highest-scored one.
    chunks = [
        _chunk("top", "Unique first passage about pricing tiers.", score=0.95),
        _chunk("boiler_hi", _BOILERPLATE, document_id="a", score=0.90),
        _chunk("mid", "Unique middle passage about latency budgets.", score=0.85),
        _chunk("boiler_lo", _BOILERPLATE, document_id="b", score=0.60),
    ]
    kept = _suppress_near_duplicates(chunks)
    assert [c.chunk.chunk_id for c in kept] == ["top", "boiler_hi", "mid"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_and_single_pass_through():
    assert _suppress_near_duplicates([]) == []
    one = [_chunk("a", _BOILERPLATE)]
    assert _suppress_near_duplicates(one) == one


def test_empty_content_chunks_do_not_crash():
    a = _chunk("a", "")
    b = _chunk("b", "")
    c = _chunk("c", "Real content here about something specific and unique.")
    kept = _suppress_near_duplicates([a, b, c])
    # Two empty chunks normalize identically → one collapses; the real one stays.
    kept_ids = {ch.chunk.chunk_id for ch in kept}
    assert "c" in kept_ids
    assert len(kept) == 2
