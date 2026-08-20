"""Pydantic data models for the entire pipeline.

Every stage produces typed outputs that flow into the next stage.
Models are intentionally flat where possible — deep nesting is avoided
unless the data genuinely has hierarchical structure (chunks do).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, computed_field


# ---------------------------------------------------------------------------
# Stage 1 — File Detection
# ---------------------------------------------------------------------------

class FileCategory(str, Enum):
    """Broad file category determined by magic bytes / extension."""
    PDF = "pdf"
    DOCX = "docx"
    DOC = "doc"
    PPTX = "pptx"
    PPT = "ppt"
    XLSX = "xlsx"
    XLS = "xls"
    CSV = "csv"
    TSV = "tsv"
    MARKDOWN = "markdown"
    PLAINTEXT = "plaintext"
    HTML = "html"
    XML = "xml"
    JSON = "json"
    IMAGE = "image"
    UNKNOWN = "unknown"


class FileDetectionResult(BaseModel):
    """Output of Stage 1 — what kind of file is this?"""
    file_path: str
    file_category: FileCategory
    mime_type: str
    file_size_bytes: int
    extension: str


# ---------------------------------------------------------------------------
# Stage 2 — Classification
# ---------------------------------------------------------------------------

class PageStructure(str, Enum):
    """Per-page structural classification."""
    NATIVE_TEXT = "native_text"
    SCANNED = "scanned"
    MIXED = "mixed"


class DocumentType(str, Enum):
    """Semantic document type — drives downstream extraction behavior."""
    SCIENTIFIC_PAPER = "scientific_paper"
    FINANCIAL_REPORT = "financial_report"
    LEGAL_CONTRACT = "legal_contract"
    INVOICE = "invoice"
    FORM = "form"
    PRESENTATION = "presentation"
    SPREADSHEET = "spreadsheet"
    GENERAL = "general"
    DATABASE = "database"


class ClassificationResult(BaseModel):
    """Output of Stage 2 — structural + semantic classification."""
    structural: PageStructure
    page_structures: list[PageStructure] = Field(default_factory=list)
    document_type: DocumentType = DocumentType.GENERAL
    classification_confidence: float = 1.0


# ---------------------------------------------------------------------------
# Stage 3–8 — Parsed content
# ---------------------------------------------------------------------------

class TableData(BaseModel):
    """An extracted table — kept atomic, never split during chunking."""
    page_number: int
    table_index: int = 0
    markdown: str = ""
    html: str = ""
    rows: list[list[str]] = Field(default_factory=list)
    confidence: float = 1.0
    extraction_method: str = ""


class FigureData(BaseModel):
    """An extracted figure/chart/image with its description."""
    page_number: int
    figure_index: int = 0
    caption: str = ""
    description: str = ""
    image_path: str = ""
    confidence: float = 1.0
    extraction_method: str = ""


class PageContent(BaseModel):
    """Parsed content for a single page."""
    page_number: int
    structure: PageStructure = PageStructure.NATIVE_TEXT
    text: str = ""
    markdown: str = ""
    tables: list[TableData] = Field(default_factory=list)
    figures: list[FigureData] = Field(default_factory=list)
    ocr_confidence: float = 1.0
    ocr_method: str = ""
    layout_headings: list[str] = Field(default_factory=list)


class ParsedDocument(BaseModel):
    """Unified output of Stages 3–8 — the fully parsed document."""
    file_path: str
    file_category: FileCategory
    document_type: DocumentType = DocumentType.GENERAL
    total_pages: int = 0
    pages: list[PageContent] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 9 — Chunks
# ---------------------------------------------------------------------------

class ChunkType(str, Enum):
    """What kind of content this chunk holds — affects retrieval behavior."""
    PROSE = "prose"
    TABLE = "table"
    FIGURE_CAPTION = "figure_caption"
    FOOTNOTE = "footnote"
    HEADING = "heading"
    CODE = "code"
    KEY_VALUE = "key_value"
    SQL_RESULT = "sql_result"
    SQL_SCHEMA = "sql_schema"


class Chunk(BaseModel):
    """A single chunk ready for embedding and storage."""
    chunk_id: str
    document_id: str
    chunk_type: ChunkType = ChunkType.PROSE
    content: str
    token_count: int = 0

    # Hierarchy metadata for parent-document retrieval
    page_number: int = 0
    section_hierarchy: list[str] = Field(default_factory=list)
    parent_chunk_id: str | None = None

    # Provenance
    document_type: DocumentType = DocumentType.GENERAL
    source_file: str = ""
    confidence: float = 1.0


# ---------------------------------------------------------------------------
# Stage 12–14 — Retrieval and generation
# ---------------------------------------------------------------------------

class RetrievedChunk(BaseModel):
    """A chunk returned by retrieval, scored and ready for reranking."""
    chunk: Chunk
    score: float = 0.0
    retrieval_method: str = ""  # "dense", "sparse", "hybrid"


class Citation(BaseModel):
    """A citation linking an answer span to a source chunk."""
    chunk_id: str
    source_file: str
    page_number: int = 0
    relevance_score: float = 0.0


class ThinkingStep(BaseModel):
    """One step in the query's reasoning trace, shown as a persistent
    Claude-style 'thinking' block in the UI."""
    label: str
    detail: str = ""


class TokenUsage(BaseModel):
    """Token accounting for a query, normalized across every provider.

    Providers report usage in different shapes (OpenAI-style prompt/completion
    with an optional reasoning sub-count; Gemini's promptTokenCount /
    candidatesTokenCount / thoughtsTokenCount). We map them all into one shape
    so the UI shows a single consistent breakdown regardless of which lane
    answered::

        total = input + output + thinking

    A single query typically makes several LLM calls (contextualize a
    follow-up, classify intent, generate the answer). Because every call in a
    request goes through the same per-request router, those calls accumulate
    here — so the number reflects the *whole* cost of producing the answer,
    not just the final generation. ``calls`` records how many LLM round-trips
    that was, which is itself a lever for reducing spend.
    """
    input_tokens: int = 0
    output_tokens: int = 0      # visible answer tokens
    thinking_tokens: int = 0    # hidden reasoning tokens (0 on non-reasoning lanes)
    calls: int = 0              # number of LLM round-trips folded into this total
    provider: str = ""          # provider/model that produced the final answer
    model: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.thinking_tokens

    @property
    def has_data(self) -> bool:
        return self.total_tokens > 0

    def add_call(self, other: "TokenUsage") -> None:
        """Fold one LLM call's usage into this running total (in place).

        Counts accumulate; provider/model track the *most recent* non-empty
        call, so after the generation step they name the model that actually
        wrote the visible answer rather than an auxiliary classifier.
        """
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.thinking_tokens += other.thinking_tokens
        if other.total_tokens > 0:
            self.calls += 1
        if other.provider:
            self.provider = other.provider
        if other.model:
            self.model = other.model


class QueryResult(BaseModel):
    """Final output of the query pipeline."""
    query: str
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    model_used: str = ""
    reasoning_task: str = ""
    chunks_retrieved: int = 0
    chunks_after_rerank: int = 0
    thinking: list[ThinkingStep] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
