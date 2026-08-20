"""Folder auto-ingestion — watch a drop-folder and ingest new files.

This is the "automation" layer over :class:`~src.pipeline.ingestion.IngestionPipeline`.
Drop files into the watched folder (``settings.auto_ingest_dir``) and a scan
runs each one through the full pipeline. Identity is content-addressed, so the
scan is **idempotent**: files already ingested are skipped, and files may safely
stay in the folder across scans without producing duplicates.

Three ways to drive it, all sharing :func:`scan_and_ingest`:

  * on demand — the ``POST /ingest/folder`` API endpoint (used by the UI's
    "Scan folder" action, which shows a short popup with the result);
  * once on startup — ``AUTO_INGEST_ON_STARTUP=true``;
  * periodically — ``AUTO_INGEST_INTERVAL_SECONDS=N`` runs a background loop.

The service depends only on the small ``_Ingester`` interface (an ``ingest``
coroutine returning a result with a ``skipped`` flag), so it is trivially
testable with a fake pipeline and no heavy document-processing dependencies.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from src.core.config import settings

logger = logging.getLogger(__name__)

# Bound concurrent ingestions so a full folder can't fan out into hundreds of
# simultaneous provider calls (mirrors the /upload/batch limit).
_MAX_CONCURRENCY = 3


class _IngestResult(Protocol):
    """The slice of ``IngestionResult`` the folder scanner relies on."""

    skipped: bool
    total_chunks: int
    document_id: str


class _Ingester(Protocol):
    """Minimal interface the scanner needs — satisfied by ``IngestionPipeline``."""

    async def ingest(self, file_path: str | Path) -> _IngestResult: ...


@dataclass
class FolderIngestionResult:
    """Summary of one drop-folder scan."""

    folder: str
    scanned: int = 0
    ingested: int = 0
    skipped: int = 0
    failed: int = 0
    ingested_files: list[str] = field(default_factory=list)
    failed_files: list[dict[str, str]] = field(default_factory=list)

    @property
    def message(self) -> str:
        """A short, human-readable status suitable for a UI toast/popup."""
        if self.scanned == 0:
            return "The watch folder is empty — nothing to ingest."
        if self.ingested == 0 and self.failed == 0:
            return "No new files — everything in the folder is already ingested."
        parts = [f"Ingested {self.ingested} new file(s)"]
        if self.skipped:
            parts.append(f"{self.skipped} already ingested")
        if self.failed:
            parts.append(f"{self.failed} failed")
        return ", ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "folder": self.folder,
            "scanned": self.scanned,
            "ingested": self.ingested,
            "skipped": self.skipped,
            "failed": self.failed,
            "ingested_files": self.ingested_files,
            "failed_files": self.failed_files,
            "message": self.message,
        }


def _discover_files(folder: Path) -> list[Path]:
    """Return the files eligible for ingestion under ``folder``.

    Recurses, but ignores dotfiles and anything inside a dot-directory (e.g.
    ``.git``, macOS ``.Trashes``) and empty files. Sorted for deterministic,
    reproducible scans.
    """
    if not folder.exists():
        return []
    files: list[Path] = []
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(folder).parts):
            continue
        try:
            if path.stat().st_size == 0:
                continue
        except OSError:
            continue
        files.append(path)
    return sorted(files)


async def scan_and_ingest(
    pipeline: _Ingester | None = None,
    folder: str | Path | None = None,
) -> FolderIngestionResult:
    """Scan the drop-folder and ingest every new file through the pipeline.

    Args:
        pipeline: the ingester to use. Defaults to a fresh ``IngestionPipeline``.
            Injectable so tests (and alternative pipelines) can stand in.
        folder: the folder to scan. Defaults to ``settings.auto_ingest_dir``.

    Returns:
        A :class:`FolderIngestionResult` summarizing the scan — safe to surface
        directly to the UI (its ``message`` covers the empty / nothing-new cases).
    """
    target = Path(folder) if folder is not None else settings.auto_ingest_dir
    result = FolderIngestionResult(folder=str(target))

    files = _discover_files(target)
    result.scanned = len(files)
    if not files:
        logger.info("Folder scan: '%s' has no ingestable files", target)
        return result

    # Build the real pipeline lazily so importing this module never drags in the
    # heavy document-processing stack (only needed when a scan actually runs).
    if pipeline is None:
        from src.pipeline.ingestion import IngestionPipeline

        pipeline = IngestionPipeline()

    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _ingest_one(path: Path) -> None:
        async with semaphore:
            try:
                res = await pipeline.ingest(path)
            except Exception as e:
                logger.exception("Folder scan: failed to ingest '%s'", path.name)
                result.failed += 1
                result.failed_files.append({"file": path.name, "error": str(e)})
                return
        if getattr(res, "skipped", False):
            result.skipped += 1
        else:
            result.ingested += 1
            result.ingested_files.append(path.name)

    await asyncio.gather(*(_ingest_one(p) for p in files))

    logger.info(
        "Folder scan of '%s': %d scanned, %d ingested, %d skipped, %d failed",
        target,
        result.scanned,
        result.ingested,
        result.skipped,
        result.failed,
    )
    return result


async def run_periodic_scan(interval_seconds: int) -> None:
    """Background loop that re-scans the drop-folder every ``interval_seconds``.

    Cancellation-safe (meant to be launched as an asyncio task and cancelled on
    shutdown) and self-healing — a failed scan is logged and the loop continues.
    """
    logger.info("Auto-ingest: periodic folder scan enabled every %ds", interval_seconds)
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await scan_and_ingest()
            except Exception:
                logger.exception("Auto-ingest: periodic scan failed — will retry")
    except asyncio.CancelledError:
        logger.info("Auto-ingest: periodic folder scan stopped")
        raise
