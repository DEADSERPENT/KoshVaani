import httpx

from app.config import Settings
from app.core.errors import AppError
from app.providers.resilience import CircuitBreaker, send_with_retry
from app.providers.speech_base import SynthesisResult

MAX_CHARS = 2000  # Deepgram Aura per-request limit


class DeepgramTTS:
    """Deepgram Aura text-to-speech (free credit on signup, key from console.deepgram.com)."""

    name = "deepgram_aura"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        if not settings.deepgram_api_key:
            raise AppError("PROVIDER_UNAVAILABLE", "DEEPGRAM_API_KEY is not configured.")
        self.s = settings
        self.client = client or httpx.AsyncClient(timeout=settings.speech_timeout_seconds)
        self.breaker = CircuitBreaker()

    async def synthesize(self, text: str, *, voice: str | None = None) -> SynthesisResult:
        text = text.strip()
        if not text:
            raise AppError("VALIDATION_ERROR", "There is no text to speak.")
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS]

        async def send():
            return await self.client.post(
                self.s.deepgram_tts_url,
                params={"model": voice or self.s.deepgram_tts_model, "encoding": "mp3"},
                headers={"Authorization": f"Token {self.s.deepgram_api_key}"},  # never logged
                json={"text": text})

        resp = await send_with_retry(self.breaker, send)
        return SynthesisResult(audio=resp.content, content_type=resp.headers.get("content-type", "audio/mpeg"),
                               provider=self.name, request_id=resp.headers.get("dg-request-id"))
