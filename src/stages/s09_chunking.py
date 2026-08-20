"""Stage 9 — Hybrid Chunking.

Architecture doc mandate: "hybrid layout-aware + semantic chunking, not any
single strategy in isolation."

The approach:
1. Start from Stage 5 structural output (headings, sections, tables, figures tagged)
2. Tables and figures become atomic chunks (never split)
3. Prose sections are semantically chunked within structural boundaries
   (~300-800 tokens, ~10-15% overlap)
4. Every chunk carries hierarchy metadata for parent-document retrieval
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from src.core.config import settings
from src.models.schemas import (
    Chunk,
    ChunkType,
    DocumentType,
    FigureData,
    PageContent,
    ParsedDocument,
    TableData,
)

logger = logging.getLogger(__name__)


def chunk_document(document: ParsedDocument) -> list[Chunk]:
    """Chunk a parsed document using the hybrid strategy.

    Returns a list of Chunks ready for embedding.
    """
    doc_id = _make_doc_id(document.file_path)
    chunks: list[Chunk] = []
    chunk_counter = 0

    for page in document.pages:
        # 1. Tables → atomic chunks (never split)
        for table in page.tables:
            chunk_counter += 1
            content = table.markdown or _rows_to_text(table.rows)
            if content.strip():
                chunks.append(Chunk(
                    chunk_id=f"{doc_id}_chunk_{chunk_counter:04d}",
                    document_id=doc_id,
                    chunk_type=ChunkType.TABLE,
                    content=content,
                    token_count=_estimate_tokens(content),
                    page_number=page.page_number,
                    section_hierarchy=page.layout_headings[:3],
                    document_type=document.document_type,
                    source_file=document.file_path,
                    confidence=table.confidence,
                ))

        # 2. Figures → atomic chunks (caption + description)
        for figure in page.figures:
            chunk_counter += 1
            content = _figure_to_text(figure)
            if content.strip():
                chunks.append(Chunk(
                    chunk_id=f"{doc_id}_chunk_{chunk_counter:04d}",
                    document_id=doc_id,
                    chunk_type=ChunkType.FIGURE_CAPTION,
                    content=content,
                    token_count=_estimate_tokens(content),
                    page_number=page.page_number,
                    section_hierarchy=page.layout_headings[:3],
                    document_type=document.document_type,
                    source_file=document.file_path,
                    confidence=figure.confidence,
                ))

        # 3. Prose → semantic chunking within structural boundaries
        text = page.markdown if page.markdown else page.text
        if text.strip():
            prose_chunks = _chunk_prose(
                text,
                doc_id=doc_id,
                page_number=page.page_number,
                section_hierarchy=page.layout_headings[:3],
                document_type=document.document_type,
                source_file=document.file_path,
                confidence=page.ocr_confidence,
                start_counter=chunk_counter,
            )
            chunk_counter += len(prose_chunks)
            chunks.extend(prose_chunks)

    # Set parent-child relationships (sections → chunks within them)
    _set_parent_relationships(chunks)

    logger.info(
        "Chunked '%s' into %d chunks (tables: %d, figures: %d, prose: %d)",
        Path(document.file_path).name,
        len(chunks),
        sum(1 for c in chunks if c.chunk_type == ChunkType.TABLE),
        sum(1 for c in chunks if c.chunk_type == ChunkType.FIGURE_CAPTION),
        sum(1 for c in chunks if c.chunk_type == ChunkType.PROSE),
    )

    return chunks


def _chunk_prose(
    text: str,
    *,
    doc_id: str,
    page_number: int,
    section_hierarchy: list[str],
    document_type: DocumentType,
    source_file: str,
    confidence: float,
    start_counter: int,
) -> list[Chunk]:
    """Split prose text into chunks respecting structural boundaries.

    Strategy:
    1. Split on heading boundaries (never cross a heading)
    2. Within each section, split on paragraph boundaries
    3. Apply token-count limits with overlap
    """
    target_tokens = settings.chunk_target_tokens
    overlap_fraction = settings.chunk_overlap_fraction
    overlap_tokens = int(target_tokens * overlap_fraction)

    # Split by sections (heading boundaries)
    sections = _split_by_headings(text)
    chunks: list[Chunk] = []
    counter = start_counter

    for section_text, section_heading in sections:
        current_hierarchy = list(section_hierarchy)
        if section_heading:
            current_hierarchy.append(section_heading)

        # Split section into paragraph-based chunks
        paragraphs = [p.strip() for p in section_text.split("\n\n") if p.strip()]

        current_chunk_parts: list[str] = []
        current_tokens = 0

        for para in paragraphs:
            para_tokens = _estimate_tokens(para)

            if current_tokens + para_tokens > target_tokens and current_chunk_parts:
                # Emit current chunk
                counter += 1
                content = "\n\n".join(current_chunk_parts)
                chunks.append(Chunk(
                    chunk_id=f"{doc_id}_chunk_{counter:04d}",
                    document_id=doc_id,
                    chunk_type=ChunkType.PROSE,
                    content=content,
                    token_count=_estimate_tokens(content),
                    page_number=page_number,
                    section_hierarchy=current_hierarchy,
                    document_type=document_type,
                    source_file=source_file,
                    confidence=confidence,
                ))

                # Overlap: keep the last part(s) that fit within overlap budget
                overlap_parts: list[str] = []
                overlap_count = 0
                for part in reversed(current_chunk_parts):
                    part_tokens = _estimate_tokens(part)
                    if overlap_count + part_tokens <= overlap_tokens:
                        overlap_parts.insert(0, part)
                        overlap_count += part_tokens
                    else:
                        break

                current_chunk_parts = overlap_parts
                current_tokens = overlap_count

            current_chunk_parts.append(para)
            current_tokens += para_tokens

        # Emit remaining chunk
        if current_chunk_parts:
            counter += 1
            content = "\n\n".join(current_chunk_parts)
            chunks.append(Chunk(
                chunk_id=f"{doc_id}_chunk_{counter:04d}",
                document_id=doc_id,
                chunk_type=ChunkType.PROSE,
                content=content,
                token_count=_estimate_tokens(content),
                page_number=page_number,
                section_hierarchy=current_hierarchy,
                document_type=document_type,
                source_file=source_file,
                confidence=confidence,
            ))

    return chunks


def _split_by_headings(text: str) -> list[tuple[str, str]]:
    """Split text into (section_text, heading) pairs at Markdown heading boundaries."""
    heading_pattern = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)
    matches = list(heading_pattern.finditer(text))

    if not matches:
        return [(text, "")]

    sections: list[tuple[str, str]] = []

    # Content before first heading
    if matches[0].start() > 0:
        pre_text = text[: matches[0].start()].strip()
        if pre_text:
            sections.append((pre_text, ""))

    # Each heading and its content
    for i, match in enumerate(matches):
        heading = match.group(2).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section_text = text[start:end].strip()
        if section_text:
            sections.append((section_text, heading))

    return sections if sections else [(text, "")]


def _set_parent_relationships(chunks: list[Chunk]) -> None:
    """Set parent_chunk_id for chunks within the same section hierarchy."""
    section_leaders: dict[str, str] = {}

    for chunk in chunks:
        hierarchy_key = " > ".join(chunk.section_hierarchy)
        if hierarchy_key not in section_leaders:
            section_leaders[hierarchy_key] = chunk.chunk_id
        else:
            chunk.parent_chunk_id = section_leaders[hierarchy_key]


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token for English text."""
    return max(1, len(text) // 4)


def _make_doc_id(file_path: str) -> str:
    """Generate a stable document ID from the file path."""
    return hashlib.sha256(file_path.encode()).hexdigest()[:16]


def _rows_to_text(rows: list[list[str]]) -> str:
    """Convert table rows to readable text."""
    return "\n".join(" | ".join(row) for row in rows)


def _figure_to_text(figure: FigureData) -> str:
    """Convert figure data to a chunk-ready text representation."""
    parts: list[str] = []
    if figure.caption:
        parts.append(f"Figure {figure.figure_index + 1}: {figure.caption}")
    if figure.description:
        parts.append(figure.description)
    return "\n".join(parts)
