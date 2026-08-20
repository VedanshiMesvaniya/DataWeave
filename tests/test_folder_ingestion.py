"""Tests for the drop-folder auto-ingestion service.

The service is injected with a fake ingester, so these exercise the scan /
dedup-reporting / summary logic without any document-processing dependencies.
"""

from dataclasses import dataclass

import pytest

from src.pipeline.folder_ingestion import (
    FolderIngestionResult,
    _discover_files,
    scan_and_ingest,
)


@dataclass
class _FakeResult:
    skipped: bool = False
    total_chunks: int = 1
    document_id: str = "doc"


class _FakePipeline:
    """Records ingests; treats any file whose name contains 'dupe' as already
    ingested, and any file whose name contains 'boom' as a failure."""

    def __init__(self):
        self.ingested: list[str] = []

    async def ingest(self, file_path):
        name = str(file_path)
        self.ingested.append(name)
        if "boom" in name:
            raise RuntimeError("kaboom")
        return _FakeResult(skipped="dupe" in name)


def _touch(path, content: bytes = b"data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# _discover_files
# ---------------------------------------------------------------------------

def test_discover_skips_dotfiles_dotdirs_and_empty(tmp_path):
    _touch(tmp_path / "a.pdf")
    _touch(tmp_path / "sub" / "b.txt")
    _touch(tmp_path / ".hidden.pdf")           # dotfile → skipped
    _touch(tmp_path / ".git" / "config")       # inside dot-dir → skipped
    _touch(tmp_path / "empty.pdf", b"")        # zero-byte → skipped

    found = {p.name for p in _discover_files(tmp_path)}
    assert found == {"a.pdf", "b.txt"}


def test_discover_missing_folder_is_empty(tmp_path):
    assert _discover_files(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# scan_and_ingest
# ---------------------------------------------------------------------------

async def test_empty_folder_reports_nothing(tmp_path):
    pipeline = _FakePipeline()
    result = await scan_and_ingest(pipeline=pipeline, folder=tmp_path)
    assert result.scanned == 0
    assert result.ingested == 0
    assert "empty" in result.message.lower()
    assert pipeline.ingested == []  # never touched the pipeline


async def test_new_files_are_ingested(tmp_path):
    _touch(tmp_path / "one.pdf")
    _touch(tmp_path / "two.pdf")
    result = await scan_and_ingest(pipeline=_FakePipeline(), folder=tmp_path)
    assert result.scanned == 2
    assert result.ingested == 2
    assert result.skipped == 0
    assert set(result.ingested_files) == {"one.pdf", "two.pdf"}
    assert result.message == "Ingested 2 new file(s)."


async def test_already_ingested_files_are_skipped(tmp_path):
    _touch(tmp_path / "fresh.pdf")
    _touch(tmp_path / "dupe.pdf")   # fake pipeline reports skipped
    result = await scan_and_ingest(pipeline=_FakePipeline(), folder=tmp_path)
    assert result.ingested == 1
    assert result.skipped == 1
    assert result.ingested_files == ["fresh.pdf"]


async def test_all_skipped_reports_nothing_new(tmp_path):
    _touch(tmp_path / "dupe1.pdf")
    _touch(tmp_path / "dupe2.pdf")
    result = await scan_and_ingest(pipeline=_FakePipeline(), folder=tmp_path)
    assert result.ingested == 0
    assert result.skipped == 2
    assert "no new files" in result.message.lower()


async def test_failures_are_isolated(tmp_path):
    _touch(tmp_path / "good.pdf")
    _touch(tmp_path / "boom.pdf")   # fake pipeline raises
    result = await scan_and_ingest(pipeline=_FakePipeline(), folder=tmp_path)
    assert result.ingested == 1
    assert result.failed == 1
    assert result.failed_files == [{"file": "boom.pdf", "error": "kaboom"}]
    assert "failed" in result.message.lower()


def test_result_to_dict_shape():
    r = FolderIngestionResult(folder="/x", scanned=1, ingested=1, ingested_files=["a"])
    d = r.to_dict()
    assert d["folder"] == "/x"
    assert d["ingested"] == 1
    assert d["message"] == "Ingested 1 new file(s)."
    assert set(d) == {
        "folder", "scanned", "ingested", "skipped", "failed",
        "ingested_files", "failed_files", "message",
    }
