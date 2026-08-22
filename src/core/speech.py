"""Speech-to-text — local audio transcription via faster-whisper.

Kept deliberately isolated from the RAG/query stack: its only job is
``audio bytes -> text``. It knows nothing about Qdrant, the provider router,
or the query pipeline, and the query pipeline never imports it directly —
transcribed text re-enters the system as an ordinary question string via the
existing ``/api/query`` (or chat) endpoint, exactly like typed input.

The model (``large-v3-turbo`` by default) is downloaded once from the public
Hugging Face repo on first use and cached locally by faster-whisper/huggingface
under ``~/.cache/huggingface``; no API key is required.

Loading is lazy and happens at most once per process, since constructing a
``WhisperModel`` reads the (multi-hundred-MB) model into memory and — on the
first-ever run — triggers the download. A request-time import keeps
``faster-whisper`` an optional dependency for anyone who never uses this
feature.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from src.core.config import settings

logger = logging.getLogger(__name__)

__all__ = ["SpeechTranscriber", "get_transcriber", "TranscriptionError"]


class TranscriptionError(RuntimeError):
    """Raised when audio can't be transcribed (bad file, model load failure, etc.)."""


class SpeechTranscriber:
    """Thin wrapper around a lazily-loaded ``faster_whisper.WhisperModel``.

    Not thread-safe to construct concurrently — use :func:`get_transcriber`,
    which guards initialization with a lock, rather than instantiating this
    directly.
    """

    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
    ) -> None:
        self.model_size = model_size or settings.whisper_model_size
        self.device = device or settings.whisper_device
        self.compute_type = compute_type or settings.whisper_compute_type
        self._model = None
        self._load_lock = threading.Lock()

    def _ensure_loaded(self):
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise TranscriptionError(
                    "faster-whisper is not installed. Run "
                    "`pip install faster-whisper` (see requirements)."
                ) from exc

            logger.info(
                "Loading Whisper model '%s' (device=%s, compute_type=%s)...",
                self.model_size,
                self.device,
                self.compute_type,
            )
            try:
                self._model = WhisperModel(
                    self.model_size,
                    device=self.device,
                    compute_type=self.compute_type,
                )
            except Exception as exc:
                raise TranscriptionError(f"Failed to load Whisper model: {exc}") from exc
            logger.info("Whisper model '%s' loaded", self.model_size)
            return self._model

    def transcribe_file(self, audio_path: str) -> str:
        """Transcribe an audio file on disk and return the recognized text.

        Runs synchronously (faster-whisper is CPU/GPU-bound, not I/O-bound) —
        callers on the async request path should use :meth:`transcribe_file_async`.
        """
        model = self._ensure_loaded()
        try:
            segments, _info = model.transcribe(audio_path, beam_size=5)
            text = " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as exc:
            raise TranscriptionError(f"Transcription failed: {exc}") from exc

        if not text:
            raise TranscriptionError("No speech detected in the provided audio")
        return text

    async def transcribe_file_async(self, audio_path: str) -> str:
        """Async wrapper — offloads the blocking transcription call to a thread
        so it doesn't block the FastAPI event loop."""
        return await asyncio.to_thread(self.transcribe_file, audio_path)


# Process-wide singleton so the (large) model is loaded at most once, mirroring
# the module-level `settings` singleton pattern used elsewhere in src/core.
_transcriber: SpeechTranscriber | None = None
_transcriber_lock = threading.Lock()


def get_transcriber() -> SpeechTranscriber:
    """Return the shared :class:`SpeechTranscriber` instance, creating it on first call."""
    global _transcriber
    if _transcriber is None:
        with _transcriber_lock:
            if _transcriber is None:
                _transcriber = SpeechTranscriber()
    return _transcriber
