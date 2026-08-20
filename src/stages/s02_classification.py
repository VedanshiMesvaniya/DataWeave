"""Stage 2 — Document Classification.

Two sub-stages:
  2a. Structural classification (local, deterministic) — is each page native-text,
      scanned, or mixed? Uses PyMuPDF text-density heuristic.
  2b. Semantic classification (free LLM) — is this an invoice, contract, scientific
      paper, etc.? Uses Gemini Flash-Lite or Groq Llama 8B.

The structural classification drives per-page routing in Stage 3+4 (native pages
skip OCR, scanned pages go through the OCR chain). The semantic classification
drives downstream extraction behavior (invoices get key-value extraction,
contracts get clause-level chunking, etc.).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pymupdf as fitz  # PyMuPDF

from src.core.provider_client import ProviderRouter
from src.models.schemas import (
    ClassificationResult,
    DocumentType,
    FileCategory,
    FileDetectionResult,
    PageStructure,
)

logger = logging.getLogger(__name__)

# Minimum meaningful characters per page to consider it "native text"
_MIN_CHARS_PER_PAGE = 100

# If the ratio of "real" (non-whitespace, non-garbage) chars to total extracted
# chars is below this, the text layer is likely garbage OCR from a bad earlier pass.
_MIN_QUALITY_RATIO = 0.6


def _classify_page_structure(page: fitz.Page) -> PageStructure:
    """Classify a single PDF page as native-text, scanned, or mixed.

    Heuristic: extract text, compute character density, check quality.
    """
    text = page.get_text("text")
    char_count = len(text.strip())

    if char_count < _MIN_CHARS_PER_PAGE:
        # Very little text — likely scanned
        # But check if there are any images on the page
        images = page.get_images(full=True)
        if images:
            return PageStructure.SCANNED
        # No images and no text — could be a blank page
        return PageStructure.SCANNED

    # Check text quality — is it real text or garbage from a bad OCR pass?
    printable = sum(1 for c in text if c.isprintable() or c in "\n\t")
    quality_ratio = printable / len(text) if text else 0.0

    if quality_ratio < _MIN_QUALITY_RATIO:
        # Text layer exists but looks like garbage
        return PageStructure.SCANNED

    # Has both substantial text and images — might be mixed
    images = page.get_images(full=True)
    if images and char_count < _MIN_CHARS_PER_PAGE * 3:
        return PageStructure.MIXED

    return PageStructure.NATIVE_TEXT


def classify_structure(file_path: str | Path, detection: FileDetectionResult) -> ClassificationResult:
    """Stage 2a — Structural classification (local, deterministic).

    For PDFs: per-page text-density analysis.
    For other formats: deterministic based on file type.
    """
    if detection.file_category == FileCategory.PDF:
        return _classify_pdf_structure(str(file_path))

    if detection.file_category == FileCategory.IMAGE:
        return ClassificationResult(
            structural=PageStructure.SCANNED,
            page_structures=[PageStructure.SCANNED],
        )

    # All other formats (DOCX, XLSX, MD, HTML, etc.) are native text
    return ClassificationResult(
        structural=PageStructure.NATIVE_TEXT,
        page_structures=[PageStructure.NATIVE_TEXT],
    )


def _classify_pdf_structure(file_path: str) -> ClassificationResult:
    """Analyze each page of a PDF for text density."""
    doc = fitz.open(file_path)
    page_structures: list[PageStructure] = []

    for page in doc:
        structure = _classify_page_structure(page)
        page_structures.append(structure)

    doc.close()

    # Determine overall document structure
    unique = set(page_structures)
    if len(unique) == 1:
        overall = unique.pop()
    elif PageStructure.SCANNED in unique and PageStructure.NATIVE_TEXT in unique:
        overall = PageStructure.MIXED
    else:
        overall = PageStructure.MIXED

    logger.info(
        "PDF structure: %s (pages: %s)",
        overall.value,
        ", ".join(s.value for s in page_structures),
    )

    return ClassificationResult(
        structural=overall,
        page_structures=page_structures,
    )


# ---------------------------------------------------------------------------
# Stage 2b — Semantic classification via LLM
# ---------------------------------------------------------------------------

_CLASSIFICATION_PROMPT = """You are a document classification system. Given the first page(s) of a document, classify it into exactly one of these categories:

- scientific_paper: Academic or scientific publication, journal article, preprint
- financial_report: Financial statement, annual report, quarterly earnings, 10-K/10-Q
- legal_contract: Contract, agreement, terms of service, NDA, lease
- invoice: Invoice, receipt, purchase order, billing statement
- form: Application form, questionnaire, survey, government form
- presentation: Slide deck, presentation (already identified from file format)
- spreadsheet: Spreadsheet data (already identified from file format)
- general: Any other document type

Respond with ONLY a JSON object:
{"document_type": "<category>", "confidence": <0.0-1.0>}

Document text (first ~2000 characters):
---
{text}
---"""


async def classify_semantic(
    text_sample: str,
    detection: FileDetectionResult,
    router: ProviderRouter,
) -> DocumentType:
    """Stage 2b — Semantic classification via LLM.

    Takes the first ~2000 characters of extracted text and classifies
    the document type to drive downstream extraction behavior.
    """
    # Short-circuit for file types that are self-classifying
    if detection.file_category in (FileCategory.PPTX, FileCategory.PPT):
        return DocumentType.PRESENTATION
    if detection.file_category in (FileCategory.XLSX, FileCategory.XLS, FileCategory.CSV, FileCategory.TSV):
        return DocumentType.SPREADSHEET

    # Truncate to ~2000 chars — enough for classification, cheap on tokens
    sample = text_sample[:2000].lower()

    if not sample.strip():
        logger.info("No text available for semantic classification — defaulting to GENERAL")
        return DocumentType.GENERAL

    # Local heuristic fast-path for common types
    if "abstract" in sample and "introduction" in sample and "references" in sample:
        return DocumentType.SCIENTIFIC_PAPER
    if "invoice" in sample and ("total" in sample or "due" in sample or "tax" in sample):
        return DocumentType.INVOICE
    if "financial" in sample and ("revenue" in sample or "earnings" in sample or "quarterly" in sample):
        return DocumentType.FINANCIAL_REPORT

    try:
        import asyncio
        response = await asyncio.wait_for(
            router.chat(
                "semantic_classification",
                messages=[
                    {"role": "user", "content": _CLASSIFICATION_PROMPT.format(text=text_sample[:2000])},
                ],
                response_format={"type": "json_object"},
                max_tokens=100,
            ),
            timeout=10.0,
        )

        data = json.loads(response)
        doc_type_str = data.get("document_type", "general")

        try:
            doc_type = DocumentType(doc_type_str)
        except ValueError:
            logger.warning("LLM returned unknown document type: %s", doc_type_str)
            doc_type = DocumentType.GENERAL

        confidence = float(data.get("confidence", 0.8))
        logger.info("Semantic classification: %s (confidence: %.2f)", doc_type.value, confidence)
        return doc_type

    except asyncio.TimeoutError:
        logger.warning("Semantic classification timed out — defaulting to GENERAL")
        return DocumentType.GENERAL
    except Exception as e:
        logger.warning("Semantic classification failed: %s — defaulting to GENERAL", e)
        return DocumentType.GENERAL
