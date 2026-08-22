"""Tests for src.core.speech and src.api.speech — local voice-input transcription.

faster-whisper is an optional/heavy dependency, so these tests never load a
real model: the "happy path" mocks WhisperModel, and the "not installed" path
relies on faster-whisper genuinely being absent in the test environment (it's
not in requirements.txt's always-installed set — see pyproject.toml, where
it's a real dependency but downloading the multi-hundred-MB model weights is
never exercised in CI).
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from src.core.speech import SpeechTranscriber, TranscriptionError, get_transcriber


def _fake_segment(text: str):
    seg = MagicMock()
    seg.text = text
    return seg


def test_transcribe_file_raises_when_dependency_missing(monkeypatch):
    """If faster-whisper truly isn't importable, loading should fail cleanly
    with TranscriptionError, not a raw ImportError leaking to the API layer."""
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # force ImportError
    transcriber = SpeechTranscriber(model_size="tiny", device="cpu", compute_type="int8")

    with pytest.raises(TranscriptionError, match="faster-whisper is not installed"):
        transcriber.transcribe_file("does-not-matter.webm")


def test_transcribe_file_success_joins_segments(monkeypatch):
    """A successful transcribe() call should join segment texts into one string."""
    fake_model_instance = MagicMock()
    fake_model_instance.transcribe.return_value = (
        [_fake_segment("hello "), _fake_segment("world")],
        MagicMock(),
    )
    fake_whisper_model_cls = MagicMock(return_value=fake_model_instance)

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = fake_whisper_model_cls
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    transcriber = SpeechTranscriber(model_size="tiny", device="cpu", compute_type="int8")
    text = transcriber.transcribe_file("clip.webm")

    assert text == "hello world"
    fake_whisper_model_cls.assert_called_once_with("tiny", device="cpu", compute_type="int8")
    fake_model_instance.transcribe.assert_called_once_with("clip.webm", beam_size=5)


def test_transcribe_file_raises_on_empty_speech(monkeypatch):
    """No segments (or all-blank segments) should surface as TranscriptionError,
    not silently return an empty string that gets sent through the RAG pipeline."""
    fake_model_instance = MagicMock()
    fake_model_instance.transcribe.return_value = ([], MagicMock())
    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = MagicMock(return_value=fake_model_instance)
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    transcriber = SpeechTranscriber(model_size="tiny", device="cpu", compute_type="int8")

    with pytest.raises(TranscriptionError, match="No speech detected"):
        transcriber.transcribe_file("silence.webm")


def test_model_loaded_at_most_once(monkeypatch):
    """Repeated transcribe calls must reuse the already-loaded model instance —
    constructing WhisperModel is expensive and can trigger a multi-hundred-MB
    download, so it must not happen per request."""
    fake_model_instance = MagicMock()
    fake_model_instance.transcribe.return_value = ([_fake_segment("hi")], MagicMock())
    fake_whisper_model_cls = MagicMock(return_value=fake_model_instance)
    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = fake_whisper_model_cls
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    transcriber = SpeechTranscriber(model_size="tiny", device="cpu", compute_type="int8")
    transcriber.transcribe_file("a.webm")
    transcriber.transcribe_file("b.webm")

    fake_whisper_model_cls.assert_called_once()


def test_get_transcriber_returns_singleton():
    assert get_transcriber() is get_transcriber()


@pytest.mark.asyncio
async def test_transcribe_file_async_delegates_to_sync(monkeypatch):
    fake_model_instance = MagicMock()
    fake_model_instance.transcribe.return_value = ([_fake_segment("async hi")], MagicMock())
    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = MagicMock(return_value=fake_model_instance)
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    transcriber = SpeechTranscriber(model_size="tiny", device="cpu", compute_type="int8")
    text = await transcriber.transcribe_file_async("clip.webm")

    assert text == "async hi"
