"""Concurrency regression: two simultaneous ingests of identical bytes.

``check()`` (dedup) and ``create_version()`` (commit) sit at opposite ends of
the embed+store pipeline. Without a lock spanning them, two concurrent uploads
of the same file both pass the dedup check and both fully ingest — double
spending embedding quota and writing duplicate vectors. The per-content-hash
lock added to IngestionPipeline must let exactly one run the pipeline and make
the other skip.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

# The ingestion pipeline transitively imports pdfplumber (Stage 6). Skip cleanly
# where it isn't installed — same reason the e2e ingestion test is excluded.
pytest.importorskip("pdfplumber")

from src.core.ingestion_registry import IngestionRegistry  # noqa: E402
from src.pipeline.ingestion import (  # noqa: E402
    IngestionPipeline,
    IngestionResult,
    _INGEST_LOCKS,
)


@pytest.mark.asyncio
async def test_concurrent_identical_ingest_runs_pipeline_once(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_bytes(b"identical bytes to ingest twice")

    registry = IngestionRegistry(registry_path=tmp_path / "registry.json")
    pipe = IngestionPipeline(
        router=AsyncMock(),
        embedding_service=AsyncMock(),
        vector_store=AsyncMock(),
        registry=registry,
    )

    run_calls = 0

    async def fake_run_pipeline(path):
        nonlocal run_calls
        run_calls += 1
        # Yield control so the second coroutine reaches the lock while we're
        # still "embedding" — this is exactly the TOCTOU window.
        await asyncio.sleep(0.05)
        return IngestionResult(
            file_path=str(path),
            file_category="text",
            document_type="general",
            total_pages=1,
            total_chunks=3,
            document_id="doc-1",
        )

    pipe._run_pipeline = fake_run_pipeline  # type: ignore[assignment]

    try:
        results = await asyncio.gather(pipe.ingest(f), pipe.ingest(f))
    finally:
        _INGEST_LOCKS.clear()  # module-global — don't leak into other tests

    # Exactly one coroutine ran the (expensive) pipeline.
    assert run_calls == 1

    skipped = [r for r in results if r.skipped]
    ingested = [r for r in results if not r.skipped]
    assert len(ingested) == 1
    assert len(skipped) == 1

    # Registry holds a single active version — no duplicate.
    active = registry.get_active()
    assert len(active) == 1
    assert active[0]["document_id"] == "doc-1"
