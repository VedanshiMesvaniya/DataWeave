"""Ingestion Pipeline — orchestrates Stages 1–11.

Takes a file path, runs it through detection → classification → parsing →
OCR → layout → tables → visuals → chunking → embedding → vector store.

Features:
  - Deduplication via SHA-256 registry (skips already-ingested files)
  - True hybrid dense+sparse embeddings passed to vector store
  - ingest_with_progress() async generator for SSE-based progress streaming
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, AsyncGenerator

from src.core.config import settings
from src.core.ingestion_registry import IngestionRegistry, RegistryStatus
from src.core.provider_client import ProviderRouter
from src.core.rate_limiter import get_shared_rate_limiter
from src.models.schemas import Chunk, ParsedDocument
from src.stages.s01_file_detection import detect_file
from src.stages.s02_classification import classify_semantic, classify_structure
from src.stages.s03_parsing import parse_document
from src.stages.s04_ocr import run_ocr
from src.stages.s05_layout import analyze_layout
from src.stages.s06_tables import extract_tables
from src.stages.s07_s08_visuals import analyze_visuals
from src.stages.s09_chunking import chunk_document
from src.stages.s10_embeddings import EmbeddingService
from src.stages.s11_vector_store import QdrantStore

logger = logging.getLogger(__name__)


# Per-content-hash locks serialize ingestion of *identical* bytes within this
# process. ``check()`` (dedup) and ``create_version()`` (commit) are far apart —
# the whole embed+store pipeline awaits in between — so two concurrent uploads
# of the same file could both pass the dedup check and both fully ingest,
# double-spending embedding quota and writing duplicate vectors. Holding a lock
# keyed on the content hash across check→commit closes that gap. Distinct files
# hash differently, so unrelated ingestions stay fully concurrent. The dict is
# bounded by the number of distinct documents ever ingested (small for this app).
_INGEST_LOCKS: dict[str, "asyncio.Lock"] = {}


def _lock_for_hash(content_hash: str) -> "asyncio.Lock":
    """Get (or lazily create) the process-wide lock for a content hash.

    Safe without its own guard: the get-or-create runs synchronously with no
    ``await``, so the single-threaded event loop can't interleave two creators.
    """
    lock = _INGEST_LOCKS.get(content_hash)
    if lock is None:
        lock = asyncio.Lock()
        _INGEST_LOCKS[content_hash] = lock
    return lock


def _discard_redundant_upload(path: Path) -> None:
    """Delete a just-uploaded file whose content was already ingested.

    Uploads land in a unique per-upload subdirectory, so a duplicate upload
    leaves a redundant copy on disk. Remove the file (and its now-empty
    subdirectory) so duplicates don't accumulate. Best-effort — never fatal.
    """
    try:
        path.unlink(missing_ok=True)
        parent = path.parent
        # Only remove the subdirectory if it's an (empty) per-upload dir.
        if parent.name and parent != parent.parent and not any(parent.iterdir()):
            parent.rmdir()
    except Exception as e:
        logger.debug("Could not clean up redundant upload '%s': %s", path, e)


# Stage labels for progress events
_STAGE_LABELS: dict[int, str] = {
    1: "File detection",
    2: "Document classification",
    3: "Content parsing",
    4: "OCR (scanned pages)",
    5: "Layout analysis",
    6: "Table extraction",
    7: "Visual analysis",
    8: "Chunking",
    9: "Embedding",
    10: "Storing in vector DB",
}


class IngestionPipeline:
    """Orchestrates the full document ingestion flow (Stages 1–11)."""

    def __init__(
        self,
        router: ProviderRouter | None = None,
        embedding_service: EmbeddingService | None = None,
        vector_store: QdrantStore | None = None,
        registry: IngestionRegistry | None = None,
    ) -> None:
        self._rate_limiter = get_shared_rate_limiter()
        self._router = router or ProviderRouter()
        self._embeddings = embedding_service or EmbeddingService(self._rate_limiter)
        self._store = vector_store or QdrantStore(embedding_service=self._embeddings)
        self._registry = registry or IngestionRegistry()

    async def ingest(self, file_path: str | Path) -> "IngestionResult":
        """Ingest a single document through the full pipeline.

        Checks the content-addressed registry first:
          - Already ingested (identical content) → returns immediately, skipped=True
          - New content → a distinct document, ingested and registered as a new
            active version (a same-name file never displaces an existing one)

        To swap new content in for an existing document, use :meth:`replace`,
        which supersedes the old version instead of adding a parallel one.

        Returns an IngestionResult with metadata about what was processed.
        """
        path = Path(file_path)
        logger.info("=== Ingesting: %s ===", path.name)

        # ── Deduplication check ──────────────────────────────────────────────
        # ARCH-9: registry.check() hashes the file (blocking for large PDFs)
        # and reads the JSON/Qdrant registry — both must run off the event loop.
        check = await asyncio.to_thread(self._registry.check, path)

        if check.status == RegistryStatus.ALREADY_INGESTED:
            entry = check.old_entry or {}
            logger.info("Skipping '%s' — identical content already ingested", path.name)
            _discard_redundant_upload(path)
            return IngestionResult(
                file_path=str(path),
                file_category=entry.get("file_category", "unknown"),
                document_type=entry.get("document_type", "general"),
                total_pages=entry.get("total_pages", 0),
                total_chunks=entry.get("total_chunks", 0),
                warnings=[],
                skipped=True,
            )

        # ── Pipeline ────────────────────────────────────────────────────────
        # Any new content is a distinct document (content-addressed identity), so
        # there is nothing to delete first — a same-name file never displaces an
        # existing one. The per-hash lock serializes concurrent ingests of the
        # same bytes; re-check inside it so a race that committed while we waited
        # is detected and skipped instead of double-ingested.
        async with _lock_for_hash(check.sha256):
            dupe = await asyncio.to_thread(
                self._registry.active_entry_for_hash, check.sha256
            )
            if dupe is not None:
                logger.info(
                    "Skipping '%s' — identical content ingested concurrently", path.name
                )
                _discard_redundant_upload(path)
                return IngestionResult(
                    file_path=str(path),
                    file_category=dupe.get("file_category", "unknown"),
                    document_type=dupe.get("document_type", "general"),
                    total_pages=dupe.get("total_pages", 0),
                    total_chunks=dupe.get("total_chunks", 0),
                    warnings=[],
                    skipped=True,
                    document_id=dupe.get("document_id", ""),
                )

            result = await self._run_pipeline(path)

            # Register after successful ingestion (brand-new document, no supersede)
            await self._commit_version(
                path,
                document_id=result.document_id,
                total_chunks=result.total_chunks,
                content_hash=check.sha256,
                supersedes=None,
            )

        return result

    async def _commit_version(
        self,
        path: Path,
        document_id: str,
        total_chunks: int,
        content_hash: str,
        supersedes: str | None,
    ) -> None:
        """Commit a freshly-indexed version, performing the cutover if replacing.

        Assumes the content is already embedded and upserted (chunks active).
        Steps, each durable before the next, so there is never a window with
        zero active versions:

            create new version (registry) → hide old chunks (Qdrant)
            → supersede old (registry)

        For a brand-new document (``supersedes is None``) only the first step
        runs.
        """
        # ARCH-9: registry writes (JSON file I/O or Qdrant upsert) must not
        # block the event loop — run them in the default thread-pool executor.
        await asyncio.to_thread(
            self._registry.create_version,
            path,
            content_hash,
            total_chunks,
            document_id,
            supersedes,
        )
        if not supersedes:
            return

        try:
            await self._store.set_document_active(supersedes, False)
        except Exception:
            logger.exception(
                "Replace: failed to deactivate old chunks for %s — new version "
                "is live but stale chunks may linger",
                supersedes,
            )
        await asyncio.to_thread(self._registry.supersede, supersedes, document_id)
        logger.info("Cutover complete: %s → %s", supersedes, document_id)

    async def ingest_with_progress(
        self, file_path: str | Path, supersedes: str | None = None
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Async generator that yields SSE-compatible progress events for each stage.

        Each event dict has: stage (int), label (str), status ("running"|"done"|"skipped"|"error")
        The final event has type="complete" with the full IngestionResult dict.

        Usage:
            async for event in pipeline.ingest_with_progress(path):
                yield f"data: {json.dumps(event)}\\n\\n"
        """
        path = Path(file_path)

        # ── Deduplication check ──────────────────────────────────────────────
        # Content-addressed: identical content is skipped; anything else is a
        # new, distinct document (a same-name file never displaces an existing
        # one), so there is nothing to delete first.
        # ARCH-9: SHA-256 hashing blocks; run off the event loop.
        check = await asyncio.to_thread(self._registry.check, path)
        if check.status == RegistryStatus.ALREADY_INGESTED:
            entry = check.old_entry or {}
            _discard_redundant_upload(path)
            yield {"type": "skipped", "file": path.name, "reason": "Already ingested (identical content)"}
            yield {
                "type": "complete",
                "skipped": True,
                "result": {
                    "file_path": str(path),
                    "total_chunks": entry.get("total_chunks", 0),
                    "total_pages": entry.get("total_pages", 0),
                    "warnings": [],
                },
            }
            return

        # Serialize concurrent ingests of identical bytes across the whole
        # embed+store pipeline (see ingest() / _lock_for_hash), re-checking dedup
        # inside the lock so a race that committed while we waited is skipped
        # rather than double-ingested.
        async with _lock_for_hash(check.sha256):
            dupe = await asyncio.to_thread(
                self._registry.active_entry_for_hash, check.sha256
            )
            if dupe is not None:
                _discard_redundant_upload(path)
                yield {"type": "skipped", "file": path.name, "reason": "Already ingested (identical content)"}
                yield {
                    "type": "complete",
                    "skipped": True,
                    "result": {
                        "file_path": str(path),
                        "total_chunks": dupe.get("total_chunks", 0),
                        "total_pages": dupe.get("total_pages", 0),
                        "warnings": [],
                    },
                }
                return

            async for event in self._staged_ingest_with_progress(
                path, content_hash=check.sha256, supersedes=supersedes
            ):
                yield event

    async def _staged_ingest_with_progress(
        self, path: Path, *, content_hash: str, supersedes: str | None
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Run Stages 1–10 with SSE progress events, then commit the version.

        Split out of :meth:`ingest_with_progress` so the public method can hold
        the per-hash lock across the whole run (dedup re-check → commit).
        """

        def _event(stage: int, status: str, **extra: Any) -> dict[str, Any]:
            return {
                "type": "progress",
                "stage": stage,
                "total_stages": len(_STAGE_LABELS),
                "label": _STAGE_LABELS.get(stage, f"Stage {stage}"),
                "status": status,
                **extra,
            }

        # ── Stage 1: File Detection ──────────────────────────────────────────
        yield _event(1, "running")
        try:
            detection = detect_file(path)
            yield _event(1, "done", detail=detection.file_category.value)
        except Exception as e:
            yield _event(1, "error", error=str(e))
            yield {"type": "error", "message": str(e)}
            return

        # ── Stage 2: Classification ──────────────────────────────────────────
        yield _event(2, "running")
        try:
            classification = classify_structure(path, detection)
            yield _event(2, "done", detail=classification.structural.value)
        except Exception as e:
            yield _event(2, "error", error=str(e))
            return

        # ── Stage 3: Parsing ─────────────────────────────────────────────────
        yield _event(3, "running")
        try:
            document = parse_document(path, detection, classification)
            # Semantic classification needs parsed text
            text_sample = ""
            for page in document.pages:
                if page.text:
                    text_sample += page.text
                    if len(text_sample) > 2000:
                        break
            document.document_type = await classify_semantic(text_sample, detection, self._router)
            yield _event(3, "done", detail=f"{document.total_pages} pages, type={document.document_type.value}")
        except Exception as e:
            yield _event(3, "error", error=str(e))
            return

        # ── Stage 4: OCR ─────────────────────────────────────────────────────
        # Stages 4–7 depend on OCR/vision providers that routinely 429 on free
        # tiers. A failure here must NOT abort the whole document — degrade
        # gracefully and keep the text already parsed in Stage 3, so the doc
        # still gets chunked, embedded, and stored.
        yield _event(4, "running")
        try:
            document = await run_ocr(document, self._router)
            yield _event(4, "done")
        except Exception as e:
            logger.warning("Stage 4 (OCR) failed for '%s': %s — continuing without it", path.name, e)
            document.warnings.append(f"OCR skipped: {e}")
            yield _event(4, "error", error=str(e))

        # ── Stage 5: Layout Analysis ─────────────────────────────────────────
        yield _event(5, "running")
        try:
            document = await analyze_layout(document, self._router)
            yield _event(5, "done")
        except Exception as e:
            logger.warning("Stage 5 (layout) failed for '%s': %s — continuing", path.name, e)
            document.warnings.append(f"Layout analysis skipped: {e}")
            yield _event(5, "error", error=str(e))

        # ── Stage 6: Table Extraction ────────────────────────────────────────
        yield _event(6, "running")
        try:
            document = await extract_tables(document, self._router)
            table_count = sum(len(p.tables) for p in document.pages)
            yield _event(6, "done", detail=f"{table_count} tables")
        except Exception as e:
            logger.warning("Stage 6 (tables) failed for '%s': %s — continuing", path.name, e)
            document.warnings.append(f"Table extraction skipped: {e}")
            yield _event(6, "error", error=str(e))

        # ── Stage 7: Visual Analysis ─────────────────────────────────────────
        yield _event(7, "running")
        try:
            document = await analyze_visuals(document, self._router)
            yield _event(7, "done")
        except Exception as e:
            logger.warning("Stage 7 (visuals) failed for '%s': %s — continuing", path.name, e)
            document.warnings.append(f"Visual analysis skipped: {e}")
            yield _event(7, "error", error=str(e))

        # ── Stage 8: Chunking ────────────────────────────────────────────────
        yield _event(8, "running")
        try:
            chunks = chunk_document(document)
            yield _event(8, "done", detail=f"{len(chunks)} chunks")
        except Exception as e:
            yield _event(8, "error", error=str(e))
            return

        if not chunks:
            yield {"type": "warning", "message": "No chunks produced — document may be empty"}
            yield {
                "type": "complete",
                "skipped": False,
                "result": {
                    "file_path": str(path),
                    "total_chunks": 0,
                    "total_pages": document.total_pages,
                    "warnings": document.warnings,
                },
            }
            return

        # ── Stage 9: Embeddings ──────────────────────────────────────────────
        yield _event(9, "running", detail=f"Embedding {len(chunks)} chunks")
        try:
            vectors, sparse_vectors = await self._embeddings.embed_chunks(chunks)
            has_sparse = any(not sv.is_empty() for sv in sparse_vectors)
            yield _event(9, "done", detail=f"sparse={'yes' if has_sparse else 'no'}")
        except Exception as e:
            yield _event(9, "error", error=str(e))
            return

        # ── Stage 10: Vector Store ───────────────────────────────────────────
        yield _event(10, "running")
        try:
            await self._store.upsert(chunks, vectors, sparse_vectors)
            yield _event(10, "done")
        except Exception as e:
            yield _event(10, "error", error=str(e))
            return

        # ── Registration & Complete ──────────────────────────────────────────
        # Content is fully indexed now, so committing the version (and, for a
        # replace, cutting over from the old one) is safe — the old version was
        # live throughout the stages above.
        document_id = chunks[0].document_id if chunks else ""
        await self._commit_version(
            path,
            document_id=document_id,
            total_chunks=len(chunks),
            content_hash=content_hash,
            supersedes=supersedes,
        )

        yield {
            "type": "complete",
            "skipped": False,
            "result": {
                "file_path": str(path),
                "file_category": detection.file_category.value,
                "document_type": document.document_type.value,
                "total_pages": document.total_pages,
                "total_chunks": len(chunks),
                "warnings": document.warnings,
                "document_id": document_id,
            },
        }

    async def replace(
        self, old_document_id: str, file_path: str | Path
    ) -> "IngestionResult":
        """Replace an existing document with new content — safe atomic cutover.

        The old version stays fully live until the new one is completely indexed.
        Ordering (each step durable before the next):

            1. index new content        (embed + upsert, chunks born active)
            2. create new version        (registry: new active, supersedes old)
            3. hide old chunks in Qdrant (set active=False)
            4. supersede old in registry (registry: old inactive, linked)

        A failure before step 2 leaves the old version fully active (the new
        chunks are simply orphaned and re-created on retry, since document_id is
        derived from the unique upload path). A failure between 2 and 4 leaves
        *both* versions active — a harmless duplicate, never zero. There is no
        ordering that yields zero active versions.
        """
        path = Path(file_path)
        old = self._registry.get_by_document_id(old_document_id)
        if old is None:
            raise ValueError(f"Cannot replace unknown document_id={old_document_id!r}")

        # Identical content → nothing to replace.
        check = self._registry.check(path)
        if check.status == RegistryStatus.ALREADY_INGESTED:
            logger.info(
                "Replace '%s' → identical content already active; no-op", path.name
            )
            _discard_redundant_upload(path)
            return IngestionResult(
                file_path=str(path),
                file_category=old.get("file_category", "unknown"),
                document_type=old.get("document_type", "general"),
                total_pages=old.get("total_pages", 0),
                total_chunks=old.get("total_chunks", 0),
                warnings=[],
                skipped=True,
                document_id=check.old_document_id or old_document_id,
            )

        # 1) Index the new content (chunks are upserted active=True).
        result = await self._run_pipeline(path)
        if not result.document_id or result.total_chunks == 0:
            # Nothing indexed — do NOT touch the old version.
            logger.warning(
                "Replace '%s' produced no chunks; keeping old version %s active",
                path.name,
                old_document_id,
            )
            return result

        # 2–4) Commit the new version and cut over from the old one.
        await self._commit_version(
            path,
            document_id=result.document_id,
            total_chunks=result.total_chunks,
            content_hash=check.sha256,
            supersedes=old_document_id,
        )

        logger.info(
            "Replaced document %s → %s ('%s', %d chunks)",
            old_document_id,
            result.document_id,
            path.name,
            result.total_chunks,
        )
        return result

    # ------------------------------------------------------------------
    # Internal: shared pipeline logic (no progress events)
    # ------------------------------------------------------------------

    async def _run_pipeline(self, path: Path) -> "IngestionResult":
        """Run the full ingestion pipeline without progress events."""
        # Stage 1 — File Detection
        logger.info("[Stage 1] File detection")
        detection = detect_file(path)

        # Stage 2a — Structural Classification
        logger.info("[Stage 2a] Structural classification")
        classification = classify_structure(path, detection)

        # Stage 3 — Parsing
        logger.info("[Stage 3] Parsing")
        document = parse_document(path, detection, classification)

        # Stage 2b — Semantic Classification (needs text from parsing)
        logger.info("[Stage 2b] Semantic classification")
        text_sample = ""
        for page in document.pages:
            if page.text:
                text_sample += page.text
                if len(text_sample) > 2000:
                    break
        document.document_type = await classify_semantic(text_sample, detection, self._router)

        # Stages 4–7 — OCR/layout/tables/visuals. Provider-dependent and
        # failure-prone on free tiers, so each is non-fatal: on error we keep
        # the document as parsed and move on rather than dropping the upload.
        for stage_num, stage_name, stage_fn in (
            (4, "OCR", run_ocr),
            (5, "Layout analysis", analyze_layout),
            (6, "Table extraction", extract_tables),
            (7, "Visual analysis", analyze_visuals),
        ):
            logger.info("[Stage %d] %s", stage_num, stage_name)
            try:
                document = await stage_fn(document, self._router)
            except Exception as e:
                logger.warning("[Stage %d] %s failed: %s — continuing", stage_num, stage_name, e)
                document.warnings.append(f"{stage_name} skipped: {e}")

        # Stage 9 — Chunking
        logger.info("[Stage 9] Chunking")
        chunks = chunk_document(document)

        if not chunks:
            logger.warning("No chunks produced from '%s'", path.name)
            return IngestionResult(
                file_path=str(path),
                file_category=detection.file_category.value,
                document_type=document.document_type.value,
                total_pages=document.total_pages,
                total_chunks=0,
                warnings=document.warnings + ["No chunks produced"],
                document_id="",
            )

        # Stage 10 — Embeddings (returns dense + sparse)
        logger.info("[Stage 10] Embedding %d chunks", len(chunks))
        vectors, sparse_vectors = await self._embeddings.embed_chunks(chunks)

        # Stage 11 — Vector Store (with sparse vectors)
        logger.info("[Stage 11] Storing in vector DB")
        await self._store.upsert(chunks, vectors, sparse_vectors)

        document_id = chunks[0].document_id if chunks else ""

        result = IngestionResult(
            file_path=str(path),
            file_category=detection.file_category.value,
            document_type=document.document_type.value,
            total_pages=document.total_pages,
            total_chunks=len(chunks),
            warnings=document.warnings,
            document_id=document_id,
        )

        logger.info(
            "=== Ingestion complete: %s → %d chunks stored ===",
            path.name,
            len(chunks),
        )
        return result


async def reconcile_active_flags(
    registry: IngestionRegistry | None = None,
    store: QdrantStore | None = None,
) -> dict[str, int]:
    """Force the vector store's chunk ``active`` flags to match registry state.

    The registry (durable in Qdrant) is the source of truth for which version is
    live. A crash *between* marking a version superseded and flipping its chunks
    inactive would leave stale chunks retrievable. Running this at startup closes
    that gap: every superseded document's chunks are forced inactive. Idempotent
    and safe to run repeatedly — it only ever hides already-superseded content.
    """
    registry = registry or IngestionRegistry()
    store = store or QdrantStore()

    superseded = registry.get_superseded_ids()
    reconciled = 0
    for doc_id in superseded:
        try:
            await store.set_document_active(doc_id, False)
            reconciled += 1
        except Exception:
            logger.exception("Reconcile: failed to deactivate chunks for %s", doc_id)

    if superseded:
        logger.info(
            "Reconcile: ensured %d/%d superseded document(s) are hidden from retrieval",
            reconciled,
            len(superseded),
        )
    return {"superseded": len(superseded), "reconciled": reconciled}


class IngestionResult:
    """Result of a document ingestion."""

    def __init__(
        self,
        file_path: str,
        file_category: str,
        document_type: str,
        total_pages: int,
        total_chunks: int,
        warnings: list[str] | None = None,
        skipped: bool = False,
        document_id: str = "",
    ) -> None:
        self.file_path = file_path
        self.file_category = file_category
        self.document_type = document_type
        self.total_pages = total_pages
        self.total_chunks = total_chunks
        self.warnings = warnings or []
        self.skipped = skipped
        self.document_id = document_id

    def to_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "file_category": self.file_category,
            "document_type": self.document_type,
            "total_pages": self.total_pages,
            "total_chunks": self.total_chunks,
            "warnings": self.warnings,
            "skipped": self.skipped,
            "document_id": self.document_id,
        }
