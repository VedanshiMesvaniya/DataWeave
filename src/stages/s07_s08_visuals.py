"""Stages 7 & 8 — Chart/Graph Analysis and Image Understanding.

Combined into one module because they share the same vision-LLM infrastructure.
Separated by prompt and routing — charts get chart-specific prompts and
model preferences, general images get broader understanding prompts.

Stage 7: Qwen3-VL primary → Gemini Flash escalation → optional cross-check
Stage 8: Qwen3-VL primary → Gemini Flash → Nemotron-Nano-VL fallback
"""

from __future__ import annotations

import logging
from pathlib import Path

import pymupdf as fitz  # PyMuPDF

from src.core.provider_client import ProviderRouter
from src.models.schemas import FigureData, PageContent, ParsedDocument

logger = logging.getLogger(__name__)


_STAGE_TIMEOUT = 120.0  # seconds for the entire visual analysis stage


async def analyze_visuals(
    document: ParsedDocument,
    router: ProviderRouter,
) -> ParsedDocument:
    """Extract and analyze charts, graphs, and images from document pages.

    Runs on pages that have embedded images detected by PyMuPDF.
    Enforces a per-image timeout (30s) and an overall stage timeout (120s)
    so that provider 503s / hangs can never block the ingestion pipeline.
    """
    import asyncio

    try:
        return await asyncio.wait_for(
            _analyze_visuals_impl(document, router),
            timeout=_STAGE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Visual analysis timed out after %.0fs for '%s' — continuing without figures",
            _STAGE_TIMEOUT, document.file_path,
        )
        return document


async def _analyze_visuals_impl(
    document: ParsedDocument,
    router: ProviderRouter,
) -> ParsedDocument:
    """Internal implementation of analyze_visuals (wrapped by timeout above)."""
    if not document.file_path.lower().endswith(".pdf"):
        # For non-PDFs, visual analysis happens during Stage 3 parsing
        # (e.g., PPTX slides rendered to images)
        return document

    try:
        doc = fitz.open(document.file_path)
    except Exception as e:
        logger.warning("Could not open document for visual analysis: %s", e)
        return document

    for idx, page_content in enumerate(document.pages):
        try:
            page = doc[page_content.page_number - 1]
            images = page.get_images(full=True)
        except Exception:
            continue

        if not images:
            continue

        import asyncio

        semaphore = asyncio.Semaphore(3)
        _VISION_TIMEOUT = 30.0  # seconds per image — prevents hung 503s from blocking

        async def process_figure(fig_idx: int, img_info: tuple) -> FigureData | None:
            xref = img_info[0]
            try:
                base_image = doc.extract_image(xref)
                if base_image is None:
                    return None

                image_data = base_image["image"]
                mime_type = f"image/{base_image.get('ext', 'png')}"

                # Skip tiny images (likely icons/bullets, not figures)
                width = base_image.get("width", 0)
                height = base_image.get("height", 0)
                if width < 100 or height < 100:
                    return None

                # Determine if this looks like a chart/graph or a general image
                is_chart = _likely_chart(width, height)

                async with semaphore:
                    try:
                        if is_chart:
                            description = await asyncio.wait_for(
                                _analyze_chart(image_data, mime_type, router),
                                timeout=_VISION_TIMEOUT,
                            )
                            task_used = "chart_analysis"
                        else:
                            description = await asyncio.wait_for(
                                _analyze_image(image_data, mime_type, router),
                                timeout=_VISION_TIMEOUT,
                            )
                            task_used = "image_understanding"
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Vision timeout (>%.0fs) for xref %d on page %d — skipping",
                            _VISION_TIMEOUT, xref, page_content.page_number,
                        )
                        return None

                return FigureData(
                    page_number=page_content.page_number,
                    figure_index=fig_idx,
                    description=description,
                    confidence=0.85 if description else 0.0,
                    extraction_method=task_used,
                )

            except Exception as e:
                logger.debug("Could not extract image xref %d: %s", xref, e)
                return None

        tasks = [process_figure(fig_idx, img_info) for fig_idx, img_info in enumerate(images)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        figures = [
            r for r in results
            if r is not None and not isinstance(r, BaseException)
        ]

        if figures:
            document.pages[idx].figures = figures

    doc.close()
    return document


def _likely_chart(width: int, height: int) -> bool:
    """Rough heuristic: charts tend to be wider/taller than photos.

    This is intentionally simple — the vision-LLM will handle the real
    classification. We're just choosing which prompt to use.
    """
    aspect = width / height if height > 0 else 1.0
    # Charts are often wider than tall, and of moderate size
    return 0.5 < aspect < 3.0 and width > 200 and height > 200


async def _analyze_chart(
    image_data: bytes,
    mime_type: str,
    router: ProviderRouter,
) -> str:
    """Analyze a chart/graph image — extract data, axes, trends."""
    prompt = """Analyze this chart or graph in detail.

Extract and describe:
1. Chart type (bar, line, pie, scatter, etc.)
2. Title and axis labels
3. All data points or values visible (be precise with numbers)
4. Key trends, patterns, or comparisons shown
5. Any legends or annotations

Be precise with numbers — if a bar shows 42.3%, report 42.3%, not "about 40%".
If you cannot read a value clearly, say so rather than guessing.

Analysis:"""

    try:
        return await router.vision(
            "chart_analysis",
            image_data,
            prompt,
            mime_type=mime_type,
            max_tokens=2048,
        )
    except Exception as e:
        logger.warning("Chart analysis failed: %s", e)
        return ""


async def _analyze_image(
    image_data: bytes,
    mime_type: str,
    router: ProviderRouter,
) -> str:
    """Analyze a general image — describe content, context, relevance."""
    prompt = """Describe this image in the context of a document.

Include:
1. What the image shows (objects, people, scenes, diagrams)
2. Any text visible in the image
3. How it relates to a document context (is it a diagram, photo, logo, screenshot?)
4. Any important details that would help someone understand the document without seeing this image

Be factual and concise. Do not speculate beyond what is visible.

Description:"""

    try:
        return await router.vision(
            "image_understanding",
            image_data,
            prompt,
            mime_type=mime_type,
            max_tokens=1024,
        )
    except Exception as e:
        logger.warning("Image analysis failed: %s", e)
        return ""
