"""Tests for Stages 1–9 and core infrastructure.

These tests use local data only — no API keys required.
They verify the local/deterministic parts of each stage.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.core.confidence import check_ocr_confidence, check_table_extraction, texts_agree
from src.core.rate_limiter import RateLimiter, ProviderLimits
from src.models.schemas import (
    Chunk,
    ChunkType,
    ClassificationResult,
    DocumentType,
    FileCategory,
    FileDetectionResult,
    PageContent,
    PageStructure,
    ParsedDocument,
)
from src.stages.s01_file_detection import detect_file
from src.stages.s09_chunking import chunk_document


# ---------------------------------------------------------------------------
# Stage 1 — File Detection
# ---------------------------------------------------------------------------

class TestFileDetection:
    """Test file type detection using real temporary files."""

    def test_detect_json_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.json"
        f.write_text('{"key": "value"}')
        result = detect_file(f)
        assert result.file_category == FileCategory.JSON
        assert result.extension == ".json"
        assert result.file_size_bytes > 0

    def test_detect_csv_file(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("name,age\nAlice,30\nBob,25")
        result = detect_file(f)
        assert result.file_category == FileCategory.CSV

    def test_detect_markdown_file(self, tmp_path: Path) -> None:
        f = tmp_path / "readme.md"
        f.write_text("# Hello\n\nThis is a test.")
        result = detect_file(f)
        assert result.file_category == FileCategory.MARKDOWN

    def test_detect_html_file(self, tmp_path: Path) -> None:
        f = tmp_path / "page.html"
        f.write_text("<html><body>Hello</body></html>")
        result = detect_file(f)
        assert result.file_category == FileCategory.HTML

    def test_detect_plaintext_file(self, tmp_path: Path) -> None:
        f = tmp_path / "notes.txt"
        f.write_text("Just some plain text.")
        result = detect_file(f)
        assert result.file_category == FileCategory.PLAINTEXT

    def test_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            detect_file("/nonexistent/file.pdf")

    def test_unknown_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "mystery.xyz123"
        f.write_bytes(b"random binary data here")
        result = detect_file(f)
        # Should either detect via magic or fall back to UNKNOWN
        assert isinstance(result.file_category, FileCategory)


# ---------------------------------------------------------------------------
# Stage 3 — Parsing (local formats only)
# ---------------------------------------------------------------------------

class TestParsing:
    """Test parsing of text-based formats (no external dependencies)."""

    def test_parse_json(self, tmp_path: Path) -> None:
        from src.stages.s03_parsing import parse_document

        f = tmp_path / "test.json"
        data = {"users": [{"name": "Alice"}, {"name": "Bob"}]}
        f.write_text(json.dumps(data))

        detection = FileDetectionResult(
            file_path=str(f),
            file_category=FileCategory.JSON,
            mime_type="application/json",
            file_size_bytes=f.stat().st_size,
            extension=".json",
        )
        classification = ClassificationResult(structural=PageStructure.NATIVE_TEXT)

        result = parse_document(f, detection, classification)
        assert result.total_pages == 1
        assert "Alice" in result.pages[0].text
        assert "Bob" in result.pages[0].text

    def test_parse_csv(self, tmp_path: Path) -> None:
        from src.stages.s03_parsing import parse_document

        f = tmp_path / "data.csv"
        f.write_text("name,age,city\nAlice,30,NYC\nBob,25,LA")

        detection = FileDetectionResult(
            file_path=str(f),
            file_category=FileCategory.CSV,
            mime_type="text/csv",
            file_size_bytes=f.stat().st_size,
            extension=".csv",
        )
        classification = ClassificationResult(structural=PageStructure.NATIVE_TEXT)

        result = parse_document(f, detection, classification)
        assert result.total_pages == 1
        assert "Alice" in result.pages[0].text
        assert result.pages[0].ocr_confidence == 1.0

    def test_parse_html(self, tmp_path: Path) -> None:
        from src.stages.s03_parsing import parse_document

        f = tmp_path / "page.html"
        f.write_text("""
        <html>
        <head><title>Test</title><script>var x=1;</script></head>
        <body>
            <h1>Hello World</h1>
            <p>This is content.</p>
        </body>
        </html>
        """)

        detection = FileDetectionResult(
            file_path=str(f),
            file_category=FileCategory.HTML,
            mime_type="text/html",
            file_size_bytes=f.stat().st_size,
            extension=".html",
        )
        classification = ClassificationResult(structural=PageStructure.NATIVE_TEXT)

        result = parse_document(f, detection, classification)
        assert "Hello World" in result.pages[0].text
        assert "var x=1" not in result.pages[0].text  # Script should be stripped

    def test_parse_plaintext(self, tmp_path: Path) -> None:
        from src.stages.s03_parsing import parse_document

        f = tmp_path / "notes.txt"
        content = "Line 1\nLine 2\nLine 3"
        f.write_text(content)

        detection = FileDetectionResult(
            file_path=str(f),
            file_category=FileCategory.PLAINTEXT,
            mime_type="text/plain",
            file_size_bytes=f.stat().st_size,
            extension=".txt",
        )
        classification = ClassificationResult(structural=PageStructure.NATIVE_TEXT)

        result = parse_document(f, detection, classification)
        assert result.pages[0].text == content


# ---------------------------------------------------------------------------
# Stage 9 — Chunking
# ---------------------------------------------------------------------------

class TestChunking:
    """Test the hybrid chunking strategy."""

    def test_basic_prose_chunking(self) -> None:
        text = "\n\n".join([f"This is paragraph {i} with some content." for i in range(20)])
        doc = ParsedDocument(
            file_path="/test/doc.txt",
            file_category=FileCategory.PLAINTEXT,
            total_pages=1,
            pages=[
                PageContent(
                    page_number=1,
                    structure=PageStructure.NATIVE_TEXT,
                    text=text,
                    ocr_confidence=1.0,
                )
            ],
        )

        chunks = chunk_document(doc)
        assert len(chunks) > 0
        assert all(c.chunk_type == ChunkType.PROSE for c in chunks)
        assert all(c.document_id for c in chunks)
        assert all(c.chunk_id for c in chunks)

    def test_table_atomic_chunking(self) -> None:
        from src.models.schemas import TableData

        doc = ParsedDocument(
            file_path="/test/doc.pdf",
            file_category=FileCategory.PDF,
            total_pages=1,
            pages=[
                PageContent(
                    page_number=1,
                    structure=PageStructure.NATIVE_TEXT,
                    text="Some surrounding text.",
                    tables=[
                        TableData(
                            page_number=1,
                            markdown="| Col1 | Col2 |\n|---|---|\n| A | B |",
                            rows=[["Col1", "Col2"], ["A", "B"]],
                        )
                    ],
                )
            ],
        )

        chunks = chunk_document(doc)
        table_chunks = [c for c in chunks if c.chunk_type == ChunkType.TABLE]
        assert len(table_chunks) == 1
        assert "Col1" in table_chunks[0].content

    def test_heading_boundary_respected(self) -> None:
        text = "## Section 1\n\nContent of section 1.\n\n## Section 2\n\nContent of section 2."
        doc = ParsedDocument(
            file_path="/test/doc.md",
            file_category=FileCategory.MARKDOWN,
            total_pages=1,
            pages=[
                PageContent(
                    page_number=1,
                    structure=PageStructure.NATIVE_TEXT,
                    text=text,
                )
            ],
        )

        chunks = chunk_document(doc)
        # Each section should be its own chunk (text is short)
        assert len(chunks) >= 2

    def test_chunk_metadata(self) -> None:
        doc = ParsedDocument(
            file_path="/test/report.pdf",
            file_category=FileCategory.PDF,
            document_type=DocumentType.FINANCIAL_REPORT,
            total_pages=1,
            pages=[
                PageContent(
                    page_number=1,
                    structure=PageStructure.NATIVE_TEXT,
                    text="Revenue increased by 15% year over year.",
                    ocr_confidence=1.0,
                )
            ],
        )

        chunks = chunk_document(doc)
        assert len(chunks) > 0
        assert chunks[0].document_type == DocumentType.FINANCIAL_REPORT
        assert chunks[0].source_file == "/test/report.pdf"
        assert chunks[0].page_number == 1


# ---------------------------------------------------------------------------
# Core — Confidence Gates
# ---------------------------------------------------------------------------

class TestConfidence:
    """Test OCR confidence checking and table validation."""

    def test_high_quality_ocr(self) -> None:
        text = "This is a clean sentence extracted from a well-scanned document. The quality is excellent."
        report = check_ocr_confidence(text, reported_confidence=0.95)
        assert report.is_acceptable
        assert report.confidence_score > 0.8

    def test_low_quality_ocr(self) -> None:
        text = "Th!$ 1$ g@rb@g3 t3xt w!th m@ny err0r$ $$$ %%% ^^^"
        report = check_ocr_confidence(text, reported_confidence=0.3)
        assert not report.is_acceptable
        assert report.recommendation in ("escalate", "flag_for_review")

    def test_empty_ocr(self) -> None:
        report = check_ocr_confidence("")
        assert not report.is_acceptable
        assert report.confidence_score == 0.0

    def test_table_validation_good(self) -> None:
        rows = [["Name", "Age"], ["Alice", "30"], ["Bob", "25"]]
        report = check_table_extraction(rows)
        assert report.is_acceptable

    def test_table_validation_too_few_rows(self) -> None:
        rows = [["Lonely"]]
        report = check_table_extraction(rows)
        assert not report.is_acceptable

    def test_texts_agree(self) -> None:
        a = "The revenue for Q3 2024 was $45.2 million, representing a 15% increase."
        b = "The revenue for Q3 2024 was $45.2 million, representing a 15% increase."
        assert texts_agree(a, b)

    def test_texts_disagree(self) -> None:
        a = "The revenue was $45.2 million."
        b = "The cost of goods sold was $12.3 million."
        assert not texts_agree(a, b)


# ---------------------------------------------------------------------------
# Core — Rate Limiter
# ---------------------------------------------------------------------------

class TestRateLimiter:
    """Test rate limiter tracking."""

    @pytest.mark.asyncio
    async def test_basic_acquire(self) -> None:
        limiter = RateLimiter(limits={
            "test_provider": ProviderLimits(rpm=100, rpd=1000),
        })
        # Should not raise
        await limiter.acquire("test_provider")
        stats = limiter.get_stats()
        assert stats["test_provider"]["rpd_used"] == 1

    def test_report_429(self) -> None:
        limiter = RateLimiter()
        limiter.report_429("gemini", retry_after=5.0)
        # State should have backoff set
        state = limiter._get_state("gemini")
        assert state.backoff_until > 0


# ---------------------------------------------------------------------------
# Models — Schema validation
# ---------------------------------------------------------------------------

class TestSchemas:
    """Test Pydantic model serialization/validation."""

    def test_chunk_serialization(self) -> None:
        chunk = Chunk(
            chunk_id="abc123_chunk_0001",
            document_id="abc123",
            chunk_type=ChunkType.PROSE,
            content="Test content",
            token_count=3,
            page_number=1,
            source_file="/test/doc.pdf",
        )
        data = chunk.model_dump()
        assert data["chunk_id"] == "abc123_chunk_0001"
        assert data["chunk_type"] == "prose"

        # Round-trip
        restored = Chunk.model_validate(data)
        assert restored.chunk_id == chunk.chunk_id

    def test_file_detection_result(self) -> None:
        result = FileDetectionResult(
            file_path="/test/doc.pdf",
            file_category=FileCategory.PDF,
            mime_type="application/pdf",
            file_size_bytes=12345,
            extension=".pdf",
        )
        assert result.file_category == FileCategory.PDF
