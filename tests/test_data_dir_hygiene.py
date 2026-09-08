"""Guard against stray files accumulating in data/.

``data/`` is git-ignored local runtime state (see .gitignore), so nothing in
it is ever reviewed in a diff. That makes it easy for one-off debug or mock
files to pile up silently — e.g. an experiment writes ``telemetry_events.jsonl``
or ``learned_patterns.jsonl`` once, the code that wrote it is deleted later,
and the orphan file just sits there forever with nothing to clean it up.

This test enumerates every top-level entry actually produced by the current
codebase (state.py, pipeline_metrics.py, metadata_store.py, config.py) and
fails if ``data/`` contains anything outside that allow-list. If a new
feature legitimately needs a new file under ``data/``, add it to
``EXPECTED_TOP_LEVEL_ENTRIES`` in the same change.

The test is a no-op (skipped) when ``data/`` doesn't exist yet, e.g. on a
fresh checkout that hasn't run the app.
"""

from __future__ import annotations

import pytest

from src.core.config import DATA_DIR

# Every file/directory the current code is actually known to create under
# data/. Keep this in sync with src/core/state.py, src/core/pipeline_metrics.py,
# src/core/metadata_store.py, and src/core/config.py's upload/processed/inbox
# dirs. See docs/ARCHITECTURE.md "Database Replacement" section for context.
EXPECTED_TOP_LEVEL_ENTRIES = {
    # UIStateManager (src/core/state.py)
    "chats.json",
    "messages.json",
    "documents.json",
    "settings.json",
    # Pipeline health metrics (src/core/pipeline_metrics.py)
    "pipeline_metrics.jsonl",
    # Dedup registry / legacy metadata backend (src/core/metadata_store.py)
    "ingested_files.json",
    # SQL retrieval backing store (scripts/setup_db.py, scripts/load_mysql_dump.py)
    "live_data.db",
    # Runtime directories (src/core/config.py)
    "uploads",
    "processed",
    "inbox",
    # Common local-dev noise that isn't part of the app but is harmless
    ".gitkeep",
}


def test_data_dir_has_no_unexpected_files():
    """Fail if data/ contains a file no current code path writes.

    This catches orphaned mock/telemetry/debug files (e.g. a stray
    ``mock_telemetry_events.jsonl`` or ``audit_mock_events.jsonl`` left over
    from a removed experiment) before they're mistaken for a real feature.
    """
    if not DATA_DIR.exists():
        pytest.skip("data/ does not exist yet (fresh checkout)")

    actual_entries = {p.name for p in DATA_DIR.iterdir()}
    unexpected = actual_entries - EXPECTED_TOP_LEVEL_ENTRIES

    assert not unexpected, (
        f"Unexpected file(s) in data/: {sorted(unexpected)}. "
        "Nothing in the current codebase writes these — they're likely "
        "leftovers from a removed feature or a local experiment. Either "
        "delete them, or if they're from a real feature, add them to "
        "EXPECTED_TOP_LEVEL_ENTRIES in this test."
    )
