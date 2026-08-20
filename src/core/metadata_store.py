"""Durable, platform-independent metadata persistence for the document registry.

The document registry's authoritative store is a **Qdrant collection**, not the
local filesystem. Qdrant is already the app's durable, hosted vector store, so
keeping document metadata there means:

  * **One durable source of truth.** Metadata lives in the same managed service
    as the embeddings it describes — they cannot drift apart when the app moves
    between hosts.
  * **No local-disk assumption.** Nothing depends on a persistent volume. A
    container restart, redeploy, or migration to any platform (Docker, K8s, a
    VPS, any PaaS) preserves all metadata as long as ``QDRANT_URL`` points at the
    same cluster.
  * **Automatic migration.** On first use, if the Qdrant metadata collection is
    empty and a legacy ``data/ingested_files.json`` exists, its entries are
    imported (and upgraded from the old sha256-keyed schema) exactly once.

When Qdrant is not configured (local development), a file-backed JSON store is
used instead. In that mode the JSON file *is* the store; in Qdrant mode any
local file is only an import seed, never authoritative.

The backend exposes two operations — a full snapshot read and an atomic batch
write (upserts + deletes together) — which is all the registry's version state
machine needs, and which each backend can implement atomically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from src.core.config import DATA_DIR, settings
from src.core.file_lock import LockMode, locked

logger = logging.getLogger(__name__)

_REGISTRY_FILE = DATA_DIR / "ingested_files.json"
_DOCUMENTS_COLLECTION = "globle_mind_documents"


# ---------------------------------------------------------------------------
# Schema migration (shared by both backends)
# ---------------------------------------------------------------------------

def _utcnow_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.UTC).isoformat()


def migrate_registry(raw: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Upgrade a legacy (sha256-keyed) registry to the lineage schema.

    Legacy entries look like::

        {"<sha256>": {"document_id", "file_name", "file_path",
                      "file_size_bytes", "total_chunks", "ingested_at"}}

    Detection: legacy entries lack a ``content_hash`` field. Each becomes a
    standalone active lineage rooted at itself. Returns ``(entries, changed)``
    keyed by ``document_id``. Idempotent.
    """
    if not raw:
        return {}, False
    if all("content_hash" in e for e in raw.values() if isinstance(e, dict)):
        # Already lineage schema — but ensure it is keyed by document_id.
        rekeyed: dict[str, Any] = {}
        changed = False
        for key, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            doc_id = entry.get("document_id") or key
            rekeyed[doc_id] = entry
            if doc_id != key:
                changed = True
        return rekeyed, changed

    upgraded: dict[str, Any] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        if "content_hash" in entry:
            doc_id = entry.get("document_id") or key
            upgraded[doc_id] = entry
            continue
        doc_id = entry.get("document_id") or key
        upgraded[doc_id] = {
            "document_id": doc_id,
            "content_hash": key,  # legacy key WAS the sha256
            "filename": entry.get("file_name", ""),
            "file_path": entry.get("file_path", ""),
            "file_size_bytes": entry.get("file_size_bytes", 0),
            "total_chunks": entry.get("total_chunks", 0),
            "created_at": entry.get("ingested_at", _utcnow_iso()),
            "supersedes": None,
            "superseded_by": None,
            "active": True,
            "lineage_root": doc_id,
        }
    logger.info("Registry: migrated %d legacy entries to lineage schema", len(upgraded))
    return upgraded, True


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class MetadataBackend(Protocol):
    """Durable persistence for document metadata — swappable."""

    def load_all(self) -> dict[str, dict[str, Any]]:
        """Return the full registry snapshot, keyed by ``document_id``."""
        ...

    def write_batch(
        self, upserts: list[dict[str, Any]], deletes: list[str]
    ) -> None:
        """Apply upserts and deletes in one atomic operation."""
        ...


# ---------------------------------------------------------------------------
# JSON file backend (local development / fallback)
# ---------------------------------------------------------------------------

class JsonMetadataBackend:
    """File-backed metadata store with cross-platform locking and atomic writes.

    Authoritative only when Qdrant is not configured. Otherwise the file serves
    purely as a one-time import seed for :class:`QdrantMetadataBackend`.
    """

    def __init__(self, path: Path = _REGISTRY_FILE) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> dict[str, dict[str, Any]]:
        raw = self._read()
        migrated, changed = migrate_registry(raw)
        if changed:
            self._write(migrated)
        return migrated

    def write_batch(
        self, upserts: list[dict[str, Any]], deletes: list[str]
    ) -> None:
        data = self.load_all()
        for entry in upserts:
            data[entry["document_id"]] = entry
        for doc_id in deletes:
            data.pop(doc_id, None)
        self._write(data)

    # -- internal --------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            with open(self._path, encoding="utf-8") as f:
                with locked(f, LockMode.SHARED):
                    return json.load(f)
        except Exception as e:
            logger.error("Metadata: failed to load %s: %s", self._path, e)
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        try:
            content = json.dumps(data, indent=2, ensure_ascii=False)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent), suffix=".tmp", prefix="registry"
            )
            try:
                with open(fd, "w", encoding="utf-8") as f:
                    with locked(f, LockMode.EXCLUSIVE):
                        f.write(content)
                        f.flush()
                os.replace(tmp_path, self._path)  # atomic on POSIX and Windows
            except Exception:
                Path(tmp_path).unlink(missing_ok=True)
                raise
        except Exception as e:
            logger.error("Metadata: failed to save: %s", e)


