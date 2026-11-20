from dataclasses import dataclass
from typing import Protocol


@dataclass
class TranscriptResult:
    text: str
    language: str
    provider: str
    is_final: bool = True
    confidence: float | None = None  # None when the provider does not return one; never invented
    request_id: str | None = None
    duration_seconds: float | None = None


@dataclass
class SynthesisResult:
    audio: bytes
    content_type: str
    provider: str
    request_id: str | None = None


class SpeechToTextProvider(Protocol):
    name: str

    async def transcribe(self, audio: bytes, *, content_type: str, language: str = "en") -> TranscriptResult: ...


class TextToSpeechProvider(Protocol):
    name: str

    async def synthesize(self, text: str, *, voice: str | None = None) -> SynthesisResult: ...
