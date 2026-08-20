"""Stage 4 — OCR.

Multi-tier OCR chain for scanned pages and images:
  1. OCR.space Engine 2 — fast first pass (~90-95% on clean scans)
  2. Confidence gate — check quality, garbage ratio, word ratio
  3. OCR.space Engine 3 — handwriting/table-aware re-pass (if Engine 2 was low-confidence)
  4. Vision-LLM OCR (Qwen3-VL via NIM, or Gemini Flash) — final escalation
  5. Reconciliation — if dual outputs disagree, flag for review

Only called on pages where Stage 3 left text empty (structure == SCANNED
or ocr_method == "pending_ocr"). Native-text pages are never re-OCR'd —
that would be a quality regression.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pymupdf as fitz  # PyMuPDF
import httpx

from src.core.confidence import ConfidenceReport, check_ocr_confidence, texts_agree
from src.core.config import settings
from src.core.provider_client import ProviderRouter
from src.models.schemas import PageContent, PageStructure, ParsedDocument

logger = logging.getLogger(__name__)

_OCR_SPACE_URL = "https://api.ocr.space/parse/image"


async def run_ocr(
    document: ParsedDocument,
    router: ProviderRouter,
) -> ParsedDocument:
    """Run OCR on all pages that need it (structure == SCANNED or pending_ocr).

    Mutates the document's pages in-place, filling in text and OCR metadata.
    Native-text pages are skipped entirely.
    """
    pages_needing_ocr = [
        (i, p) for i, p in enumerate(document.pages)
        if p.structure == PageStructure.SCANNED and p.ocr_method == "pending_ocr"
    ]

    if not pages_needing_ocr:
        logger.info("No pages need OCR — skipping Stage 4")
        return document

    logger.info("Running OCR on %d pages", len(pages_needing_ocr))

    for idx, page in pages_needing_ocr:
        # Get page image
        image_data = _get_page_image(document.file_path, page.page_number)
        if image_data is None:
            document.warnings.append(f"Page {page.page_number}: could not extract image for OCR")
            continue

        # Run the tiered OCR chain
        text, confidence, method = await _ocr_chain(image_data, router)

        document.pages[idx].text = text
        document.pages[idx].ocr_confidence = confidence
        document.pages[idx].ocr_method = method

        logger.info(
            "Page %d OCR: method=%s, confidence=%.2f, chars=%d",
            page.page_number,
            method,
            confidence,
            len(text),
        )

    return document


def _get_page_image(file_path: str, page_number: int) -> bytes | None:
    """Extract a page as a PNG image from a PDF, or read an image file directly."""
    path = Path(file_path)

    # Standalone image file
    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp"):
        return path.read_bytes()

    # PDF page → PNG
    try:
        doc = fitz.open(file_path)
        page = doc[page_number - 1]
        # Render at 300 DPI for good OCR quality
        pix = page.get_pixmap(dpi=300)
        image_data = pix.tobytes("png")
        doc.close()
        return image_data
    except Exception as e:
        logger.error("Failed to extract page %d image from '%s': %s", page_number, file_path, e)
        return None


async def _ocr_chain(
    image_data: bytes,
    router: ProviderRouter,
) -> tuple[str, float, str]:
    """Multi-tier OCR chain with confidence gating and escalation.

    Returns (text, confidence_score, method_used).
    """
    best_ocr_text: str = ""
    best_ocr_report: ConfidenceReport | None = None

    # Tier 1: OCR.space Engine 2 (fast, high-volume workhorse)
    if settings.ocr_space_api_key:
        text_e2, conf_e2 = await _ocr_space(image_data, engine=2)
        report_e2 = check_ocr_confidence(text_e2, reported_confidence=conf_e2)

        if report_e2.is_acceptable:
            return text_e2, report_e2.confidence_score, "ocr_space_engine2"

        logger.info("Engine 2 confidence low (%.2f) — escalating", report_e2.confidence_score)
        best_ocr_text = text_e2
        best_ocr_report = report_e2

        # Tier 2: OCR.space Engine 3 (handwriting/table-aware)
        text_e3, conf_e3 = await _ocr_space(image_data, engine=3)
        report_e3 = check_ocr_confidence(text_e3, reported_confidence=conf_e3)

        if report_e3.is_acceptable:
            return text_e3, report_e3.confidence_score, "ocr_space_engine3"

        logger.info("Engine 3 confidence low (%.2f) — escalating to vision-LLM", report_e3.confidence_score)

        # Keep the better of Engine 2 and Engine 3
        if text_e3 and (best_ocr_report is None or report_e3.confidence_score > best_ocr_report.confidence_score):
            best_ocr_text = text_e3

    # Tier 3: Vision-LLM OCR (Qwen3-VL / Gemini Flash)
    text_vlm = await _vision_llm_ocr(image_data, router)
    report_vlm = check_ocr_confidence(text_vlm)

    # Tier 4: Reconciliation — if we have both OCR.space and VLM outputs, compare
    if best_ocr_text and text_vlm:
        if texts_agree(best_ocr_text, text_vlm):
            # Both agree — use VLM (generally higher quality on ambiguous text)
            return text_vlm, max(report_vlm.confidence_score, 0.85), "vision_llm_confirmed"
        else:
            # Disagreement — flag it, keep both, use VLM as primary
            logger.warning(
                "OCR disagreement detected — VLM and OCR.space outputs differ materially"
            )
            return text_vlm, report_vlm.confidence_score * 0.9, "vision_llm_disputed"

    return text_vlm, report_vlm.confidence_score, "vision_llm"


async def _ocr_space(
    image_data: bytes,
    *,
    engine: int = 2,
) -> tuple[str, float]:
    """Call OCR.space API. Returns (text, average_confidence)."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            files = {"file": ("page.png", image_data, "image/png")}
            data = {
                "apikey": settings.ocr_space_api_key,
                "language": "eng",
                "isOverlayRequired": "true",  # Needed for per-line confidence
                "OCREngine": str(engine),
                "scale": "true",
                "isTable": "true" if engine == 3 else "false",
            }

            response = await client.post(_OCR_SPACE_URL, files=files, data=data)
            response.raise_for_status()
            result = response.json()

        if result.get("IsErroredOnProcessing", False):
            error_msg = result.get("ErrorMessage", ["Unknown error"])
            logger.warning("OCR.space error: %s", error_msg)
            return "", 0.0

        parsed_results = result.get("ParsedResults", [])
        if not parsed_results:
            return "", 0.0

        text_parts: list[str] = []
        confidences: list[float] = []

        for pr in parsed_results:
            text_parts.append(pr.get("ParsedText", ""))

            # Extract per-line confidence from overlay data
            overlay = pr.get("TextOverlay", {})
            for line in overlay.get("Lines", []):
                for word in line.get("Words", []):
                    conf = word.get("Confidence", None)
                    if conf is not None:
                        confidences.append(conf / 100.0)  # OCR.space reports 0-100

        text = "\n".join(text_parts)
        avg_conf = sum(confidences) / len(confidences) if confidences else 0.75

        return text, avg_conf

    except Exception as e:
        logger.warning("OCR.space Engine %d failed: %s", engine, e)
        return "", 0.0


async def _vision_llm_ocr(
    image_data: bytes,
    router: ProviderRouter,
) -> str:
    """Use a vision LLM to transcribe text from an image.

    Prompted to also self-report confidence on ambiguous words —
    something classical OCR engines can't do.
    """
    prompt = """Transcribe ALL text visible in this image as accurately as possible.

Rules:
- Preserve the original layout and reading order
- Use Markdown formatting (headings with #, tables with | pipes, lists with -)
- For tables, preserve column alignment and all cell values
- If any word is ambiguous or unclear, put it in [brackets] with a ? suffix, like [unclear?]
- Preserve mathematical notation using LaTeX when applicable
- Do NOT add any text that isn't visible in the image
- Do NOT summarize or paraphrase — transcribe verbatim

Output the transcribed text:"""

    try:
        return await router.vision(
            "ocr_vision",
            image_data,
            prompt,
            mime_type="image/png",
            max_tokens=8192,
        )
    except Exception as e:
        logger.error("Vision-LLM OCR failed: %s", e)
        return ""
