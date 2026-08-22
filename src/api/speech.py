"""Speech API — voice-to-text transcription for the chat input box.

Deliberately minimal: receive audio, transcribe it, return text. It does not
touch the query pipeline — the frontend drops the returned text into the
existing input box, where it's just typed-looking text until the user hits
Send and it goes through ``/api/query`` / the chat endpoints like any other
question.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from src.core.config import settings
from src.core.speech import TranscriptionError, get_transcriber

logger = logging.getLogger(__name__)
router = APIRouter()

# Extensions we accept from the browser's MediaRecorder / a file picker.
# Actual format validation happens inside faster-whisper/ffmpeg; this is just
# a cheap first filter on obviously-wrong uploads.
_ALLOWED_SUFFIXES = {".webm", ".ogg", ".wav", ".mp3", ".m4a", ".mp4", ".flac"}


@router.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...)) -> dict:
    """Transcribe an uploaded audio clip to text using local Whisper.

    Returns ``{"text": "..."}`` on success. The audio is written to a
    temporary file for the duration of the request and always deleted
    afterwards — nothing is persisted.
    """
    suffix = Path(file.filename or "").suffix.lower()
    if suffix and suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Unsupported audio format: {suffix}")

    max_bytes = settings.max_audio_upload_mb * 1024 * 1024
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=suffix or ".webm", prefix="voice_"
        ) as tmp:
            tmp_path = tmp.name
            size = 0
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Audio clip exceeds {settings.max_audio_upload_mb}MB limit",
                    )
                tmp.write(chunk)

        if size == 0:
            raise HTTPException(status_code=400, detail="Empty audio upload")

        transcriber = get_transcriber()
        text = await transcriber.transcribe_file_async(tmp_path)
        return {"status": "success", "text": text}

    except HTTPException:
        raise
    except TranscriptionError as exc:
        logger.warning("Transcription failed: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        logger.exception("Unexpected error during transcription")
        raise HTTPException(status_code=500, detail="Transcription failed. Please try again.")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
