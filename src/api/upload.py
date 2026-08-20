"""Upload API — file ingestion endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from src.core.config import settings
from src.core.paths import safe_basename, unique_upload_dest
from src.pipeline.ingestion import IngestionPipeline

logger = logging.getLogger(__name__)
router = APIRouter()


def _resolve_upload_path(filename: str | None) -> Path:
    """Validate an untrusted upload filename and return a safe destination path.

    ``safe_basename`` collapses the name to a basename so a crafted filename
    like ``"../../etc/cron.d/x"`` can't escape the uploads directory; the result
    is then placed in a unique per-upload subdirectory (see
    :func:`~src.core.paths.unique_upload_dest`).
    """
    name = safe_basename(filename or "")
    if name is None:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return unique_upload_dest(settings.upload_dir, name)


@router.post("/upload")
async def upload_document(file: UploadFile = File(...)) -> dict:
    """Upload and ingest a single document into the RAG pipeline."""
    upload_path = _resolve_upload_path(file.filename)
    try:
        with open(upload_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception:
        logger.exception("Failed to save uploaded file '%s'", upload_path.name)
        raise HTTPException(status_code=500, detail="Failed to save uploaded file")

    try:
        pipeline = IngestionPipeline()
        result = await pipeline.ingest(upload_path)
        return {
            "status": "success",
            "message": f"Ingested '{upload_path.name}' successfully",
            **result.to_dict(),
        }
    except Exception:
        logger.exception("Ingestion failed for '%s'", upload_path.name)
        raise HTTPException(status_code=500, detail="Ingestion failed")


@router.post("/upload/batch")
async def upload_documents_batch(files: list[UploadFile] = File(...)) -> dict:
    """Upload and ingest multiple documents concurrently (max 3 at a time)."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    pipeline = IngestionPipeline()
    semaphore = asyncio.Semaphore(3)
    results = []
    errors = []

    async def _process_file(file: UploadFile) -> None:
        name = safe_basename(file.filename or "")
        if name is None:
            errors.append({"file": file.filename or "", "error": "Invalid filename"})
            return

        # Use a unique per-upload subdirectory (same as the other endpoints) so
        # two batch files with the same name — or a batch file colliding with an
        # existing upload — never overwrite each other on disk.
        upload_path = unique_upload_dest(settings.upload_dir, name)
        try:
            with open(upload_path, "wb") as f:
                shutil.copyfileobj(file.file, f)
        except Exception:
            logger.exception("Failed to save uploaded file '%s'", name)
            errors.append({"file": name, "error": "Failed to save file"})
            return

        async with semaphore:
            try:
                res = await pipeline.ingest(upload_path)
                results.append(res.to_dict())
            except Exception:
                logger.exception("Batch ingestion failed for '%s'", name)
                errors.append({"file": name, "error": "Ingestion failed"})

    await asyncio.gather(*[_process_file(f) for f in files])

    return {
        "status": "success" if not errors else "partial_success",
        "processed_count": len(results),
        "error_count": len(errors),
        "results": results,
        "errors": errors,
    }


@router.post("/upload/stream")
async def upload_document_stream(file: UploadFile = File(...)):
    """Upload a document and receive real-time ingestion progress via SSE."""
    upload_path = _resolve_upload_path(file.filename)
    try:
        with open(upload_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception:
        logger.exception("Failed to save uploaded file '%s'", upload_path.name)
        raise HTTPException(status_code=500, detail="Failed to save uploaded file")

    pipeline = IngestionPipeline()

    async def _event_generator() -> AsyncGenerator[str, None]:
        try:
            async for event in pipeline.ingest_with_progress(upload_path):
                # Format as Server-Sent Events (SSE)
                yield f"data: {json.dumps(event)}\n\n"
        except Exception:
            logger.exception("Streaming ingestion failed for '%s'", upload_path.name)
            error_event = {
                "stage": 0,
                "label": "complete",
                "status": "error",
                "error": "Ingestion failed",
            }
            yield f"data: {json.dumps(error_event)}\n\n"

    return StreamingResponse(_event_generator(), media_type="text/event-stream")


@router.post("/ingest/folder")
async def scan_ingest_folder() -> dict:
    """Scan the watched drop-folder and ingest any new files.

    Content-addressed dedup makes this idempotent: files already ingested are
    skipped, so it's safe to call repeatedly. The returned ``message`` is a
    short, ready-to-display status (covers the empty-folder and nothing-new
    cases) that the UI surfaces as a popup.
    """
    from src.pipeline.folder_ingestion import scan_and_ingest

    try:
        result = await scan_and_ingest()
    except Exception:
        logger.exception("Folder scan failed")
        raise HTTPException(status_code=500, detail="Folder scan failed")
    return {"status": "success", **result.to_dict()}


@router.post("/documents/{old_document_id}/replace")
async def replace_document(old_document_id: str, file: UploadFile = File(...)) -> dict:
    """Replace an existing document with a new file (safe atomic cutover).

    The old version stays live until the new one is fully indexed, so a failure
    mid-ingestion never leaves the document with no active version.
    """
    upload_path = _resolve_upload_path(file.filename)
    try:
        with open(upload_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception:
        logger.exception("Failed to save uploaded file '%s'", upload_path.name)
        raise HTTPException(status_code=500, detail="Failed to save uploaded file")

    try:
        pipeline = IngestionPipeline()
        result = await pipeline.replace(old_document_id, upload_path)
        return {
            "status": "success",
            "message": f"Replaced document with '{upload_path.name}'",
            "old_document_id": old_document_id,
            **result.to_dict(),
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        logger.exception("Replace failed for document '%s'", old_document_id)
        raise HTTPException(status_code=500, detail="Replace failed")


@router.post("/documents/{old_document_id}/replace/stream")
async def replace_document_stream(old_document_id: str, file: UploadFile = File(...)):
    """Replace a document and receive real-time ingestion progress via SSE.

    Streams the same per-stage progress as ``/upload/stream``; the version
    cutover (old → new) happens once the new content is fully indexed.
    """
    from src.core.ingestion_registry import IngestionRegistry

    if IngestionRegistry().get_by_document_id(old_document_id) is None:
        raise HTTPException(status_code=404, detail="Document to replace not found")

    upload_path = _resolve_upload_path(file.filename)
    try:
        with open(upload_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception:
        logger.exception("Failed to save uploaded file '%s'", upload_path.name)
        raise HTTPException(status_code=500, detail="Failed to save uploaded file")

    pipeline = IngestionPipeline()

    async def _event_generator() -> AsyncGenerator[str, None]:
        try:
            async for event in pipeline.ingest_with_progress(
                upload_path, supersedes=old_document_id
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception:
            logger.exception("Streaming replace failed for '%s'", old_document_id)
            error_event = {
                "stage": 0,
                "label": "complete",
                "status": "error",
                "error": "Replace failed",
            }
            yield f"data: {json.dumps(error_event)}\n\n"

    return StreamingResponse(_event_generator(), media_type="text/event-stream")
