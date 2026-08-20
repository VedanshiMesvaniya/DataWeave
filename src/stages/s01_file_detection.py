"""Stage 1 — File Detection.

Deterministic, local, zero-API-call file type identification.
Uses python-magic (libmagic bindings) with filetype as fallback,
cross-checked against file extension.

Per the architecture doc: "spending an API call on [file-type detection]
would be pure over-engineering."
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from src.models.schemas import FileCategory, FileDetectionResult

logger = logging.getLogger(__name__)

# Map MIME types to our FileCategory enum
_MIME_MAP: dict[str, FileCategory] = {
    "application/pdf": FileCategory.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": FileCategory.DOCX,
    "application/msword": FileCategory.DOC,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": FileCategory.PPTX,
    "application/vnd.ms-powerpoint": FileCategory.PPT,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": FileCategory.XLSX,
    "application/vnd.ms-excel": FileCategory.XLS,
    "text/csv": FileCategory.CSV,
    "text/tab-separated-values": FileCategory.TSV,
    "text/markdown": FileCategory.MARKDOWN,
    "text/plain": FileCategory.PLAINTEXT,
    "text/html": FileCategory.HTML,
    "text/xml": FileCategory.XML,
    "application/xml": FileCategory.XML,
    "application/json": FileCategory.JSON,
}

_EXT_MAP: dict[str, FileCategory] = {
    ".pdf": FileCategory.PDF,
    ".docx": FileCategory.DOCX,
    ".doc": FileCategory.DOC,
    ".pptx": FileCategory.PPTX,
    ".ppt": FileCategory.PPT,
    ".xlsx": FileCategory.XLSX,
    ".xls": FileCategory.XLS,
    ".csv": FileCategory.CSV,
    ".tsv": FileCategory.TSV,
    ".md": FileCategory.MARKDOWN,
    ".markdown": FileCategory.MARKDOWN,
    ".txt": FileCategory.PLAINTEXT,
    ".html": FileCategory.HTML,
    ".htm": FileCategory.HTML,
    ".xml": FileCategory.XML,
    ".json": FileCategory.JSON,
    ".png": FileCategory.IMAGE,
    ".jpg": FileCategory.IMAGE,
    ".jpeg": FileCategory.IMAGE,
    ".gif": FileCategory.IMAGE,
    ".bmp": FileCategory.IMAGE,
    ".tiff": FileCategory.IMAGE,
    ".tif": FileCategory.IMAGE,
    ".webp": FileCategory.IMAGE,
    ".svg": FileCategory.IMAGE,
}

_IMAGE_MIME_PREFIX = "image/"


def _detect_via_magic(file_path: Path) -> str | None:
    """Try python-magic first (most reliable — reads actual file bytes)."""
    try:
        import magic

        mime = magic.from_file(str(file_path), mime=True)
        return mime
    except ImportError:
        logger.debug("python-magic not installed, skipping magic-byte detection")
        return None
    except Exception as e:
        logger.debug("python-magic failed: %s", e)
        return None


def _detect_via_filetype(file_path: Path) -> str | None:
    """Fallback: filetype library (pure Python, no libmagic dependency)."""
    try:
        import filetype as ft

        kind = ft.guess(str(file_path))
        return kind.mime if kind else None
    except ImportError:
        logger.debug("filetype library not installed, skipping")
        return None
    except Exception as e:
        logger.debug("filetype detection failed: %s", e)
        return None


def _detect_via_mimetypes(file_path: Path) -> str | None:
    """Last resort: stdlib mimetypes (extension-based only)."""
    mime, _ = mimetypes.guess_type(str(file_path))
    return mime


def _mime_to_category(mime: str) -> FileCategory:
    """Convert a MIME type string to our FileCategory enum."""
    if mime in _MIME_MAP:
        return _MIME_MAP[mime]
    if mime.startswith(_IMAGE_MIME_PREFIX):
        return FileCategory.IMAGE
    return FileCategory.UNKNOWN


def detect_file(file_path: str | Path) -> FileDetectionResult:
    """Detect the file type using a layered approach: magic → filetype → extension.

    Returns a FileDetectionResult with the detected category, MIME type,
    file size, and extension.
    """
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    extension = path.suffix.lower()
    file_size = path.stat().st_size

    # Layer 1: magic bytes (most reliable)
    mime = _detect_via_magic(path)

    # Layer 2: filetype library
    if mime is None:
        mime = _detect_via_filetype(path)

    # Layer 3: stdlib mimetypes (extension-based)
    if mime is None:
        mime = _detect_via_mimetypes(path)

    # Determine category
    if mime:
        category = _mime_to_category(mime)
    else:
        category = _EXT_MAP.get(extension, FileCategory.UNKNOWN)
        mime = "application/octet-stream"

    # Cross-check: if magic says one thing and extension says another,
    # trust magic bytes but log the discrepancy.
    ext_category = _EXT_MAP.get(extension, FileCategory.UNKNOWN)
    if ext_category != FileCategory.UNKNOWN and category != ext_category:
        logger.warning(
            "File type mismatch for '%s': magic says %s (%s), extension says %s — trusting magic",
            path.name,
            category.value,
            mime,
            ext_category.value,
        )

    # If magic detection failed entirely but extension is known, use extension
    if category == FileCategory.UNKNOWN and ext_category != FileCategory.UNKNOWN:
        category = ext_category
        logger.info("Using extension-based detection for '%s': %s", path.name, category.value)

    logger.info("Detected '%s' as %s (MIME: %s)", path.name, category.value, mime)

    return FileDetectionResult(
        file_path=str(path),
        file_category=category,
        mime_type=mime,
        file_size_bytes=file_size,
        extension=extension,
    )
