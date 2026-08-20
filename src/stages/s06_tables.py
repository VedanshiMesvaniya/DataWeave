"""Stage 6 — Table Extraction.

Decision tree from the architecture doc:
  - Native PDF with ruled table → Camelot (lattice mode) / pdfplumber
  - Native PDF with borderless table → Camelot (stream mode), escalate to vision-LLM if malformed
  - Scanned page → Vision-LLM table-to-Markdown/HTML extraction

Camelot/pdfplumber cost nothing and are exact on well-formed ruled tables —
always try them first on native PDFs.
"""

from __future__ import annotations

import logging
from pathlib import Path
 
import pymupdf as fitz  # PyMuPDF
import pdfplumber

from src.core.confidence import check_table_extraction
from src.core.provider_client import ProviderRouter
from src.models.schemas import PageContent, PageStructure, ParsedDocument, TableData

logger = logging.getLogger(__name__)


async def extract_tables(
    document: ParsedDocument,
    router: ProviderRouter,
) -> ParsedDocument:
    """Extract tables from all pages of the document."""
    is_pdf = document.file_path.lower().endswith(".pdf")

    for idx, page in enumerate(document.pages):
        tables: list[TableData] = []

        if is_pdf and page.structure == PageStructure.NATIVE_TEXT:
            # Try local extraction first
            tables = _extract_tables_pdfplumber(document.file_path, page.page_number)

            # Validate each table
            for table in tables:
                report = check_table_extraction(table.rows)
                table.confidence = report.confidence_score

                if not report.is_acceptable:
                    logger.info(
                        "Table on page %d has quality issues: %s — escalating to vision-LLM",
                        page.page_number,
                        report.issues,
                    )
                    vlm_table = await _vision_table_extraction(
                        document.file_path, page.page_number, router
                    )
                    if vlm_table:
                        tables = [vlm_table]
                    break

        elif page.structure == PageStructure.SCANNED and page.text:
            # Scanned page — use vision-LLM directly
            vlm_table = await _vision_table_extraction(
                document.file_path, page.page_number, router
            )
            if vlm_table:
                tables = [vlm_table]

        if tables:
            document.pages[idx].tables = tables

    return document


def _extract_tables_pdfplumber(file_path: str, page_number: int) -> list[TableData]:
    """Extract tables from a native PDF page using pdfplumber."""
    try:
        with pdfplumber.open(file_path) as pdf:
            if page_number - 1 >= len(pdf.pages):
                return []

            page = pdf.pages[page_number - 1]
            raw_tables = page.extract_tables()

            tables: list[TableData] = []
            for table_idx, raw_table in enumerate(raw_tables):
                if not raw_table:
                    continue

                # Clean cells
                rows: list[list[str]] = []
                for row in raw_table:
                    cleaned = [str(cell).strip() if cell else "" for cell in row]
                    rows.append(cleaned)

                # Convert to Markdown
                markdown = _rows_to_markdown(rows)

                tables.append(TableData(
                    page_number=page_number,
                    table_index=table_idx,
                    markdown=markdown,
                    rows=rows,
                    confidence=1.0,
                    extraction_method="pdfplumber",
                ))

            return tables

    except Exception as e:
        logger.warning("pdfplumber table extraction failed on page %d: %s", page_number, e)
        return []


def _rows_to_markdown(rows: list[list[str]]) -> str:
    """Convert a list of rows to a Markdown table."""
    if not rows:
        return ""

    # Normalize column count
    max_cols = max(len(row) for row in rows)
    normalized = [row + [""] * (max_cols - len(row)) for row in rows]

    lines: list[str] = []
    # Header
    lines.append("| " + " | ".join(normalized[0]) + " |")
    lines.append("| " + " | ".join(["---"] * max_cols) + " |")
    # Body
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)


async def _vision_table_extraction(
    file_path: str,
    page_number: int,
    router: ProviderRouter,
) -> TableData | None:
    """Use vision-LLM to extract a table from a page image."""
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
            logger.warning("Could not extract page image for table extraction: %s", e)
            return None

    prompt = """Extract ALL tables from this document page.

For each table, output it as a Markdown table with | pipe delimiters.
- Preserve ALL cell values exactly as they appear
- Include column headers
- Use --- for the header separator row
- Preserve colspan/rowspan by repeating values in merged cells
- If a cell is empty, leave it blank between pipes
- If there are multiple tables, separate them with a blank line

If there are no tables on this page, respond with: NO_TABLES

Output:"""

    try:
        result = await router.vision(
            "table_extraction",
            image_data,
            prompt,
            mime_type="image/png",
            max_tokens=4096,
        )

        if "NO_TABLES" in result:
            return None

        return TableData(
            page_number=page_number,
            table_index=0,
            markdown=result.strip(),
            rows=[],  # Could parse Markdown back to rows if needed
            confidence=0.85,
            extraction_method="vision_llm",
        )

    except Exception as e:
        logger.warning("Vision table extraction failed on page %d: %s", page_number, e)
        return None
