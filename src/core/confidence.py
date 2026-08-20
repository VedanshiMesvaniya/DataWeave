"""Confidence and QA gates — flags low-quality extraction before it enters the vector store.

This addresses the architecture doc's explicit callout (Section 5) that a missing
confidence gate between extraction and chunking is a real gap that causes silent
corruption in the RAG pipeline.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Characters that are almost certainly OCR garbage
_GARBAGE_PATTERN = re.compile(r"[^\w\s.,;:!?'\"\-(){}[\]@#$%&*/+=<>|~`^\\€£¥°±²³µ¶·¹º¿÷]")
_DICT_WORD_PATTERN = re.compile(r"\b[a-zA-Z]{2,}\b")


@dataclass
class ConfidenceReport:
    """Result of a confidence check on extracted content."""
    is_acceptable: bool
    confidence_score: float  # 0.0 – 1.0
    issues: list[str]
    recommendation: str  # "accept", "escalate", "flag_for_review"


def check_ocr_confidence(
    text: str,
    *,
    reported_confidence: float | None = None,
    min_confidence: float = 0.75,
    max_garbage_ratio: float = 0.15,
    min_word_ratio: float = 0.50,
) -> ConfidenceReport:
    """Evaluate OCR output quality using multiple heuristics.

    Checks:
    1. Provider-reported confidence (if available)
    2. Garbage character ratio
    3. Dictionary-like word ratio (proxy for meaningful text)
    4. Empty/near-empty output
    """
    issues: list[str] = []

    if not text or len(text.strip()) < 10:
        return ConfidenceReport(
            is_acceptable=False,
            confidence_score=0.0,
            issues=["Output is empty or near-empty"],
            recommendation="escalate",
        )

    scores: list[float] = []

    # 1. Provider-reported confidence
    if reported_confidence is not None:
        scores.append(reported_confidence)
        if reported_confidence < min_confidence:
            issues.append(
                f"Provider confidence {reported_confidence:.2f} < threshold {min_confidence:.2f}"
            )

    # 2. Garbage character ratio
    total_chars = len(text)
    garbage_chars = len(_GARBAGE_PATTERN.findall(text))
    garbage_ratio = garbage_chars / total_chars if total_chars > 0 else 0.0
    garbage_score = max(0.0, 1.0 - (garbage_ratio / max_garbage_ratio))
    scores.append(garbage_score)
    if garbage_ratio > max_garbage_ratio:
        issues.append(f"High garbage character ratio: {garbage_ratio:.2%}")

    # 3. Dictionary-like word ratio
    words = text.split()
    if words:
        dict_words = _DICT_WORD_PATTERN.findall(text)
        word_ratio = len(dict_words) / len(words)
        word_score = min(1.0, word_ratio / min_word_ratio)
        scores.append(word_score)
        if word_ratio < min_word_ratio:
            issues.append(f"Low recognizable-word ratio: {word_ratio:.2%}")
    else:
        scores.append(0.0)
        issues.append("No words detected")

    # Aggregate
    confidence = sum(scores) / len(scores) if scores else 0.0

    if confidence >= min_confidence and not issues:
        recommendation = "accept"
    elif confidence >= min_confidence * 0.6:
        recommendation = "escalate"
    else:
        recommendation = "flag_for_review"

    return ConfidenceReport(
        is_acceptable=confidence >= min_confidence and len(issues) == 0,
        confidence_score=round(confidence, 3),
        issues=issues,
        recommendation=recommendation,
    )


def check_table_extraction(
    rows: list[list[str]],
    *,
    min_rows: int = 2,
    min_cols: int = 2,
    max_empty_ratio: float = 0.5,
) -> ConfidenceReport:
    """Validate an extracted table for structural integrity."""
    issues: list[str] = []

    if len(rows) < min_rows:
        issues.append(f"Too few rows: {len(rows)} < {min_rows}")

    if rows:
        col_counts = [len(row) for row in rows]
        if max(col_counts) < min_cols:
            issues.append(f"Too few columns: {max(col_counts)} < {min_cols}")

        # Check column count consistency
        if len(set(col_counts)) > 1:
            issues.append(f"Inconsistent column counts: {set(col_counts)}")

        # Check empty cell ratio
        total_cells = sum(col_counts)
        empty_cells = sum(1 for row in rows for cell in row if not cell.strip())
        empty_ratio = empty_cells / total_cells if total_cells > 0 else 1.0
        if empty_ratio > max_empty_ratio:
            issues.append(f"High empty-cell ratio: {empty_ratio:.2%}")

    confidence = 1.0 - (len(issues) * 0.25)
    confidence = max(0.0, min(1.0, confidence))

    return ConfidenceReport(
        is_acceptable=len(issues) == 0,
        confidence_score=round(confidence, 3),
        issues=issues,
        recommendation="accept" if not issues else "escalate",
    )


def texts_agree(text_a: str, text_b: str, *, threshold: float = 0.85) -> bool:
    """Quick check whether two OCR outputs agree enough to trust either one.

    Uses character-level similarity (good enough for OCR reconciliation).
    For a proper implementation, this would use difflib or Levenshtein —
    but for the gate check, a simple token overlap ratio is fast and sufficient.
    """
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())

    if not words_a and not words_b:
        return True
    if not words_a or not words_b:
        return False

    intersection = words_a & words_b
    union = words_a | words_b
    jaccard = len(intersection) / len(union)

    return jaccard >= threshold
