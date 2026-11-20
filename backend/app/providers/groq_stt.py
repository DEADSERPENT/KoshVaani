import httpx

from app.config import Settings
from app.core.errors import AppError
from app.providers.resilience import CircuitBreaker, send_with_retry
from app.providers.speech_base import TranscriptResult

_EXT = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3",
        "audio/mp4": "m4a", "audio/flac": "flac"}


class GroqWhisperSTT:
    """Groq's OpenAI-compatible Whisper endpoint (free tier, key from console.groq.com)."""

    name = "groq_whisper"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        if not settings.groq_api_key:
            raise AppError("PROVIDER_UNAVAILABLE", "GROQ_API_KEY is not configured.")
        self.s = settings
        self.client = client or httpx.AsyncClient(timeout=settings.speech_timeout_seconds)
        self.breaker = CircuitBreaker()

    async def transcribe(self, audio: bytes, *, content_type: str, language: str = "en") -> TranscriptResult:
        base = content_type.split(";")[0].strip().lower()
        ext = _EXT.get(base)
        if ext is None:
            raise AppError("VALIDATION_ERROR", "Unsupported audio format.")

        async def send():
            return await self.client.post(
                self.s.groq_stt_url,
                headers={"Authorization": f"Bearer {self.s.groq_api_key}"},  # never logged
                files={"file": (f"utterance.{ext}", audio, base)},
                data={"model": self.s.groq_stt_model, "language": language.split("-")[0],
                      "response_format": "verbose_json", "temperature": "0"})

        resp = await send_with_retry(self.breaker, send)
        try:
            body = resp.json()
            text = (body.get("text") or "").strip()
        except ValueError as exc:
            raise AppError("PROVIDER_UNAVAILABLE", "The speech service returned an unreadable response.") from exc
        return TranscriptResult(text=text, language=body.get("language", language), provider=self.name,
                                request_id=resp.headers.get("x-request-id"), duration_seconds=body.get("duration"))
