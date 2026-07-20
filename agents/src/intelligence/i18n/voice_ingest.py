"""Voice Ingest Module.

Speech-to-Text pipeline using Whisper or deterministic fixtures.

AI_MODE gating (Phase 1 review):
  - disabled      → DisabledWhisperSTT (always returns unavailable)
  - deterministic → DeterministicWhisperSTT (fixed fixture, no network)
  - live          → WhisperSTT (uses WHISPER_URL, no OLLAMA_URL fallback)
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Literal

import httpx

from orchestrator.ai_mode import AIMode, resolve_ai_mode

logger = logging.getLogger(__name__)

AudioFormat = Literal["webm", "wav", "mp3", "ogg", "flac", "m4a"]


@dataclass
class TranscriptResult:
    """Result of speech-to-text transcription."""

    text: str
    language: str | None
    confidence: float
    duration_seconds: float
    processing_time_ms: float
    model_used: str
    error: str | None = None
    error_code: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None and bool(self.text)


# ---------------------------------------------------------------------------
# Disabled / Deterministic STT providers
# ---------------------------------------------------------------------------


class DisabledWhisperSTT:
    """STT provider that always returns unavailable (AI_MODE=disabled)."""

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        audio_format: AudioFormat = "webm",
        language_hint: str | None = None,
    ) -> TranscriptResult:
        return TranscriptResult(
            text="",
            language=None,
            confidence=0.0,
            duration_seconds=0.0,
            processing_time_ms=0.0,
            model_used="disabled",
            error="Voice transcription is not available (AI_MODE=disabled)",
            error_code="voice_ai_disabled",
        )


class DeterministicWhisperSTT:
    """STT provider that returns a fixed fixture (AI_MODE=deterministic).

    Never accesses a network. Used for CI and offline development.
    """

    # Fixed fixture — deterministic, repeatable output.
    FIXTURE_TRANSCRIPT = (
        "This is a deterministic transcription for CI and testing purposes. "
        "Voice input is not processed by a real speech-to-text model."
    )

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        audio_format: AudioFormat = "webm",
        language_hint: str | None = None,
    ) -> TranscriptResult:
        return TranscriptResult(
            text=self.FIXTURE_TRANSCRIPT,
            language=language_hint or "en",
            confidence=0.9,
            duration_seconds=len(audio_bytes) / 16000 / 2 if audio_bytes else 0.0,
            processing_time_ms=1.0,
            model_used="deterministic-stt-v1",
            error_code=None,  # success
        )


# ---------------------------------------------------------------------------
# Live Whisper STT
# ---------------------------------------------------------------------------


class WhisperSTT:
    """Speech-to-Text using OpenAI Whisper via a configurable endpoint.

    Uses WHISPER_URL env var; does NOT fall back to OLLAMA_URL.
    """

    def __init__(
        self,
        *,
        whisper_url: str | None = None,
        model: str = "whisper",
        timeout: float = 30.0,
    ):
        self._whisper_url = whisper_url or os.environ.get("WHISPER_URL", "")
        if not self._whisper_url:
            raise RuntimeError(
                "WHISPER_URL must be set when AI_MODE=live "
                "and voice transcription is used"
            )
        self._model = model
        self._timeout = timeout

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        audio_format: AudioFormat = "webm",
        language_hint: str | None = None,
    ) -> TranscriptResult:
        """Transcribe audio to text.

        Args:
            audio_bytes: Raw audio data
            audio_format: Audio format (webm, wav, mp3, etc.)
            language_hint: Optional language hint for better accuracy

        Returns:
            TranscriptResult with transcript and metadata
        """
        start_time = time.time()

        if not audio_bytes:
            return TranscriptResult(
                text="",
                language=None,
                confidence=0.0,
                duration_seconds=0.0,
                processing_time_ms=0.0,
                model_used=self._model,
                error="Empty audio input",
                error_code="voice_transcription_failed",
            )

        try:
            # Try Ollama-style API first
            result = await self._transcribe_ollama(
                audio_bytes, audio_format, language_hint
            )
            if result.success:
                return result

            # Fallback to OpenAI Whisper API style
            result = await self._transcribe_openai_style(
                audio_bytes, audio_format, language_hint
            )
            return result

        except Exception:
            logger.exception("Transcription failed")
            return TranscriptResult(
                text="",
                language=None,
                confidence=0.0,
                duration_seconds=0.0,
                processing_time_ms=(time.time() - start_time) * 1000,
                model_used=self._model,
                error="Transcription service error",
                error_code="voice_transcription_failed",
            )

    async def _transcribe_ollama(
        self,
        audio_bytes: bytes,
        audio_format: AudioFormat,
        language_hint: str | None,
    ) -> TranscriptResult:
        """Transcribe using Ollama multimodal endpoint."""
        start_time = time.time()

        try:
            # Encode audio as base64 for Ollama
            audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

            prompt = "Transcribe this audio exactly as spoken. Return only the transcription, no additional text."
            if language_hint:
                prompt = f"Transcribe this audio in {language_hint}. Return only the transcription, no additional text."

            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._whisper_url}/api/generate",
                    json={
                        "model": self._model,
                        "prompt": prompt,
                        "images": [audio_b64],  # Ollama uses 'images' for multimodal
                        "stream": False,
                    },
                )

                if response.status_code != 200:
                    return TranscriptResult(
                        text="",
                        language=None,
                        confidence=0.0,
                        duration_seconds=0.0,
                        processing_time_ms=(time.time() - start_time) * 1000,
                        model_used=self._model,
                        error="Transcription service error",
                        error_code="voice_transcription_failed",
                    )

                data = response.json()
                text = data.get("response", "").strip()

                return TranscriptResult(
                    text=text,
                    language=language_hint,
                    confidence=0.85 if text else 0.0,
                    duration_seconds=len(audio_bytes) / 16000 / 2,  # Rough estimate
                    processing_time_ms=(time.time() - start_time) * 1000,
                    model_used=self._model,
                )

        except Exception:
            logger.exception("Ollama transcription failed")
            return TranscriptResult(
                text="",
                language=None,
                confidence=0.0,
                duration_seconds=0.0,
                processing_time_ms=(time.time() - start_time) * 1000,
                model_used=self._model,
                error="Transcription service error",
                error_code="voice_transcription_failed",
            )

    async def _transcribe_openai_style(
        self,
        audio_bytes: bytes,
        audio_format: AudioFormat,
        language_hint: str | None,
    ) -> TranscriptResult:
        """Transcribe using OpenAI Whisper API style endpoint."""
        start_time = time.time()

        try:
            # Write to temp file for multipart upload
            with tempfile.NamedTemporaryFile(
                suffix=f".{audio_format}", delete=False
            ) as f:
                f.write(audio_bytes)
                temp_path = f.name

            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    with open(temp_path, "rb") as audio_file:
                        files = {
                            "file": (
                                f"audio.{audio_format}",
                                audio_file,
                                f"audio/{audio_format}",
                            )
                        }
                        data = {"model": "whisper-1"}
                        if language_hint:
                            data["language"] = language_hint

                        response = await client.post(
                            f"{self._whisper_url}/v1/audio/transcriptions",
                            files=files,
                            data=data,
                        )

                        if response.status_code != 200:
                            return TranscriptResult(
                                text="",
                                language=None,
                                confidence=0.0,
                                duration_seconds=0.0,
                                processing_time_ms=(time.time() - start_time) * 1000,
                                model_used="whisper-1",
                                error="Transcription service error",
                                error_code="voice_transcription_failed",
                            )

                        result = response.json()
                        text = result.get("text", "").strip()

                        return TranscriptResult(
                            text=text,
                            language=result.get("language", language_hint),
                            confidence=0.9 if text else 0.0,
                            duration_seconds=result.get(
                                "duration", len(audio_bytes) / 16000 / 2
                            ),
                            processing_time_ms=(time.time() - start_time) * 1000,
                            model_used="whisper-1",
                        )
            finally:
                # Clean up temp file
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

        except Exception:
            logger.exception("OpenAI-style transcription failed")
            return TranscriptResult(
                text="",
                language=None,
                confidence=0.0,
                duration_seconds=0.0,
                processing_time_ms=(time.time() - start_time) * 1000,
                model_used="whisper-1",
                error="Transcription service error",
                error_code="voice_transcription_failed",
            )


# Default STT instance (AI_MODE-aware)
_default_stt: WhisperSTT | DisabledWhisperSTT | DeterministicWhisperSTT | None = None


def get_stt() -> WhisperSTT | DisabledWhisperSTT | DeterministicWhisperSTT:
    """Get the default STT instance, respecting AI_MODE."""
    global _default_stt
    if _default_stt is not None:
        return _default_stt

    mode = resolve_ai_mode()
    if mode is AIMode.DISABLED:
        _default_stt = DisabledWhisperSTT()
    elif mode is AIMode.DETERMINISTIC:
        _default_stt = DeterministicWhisperSTT()
    else:
        _default_stt = WhisperSTT()
    return _default_stt


async def transcribe_audio(
    audio_bytes: bytes,
    *,
    audio_format: AudioFormat = "webm",
    language_hint: str | None = None,
) -> TranscriptResult:
    """Convenience function to transcribe audio.

    Args:
        audio_bytes: Raw audio data
        audio_format: Audio format
        language_hint: Optional language hint

    Returns:
        TranscriptResult with transcript and metadata
    """
    if not audio_bytes:
        return TranscriptResult(
            text="",
            language=None,
            confidence=0.0,
            duration_seconds=0.0,
            processing_time_ms=0.0,
            model_used="none",
            error="Empty audio input",
            error_code="voice_transcription_failed",
        )
    stt = get_stt()
    return await stt.transcribe(
        audio_bytes, audio_format=audio_format, language_hint=language_hint
    )
