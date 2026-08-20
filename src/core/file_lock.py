"""Cross-platform advisory file locking.

Wraps ``portalocker`` so the JSON state stores (chat state, ingestion registry)
behave identically on Linux, macOS, and Windows. This replaces the previous
``fcntl``-based locking, which is Unix-only and prevented the project from
running on Windows.

Both callers share this single abstraction so the locking strategy lives in one
place and can be swapped without touching the stores.
"""

from __future__ import annotations

import contextlib
from enum import Enum
from typing import IO, Iterator

import portalocker


class LockMode(Enum):
    """Advisory lock kind."""

    SHARED = "shared"        # read lock — many concurrent readers allowed
    EXCLUSIVE = "exclusive"  # write lock — single holder


@contextlib.contextmanager
def locked(file: IO, mode: LockMode) -> Iterator[IO]:
    """Hold an advisory lock on ``file`` for the duration of the ``with`` block.

    The lock is always released, even if the body raises. Uses ``portalocker``
    so the same call works across platforms (``flock`` on POSIX, ``LockFileEx``
    on Windows).
    """
    flags = portalocker.LOCK_SH if mode is LockMode.SHARED else portalocker.LOCK_EX
    portalocker.lock(file, flags)
    try:
        yield file
    finally:
        portalocker.unlock(file)
