"""Deterministic speech stand-ins for automated tests and offline development.

These are NOT speech recognition or synthesis: the mock STT just reads the audio bytes as UTF-8 text and the
mock TTS returns a short silent WAV. Traces label them provider="mock".
"""
import io
import wave

from app.core.errors import AppError
from app.providers.speech_base import SynthesisResult, TranscriptResult


class MockSTT:
    name = "mock"

    async def transcribe(self, audio: bytes, *, content_type: str, language: str = "en") -> TranscriptResult:
        try:
            text = audio.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise AppError("VALIDATION_ERROR", "Mock STT only accepts UTF-8 text bytes.") from exc
        return TranscriptResult(text=text, language=language, provider=self.name, request_id="mock-stt")


class MockTTS:
    name = "mock"

    async def synthesize(self, text: str, *, voice: str | None = None) -> SynthesisResult:
        if not text.strip():
            raise AppError("VALIDATION_ERROR", "There is no text to speak.")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 3200)  # 0.2 s of silence
        return SynthesisResult(audio=buf.getvalue(), content_type="audio/wav", provider=self.name,
                               request_id="mock-tts")