# ---------------------------------------------------------------------------
# Qdrant backend (durable source of truth)
# ---------------------------------------------------------------------------

def _point_id(document_id: str) -> int:
    """Stable positive 63-bit point id from a document_id string.

    document_ids are not necessarily UUIDs (they are derived from the upload
    path), so they are hashed to an integer id Qdrant always accepts — the same
    scheme the chunk store uses.
    """
    return int(hashlib.sha256(document_id.encode()).hexdigest(), 16) % (2**63)


# Collections seeded from a legacy JSON file this process — import runs once.
_seeded: set[str] = set()


class QdrantMetadataBackend:
    """Document metadata persisted in a dedicated Qdrant collection.

    Each document version is one point (id = hash of document_id, a placeholder
    1-d vector, the lineage entry as payload). Reads scroll the whole collection
    — document counts are small (one point per version), far smaller than the
    chunk collection. This is the authoritative, host-independent store.
    """

    def __init__(
        self,
        collection_name: str = _DOCUMENTS_COLLECTION,
        seed_path: Path | None = _REGISTRY_FILE,
    ) -> None:
        self._collection = collection_name
        self._seed_path = seed_path
        self._client: Any = None

    # -- client / collection lifecycle -----------------------------------

    def _get_client(self) -> Any:
        if self._client is None:
            from qdrant_client import QdrantClient

            self._client = QdrantClient(
                url=settings.qdrant_url, api_key=settings.qdrant_api_key
            )
            self._ensure_collection()
            self._seed_if_empty()
        return self._client

    def _ensure_collection(self) -> None:
        from qdrant_client.models import Distance, VectorParams

        existing = {c.name for c in self._client.get_collections().collections}
        if self._collection in existing:
            return
        # Placeholder 1-d vector — this collection is queried by payload scroll,
        # never by vector similarity.
        self._client.create_collection(
            collection_name=self._collection,
            vectors_config=VectorParams(size=1, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant metadata collection '%s'", self._collection)

    def _seed_if_empty(self) -> None:
        """One-time import of a legacy JSON registry into an empty collection."""
        if self._collection in _seeded:
            return
        _seeded.add(self._collection)

        if self._seed_path is None or not self._seed_path.exists():
            return
        try:
            count = self._client.count(self._collection, exact=True).count
        except Exception as e:
            logger.warning("Metadata: could not count '%s': %s", self._collection, e)
            return
        if count > 0:
            return

        try:
            with open(self._seed_path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            logger.warning("Metadata: could not read seed %s: %s", self._seed_path, e)
            return

        migrated, _ = migrate_registry(raw)
        if not migrated:
            return
        logger.info(
            "Metadata: seeding Qdrant collection '%s' with %d entries from %s",
            self._collection,
            len(migrated),
            self._seed_path,
        )
        self._upsert_points(list(migrated.values()))

    # -- MetadataBackend -------------------------------------------------

    def load_all(self) -> dict[str, dict[str, Any]]:
        client = self._get_client()
        result: dict[str, dict[str, Any]] = {}
        offset: Any = None
        while True:
            points, offset = client.scroll(
                collection_name=self._collection,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for p in points:
                payload = p.payload or {}
                doc_id = payload.get("document_id")
                if doc_id:
                    result[doc_id] = payload
            if offset is None:
                break
        return result

    def write_batch(
        self, upserts: list[dict[str, Any]], deletes: list[str]
    ) -> None:
        client = self._get_client()
        if upserts:
            self._upsert_points(upserts)
        if deletes:
            from qdrant_client.models import PointIdsList

            client.delete(
                collection_name=self._collection,
                points_selector=PointIdsList(points=[_point_id(d) for d in deletes]),
            )

    # -- internal --------------------------------------------------------

    def _upsert_points(self, entries: list[dict[str, Any]]) -> None:
        from qdrant_client.models import PointStruct

        points = [
            PointStruct(
                id=_point_id(entry["document_id"]),
                vector=[0.0],
                payload=entry,
            )
            for entry in entries
            if entry.get("document_id")
        ]
        if points:
            self._client.upsert(collection_name=self._collection, points=points)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_metadata_backend(seed_path: Path = _REGISTRY_FILE) -> MetadataBackend:
    """Return the durable metadata backend for the current configuration.

    Qdrant when configured (the durable, host-independent source of truth);
    otherwise a local JSON file for development.
    """
    if settings.qdrant_url and settings.qdrant_api_key:
        return QdrantMetadataBackend(seed_path=seed_path)
    logger.info("Metadata: Qdrant not configured — using local JSON store (dev mode)")
    return JsonMetadataBackend(seed_path)
