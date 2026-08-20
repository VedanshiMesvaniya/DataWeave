"""Stage 5 — Layout Analysis.

Two-layer approach:
  1. Structural layer (native PDFs): PyMuPDF block/line/span geometry gives heading
     detection, column ordering, and footnote-region detection heuristically.
  2. Semantic layer (scanned pages): Vision-LLM structured-Markdown prompt.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pymupdf as fitz  # PyMuPDF

from src.core.provider_client import ProviderRouter
from src.models.schemas import PageContent, PageStructure, ParsedDocument

logger = logging.getLogger(__name__)


async def analyze_layout(
    document: ParsedDocument,
    router: ProviderRouter,
) -> ParsedDocument:
    """Analyze layout for each page — detect headings, reading order, structure."""
    for idx, page in enumerate(document.pages):
        if page.structure == PageStructure.NATIVE_TEXT:
            headings = _extract_headings_native(document.file_path, page.page_number)
            document.pages[idx].layout_headings = headings
            if headings and not page.markdown:
                document.pages[idx].markdown = _text_to_markdown_with_headings(
                    page.text, headings
                )
        elif page.text:
            # Scanned page with OCR text — use vision-LLM for structure
            markdown = await _vision_layout_analysis(
                document.file_path, page.page_number, router
            )
            if markdown:
                document.pages[idx].markdown = markdown

    return document


def _extract_headings_native(file_path: str, page_number: int) -> list[str]:
    """Extract headings from a native PDF page using font-size heuristics.

    Logic: any text span with a font size meaningfully larger than the page's
    median font size is likely a heading.
    """
    try:
        doc = fitz.open(file_path)
        page = doc[page_number - 1]
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        doc.close()
    except Exception as e:
        logger.debug("Could not extract layout from page %d: %s", page_number, e)
        return []

    # Collect all font sizes
    font_sizes: list[float] = []
    text_spans: list[tuple[str, float]] = []

    for block in blocks.get("blocks", []):
        if block.get("type") != 0:  # text block
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                size = span.get("size", 0.0)
                if text and size > 0:
                    font_sizes.append(size)
                    text_spans.append((text, size))

    if not font_sizes:
        return []

    # Compute median font size
    sorted_sizes = sorted(font_sizes)
    median_size = sorted_sizes[len(sorted_sizes) // 2]

    # Heading threshold: 20% larger than median
    heading_threshold = median_size * 1.2

    headings: list[str] = []
    for text, size in text_spans:
        if size >= heading_threshold and len(text) > 2:
            headings.append(text)

    return headings


def _text_to_markdown_with_headings(text: str, headings: list[str]) -> str:
    """Convert plain text to rough Markdown by marking detected headings."""
    lines = text.split("\n")
    result: list[str] = []

    heading_set = set(headings)

    for line in lines:
        stripped = line.strip()
        if stripped in heading_set:
            result.append(f"## {stripped}")
        else:
            result.append(line)

    return "\n".join(result)


async def _vision_layout_analysis(
    file_path: str,
    page_number: int,
    router: ProviderRouter,
) -> str:
    """Use a vision-LLM to produce structured Markdown from a scanned page image."""
    path = Path(file_path)

    # Get page image
    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp"):
        image_data = path.read_bytes()
    else:
        try:
            doc = fitz.open(file_path)
            page = doc[page_number - 1]
            pix = page.get_pixmap(dpi=200)
            image_data = pix.tobytes("png")
            doc.close()
        except Exception as e:
            logger.warning("Could not extract page image for layout analysis: %s", e)
            return ""

    prompt = """Analyze the layout of this document page and produce a clean, structured Markdown representation.

Rules:
- Use # for main title, ## for section headings, ### for subsection headings
- Represent tables using Markdown table syntax (| pipes)
- Preserve reading order: left-to-right, top-to-bottom across columns
- Tag footnotes with [^N] notation
- Figure captions should be on their own line, prefixed with "**Figure N:**"
- Lists should use - or 1. 2. 3. as appropriate
- Do NOT add content that isn't on the page
- Preserve all text verbatim — this is layout analysis, not summarization

Output the structured Markdown:"""

    try:
        import asyncio
        return await asyncio.wait_for(
            router.vision(
                "layout_analysis",
                image_data,
                prompt,
                mime_type="image/png",
                max_tokens=8192,
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Vision layout analysis timed out for page %d", page_number)
        return ""
    except Exception as e:
        logger.warning("Vision layout analysis failed for page %d: %s", page_number, e)
        return ""
