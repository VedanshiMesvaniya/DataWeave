"""Path-safety helpers for handling untrusted, caller-supplied path fragments.

Kept dependency-free (stdlib only) so it can be imported by the lightweight
API layer and unit-tested without pulling in the heavy ingestion stack.

Two distinct needs:
  * Uploads collapse an untrusted filename to a bare basename inside a single
    directory — nested paths are never desired, so we flatten them.
  * Static serving allows nested sub-paths (assets/index-abc.js) but must never
    escape the served root.

Both are vulnerable to the same classes of traversal:
  - ``"../../etc/passwd"``     → dot-dot escape
  - ``"/etc/passwd"``          → absolute path resets a ``Path`` join entirely
  - ``"a/../../b"``            → escape after a legitimate-looking prefix
"""

from __future__ import annotations

import uuid
from pathlib import Path

__all__ = ["safe_basename", "contained_path", "unique_upload_dest"]


def safe_basename(filename: str) -> str | None:
    """Reduce an untrusted filename to a safe basename.

    Strips every directory component (so ``"../x"``, ``"/etc/x"`` and
    ``"a/b/x"`` all collapse to ``"x"``) and rejects values that are empty or
    resolve to a traversal token.

    Returns the safe basename, or ``None`` if the input can't be made safe and
    the caller should reject the request.
    """
    if not filename:
        return None
    # Path(...).name keeps only the final component and drops any directory
    # parts, including a leading "/" that would otherwise reset a join.
    name = Path(filename).name
    if not name or name in {".", ".."}:
        return None
    # Defense in depth: a NUL byte can truncate the path in lower layers.
    if "\x00" in name:
        return None
    return name


def unique_upload_dest(base: Path, name: str) -> Path:
    """Return ``base/<random-token>/<name>``, creating the unique subdirectory.

    Each upload lands in its own per-upload subdirectory. A fresh random token
    per call guarantees that two uploads sharing a filename (two different
    ``resume.pdf``s), or a new upload colliding with an existing one, never
    resolve to the same path and clobber each other on disk. The file keeps its
    original ``name`` inside the subdirectory, so display names and citations
    stay clean.

    ``name`` must already be a validated basename (see :func:`safe_basename`).
    """
    subdir = base / uuid.uuid4().hex[:12]
    subdir.mkdir(parents=True, exist_ok=True)
    return subdir / name


def contained_path(root: Path, relpath: str) -> Path | None:
    """Resolve ``root / relpath`` and return it only if it stays within ``root``.

    Both sides are fully resolved (symlinks included) before the containment
    check, so ``".."`` segments and absolute paths can't escape ``root``.

    Returns the resolved path, or ``None`` if it would escape ``root`` (or can't
    be resolved), in which case the caller should not serve it.
    """
    if not relpath or "\x00" in relpath:
        return None
    root_resolved = root.resolve()
    try:
        candidate = (root_resolved / relpath).resolve()
    except (ValueError, OSError):
        return None
    if candidate == root_resolved or candidate.is_relative_to(root_resolved):
        return candidate
    return None
