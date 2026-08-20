"""Ingestion Registry — content-addressed identity with document lineage.

Every ingested document has a stable identity and a place in a *lineage* — a
chain of versions that share the same logical document but differ in content
over time. This gives three guarantees at once:

  1. **Deduplication.** Identity is the SHA-256 of the file's *content*, never
     its name. Re-uploading identical bytes (any filename) is a no-op.

  2. **No data loss.** Two different files that happen to share a name (two
     people's ``resume.pdf``) are two independent documents; both are kept.

  3. **Safe replacement / versioning.** "Replace document X with this new file"
     creates a *new version* and supersedes the old one. The cutover is ordered
     so the old version stays live until the new one is fully indexed:

         index new content → create new (active) version → supersede old version

     A failure before the supersede leaves the old version fully active — there
     is never a window with zero active versions.

**Persistence is durable and platform-independent.** The registry does not own
its storage; it delegates to a :class:`~src.core.metadata_store.MetadataBackend`
whose default (when Qdrant is configured) keeps metadata in Qdrant alongside the
embeddings — a single source of truth that survives restarts, redeploys, and
migration to any host. See ``metadata_store`` for details. The registry itself
holds only the version state machine.

Entry schema (keyed by ``document_id``)::

    {
      "document_id":     "<id>",         # stable identity of THIS version
      "content_hash":    "<sha256>",     # content fingerprint / dedup key
      "filename":        "resume.pdf",   # display label only
      "file_path":       "/uploads/ab/resume.pdf",
      "file_size_bytes": 12345,
      "total_chunks":    42,
      "created_at":      "2026-07-13T…",
      "supersedes":      "<id|null>",    # the version this one replaced
      "superseded_by":   "<id|null>",    # the version that replaced this one
      "active":          true,           # is this the current live version?
      "lineage_root":    "<id>"          # first version in this lineage
    }
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from src.core.metadata_store import MetadataBackend, create_metadata_backend

logger = logging.getLogger(__name__)

_BUFFER_SIZE = 65536  # 64 KB chunks for streaming hash


class RegistryStatus(str, Enum):
    """Result of checking a file against the registry."""
    NEW_FILE = "new_file"
    ALREADY_INGESTED = "already_ingested"
    CONTENT_CHANGED = "content_changed"  # retained for API compatibility; unused


@dataclass
class RegistryCheckResult:
    """Result of a registry check with full context."""
    status: RegistryStatus
    sha256: str
    old_document_id: str | None = None
    old_entry: dict[str, Any] | None = None

    @property
    def content_hash(self) -> str:
        """Alias for ``sha256`` under the lineage vocabulary."""
        return self.sha256


def _new_document_id() -> str:
    """Mint a fresh, globally-unique document identity."""
    return str(uuid.uuid4())


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class IngestionRegistry:
    """Document registry with lineage/versioning over a durable backend.

    Keyed by ``document_id``. Every mutating operation is expressed as an atomic
    batch write to the backend (``write_batch(upserts, deletes)``), so a version
    cutover is applied consistently even across process restarts.
    """

    def __init__(
        self,
        backend: MetadataBackend | None = None,
        registry_path: Path | None = None,
    ) -> None:
        if backend is not None:
            self._backend = backend
        elif registry_path is not None:
            # Explicit path → force the JSON backend (used by tests/tools).
            from src.core.metadata_store import JsonMetadataBackend

            self._backend = JsonMetadataBackend(registry_path)
        else:
            self._backend = create_metadata_backend()

    # ------------------------------------------------------------------
    # Deduplication check
    # ------------------------------------------------------------------

    def check(self, file_path: str | Path) -> RegistryCheckResult:
        """Compute the content SHA-256 and check for an active duplicate.

        An exact content match against an **active** version is a duplicate to
        skip. Anything else — new content, or content whose only match is a
        superseded version — is a new, distinct document. Filename never decides
        identity.
        """
        path = Path(file_path)
        file_hash = self._sha256(path)
        registry = self._backend.load_all()

        active_dupe = self._find_active_by_hash(registry, file_hash)
        if active_dupe is not None:
            entry = registry[active_dupe]
            logger.info(
                "Registry: '%s' already ingested (hash=%s..., doc_id=%s)",
                path.name,
                file_hash[:12],
                active_dupe,
            )
            return RegistryCheckResult(
                status=RegistryStatus.ALREADY_INGESTED,
                sha256=file_hash,
                old_document_id=active_dupe,
                old_entry=entry,
            )

        if self._find_active_by_filename(registry, path.name) is not None:
            logger.info(
                "Registry: '%s' is new content under an existing name — "
                "adding as a separate document",
                path.name,
            )
        else:
            logger.info("Registry: '%s' is new (hash=%s...)", path.name, file_hash[:12])
        return RegistryCheckResult(status=RegistryStatus.NEW_FILE, sha256=file_hash)

    # ------------------------------------------------------------------
    # Version lifecycle
    # ------------------------------------------------------------------

    def create_version(
        self,
        file_path: str | Path,
        content_hash: str,
        total_chunks: int,
        document_id: str | None = None,
        supersedes: str | None = None,
    ) -> dict[str, Any]:
        """Record a freshly-indexed document as a new **active** version.

        Step 1 of the atomic cutover; always called *after* the content is fully
        embedded and stored. It does not touch the version being replaced — call
        :meth:`supersede` immediately after to flip the old version inactive.

        If ``supersedes`` names an existing version, the new version inherits its
        ``lineage_root`` (joins the same lineage); otherwise it starts a new
        lineage rooted at itself.
        """
        path = Path(file_path)

        doc_id = document_id or _new_document_id()
        lineage_root = doc_id
        if supersedes:
            # Only touch the backend when we actually need the prior version's
            # lineage — a brand-new document needs no read at all.
            prior = self._backend.load_all().get(supersedes)
            if prior is not None:
                lineage_root = prior.get("lineage_root", supersedes)
            else:
                logger.warning(
                    "Registry: create_version supersedes unknown doc_id=%s "
                    "— starting a fresh lineage",
                    supersedes,
                )
                supersedes = None

        entry = {
            "document_id": doc_id,
            "content_hash": content_hash,
            "filename": path.name,
            "file_path": str(path),
            "file_size_bytes": path.stat().st_size if path.exists() else 0,
            "total_chunks": total_chunks,
            "created_at": _utcnow_iso(),
            "supersedes": supersedes,
            "superseded_by": None,
            "active": True,
            "lineage_root": lineage_root,
        }
        self._backend.write_batch([entry], [])
        logger.info(
            "Registry: created version doc_id=%s (chunks=%d, supersedes=%s, root=%s)",
            doc_id,
            total_chunks,
            supersedes,
            lineage_root,
        )
        return entry

    def supersede(self, old_document_id: str, new_document_id: str) -> bool:
        """Flip the old version inactive — step 2 (final) of the cutover.

        Atomically marks ``old_document_id`` inactive and links the two versions
        (old.superseded_by = new, new.supersedes = old, new adopts the old
        lineage root). Written as a single batch so no reader observes a
        half-applied cutover. Returns True if the old version existed.
        """
        registry = self._backend.load_all()
        old = registry.get(old_document_id)
        new = registry.get(new_document_id)
        if old is None:
            logger.warning("Registry: supersede — unknown old doc_id=%s", old_document_id)
            return False
        if new is None:
            logger.warning("Registry: supersede — unknown new doc_id=%s", new_document_id)
            return False

        old["active"] = False
        old["superseded_by"] = new_document_id
        new["supersedes"] = old_document_id
        new["lineage_root"] = old.get("lineage_root", old_document_id)
        self._backend.write_batch([old, new], [])
        logger.info(
            "Registry: superseded doc_id=%s → %s (lineage=%s)",
            old_document_id,
            new_document_id,
            new["lineage_root"],
        )
        return True

    def register(
        self,
        file_path: str | Path,
        document_id: str,
        total_chunks: int,
        sha256: str | None = None,
    ) -> dict[str, Any]:
        """Register a newly-ingested document (no replacement).

        Convenience wrapper over :meth:`create_version` for the "brand-new
        document" path, preserving the historical signature used by the pipeline.
        """
        path = Path(file_path)
        content_hash = sha256 or self._sha256(path)
        return self.create_version(
            file_path=path,
            content_hash=content_hash,
            total_chunks=total_chunks,
            document_id=document_id,
            supersedes=None,
        )

    def unregister(self, document_id: str) -> bool:
        """Remove a document version by its document_id.

        If the removed version was some older version's replacement, that older
        version's back-link is cleared and it is reactivated so the lineage isn't
        left with nothing active. Returns True if found and removed.
        """
        registry = self._backend.load_all()
        if document_id not in registry:
            return False

        changed: dict[str, dict[str, Any]] = {}
        for doc_id, entry in registry.items():
            if doc_id == document_id:
                continue
            if entry.get("superseded_by") == document_id:
                entry["superseded_by"] = None
                entry["active"] = True  # its replacement is gone — reactivate
                changed[doc_id] = entry
            if entry.get("supersedes") == document_id:
                entry["supersedes"] = None
                changed[doc_id] = entry

        self._backend.write_batch(list(changed.values()), [document_id])
        logger.info("Registry: unregistered doc_id=%s", document_id)
        return True

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_all(self) -> dict[str, Any]:
        """Return the entire registry (document_id → entry dict)."""
        return self._backend.load_all()

    def get_by_document_id(self, document_id: str) -> dict[str, Any] | None:
        """Look up a single version by its document_id."""
        return self._backend.load_all().get(document_id)

    def get_active(self) -> list[dict[str, Any]]:
        """Return all currently-active document versions."""
        return [e for e in self._backend.load_all().values() if e.get("active", True)]

    def active_entry_for_hash(self, content_hash: str) -> dict[str, Any] | None:
        """Return the active version whose content matches ``content_hash``, if any.

        Unlike :meth:`check`, this does not hash a file — it takes an already
        computed content hash. Used to re-confirm dedup under a per-hash lock so
        two concurrent ingests of identical bytes don't both embed and store the
        same document.
        """
        registry = self._backend.load_all()
        doc_id = self._find_active_by_hash(registry, content_hash)
        return registry[doc_id] if doc_id is not None else None

    def get_active_ids(self) -> set[str]:
        """Return the set of active document_ids."""
        return {
            e["document_id"]
            for e in self._backend.load_all().values()
            if e.get("active", True) and e.get("document_id")
        }

    def get_superseded_ids(self) -> set[str]:
        """Return the set of inactive (superseded) document_ids."""
        return {
            e["document_id"]
            for e in self._backend.load_all().values()
            if not e.get("active", True) and e.get("document_id")
        }

    def get_versions(self, lineage_root: str) -> list[dict[str, Any]]:
        """Return every version in a lineage, oldest first."""
        versions = [
            e
            for e in self._backend.load_all().values()
            if e.get("lineage_root") == lineage_root
        ]
        return sorted(versions, key=lambda e: e.get("created_at", ""))

    def find_active_by_filename(self, filename: str) -> dict[str, Any] | None:
        """Return the active version whose display name matches, if any.

        Ambiguous when two active documents share a name; returns the most
        recently created match in that case.
        """
        matches = [
            e
            for e in self._backend.load_all().values()
            if e.get("active", True) and e.get("filename") == filename
        ]
        if not matches:
            return None
        return max(matches, key=lambda e: e.get("created_at", ""))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sha256(path: Path) -> str:
        """Compute SHA-256 hash of file contents in streaming fashion."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                block = f.read(_BUFFER_SIZE)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()

    @staticmethod
    def _find_active_by_hash(registry: dict, content_hash: str) -> str | None:
        for doc_id, entry in registry.items():
            if entry.get("content_hash") == content_hash and entry.get("active", True):
                return doc_id
        return None

    @staticmethod
    def _find_active_by_filename(registry: dict, filename: str) -> str | None:
        for doc_id, entry in registry.items():
            if entry.get("filename") == filename and entry.get("active", True):
                return doc_id
        return None
